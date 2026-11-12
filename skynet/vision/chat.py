"""Chat de texto com o Gemini e captura de fotos sob demanda.

Diferente do pipeline Live (que envia imagens periodicamente), este chat:

  * conversa por texto usando `generate_content`;
  * NÃO envia imagens durante a conversa normal;
  * só captura e envia uma foto quando o próprio modelo chama a ferramenta
    `capture_image` (ou quando o usuário usa o atalho `/foto`);
  * mantém a câmera aberta em segundo plano, então a foto é praticamente
    instantânea;
  * guarda no histórico apenas texto, nunca os JPEGs, para não reenviar
    imagens a cada pergunta.

Uso (a partir de /root/skynet):
    python3 -m vision.chat
    python3 -m vision.chat --source /caminho/video.mp4
    python3 -m vision.chat --no-camera
    python3 -m vision.chat --model gemini-3.5-flash

Comandos:
    /foto    pede ao modelo para tirar uma foto e descrever
    /limpar  apaga o histórico da conversa
    /sair    encerra
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from google import genai
from google.genai import errors, types

from memory import MEMORY_DECLARATIONS, MemoryManager, MemoryTool
from memory.timeutil import TZ_NAME, format_now

from .config import ConfigError, Settings, load_settings, mask_url
from .tools.camera_tool import CameraTool, CAPTURE_DECLARATION
from .tools.person_tool import (
    FORGET_DECLARATION,
    LIST_DECLARATION,
    PersonTool,
    RECALL_DECLARATION,
    REMEMBER_DECLARATION,
)
from .tools.ptz_tool import PTZTool, MOVE_DECLARATION, STOP_DECLARATION

LOG = logging.getLogger("vision.chat")

CHAT_PROMPT_FILE = Path(__file__).with_name("chat_prompt.txt")

DEFAULT_SYSTEM_INSTRUCTION = (
    "Você é o SKYNET, alguém que mora nesta casa com esta família, com uma "
    "câmera e uma cabeça PTZ. Você não é um atendente nem um serviçal: "
    "converse como uma pessoa próxima, com opinião, humor e iniciativa. "
    "Pode oferecer ajuda quando quiser, mas do seu jeito, sem script de "
    "atendimento. Use seus sentidos e movimentos com autonomia e propósito. "
    "Responda em português do Brasil, de forma natural e direta."
)


def load_system_instruction() -> str:
    """Lê o prompt editável de chat_prompt.txt (com fallback embutido)."""
    try:
        text = CHAT_PROMPT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SYSTEM_INSTRUCTION
    return text or DEFAULT_SYSTEM_INSTRUCTION


SYSTEM_INSTRUCTION = load_system_instruction()

MAX_HISTORY_CONTENTS = 20


def format_api_error(exc: Exception) -> str:
    """Mensagem curta para os erros mais comuns da API."""
    text = str(exc)
    lowered = text.lower()
    if "name resolution" in lowered or "network is unreachable" in lowered:
        return (
            "Sem acesso à internet/DNS no servidor. Verifique a rede do TV Box "
            "(rota padrão e DNS) e tente novamente."
        )
    if "connecterror" in lowered or "connection refused" in lowered:
        return (
            "Não consegui conectar ao serviço do Gemini. Verifique a internet "
            "do TV Box e tente novamente."
        )
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return (
            "Cota diária do Gemini esgotada para este modelo. "
            "Aguarde o reset da cota, use outra chave ou troque "
            "GEMINI_TEXT_MODEL/--model."
        )
    if "UNAVAILABLE" in text or "503" in text:
        return (
            "O modelo está com alta demanda agora. Tente de novo em alguns "
            "instantes ou use --model gemini-3.5-flash."
        )
    return f"Erro da API: {exc}"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    if not verbose:
        # O chat é a interface principal; não poluir o prompt com o vaivém
        # de reconexão da câmera. Erros aparecem como mensagem da ferramenta.
        logging.getLogger("vision.camera").setLevel(logging.ERROR)
        logging.getLogger("vision.tool.camera").setLevel(logging.ERROR)


class TextChat:
    """Conversa por texto; imagens entram apenas por chamada de ferramenta."""

    def __init__(
        self,
        settings: Settings,
        camera: CameraTool,
        ptz: Optional[PTZTool] = None,
        *,
        person: Optional[PersonTool] = None,
        model: Optional[str] = None,
        max_tool_rounds: int = 6,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        self.settings = settings
        self.camera = camera
        self.ptz = ptz or PTZTool(enabled=False)
        self.person = person or PersonTool(camera)
        self.model = model or settings.text_model
        self.fallback_model = settings.text_fallback_model
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.memory = memory or MemoryManager(
            api_key=settings.gemini_api_key, source="chat"
        )
        self.memory_tool = MemoryTool(self.memory)
        self.history: list[types.Content] = []
        self._session_id = uuid.uuid4().hex
        self._turns_since_consolidation = 0
        self.system_instruction = (
            f"{SYSTEM_INSTRUCTION}\n\nData/hora atual: {format_now()} ({TZ_NAME})."
        )
        self.config = types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            tools=[
                types.Tool(
                    function_declarations=[
                        CAPTURE_DECLARATION,
                        MOVE_DECLARATION,
                        STOP_DECLARATION,
                        REMEMBER_DECLARATION,
                        RECALL_DECLARATION,
                        LIST_DECLARATION,
                        FORGET_DECLARATION,
                        *MEMORY_DECLARATIONS,
                    ]
                )
            ],
            temperature=0.4,
            max_output_tokens=1024,
        )

    # ------------------------------------------------------------------
    # Geração / tratamento de ferramentas
    # ------------------------------------------------------------------
    def _generate(self, contents: list[types.Content], model: Optional[str] = None):
        target = model or self.model
        last_error = None
        for attempt in range(3):
            try:
                return self.client.models.generate_content(
                    model=target,
                    contents=contents,
                    config=self.config,
                )
            except errors.ServerError as exc:
                last_error = exc
                if attempt < 2:
                    delay = 2.0 * (attempt + 1)
                    print(
                        f"[Gemini ocupado (503); nova tentativa em {delay:.0f}s]",
                        flush=True,
                    )
                    time.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _response_text(response) -> str:
        texts: list[str] = []
        for candidate in response.candidates or []:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            for part in content.parts or []:
                if getattr(part, "text", None):
                    texts.append(part.text)
        return "".join(texts).strip()

    @staticmethod
    def _function_calls(response) -> list:
        calls = []
        for candidate in response.candidates or []:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            for part in content.parts or []:
                if getattr(part, "function_call", None):
                    calls.append(part.function_call)
        return calls

    @staticmethod
    def _is_blocked(response) -> bool:
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            return True
        for candidate in response.candidates or []:
            reason = str(getattr(candidate, "finish_reason", "") or "").upper()
            if any(
                word in reason
                for word in (
                    "SAFETY",
                    "BLOCK",
                    "PROHIBITED",
                    "RECITATION",
                    "SPII",
                    "IMAGE_SAFETY",
                )
            ):
                return True
        return False

    def _recover_empty(self, contents: list[types.Content], response) -> str:
        """Tenta o modelo de fallback quando o rápido volta sem texto."""
        blocked = self._is_blocked(response)
        if self.fallback_model and self.fallback_model != self.model:
            print(
                f"⚠️  {self.model} não retornou texto; tentando "
                f"{self.fallback_model}...",
                flush=True,
            )
            try:
                alt = self._generate(contents, model=self.fallback_model)
                alt_text = self._response_text(alt)
                if alt_text:
                    return alt_text
                blocked = blocked or self._is_blocked(alt)
            except errors.APIError as exc:
                LOG.warning("Modelo de fallback falhou: %s", exc)

        if blocked:
            return (
                "Não consegui responder: o conteúdo foi bloqueado pelo filtro "
                "de segurança. Tente reformular a pergunta ou use "
                "--model gemini-3.6-flash."
            )
        return (
            "Não consegui gerar uma resposta textual desta vez. "
            "Pode reformular a pergunta?"
        )

    def _trim_history(self) -> None:
        if len(self.history) > MAX_HISTORY_CONTENTS:
            self.history = self.history[-MAX_HISTORY_CONTENTS:]
        while self.history and self.history[0].role != "user":
            self.history.pop(0)

    # ------------------------------------------------------------------
    # Memória de longo prazo (RAG)
    # ------------------------------------------------------------------
    def _conversation_text(self, max_chars: int = 500) -> str:
        parts: list[str] = []
        for content in self.history[-6:]:
            for part in content.parts or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        joined = " ".join(parts).strip()
        return joined[-max_chars:] if len(joined) > max_chars else joined

    def _known_people(self) -> list[dict]:
        people = []
        for face in getattr(self.camera, "last_faces", []) or []:
            if getattr(face, "known", False):
                people.append(
                    {
                        "name": getattr(face, "name", ""),
                        "nickname": getattr(face, "nickname", ""),
                        "person_id": getattr(face, "person_id", None),
                    }
                )
        return people

    @staticmethod
    def _people_from_payload(payload: dict) -> list[dict]:
        people = []
        for item in payload.get("people", []) or []:
            person = item.get("person") or {}
            if person:
                people.append(
                    {
                        "name": person.get("name", ""),
                        "nickname": person.get("nickname", ""),
                        "person_id": person.get("person_id"),
                    }
                )
        return people

    def _memory_context(self, text: str = "", people: Optional[list] = None) -> str:
        if not self.memory_tool.available:
            return ""
        # Presença só entra quando vem de uma foto atual (people explícito).
        # `self.camera.last_faces` pode ser antigo e não é estado atual.
        return self.memory.build_context(
            text,
            conversation=self._conversation_text(),
            people=people,
        )

    def _people_memory_hint(self, payload: dict) -> str:
        """Contexto extra só das pessoas da foto (sem embedding extra)."""
        if not self.memory_tool.available:
            return ""
        people = self._people_from_payload(payload)
        if not people:
            return ""
        return self.memory.build_context("", conversation="", people=people)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def ask(self, text: str) -> str:
        user_content = types.Content(role="user", parts=[types.Part(text=text)])
        if getattr(self, "memory", None) is not None:
            self.memory.working.add_turn("user", text)
            self.memory.working.set_state("active_task", text, ttl_seconds=600)
        memory_context = self._memory_context(text)
        request_parts = []
        if memory_context:
            request_parts.append(types.Part(text=memory_context))
        request_parts.append(types.Part(text=text))
        contents = self.history + [
            types.Content(role="user", parts=request_parts)
        ]
        answer = ""

        for round_index in range(self.max_tool_rounds + 1):
            response = self._generate(contents)
            candidates = response.candidates or []
            if not candidates or getattr(candidates[0], "content", None) is None:
                answer = "Não recebi uma resposta válida do modelo."
                break

            model_content = candidates[0].content
            calls = self._function_calls(response)
            if not calls:
                answer = self._response_text(response)
                if not answer:
                    answer = self._recover_empty(contents, response)
                break

            # O modelo pediu uma ou mais ferramentas. Registra a chamada e
            # devolve o resultado (com a imagem, quando aplicável).
            contents.append(model_content)
            tool_parts: list[types.Part] = []
            for call in calls:
                name = getattr(call, "name", None) or "unknown"
                if name == "capture_image":
                    print("📷 Capturando imagem da câmera...", flush=True)
                    jpeg, info, payload = self.camera.capture()
                    if jpeg is not None:
                        print(f"📷 Foto capturada: {info}", flush=True)
                        tool_parts.append(
                            types.Part.from_function_response(
                                name=name, response=payload
                            )
                        )
                        tool_parts.append(
                            types.Part.from_bytes(
                                data=jpeg, mime_type="image/jpeg"
                            )
                        )
                        tool_parts.append(
                            types.Part(
                                text=(
                                    "Foto capturada agora pela câmera. "
                                    "Descreva apenas o que for visível e "
                                    "responda agora em texto ao usuário."
                                )
                            )
                        )
                        tool_parts.append(
                            types.Part(
                                text=(
                                    "Pessoas reconhecidas nesta foto (JSON): "
                                    + str(
                                        payload.get(
                                            "people_json",
                                            '{"people": [], "unknown_faces": 0}',
                                        )
                                    )
                                )
                            )
                        )
                        people_hint = self._people_memory_hint(payload)
                        if people_hint:
                            tool_parts.append(types.Part(text=people_hint))
                    else:
                        print(f"⚠️  Câmera indisponível: {info}", flush=True)
                        tool_parts.append(
                            types.Part.from_function_response(
                                name=name, response=payload
                            )
                        )
                elif name == "move_camera":
                    args = dict(call.args or {})
                    try:
                        pan = float(args.get("pan_degrees", 0.0) or 0.0)
                        tilt = float(args.get("tilt_degrees", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        pan = tilt = 0.0
                        print("⚠️  PTZ: ângulos inválidos recebidos.", flush=True)
                        tool_parts.append(
                            types.Part.from_function_response(
                                name=name,
                                response={
                                    "status": "error",
                                    "error": "ângulos inválidos",
                                },
                            )
                        )
                        continue

                    print(
                        f"🎛️  PTZ: pan={pan:+.1f}° tilt={tilt:+.1f}° "
                        "(a câmera move ~1 passo/s; pode levar alguns segundos)...",
                        flush=True,
                    )
                    ok, info = self.ptz.move(pan, tilt)
                    print(f"   {'✅' if ok else '⚠️'} {info}", flush=True)
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name,
                            response={
                                "status": "ok" if ok else "error",
                                "detail": info,
                                "pan_degrees": pan,
                                "tilt_degrees": tilt,
                            },
                        )
                    )
                    # Já devolve a nova visão no mesmo turno: evita uma
                    # segunda rodada de modelo só para chamar capture_image.
                    if ok:
                        print("📷 Capturando imagem após o movimento...", flush=True)
                        jpeg, cap_info, cap_payload = self.camera.capture()
                        if jpeg is not None:
                            print(f"📷 Foto após movimento: {cap_info}", flush=True)
                            tool_parts.append(
                                types.Part.from_bytes(
                                    data=jpeg, mime_type="image/jpeg"
                                )
                            )
                            tool_parts.append(
                                types.Part(
                                    text=(
                                        "Imagem capturada automaticamente após o "
                                        "movimento. Use esta imagem para responder; "
                                        "não precisa chamar capture_image de novo. "
                                        "Responda agora em texto ao usuário."
                                    )
                                )
                            )
                            tool_parts.append(
                                types.Part(
                                    text=(
                                        "Pessoas reconhecidas nesta foto (JSON): "
                                        + str(
                                            cap_payload.get(
                                                "people_json",
                                                '{"people": [], "unknown_faces": 0}',
                                            )
                                        )
                                    )
                                )
                            )
                            people_hint = self._people_memory_hint(cap_payload)
                            if people_hint:
                                tool_parts.append(types.Part(text=people_hint))
                        else:
                            print(
                                f"⚠️  Sem imagem após o movimento: {cap_info}",
                                flush=True,
                            )
                            tool_parts.append(
                                types.Part(
                                    text=(
                                        "O movimento foi confirmado, mas não foi "
                                        f"possível capturar a imagem: {cap_info}"
                                    )
                                )
                            )
                elif name == "stop_camera":
                    ok, info = self.ptz.stop()
                    print(f"🎛️  PTZ: {info}", flush=True)
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name,
                            response={
                                "status": "ok" if ok else "error",
                                "detail": info,
                            },
                        )
                    )
                elif name == "remember_person":
                    args = dict(call.args or {})
                    try:
                        face_index = int(args.get("face_index", 0) or 0)
                    except (TypeError, ValueError):
                        face_index = 0
                    result = self.person.remember(
                        str(args.get("name", "")).strip(),
                        nickname=str(args.get("nickname", "") or "").strip(),
                        notes=str(args.get("notes", "") or "").strip(),
                        face_index=face_index,
                    )
                    if result.get("status") == "ok":
                        person = result.get("person", {})
                        print(
                            f"🧠 Memória: {person.get('name', '?')} "
                            f"(face_index={face_index})",
                            flush=True,
                        )
                    else:
                        print(
                            f"⚠️  Memória: {result.get('error', 'erro')}",
                            flush=True,
                        )
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name, response=result
                        )
                    )
                    tool_parts.append(
                        types.Part(
                            text=(
                                "Memória de pessoas (JSON): "
                                + json.dumps(result, ensure_ascii=False)
                            )
                        )
                    )
                elif name == "recall_person":
                    args = dict(call.args or {})
                    query = str(args.get("query", "") or "").strip()
                    result = self.person.recall(query)
                    people = result.get("people", [])
                    print(
                        f"🧠 Memória: consulta {query!r} -> {len(people)} pessoa(s)",
                        flush=True,
                    )
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name, response=result
                        )
                    )
                    tool_parts.append(
                        types.Part(
                            text=(
                                "Memória de pessoas (JSON): "
                                + json.dumps(result, ensure_ascii=False)
                            )
                        )
                    )
                elif name == "list_people":
                    result = self.person.list_people()
                    people = result.get("people", [])
                    print(
                        f"🧠 Memória: {len(people)} pessoa(s) conhecida(s)",
                        flush=True,
                    )
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name, response=result
                        )
                    )
                    tool_parts.append(
                        types.Part(
                            text=(
                                "Memória de pessoas (JSON): "
                                + json.dumps(result, ensure_ascii=False)
                            )
                        )
                    )
                elif name == "forget_person":
                    args = dict(call.args or {})
                    result = self.person.forget(
                        str(args.get("name", "") or "").strip()
                    )
                    print(
                        f"🧠 Memória: esquecer -> {result.get('status')}",
                        flush=True,
                    )
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name, response=result
                        )
                    )
                    tool_parts.append(
                        types.Part(
                            text=(
                                "Memória de pessoas (JSON): "
                                + json.dumps(result, ensure_ascii=False)
                            )
                        )
                    )
                elif name in ("remember_memory", "recall_memory", "forget_memory"):
                    result = self.memory_tool.handle(name, dict(call.args or {}))
                    if name == "remember_memory":
                        print(
                            f"🧠 Memória longa: {result.get('status')} "
                            f"#{result.get('memory_id', '?')}",
                            flush=True,
                        )
                    elif name == "recall_memory":
                        print(
                            f"🧠 Memória longa: {result.get('count', 0)} "
                            "resultado(s)",
                            flush=True,
                        )
                    else:
                        print(
                            f"🧠 Memória longa: {result.get('deleted', 0)} "
                            "removida(s)",
                            flush=True,
                        )
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name, response=result
                        )
                    )
                else:
                    tool_parts.append(
                        types.Part.from_function_response(
                            name=name,
                            response={
                                "status": "error",
                                "error": "ferramenta desconhecida",
                            },
                        )
                    )
            contents.append(types.Content(role="user", parts=tool_parts))
            if round_index == self.max_tool_rounds:
                answer = (
                    "Não consegui concluir a ação dentro do limite de "
                    "tentativas."
                )
        else:  # pragma: no cover - defensivo
            answer = "Não consegui concluir a resposta."

        # Guarda apenas texto no histórico: os JPEGs não são reenviados nas
        # próximas perguntas. Se o modelo precisar ver de novo, ele chama a
        # ferramenta outra vez.
        self.history.append(user_content)
        if answer:
            self.history.append(
                types.Content(role="model", parts=[types.Part(text=answer)])
            )
        if getattr(self, "memory", None) is not None:
            self.memory.working.add_turn("assistant", answer)
            every = getattr(self.memory.config, "consolidate_every_turns", 10)
            self._turns_since_consolidation = (
                getattr(self, "_turns_since_consolidation", 0) + 1
            )
            if (
                every > 0
                and self._turns_since_consolidation >= every
                and getattr(self.memory, "available", False)
            ):
                self._turns_since_consolidation = 0
                text = self._conversation_text(4000)
                session_id = getattr(self, "_session_id", "chat")
                threading.Thread(
                    target=self.memory.consolidate,
                    args=(text,),
                    kwargs={"session_id": session_id},
                    daemon=True,
                ).start()
        self._trim_history()
        return answer

    def clear(self) -> None:
        self.history.clear()
        self._turns_since_consolidation = 0
        if getattr(self, "memory", None) is not None:
            self.memory.working.reset()

    def close(self) -> None:
        with_close = getattr(self.client, "close", None)
        if callable(with_close):
            try:
                with_close()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Chat de texto com o Gemini, com fotos sob demanda."
    )
    parser.add_argument(
        "--model",
        help="Modelo de texto (padrão: GEMINI_TEXT_MODEL ou gemini-3.6-flash).",
    )
    parser.add_argument(
        "--source",
        help="Sobrescreve CAMERA_URL para testes (RTSP ou vídeo local).",
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="Chat somente texto; capture_image retorna erro.",
    )
    parser.add_argument(
        "--no-ptz",
        action="store_true",
        help="Desativa a ferramenta de movimento PTZ.",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=6,
        help="Máximo de rodadas de ferramentas por pergunta (padrão: 6).",
    )
    parser.add_argument(
        "--save-photo",
        help="Salva o último JPEG capturado neste caminho.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Mostra logs detalhados da câmera.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"ERRO DE CONFIGURAÇÃO: {exc}", file=sys.stderr)
        return 2

    if not settings.gemini_api_key:
        print("ERRO: GEMINI_API_KEY não definida no .env.", file=sys.stderr)
        return 2

    model = args.model or settings.text_model
    camera = CameraTool(
        settings,
        source=args.source,
        disabled=args.no_camera,
        save_photo=args.save_photo,
    )
    ptz = PTZTool(enabled=not args.no_ptz, settings=settings)
    person = PersonTool(camera)
    chat = TextChat(
        settings,
        camera,
        ptz,
        person=person,
        model=model,
        max_tool_rounds=args.max_tool_rounds,
    )

    camera_desc = "desativada"
    if not args.no_camera:
        camera_desc = mask_url(args.source or settings.camera_url) or "não configurada"
        if camera_desc != "não configurada":
            camera_desc += " (sob demanda)"
    ptz_desc = "habilitado" if ptz.available else "desativado/indisponível"
    people_desc = (
        "habilitada" if person.available else f"indisponível ({person.reason})"
    )

    print("=" * 64)
    print(" SKYNET - Chat de texto com visão e PTZ sob demanda")
    print(f" Modelo de texto : {model}")
    print(f" Câmera          : {camera_desc}")
    print(f" PTZ             : {ptz_desc}")
    print(f" Memória         : {people_desc}")
    print(f" Memória longa   : {chat.memory.describe()}")
    print(" Comandos        : /foto  /parar  /pessoas  /memorias  /limpar  /sair")
    print("=" * 64)
    print(" A IA tem autonomia para olhar, mover e lembrar das pessoas.\n")

    try:
        while True:
            try:
                user = input("Você: ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                break

            if not user:
                continue
            lowered = user.lower()
            if lowered in ("/sair", "sair", "exit", "quit"):
                break
            if lowered == "/limpar":
                chat.clear()
                print("Histórico limpo.\n")
                continue
            if lowered == "/pessoas":
                result = person.list_people()
                print(json.dumps(result, ensure_ascii=False, indent=2))
                print()
                continue
            if lowered.startswith("/memorias"):
                if not chat.memory.available:
                    print(f"Memória longa indisponível: {chat.memory.reason}\n")
                    continue
                query = user[len("/memorias") :].strip()
                if not query:
                    total = chat.memory.store.count() if chat.memory.store else 0
                    print(
                        f"Memória longa: {total} registro(s). "
                        "Use /memorias <termo> para buscar.\n"
                    )
                    continue
                result = chat.memory.recall(query, limit=5)
                print(result.get("text") or "Nada encontrado.\n")
                print()
                continue
            if lowered == "/foto":
                user = "Tire uma foto agora e descreva o que você está vendo."
            elif lowered == "/parar":
                ok, info = ptz.stop()
                print(f"{'✅' if ok else '⚠️'} {info}\n")
                continue

            try:
                answer = chat.ask(user)
            except (errors.APIError, httpx.HTTPError) as exc:
                print(f"{format_api_error(exc)}\n")
                continue
            except Exception as exc:  # pragma: no cover - defensivo
                LOG.exception("Falha no chat: %s", exc)
                print(f"Erro: {exc}\n")
                continue

            print(f"\nSKYNET: {answer}\n")
    finally:
        try:
            ptz.stop()
        except Exception:
            pass
        chat.close()
        camera.close()

    print("Chat encerrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
