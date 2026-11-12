"""Cliente de UMA conexao com a Gemini Live API.

Responsabilidades:
  * abrir a sessao WebSocket com a configuracao validada;
  * manter uma tarefa de recebimento e uma tarefa de envio;
  * enviar frames JPEG por realtimeInput (max. 1 FPS, garantido no heartbeat);
  * armazenar o handle de SessionResumption e reagir a GoAway;
  * coletar transcricoes da resposta (o modelo 3.8 Live so responde AUDIO;
    a transcricao permite validar textualmente sem tocar audio).

A reconexao/backoff fica no SessionManager; esta classe representa apenas
uma conexao e termina quando ela cai.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack, suppress
from typing import Optional

from .config import Settings
from .diagnostics import Diagnostics

LOG = logging.getLogger("vision.gemini")

try:
    from google import genai
    from google.genai import types
except ImportError as exc:  # pragma: no cover - depende do ambiente
    genai = None  # type: ignore
    types = None  # type: ignore
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class GeminiLiveError(RuntimeError):
    """Erro generico do cliente Live."""


class NotConnectedError(GeminiLiveError):
    """Operacao solicitada sem sessao Live ativa."""


def ensure_sdk() -> None:
    if genai is None or types is None:
        raise GeminiLiveError(
            "SDK google-genai nao instalado. Rode: pip install -r requirements.txt "
            f"(detalhe: {_IMPORT_ERROR})"
        )


def _parse_duration_seconds(value) -> Optional[float]:
    """Converte Duration do proto (ex.: '9.5s') em segundos."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "total_seconds"):
        try:
            return float(value.total_seconds())
        except Exception:
            return None
    text = str(value).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


class ResponseCollector:
    """Acumula a transcricao de uma resposta e mede a latencia."""

    __slots__ = (
        "parts",
        "question_at",
        "first_token_at",
        "done_at",
        "event",
        "interrupted",
        "first_token_recorded",
    )

    def __init__(self, question_at: Optional[float] = None) -> None:
        self.parts: list[str] = []
        self.question_at = time.monotonic() if question_at is None else question_at
        self.first_token_at: Optional[float] = None
        self.done_at: Optional[float] = None
        self.event = asyncio.Event()
        self.interrupted = False
        self.first_token_recorded = False

    @property
    def text(self) -> str:
        return "".join(self.parts).strip()

    @property
    def first_token_ms(self) -> Optional[float]:
        if self.first_token_at is None:
            return None
        return (self.first_token_at - self.question_at) * 1000.0

    @property
    def turn_complete_ms(self) -> Optional[float]:
        if self.done_at is None:
            return None
        return (self.done_at - self.question_at) * 1000.0

    def add(self, text: str) -> None:
        if not text:
            return
        if self.first_token_at is None:
            self.first_token_at = time.monotonic()
        self.parts.append(text)

    def finish(self, interrupted: bool = False) -> None:
        if self.done_at is not None:
            return
        self.done_at = time.monotonic()
        self.interrupted = interrupted
        self.event.set()


class GeminiLiveClient:
    """Uma conexao Live com tarefas de envio e recebimento independentes."""

    def __init__(
        self,
        client,
        settings: Settings,
        diagnostics: Diagnostics,
        logger: Optional[logging.Logger] = None,
        *,
        handle: Optional[str] = None,
    ) -> None:
        ensure_sdk()
        self._client = client
        self._settings = settings
        self._diag = diagnostics
        self._log = logger or LOG
        self._handle = handle
        self._handle_used = bool(handle)

        self._session = None
        self._stack: Optional[AsyncExitStack] = None
        self._tasks: set[asyncio.Task] = set()
        self._closed_event = asyncio.Event()
        self._closed_reason = ""
        self._disconnect_recorded = False

        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        self._write_lock = asyncio.Lock()
        self._ask_lock = asyncio.Lock()
        self._pending: Optional[ResponseCollector] = None
        self._spontaneous_seen = False
        self._goaway_scheduled = False

    # ------------------------------------------------------------------
    # Propriedades
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._session is not None and not self._closed_event.is_set()

    @property
    def handle(self) -> Optional[str]:
        return self._handle

    @property
    def handle_used(self) -> bool:
        return self._handle_used

    @property
    def closed_reason(self) -> str:
        return self._closed_reason

    # ------------------------------------------------------------------
    # Conexao
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        config = self._build_config()
        self._diag.record_connecting()
        self._stack = AsyncExitStack()
        try:
            self._session = await self._stack.enter_async_context(
                self._client.aio.live.connect(
                    model=self._settings.gemini_model, config=config
                )
            )
        except Exception:
            await self._close_stack()
            raise

        self._diag.record_connected(self._handle_used)
        self._log.info(
            "Sessao Live aberta: model=%s handle=%s",
            self._settings.gemini_model,
            "retomado" if self._handle_used else "novo",
        )
        self._spawn(self._recv_loop(), "gemini-recv")
        self._spawn(self._send_loop(), "gemini-send")

        if self._settings.initial_prompt and not self._handle_used:
            try:
                await self.seed_instruction(self._settings.initial_prompt)
            except Exception as exc:
                self._log.warning("Falha ao semear instrucao inicial: %s", exc)
        elif self._handle_used:
            # O contexto anterior ja contem a instrucao inicial; reenvia-la
            # apenas duplicaria tokens na sessao retomada.
            self._log.info("Sessao retomada: instrucao inicial preservada no contexto.")

    def _build_config(self):
        settings = self._settings
        media_map = {
            "LOW": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "MEDIUM": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "HIGH": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            "UNSPECIFIED": types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED,
        }
        modality = getattr(types.Modality, settings.response_modality)

        # TURN_INCLUDES_ALL_INPUT e essencial: com o default do 3.8 Live
        # (AUDIO_ACTIVITY_AND_ALL_VIDEO), frames enviados por realtimeInput
        # NAO entram em um turno iniciado por texto (validado em bancada).
        realtime_config = types.RealtimeInputConfig(
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT
        )

        session_resumption = None
        if settings.session_resumption:
            session_resumption = types.SessionResumptionConfig(handle=self._handle)

        compression = None
        if settings.context_compression:
            compression = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
                trigger_tokens=settings.context_trigger_tokens,
            )

        transcription = None
        if settings.output_transcription and settings.response_modality == "AUDIO":
            transcription = types.AudioTranscriptionConfig()

        return types.LiveConnectConfig(
            response_modalities=[modality],
            media_resolution=media_map.get(settings.media_resolution),
            system_instruction=types.Content(
                parts=[types.Part(text=settings.system_instruction)]
            ),
            realtime_input_config=realtime_config,
            session_resumption=session_resumption,
            context_window_compression=compression,
            output_audio_transcription=transcription,
        )

    # ------------------------------------------------------------------
    # Envio
    # ------------------------------------------------------------------
    def submit_frame(self, jpeg: bytes) -> bool:
        """Entrega o frame mais recente ao slot de envio (nunca bloqueia)."""
        if not self.is_open:
            self._diag.record_dropped("disconnected")
            return False
        if self._send_queue.full():
            # Descarta o frame antigo; o proximo envio usa o mais recente.
            with suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
            self._diag.record_dropped("queue_full")
        try:
            self._send_queue.put_nowait(jpeg)
        except asyncio.QueueFull:
            self._diag.record_dropped("queue_full")
            return False
        self._diag.record_submitted()
        return True

    async def _send_loop(self) -> None:
        consecutive_timeouts = 0
        try:
            while not self._closed_event.is_set():
                jpeg = await self._send_queue.get()
                if self._closed_event.is_set() or self._session is None:
                    return
                started = time.perf_counter()
                try:
                    async with self._write_lock:
                        await asyncio.wait_for(
                            self._session.send_realtime_input(
                                video=types.Blob(data=jpeg, mime_type="image/jpeg")
                            ),
                            timeout=self._settings.send_timeout_sec,
                        )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    self._diag.record_send_failure(timeout=True)
                    self._log.warning(
                        "Timeout no envio do frame (%d consecutivo(s)).",
                        consecutive_timeouts,
                    )
                    if consecutive_timeouts >= 3:
                        self._signal_closed("send_timeout")
                        return
                    continue
                except Exception as exc:
                    self._diag.record_send_failure()
                    self._log.warning("Erro no envio do frame: %s", exc)
                    self._signal_closed(f"send_error: {exc}")
                    return

                elapsed_ms = (time.perf_counter() - started) * 1000.0
                consecutive_timeouts = 0
                self._diag.record_sent(elapsed_ms)
                if self._settings.log_each_frame:
                    self._log.info(
                        "Frame enviado: %.1fKB em %.2fms (fila=%d).",
                        len(jpeg) / 1024.0,
                        elapsed_ms,
                        self._send_queue.qsize(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensivo
            self._log.warning("Tarefa de envio encerrada: %s", exc)
            self._signal_closed(f"send_loop_error: {exc}")

    # ------------------------------------------------------------------
    # Recebimento
    # ------------------------------------------------------------------
    async def _recv_loop(self) -> None:
        try:
            while not self._closed_event.is_set():
                # O SDK encerra o async-for a cada turno completo; e preciso
                # reentrar em receive() para ouvir o proximo turno.
                async for message in self._session.receive():
                    self._handle_message(message)
                    if self._closed_event.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning("Recepcao encerrada: %s", exc)
            self._signal_closed(f"receive_error: {exc}")
        else:
            self._signal_closed("receive_stream_end")

    def _handle_message(self, message) -> None:
        if message.setup_complete is not None:
            session_id = getattr(message.setup_complete, "session_id", None)
            self._log.info("Setup complete recebido (session_id=%s).", session_id)

        if message.go_away is not None:
            self._handle_goaway(message.go_away)

        if message.session_resumption_update is not None:
            update = message.session_resumption_update
            if update.resumable and update.new_handle:
                self._handle = update.new_handle
                self._diag.record_resumption_update()
                if self._diag.resumption_updates <= 1:
                    self._log.info("Session resumption ativo (handle armazenado).")
                else:
                    self._log.debug("Novo handle de resumption recebido.")

        if message.usage_metadata is not None:
            usage = message.usage_metadata
            self._diag.record_usage(
                total=usage.total_token_count or 0,
                prompt=usage.prompt_token_count or 0,
                response=usage.response_token_count or 0,
            )
            self._log.debug(
                "Tokens: total=%s prompt=%s resposta=%s",
                usage.total_token_count,
                usage.prompt_token_count,
                usage.response_token_count,
            )

        if message.tool_call is not None:
            self._log.warning("Tool call recebido, mas ferramentas nao estao habilitadas.")

        if message.server_content is not None:
            self._handle_server_content(message.server_content)

    def _handle_server_content(self, content) -> None:
        collector = self._pending

        if content.output_transcription is not None:
            self._handle_text(content.output_transcription.text or "", collector)

        if content.model_turn is not None:
            for part in content.model_turn.parts or []:
                if getattr(part, "text", None):
                    # Modelos com saida TEXT (quando habilitados).
                    self._handle_text(part.text, collector)
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    # Audio nao e reproduzido nesta etapa; apenas medimos.
                    self._diag.record_audio_bytes(len(inline.data))

        if content.interrupted:
            self._log.info("Resposta interrompida pelo servidor.")
            self._spontaneous_seen = False
            if collector is not None:
                collector.finish(interrupted=True)
                self._diag.record_turn_complete(collector.turn_complete_ms)

        if content.turn_complete:
            status = content.interaction_status
            idle = status is None or status in (
                types.InteractionStatus.IDLE,
                types.InteractionStatus.INTERACTION_STATUS_UNSPECIFIED,
            )
            if idle:
                self._log.info(
                    "Turno do Gemini concluido%s.",
                    f" em {collector.turn_complete_ms:.0f}ms" if collector and collector.turn_complete_ms else "",
                )
                if collector is not None:
                    collector.finish()
                    self._diag.record_turn_complete(collector.turn_complete_ms)
            else:
                self._log.debug("turn_complete com interaction_status=%s", status)
            # Fim de turno: uma eventual resposta espontanea seguinte conta
            # novamente, sem inflar o contador a cada chunk de transcricao.
            self._spontaneous_seen = False

    def _handle_text(
        self, text: str, collector: Optional[ResponseCollector]
    ) -> None:
        """Processa texto de transcricao (AUDIO) ou de saida TEXT."""
        if not text:
            return
        self._log.info("GEMINI: %s", text)
        self._diag.record_transcription(len(text))
        if collector is not None:
            collector.add(text)
            if not collector.first_token_recorded:
                collector.first_token_recorded = True
                elapsed = collector.first_token_ms
                if elapsed is not None:
                    self._diag.record_first_token(elapsed)
                    self._log.info("Primeiro token da resposta em %.0fms.", elapsed)
        elif not self._spontaneous_seen:
            # Nenhuma pergunta pendente: o modelo falou sozinho.
            self._spontaneous_seen = True
            self._diag.record_spontaneous_response()
            self._log.warning(
                "Resposta espontanea do modelo sem pergunta pendente."
            )

    def _handle_goaway(self, goaway) -> None:
        seconds = _parse_duration_seconds(goaway.time_left)
        self._diag.record_goaway(seconds)
        if self._goaway_scheduled:
            return
        self._goaway_scheduled = True
        # Fecha antes do servidor abortar, mas sem cortar uma resposta longa
        # imediatamente. O handle de resumption ja esta armazenado.
        delay = 0.5 if seconds is None else max(0.5, min(15.0, seconds - 2.0))
        self._log.warning(
            "GoAway recebido (time_left=%s). Reconectando em %.1fs.",
            goaway.time_left,
            delay,
        )
        self._spawn(self._delayed_close(delay, "goaway"), "gemini-goaway")

    async def _delayed_close(self, delay: float, reason: str) -> None:
        await asyncio.sleep(delay)
        self._log.info("Fechando conexao atual para reconectar (%s).", reason)
        self._signal_closed(reason)

    # ------------------------------------------------------------------
    # Conteudo de controle / perguntas
    # ------------------------------------------------------------------
    async def seed_instruction(self, text: str) -> None:
        """Adiciona a instrucao inicial ao contexto sem iniciar um turno."""
        if not self.is_open:
            raise NotConnectedError("Sessao Live nao esta aberta.")
        async with self._write_lock:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user", parts=[types.Part(text=text)]
                ),
                turn_complete=False,
            )
        self._log.info(
            "Instrucao inicial semeada (%d chars, turn_complete=False).", len(text)
        )

    async def ask(self, text: str, *, timeout: float = 30.0) -> ResponseCollector:
        """Envia uma pergunta e aguarda a transcricao completa do turno."""
        if not self.is_open:
            raise NotConnectedError("Sessao Live nao esta aberta.")
        async with self._ask_lock:
            collector = ResponseCollector()
            self._pending = collector
            try:
                async with self._write_lock:
                    await self._session.send_client_content(
                        turns=types.Content(
                            role="user", parts=[types.Part(text=text)]
                        ),
                        turn_complete=True,
                    )
                self._diag.record_question()
                self._log.info("Pergunta enviada ao Gemini (%d chars).", len(text))
                try:
                    await asyncio.wait_for(collector.event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    self._log.warning(
                        "Timeout aguardando resposta do Gemini (%.0fs).", timeout
                    )
                    self._diag.record_error("gemini_response_timeout")
                return collector
            finally:
                if self._pending is collector:
                    self._pending = None

    # ------------------------------------------------------------------
    # Encerramento
    # ------------------------------------------------------------------
    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _signal_closed(self, reason: str) -> None:
        if self._closed_event.is_set():
            return
        self._closed_reason = reason
        self._closed_event.set()
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()

    async def wait_closed(self) -> str:
        await self._closed_event.wait()
        return self._closed_reason

    async def close(self, reason: str = "manual") -> None:
        self._signal_closed(reason)
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self._close_stack()
        if not self._disconnect_recorded:
            self._disconnect_recorded = True
            self._diag.record_disconnected(reason)
            self._log.info("Sessao Live encerrada (%s).", reason)
        self._session = None

    async def _close_stack(self) -> None:
        if self._stack is None:
            return
        stack, self._stack = self._stack, None
        with suppress(Exception):
            await stack.aclose()
