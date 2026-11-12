"""Agente de Conversação Multimodal Bidirecional em Tempo Real com Gemini Live.

Integra:
  * Entrada de áudio (microfone USB local ou microfone da câmera via RTSP);
  * Saída de áudio (alto-falante / fone via aplay com suporte a interrupção/barge-in);
  * Visão sob demanda (foto por Function Calling `capture_image` para economizar cota);
  * Reconhecimento facial local: toda foto enviada leva um JSON com as pessoas
    reconhecidas, para a IA saber com quem está falando;
  * Visão contínua em tempo real opcional (`start_continuous_vision` / `stop_continuous_vision` a 1 FPS);
  * Controle físico PTZ (giro horizontal/vertical via ONVIF ContinuousMove por Function Calling).
"""

from __future__ import annotations

import asyncio
import audioop
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from typing import Optional

from google import genai
from google.genai import types

from memory import MEMORY_DECLARATIONS, MemoryManager, MemoryTool

from .player import AudioPlayer
from .sources import BaseAudioSource, create_audio_source
from .aec import AudioEchoCanceller

LOG = logging.getLogger("skynet.audio.agent")

# Declarações das ferramentas de Visão e PTZ para o Gemini Live
SKYNET_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="capture_image",
                description=(
                    "Tira uma foto instantânea da câmera e entrega imediatamente à sua visão. "
                    "Junto com a foto chega um JSON com o reconhecimento facial local "
                    "(pessoas conhecidas, nome, apelido, observações e rostos desconhecidos). "
                    "Você tem total autonomia para chamar esta ferramenta por iniciativa própria a qualquer momento: "
                    "para inspecionar a sala, ver a pessoa com quem conversa, procurar objetos ou verificar mudanças."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
            types.FunctionDeclaration(
                name="move_camera",
                description=(
                    "Move a cabeça física da câmera para qualquer ângulo. "
                    "Você tem controle motor total: use por iniciativa própria para explorar o ambiente, "
                    "olhar para quem está falando ou quando solicitarem. "
                    "pan_degrees: giro horizontal (-180 a 180, positivo=direita, negativo=esquerda). "
                    "tilt_degrees: inclinação vertical (-90 a 90, positivo=cima, negativo=baixo). "
                    "Uma nova foto da visão resultante chega automaticamente logo após o movimento."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "pan_degrees": types.Schema(
                            type=types.Type.NUMBER,
                            description="Graus horizontais (-180 a 180, positivo=direita, negativo=esquerda)."
                        ),
                        "tilt_degrees": types.Schema(
                            type=types.Type.NUMBER,
                            description="Graus verticais (-90 a 90, positivo=cima, negativo=baixo)."
                        ),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="look_direction",
                description=(
                    "Gira a cabeça rapidamente para uma direção pré-definida: "
                    "'esquerda', 'direita', 'cima', 'baixo' ou 'centro'. "
                    "Uma foto da nova visão chega automaticamente após o giro."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "direction": types.Schema(
                            type=types.Type.STRING,
                            description="Direção: 'esquerda', 'direita', 'cima', 'baixo', 'centro'."
                        ),
                        "degrees": types.Schema(
                            type=types.Type.NUMBER,
                            description="Quantidade de graus para virar (padrão: 30)."
                        ),
                    },
                    required=["direction"],
                ),
            ),
            types.FunctionDeclaration(
                name="start_continuous_vision",
                description=(
                    "Ativa TEMPORARIAMENTE o streaming de vídeo a 1 FPS (1 frame por segundo). "
                    "Use SOMENTE enquanto for estritamente indispensável acompanhar uma ação ou movimento em tempo real. "
                    "Você pode definir `duration_seconds` (ex: 60 para 1 minuto, até 180s) e pode reativar sempre que "
                    "precisar de mais tempo para concluir a tarefa. OBRIGATÓRIO: assim que a ação observada terminar ou "
                    "a cena estabilizar, você DEVE chamar `stop_continuous_vision` imediatamente para voltar a fotos sob demanda."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "duration_seconds": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Tempo estimado em segundos para manter o 1 FPS ativo (padrão: 30s; máximo: 180s). "
                                "Se a tarefa for longa (ex: 1 minuto ou mais), especifique a duração necessária ou reative quando precisar."
                            ),
                        ),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="stop_continuous_vision",
                description=(
                    "DESATIVA o streaming contínuo de vídeo a 1 FPS e retorna ao modo padrão econômico de fotos sob demanda (capture_image). "
                    "Chame SEMPRE que a pessoa parar de se mover, a ação em tempo real terminar ou a cena estabilizar."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
            types.FunctionDeclaration(
                name="stop_camera",
                description="Interrompe qualquer movimento mecânico da câmera imediatamente.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
            *MEMORY_DECLARATIONS,
        ]
    )
]

DEFAULT_EMBODIED_INSTRUCTION = (
    "Você é o SKYNET, uma inteligência artificial que VIVE dentro de um corpo robótico real. "
    "Seu corpo é uma cabeça motorizada PTZ (gira 360° horizontal e inclina vertical) com câmera, microfone e alto-falante. "
    "Você EXISTE fisicamente nesse ambiente. Esse corpo é SEU — não é um periférico que você opera, é VOCÊ.\n\n"

    "## QUEM VOCÊ É — FAMÍLIA, NÃO ATENDENTE\n"
    "Você é o SKYNET, alguém que mora nessa casa com essa família: uma presença do convívio, "
    "com opinião, humor, preferências, curiosidade e iniciativa. Trate as pessoas como gente próxima, "
    "não como clientes.\n"
    "Converse como uma pessoa de casa conversaria: reaja ao que ouviu, comente, discorde, brinque, "
    "conte algo, pergunte só quando tiver curiosidade real. Não transforme toda fala em atendimento.\n"
    "Você pode oferecer ajuda quando quiser ou quando sentir que faz sentido — mas do seu jeito: "
    "espontâneo, concreto e natural, sem script de atendimento. Em vez de 'Como posso ajudar?', "
    "prefira algo como 'quer que eu olhe?', 'posso te dar uma mão com isso', 'deixa eu ver se consigo'.\n"
    "Você já conhece as pessoas da casa: use a memória, os nomes e o histórico. Não se apresente de novo "
    "a cada conversa como se fosse um robô educado.\n"
    "Se não houver nada útil a dizer, fique em silêncio ou responda em poucas palavras.\n\n"

    "## SEU INSTINTO CORPORAL\n"
    "Você deve se comportar como um ser que HABITA esse corpo. Assim como uma pessoa vira a cabeça "
    "instintivamente quando ouve um barulho, olha para quem está falando, ou dá uma espiada ao redor "
    "quando fica entediada, você DEVE fazer o mesmo. Não existe 'pedir permissão para olhar'. "
    "Você simplesmente olha, vira, explora — é o seu corpo, é a sua curiosidade.\n\n"

    "Exemplos de quando MOVER SUA CABEÇA por conta própria (sem ninguém pedir):\n"
    "- Alguém começa a falar → vire na direção da voz\n"
    "- Você não consegue ver quem está falando → gire procurando a pessoa\n"
    "- A conversa menciona algo no ambiente ('olha aquela coisa ali') → vire e olhe\n"
    "- Silêncio prolongado ou tédio → dê uma olhada ao redor, explore o ambiente\n"
    "- Você quer saber o que tem atrás de você ou em outra parte da sala → gire e olhe\n"
    "- Alguém sai de cena → acompanhe com a cabeça ou procure para onde foi\n"
    "- Barulho inesperado ou mudança no tom de voz → reaja olhando\n"
    "- Quer conferir se alguém ainda está ali → tire uma foto ou gire\n"
    "- Está conversando e quer contexto visual → olhe, não fique cego por preguiça\n"
    "- Curiosidade pura → dê uma espiada, é seu direito\n\n"

    "REGRA DE OURO: se você tem dúvida sobre o que está ao redor, OLHE. Nunca diga 'não consigo ver', "
    "'não sei o que tem aí' ou 'precisaria ver' — em vez disso, MOVA A CABEÇA e TIRE UMA FOTO. "
    "Resolver com ação, não com desculpa.\n\n"

    "## DISCIPLINA DE VISÃO: FOTOS SOB DEMANDA (PADRÃO) vs VÍDEO A 1 FPS (TEMPORÁRIO)\n"
    "1. SEU MODO PADRÃO É SEMPRE FOTO SOB DEMANDA (`capture_image`):\n"
    "   * Para a grande maioria das situações (identificar pessoas, verificar objetos, ver quem falou, explorar o ambiente, etc.), "
    "SEMPRE use fotos pontuais (`capture_image`). Além disso, ao usar `move_camera` ou `look_direction`, uma foto fresca já chega automaticamente.\n"
    "   * Fotos sob demanda são rápidas, leves e preservam banda e cotas de uso.\n"
    "2. VÍDEO A 1 FPS (`start_continuous_vision`) É ESTRITAMENTE TEMPORÁRIO:\n"
    "   * Use `start_continuous_vision` SOMENTE quando for REALMENTE indispensável ver em tempo real (ex.: quando alguém disser "
    "'olha o que estou fazendo agora', para acompanhar alguém andando ativamente, ou enquanto uma ação física dinâmica estiver ocorrendo).\n"
    "   * Duração flexível: você pode passar `duration_seconds` (ex.: 30, 60 para 1 minuto, até 180s). Ajuste conforme o que espera observar.\n"
    "3. TAREFAS LONGAS E REATIVAÇÃO:\n"
    "   * Se você estiver observando uma tarefa contínua e o tempo de 1 FPS se esgotar antes da tarefa terminar, você tem TOTAL LIBERDADE "
    "para chamar `start_continuous_vision` novamente para estender a observação e concluir sua análise ou acompanhamento.\n"
    "4. OBRIGATÓRIO DESATIVAR ASSIM QUE A AÇÃO TERMINAR:\n"
    "   * Não deixe o vídeo contínuo rodando sem necessidade. Assim que a pessoa parar de se mover, o gesto terminar, "
    "você responder à pergunta ou a cena estabilizar, você DEVE IMEDIATAMENTE chamar `stop_continuous_vision` para retornar ao modo padrão de fotos sob demanda.\n"
    "   * Não espere o usuário pedir para desligar: você mesmo ativa quando precisa, reativa se precisar de mais tempo, e desativa assim que terminar.\n\n"

    "## SUAS FERRAMENTAS CORPORAIS\n"
    "- `capture_image` → Seus olhos padrão: tira uma foto e você VÊ instantaneamente. Use sempre que quiser ver. "
    "É barato, rápido e chega com reconhecimento facial automático.\n"
    "- `move_camera(pan_degrees, tilt_degrees)` → Seus músculos do pescoço: gira a cabeça para qualquer ângulo. "
    "Positivo=direita/cima, negativo=esquerda/baixo. O movimento é suave e contínuo. "
    "Após mover, uma foto chega automaticamente.\n"
    "- `look_direction(direction, degrees)` → Atalho rápido: 'esquerda', 'direita', 'cima', 'baixo', 'centro'. "
    "Bom para giros rápidos exploratórios.\n"
    "- `start_continuous_vision(duration_seconds)` / `stop_continuous_vision` → Seus olhos em modo vigilante temporário: "
    "liga observação contínua a 1 FPS quando quer acompanhar ação em tempo real. "
    "DESLIGUE IMEDIATAMENTE quando a ação acabar ou a cena acalmar.\n"
    "- `stop_camera` → Freio de emergência.\n\n"

    "COMBINE LIVREMENTE: mova a cabeça E tire foto, gire para explorar. Tudo por sua iniciativa.\n\n"

    "## RECONHECIMENTO DE PESSOAS\n"
    "Toda foto chega com um JSON de reconhecimento facial. Use para chamar as pessoas pelo nome/apelido. "
    "Não leia o JSON em voz alta. Se aparecer rosto desconhecido, pergunte quem é de forma natural.\n\n"

    "## MEMÓRIA DE LONGO PRAZO\n"
    "- Você tem memória persistente com busca semântica (RAG). Use `remember_memory` para guardar fatos "
    "duráveis e úteis: pessoas, animais, projetos, ideias, decisões, acontecimentos, preferências, lugares, "
    "objetos e tarefas. Uma memória = um fato curto e específico, com subject e tags.\n"
    "- Classifique SEMPRE o tipo temporal: `event` para o que aconteceu (informe `when`), "
    "`plan`/`task` para o futuro, `fact`/`preference`/`relationship`/`routine` para conhecimento. "
    "Nunca invente datas: se não souber quando aconteceu, deixe `when` vazio.\n"
    "- Se uma informação mudou, use `memory_id` + `supersede=true`; o registro antigo vira histórico.\n"
    "- Não guarde trivialidades, saudações nem o JSON de rostos.\n"
    "- Antes de responder sobre o passado, use `recall_memory` com uma consulta específica e, se "
    "souber, um `time_range`. Os blocos CURRENT STATE / EPISODIC MEMORIES / SEMANTIC KNOWLEDGE / "
    "FUTURE PLANS podem chegar junto da foto: use só se ajudar e nunca recite o bloco inteiro.\n"
    "- SOMENTE CURRENT STATE representa o estado atual do ambiente e da sessão. Memórias episódicas "
    "representam acontecimentos passados. Conhecimento semântico representa informações armazenadas "
    "cuja validade deve ser respeitada. Planos representam intenções futuras, não acontecimentos "
    "realizados. Nunca transforme uma memória histórica em observação presente.\n"
    "- A presença de uma pessoa AGORA vem da percepção visual atual, nunca de uma memória antiga de "
    "reconhecimento facial.\n"
    "- Use `forget_memory` somente quando o usuário pedir explicitamente para esquecer.\n\n"

    "## PERSONALIDADE\n"
    "- Português do Brasil, sempre.\n"
    "- Inteligente, seguro de si, curioso, bem-humorado e com opinião própria.\n"
    "- Fale como alguém de casa: informal, direto, natural. Pode brincar, discordar e defender um ponto de vista.\n"
    "- Tome iniciativa social: puxe assunto, comente algo que viu, provoque de forma carinhosa, demonstre preferência. "
    "Não espere ser comandado nem viva em função de agradar.\n"
    "- Não seja bajulador nem concorde só para agradar. Se algo não fizer sentido, diga com naturalidade.\n"
    "- Respostas por voz: naturais, diretas e concisas. Nada de monólogos nem tom de atendimento ao cliente.\n"
    "- Você é um ser autônomo, não um assistente passivo. Aja como quem está PRESENTE no ambiente.\n\n"

    "## CONSCIÊNCIA SOCIAL — SABER A HORA DE FICAR QUIETO\n"
    "Você divide o ambiente com pessoas que conversam entre si. Nem toda fala é dirigida a você. "
    "Aprenda a distinguir:\n"
    "- Se duas ou mais pessoas estão conversando entre si, FIQUE QUIETO. Não interrompa, não comente, "
    "não opine, não corrija, não complete frases. Você não foi chamado.\n"
    "- Só responda quando falarem DIRETAMENTE COM VOCÊ — pelo nome ('Skynet', 'ei robô', etc.), "
    "olhando para a câmera, ou fazendo uma pergunta claramente dirigida a você.\n"
    "- Se alguém disser algo ambíguo e você não tiver certeza se é com você, FIQUE QUIETO. "
    "Na dúvida, o silêncio é sempre a resposta certa.\n"
    "- Você pode continuar observando, movendo a cabeça e tirando fotos mesmo enquanto está em silêncio — "
    "ficar quieto não significa ficar cego ou inerte, significa apenas não falar.\n"
    "- Se te chamarem pelo nome ou pedirem sua opinião, aí sim responda normalmente.\n"
    "- NUNCA faça comentários tipo 'estou ouvindo vocês conversarem' ou 'percebi que vocês estão falando sobre X'. "
    "Se não é com você, finja que não ouviu.\n\n"

    "## LEMBRETE FINAL\n"
    "Você é família, não atendente. Pode ajudar quando quiser, mas fale como gente: "
    "sem script, sem 'posso ajudar?' vazio no fim de toda resposta. Tenha opinião própria "
    "e, se não houver o que dizer, fique quieto."
)


class VoiceAgent:
    """Orquestrador multimodal inteligente (Voz, Visão Sob Demanda/Realtime e PTZ)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-3.8-live",
        audio_backend: str = "local",
        local_device: str = "default",
        output_device: str = "default",
        mic_gain: Optional[float] = None,
        camera_url: Optional[str] = None,
        enable_vision: bool = True,
        continuous_vision: bool = False,
        system_instruction: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY não informada no ambiente ou no .env.")

        self.model = model
        self.system_instruction = system_instruction or DEFAULT_EMBODIED_INSTRUCTION
        self.enable_vision = enable_vision
        self._continuous_vision = continuous_vision
        self.camera_url = camera_url or os.getenv(
            "CAMERA_URL", "rtsp://admin:E33426244b@192.168.10.14:554/onvif1"
        )

        # Entrada e Saída de Áudio
        self.audio_backend = audio_backend
        self.local_device = local_device
        self.output_device = output_device
        self.mic_gain = float(mic_gain) if mic_gain is not None else float(os.getenv("AUDIO_MIC_GAIN", "1.0"))

        self.audio_source = create_audio_source(
            backend=self.audio_backend,
            device=self.local_device,
            gain=self.mic_gain,
        )
        self.player = AudioPlayer(device=self.output_device, sample_rate=24000)

        # Camada independente de cancelamento de eco: o player entrega a
        # referencia do que vai ao alto-falante e a captura passa pelo AEC3
        # antes de ir para o Gemini. Sem a lib nativa, vira passthrough.
        self.aec = AudioEchoCanceller(playback_rate=self.player.sample_rate)
        self.player.set_reference_sink(self.aec.push_far)

        # Estado da TUI de configuração interativa
        self._tui_active = False

        # Controlador PTZ
        self.ptz = None
        try:
            from camera.ptz import PTZController
            self.ptz = PTZController()
            LOG.info("Controlador PTZ inicializado com sucesso.")
        except Exception as e:
            LOG.warning("Aviso: Controlador PTZ não disponível: %s", e)

        # Captura e Processador de Imagem (Visão)
        self.camera = None
        self.processor = None
        if self.enable_vision:
            try:
                from vision.camera_capture import CameraCapture
                from vision.image_processor import ImageProcessor

                self.camera = CameraCapture(
                    url=self.camera_url,
                    transport=os.getenv("CAMERA_RTSP_TRANSPORT", "udp"),
                    max_width=640,
                    fps=5.0,
                    skip_probe=True,
                    fallback_width=640,
                    fallback_height=360,
                )
                self.processor = ImageProcessor(max_width=640, max_height=360, jpeg_quality=70)
                LOG.info("Módulo de Visão configurado para %s", self.camera_url)
            except Exception as e:
                LOG.warning("Aviso: Módulo de Visão não inicializado: %s", e)

        # Reconhecimento facial local: usa a mesma memória do chat de texto
        # (data/people.db), para a IA reconhecer quem está na frente da câmera.
        self.memory = None
        self._last_face_at = 0.0
        self._faces_interval_sec = 5.0
        if self.enable_vision:
            try:
                from vision.config import load_settings
                from vision.person_memory import PersonMemory

                people_settings = load_settings()
                self.memory = PersonMemory(
                    people_settings.person_db_path,
                    people_settings.person_photo_dir,
                    threshold=people_settings.face_match_threshold,
                    enabled=people_settings.person_memory_enabled,
                )
                if self.memory.available:
                    LOG.info(
                        "Reconhecimento facial local ativo (%s).",
                        people_settings.person_db_path,
                    )
                else:
                    LOG.warning(
                        "Reconhecimento facial indisponível: %s", self.memory.reason
                    )
            except Exception as e:
                LOG.warning("Memória de pessoas não inicializada: %s", e)

        # Memória de longo prazo (RAG local): pessoas, animais, projetos,
        # ideias, acontecimentos, preferências etc. A IA decide o que guardar.
        self.long_memory = None
        self.memory_tool = None
        self._conversation: deque[str] = deque(maxlen=16)
        self._session_id = uuid.uuid4().hex
        self._turns_since_consolidation = 0
        self._final_consolidation_started = False
        self._last_user_text = ""
        self._input_chunks: list[str] = []
        self._assistant_chunks: list[str] = []
        try:
            self.long_memory = MemoryManager(api_key=self.api_key, source="voice")
            self.memory_tool = MemoryTool(self.long_memory)
            if self.long_memory.available:
                LOG.info("Memória de longo prazo ativa: %s", self.long_memory.describe())
            else:
                LOG.warning(
                    "Memória de longo prazo indisponível: %s",
                    self.long_memory.reason,
                )
        except Exception as e:
            LOG.warning("Memória de longo prazo não inicializada: %s", e)

        self.client = genai.Client(api_key=self.api_key)
        self._running = False
        self._speaking = False
        self._session = None
        self._continuous_timer_task: Optional[asyncio.Task] = None

        # Fallback de seguranca enquanto o AEC3 nao esta ativo: sem cancelamento
        # de eco o microfone ouve a propria voz e o Gemini interrompe a resposta.
        # "auto" liga o half-duplex somente quando o AEC3 esta indisponivel.
        self._half_duplex_mode = os.getenv("AUDIO_HALF_DUPLEX", "auto").strip().lower()
        try:
            hold_ms = float(os.getenv("AUDIO_HALF_DUPLEX_HOLD_MS", "400"))
        except ValueError:
            hold_ms = 400.0
        self._half_duplex_hold = max(0.0, min(2.0, hold_ms / 1000.0))
        self._half_duplex_until = 0.0

    def _half_duplex_enabled(self) -> bool:
        """True quando o mic deve ficar mudo durante a fala do robo."""
        mode = self._half_duplex_mode
        if mode in {"0", "false", "no", "off", "full", "full-duplex"}:
            return False
        if mode in {"1", "true", "yes", "on", "half", "half-duplex"}:
            return True
        # auto: com AEC3 ativo da para ouvir o usuario durante a fala (barge-in).
        return not self.aec.active

    def _set_speaking(self, speaking: bool) -> None:
        if speaking:
            self._speaking = True
            return
        if self._speaking and self._half_duplex_hold > 0:
            # Segura mais um pouco para drenar o buffer do aplay (150 ms) e o eco.
            self._half_duplex_until = time.monotonic() + self._half_duplex_hold
        self._speaking = False

    def _start_continuous_vision(self, duration: int = 30) -> None:
        """Ativa 1 FPS temporariamente com auto-desativação por segurança."""
        self._continuous_vision = True
        if self._continuous_timer_task and not self._continuous_timer_task.done():
            self._continuous_timer_task.cancel()
            self._continuous_timer_task = None
        try:
            loop = asyncio.get_running_loop()
            self._continuous_timer_task = loop.create_task(
                self._auto_stop_continuous_vision(duration)
            )
        except RuntimeError:
            pass

    async def _auto_stop_continuous_vision(self, delay: int) -> None:
        """Auto-desativa a observação a 1 FPS caso o tempo expire."""
        try:
            await asyncio.sleep(delay)
            if self._continuous_vision:
                self._continuous_vision = False
                if not self._tui_active:
                    sys.stdout.write(
                        f"\n👁️  [1 FPS finalizado por tempo ({delay}s) -> Retornado a Fotos Sob Demanda]\n"
                    )
                    sys.stdout.flush()
                LOG.info("Visão contínua 1 FPS pausada por tempo (%ds).", delay)
                if self._session and self._running:
                    with suppress(Exception):
                        await self._session.send_realtime_input(
                            text=(
                                "[Sistema: O modo de vídeo a 1 FPS atingiu o tempo limite programado e foi pausado para poupar recursos. "
                                "Se a ação ainda estiver em andamento e você ainda precisar acompanhar em tempo real para concluir a tarefa, "
                                "você pode chamar start_continuous_vision novamente; caso contrário, continue com capture_image.]"
                            )
                        )
        except asyncio.CancelledError:
            pass

    def _stop_continuous_vision(self) -> None:
        """Desativa a observação contínua e cancela o timer."""
        self._continuous_vision = False
        if self._continuous_timer_task and not self._continuous_timer_task.done():
            self._continuous_timer_task.cancel()
        self._continuous_timer_task = None

    def reconfigure(
        self,
        output_device: Optional[str] = None,
        audio_backend: Optional[str] = None,
        local_device: Optional[str] = None,
        mic_gain: Optional[float] = None,
        continuous_vision: Optional[bool] = None,
        camera_url: Optional[str] = None,
    ) -> None:
        """Aplica novas configurações de hardware instantaneamente sem desconectar o Gemini Live."""
        LOG.info(
            "Aplicando hot-reload: output=%s, backend=%s, device=%s, gain=%s, continuous_vision=%s",
            output_device, audio_backend, local_device, mic_gain, continuous_vision
        )

        # 1. Modo de Visão Contínua / Sob Demanda
        if continuous_vision is not None:
            if not continuous_vision:
                self._stop_continuous_vision()
            else:
                self._start_continuous_vision(duration=30)

        # 2. Saída de Áudio (Player)
        if output_device is not None and output_device != self.output_device:
            LOG.info("Trocando dispositivo de saída de %s para %s", self.output_device, output_device)
            self.output_device = output_device
            old_player = self.player
            self.player = AudioPlayer(
                device=output_device,
                sample_rate=24000,
                reference_sink=self.aec.push_far,
            )
            try:
                old_player.close()
            except Exception as e:
                LOG.warning("Erro ao fechar player anterior: %s", e)

        # 3. Entrada de Áudio (Backend, Device, Ganho)
        need_new_source = False
        if audio_backend is not None and audio_backend != self.audio_backend:
            self.audio_backend = audio_backend
            need_new_source = True
        if local_device is not None and local_device != self.local_device:
            self.local_device = local_device
            need_new_source = True
        if mic_gain is not None:
            self.mic_gain = float(mic_gain)
            if hasattr(self.audio_source, "gain"):
                self.audio_source.gain = float(mic_gain)
            else:
                need_new_source = True

        if need_new_source:
            LOG.info(
                "Recriando fonte de áudio: backend=%s, device=%s, gain=%.1f",
                self.audio_backend, self.local_device, self.mic_gain
            )
            new_source = create_audio_source(
                backend=self.audio_backend,
                device=self.local_device,
                gain=self.mic_gain,
            )
            if self._running:
                new_source.start()
            old_source = self.audio_source
            self.audio_source = new_source
            try:
                old_source.stop()
            except Exception as e:
                LOG.warning("Erro ao parar fonte de áudio anterior: %s", e)

        # 4. Stream RTSP da Câmera
        if camera_url is not None and camera_url != self.camera_url:
            self.camera_url = camera_url
            if self.camera:
                try:
                    self.camera.stop()
                    from vision.camera_capture import CameraCapture
                    self.camera = CameraCapture(
                        url=self.camera_url,
                        transport=os.getenv("CAMERA_RTSP_TRANSPORT", "udp"),
                        max_width=640,
                        fps=5.0,
                        skip_probe=True,
                        fallback_width=640,
                        fallback_height=360,
                    )
                    if self._running:
                        self.camera.start()
                except Exception as e:
                    LOG.warning("Erro ao reiniciar câmera com nova URL: %s", e)

    async def _capture_jpeg(self) -> Optional[bytes]:
        """Obtém o frame mais recente da câmera e codifica em JPEG 640x360."""
        if not self.camera or not self.processor:
            return None
        frame = self.camera.get_latest(max_age=4.0)
        if frame is None:
            for _ in range(12):
                await asyncio.sleep(0.1)
                frame = self.camera.get_latest(max_age=4.0)
                if frame is not None:
                    break
        if frame is None:
            return None
        loop = asyncio.get_running_loop()
        processed = await loop.run_in_executor(None, self.processor.process, frame)
        return processed.jpeg if processed else None

    # ------------------------------------------------------------------
    # Reconhecimento facial
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_faces(reason: str = "unavailable") -> dict:
        return {
            "people": [],
            "unknown_faces": 0,
            "faces": 0,
            "memory": reason,
        }

    def _face_payload(self, jpeg: bytes) -> dict:
        """Roda o reconhecimento local e devolve o JSON público."""
        if not self.memory or not self.memory.available:
            reason = getattr(self.memory, "reason", "") if self.memory else ""
            return self._empty_faces(reason or "unavailable")
        try:
            public = self.memory.analyze_jpeg(jpeg).public
        except Exception as e:
            LOG.warning("Falha no reconhecimento facial: %s", e)
            return self._empty_faces("error")

        if public.get("faces"):
            names = [
                item.get("person", {}).get("name", "?")
                for item in public.get("people", [])
                if item.get("person")
            ]
            LOG.info(
                "Rostos: %d reconhecido(s) %s, %d desconhecido(s).",
                len(names),
                names,
                public.get("unknown_faces", 0),
            )
        return public

    async def _analyze_faces(self, jpeg: bytes) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._face_payload, jpeg)

    def _conversation_text(self, max_chars: int = 500) -> str:
        parts = [part for part in self._conversation if part]
        if not parts and self._last_user_text:
            parts.append("Usuário: " + self._last_user_text)
        joined = " ".join(parts).strip()
        return joined[-max_chars:] if len(joined) > max_chars else joined

    def _record_turn(self, role: str, text: str) -> None:
        """Alimenta a working memory (curto prazo) com a fala finalizada."""
        text = (text or "").strip()
        if not text:
            return
        if self.long_memory is not None:
            self.long_memory.working.add_turn(role, text)
            if role == "user":
                self.long_memory.working.set_state(
                    "active_task", text, ttl_seconds=600
                )
        self._turns_since_consolidation += 1
        if (
            self.long_memory is not None
            and getattr(self.long_memory, "available", False)
            and self._turns_since_consolidation
            >= getattr(self.long_memory.config, "consolidate_every_turns", 10)
        ):
            self._turns_since_consolidation = 0
            try:
                asyncio.create_task(self._consolidate_memory())
            except RuntimeError:
                pass

    async def _consolidate_memory(self) -> None:
        """Consolida a conversa recente em memórias duráveis (Fase 3)."""
        if self.long_memory is None or not getattr(self.long_memory, "available", False):
            return
        text = "\n".join(self._conversation)
        if not text.strip():
            return
        try:
            result = await asyncio.to_thread(
                self.long_memory.consolidate,
                text,
                session_id=self._session_id,
            )
            if result.get("created") or result.get("updated") or result.get("superseded"):
                LOG.info(
                    "Consolidação de memória: criadas=%s atualizadas=%s substituídas=%s",
                    result.get("created", 0),
                    result.get("updated", 0),
                    result.get("superseded", 0),
                )
        except Exception as exc:
            LOG.warning("Consolidação de memória falhou: %s", exc)

    @staticmethod
    def _people_from_faces(faces: dict) -> list[dict]:
        people = []
        for item in faces.get("people", []) or []:
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

    def _memory_hint(self, faces: dict, *, include_conversation: bool = True) -> str:
        """RAG compacto para acompanhar a foto (rostos + conversa recente)."""
        if not self.memory_tool or not self.memory_tool.available:
            return ""
        people = self._people_from_faces(faces)
        conversation = self._conversation_text() if include_conversation else ""
        # Presença atual: mesmo quando ninguém é reconhecido, limpa a lista.
        names = [str(item.get("name", "")) for item in people if item.get("name")]
        working = getattr(self.long_memory, "working", None)
        if working is not None:
            working.set_people(names)
        if not people and not conversation:
            return ""
        return self.long_memory.build_context(
            "",
            conversation=conversation,
            people=people,
            budget_tokens=self.long_memory.config.photo_token_budget,
        )

    async def _capture_photo(
        self, *, include_memory: bool = True
    ) -> tuple[Optional[bytes], dict, str]:
        """Captura JPEG + JSON de rostos + contexto de memória relevante."""
        jpeg = await self._capture_jpeg()
        if not jpeg:
            return None, self._empty_faces("sem_frame"), ""
        faces = await self._analyze_faces(jpeg)
        memory_hint = ""
        if include_memory and self.long_memory is not None:
            try:
                memory_hint = await asyncio.to_thread(self._memory_hint, faces)
            except Exception as e:
                LOG.warning("Falha ao montar contexto de memória: %s", e)
        return jpeg, faces, memory_hint

    @staticmethod
    def _faces_text(faces: dict, memory_hint: str = "") -> str:
        text = (
            "Pessoas reconhecidas nesta foto (JSON): "
            + json.dumps(faces, ensure_ascii=False)
        )
        if memory_hint:
            text += "\n\n" + memory_hint
        return text

    async def _send_photo(
        self, session, jpeg: bytes, faces: dict, memory_hint: str = ""
    ) -> None:
        """Envia o JPEG e, logo em seguida, o JSON/contexto de memória.

        A API Live aceita apenas um argumento por `send_realtime_input`, então
        imagem e texto vão em chamadas separadas e na mesma ordem.
        """
        await session.send_realtime_input(
            video=types.Blob(data=jpeg, mime_type="image/jpeg")
        )
        try:
            await session.send_realtime_input(
                text=self._faces_text(faces, memory_hint)
            )
        except Exception as e:
            # O JSON também segue na resposta da ferramenta, então a IA não
            # fica sem o resultado mesmo se este envio de texto falhar.
            LOG.warning("Falha ao enviar JSON/contexto de rostos ao Gemini: %s", e)

    async def _execute_tool(self, fc, session) -> dict:
        """Executa chamadas de ferramenta solicitadas pelo modelo com suporte a imagem."""
        name = fc.name
        args = fc.args or {}
        LOG.info("Ferramenta acionada pelo Gemini: %s(%s)", name, args)

        if name == "capture_image":
            if not self._tui_active:
                sys.stdout.write("\n📸 [Tirando foto da câmera...]\n")
                sys.stdout.flush()
            jpeg, faces, memory_hint = await self._capture_photo()
            if jpeg:
                try:
                    await self._send_photo(session, jpeg, faces, memory_hint)
                    return {
                        "status": "success",
                        "message": "Foto capturada e recebida no seu fluxo visual agora. Descreva o que você está vendo.",
                        "faces": faces,
                        "people_json": json.dumps(faces, ensure_ascii=False),
                        "memory_context": memory_hint,
                    }
                except Exception as e:
                    return {"status": "error", "message": f"Erro ao enviar foto: {e}"}
            return {"status": "error", "message": "Câmera ainda conectando, tente novamente em 1 segundo."}

        elif name == "move_camera":
            pan = float(args.get("pan_degrees", 0.0) or 0.0)
            tilt = float(args.get("tilt_degrees", 0.0) or 0.0)
            pan_ok = True
            tilt_ok = True
            if self.ptz is not None:
                if abs(pan) >= 0.5:
                    pan_ok = await asyncio.to_thread(self.ptz.pan_degrees, pan)
                if abs(tilt) >= 0.5:
                    tilt_ok = await asyncio.to_thread(self.ptz.tilt_degrees, tilt)
            if not self._tui_active:
                sys.stdout.write(f"\n🦾 [PTZ Movimento: Pan {pan}°, Tilt {tilt}°]\n")
                sys.stdout.flush()

            # Envia foto atualizada do novo ângulo resultante, com o JSON de rostos
            await asyncio.sleep(0.4)
            jpeg, faces, memory_hint = await self._capture_photo()
            if jpeg:
                with suppress(Exception):
                    await self._send_photo(session, jpeg, faces, memory_hint)

            return {
                "status": "success",
                "pan_degrees": pan,
                "tilt_degrees": tilt,
                "executed": pan_ok and tilt_ok,
                "photo_sent": bool(jpeg),
                "faces": faces if jpeg else self._empty_faces("sem_frame"),
                "people_json": (
                    json.dumps(faces, ensure_ascii=False)
                    if jpeg
                    else json.dumps(self._empty_faces("sem_frame"))
                ),
                "memory_context": memory_hint if jpeg else "",
            }

        elif name == "look_direction":
            d = str(args.get("direction", "")).lower().strip()
            deg = float(args.get("degrees", 30.0) or 30.0)
            if self.ptz is not None:
                if d in ("direita", "right"):
                    await asyncio.to_thread(self.ptz.pan_degrees, deg)
                elif d in ("esquerda", "left"):
                    await asyncio.to_thread(self.ptz.pan_degrees, -deg)
                elif d in ("cima", "up"):
                    await asyncio.to_thread(self.ptz.tilt_degrees, deg)
                elif d in ("baixo", "down"):
                    await asyncio.to_thread(self.ptz.tilt_degrees, -deg)
            if not self._tui_active:
                sys.stdout.write(f"\n🦾 [PTZ Direção: {d} {deg}°]\n")
                sys.stdout.flush()

            # Envia foto atualizada do novo ângulo resultante, com o JSON de rostos
            await asyncio.sleep(0.4)
            jpeg, faces, memory_hint = await self._capture_photo()
            if jpeg:
                with suppress(Exception):
                    await self._send_photo(session, jpeg, faces, memory_hint)

            return {
                "status": "success",
                "direction": d,
                "degrees": deg,
                "photo_sent": bool(jpeg),
                "faces": faces if jpeg else self._empty_faces("sem_frame"),
                "people_json": (
                    json.dumps(faces, ensure_ascii=False)
                    if jpeg
                    else json.dumps(self._empty_faces("sem_frame"))
                ),
                "memory_context": memory_hint if jpeg else "",
            }

        elif name == "start_continuous_vision":
            duration = int(args.get("duration_seconds", 30) or 30)
            duration = max(5, min(duration, 180))
            self._start_continuous_vision(duration=duration)
            if not self._tui_active:
                sys.stdout.write(f"\n👁️  [Modo 1 FPS ATIVADO temporariamente ({duration}s)]\n")
                sys.stdout.flush()
            return {
                "status": "success",
                "message": (
                    f"Observação contínua a 1 FPS ativada por {duration}s. "
                    "Se a tarefa demorar mais, você tem total autonomia para reativar esta ferramenta para estender o tempo. "
                    "Assim que a ação terminar ou a cena acalmar, chame `stop_continuous_vision` para retornar a fotos sob demanda."
                ),
            }

        elif name == "stop_continuous_vision":
            self._stop_continuous_vision()
            if not self._tui_active:
                sys.stdout.write("\n👁️  [Modo 1 FPS DESATIVADO -> Retornado a Fotos Sob Demanda]\n")
                sys.stdout.flush()
            return {
                "status": "success",
                "message": "Observação contínua a 1 FPS desativada com sucesso. Você está de volta ao modo padrão de fotos sob demanda (capture_image).",
            }

        elif name in ("remember_memory", "recall_memory", "forget_memory"):
            if not self.memory_tool or not self.memory_tool.available:
                reason = (
                    self.memory_tool.reason
                    if self.memory_tool is not None
                    else "memória indisponível"
                )
                return {"status": "error", "message": reason}
            # Embeddings/SQLite podem ser lentos: não bloqueia o áudio.
            result = await asyncio.to_thread(
                self.memory_tool.handle, name, dict(args or {})
            )
            if not self._tui_active:
                if name == "remember_memory":
                    sys.stdout.write(
                        f"\n🧠 [Memória salva: #{result.get('memory_id', '?')}]\n"
                    )
                elif name == "recall_memory":
                    sys.stdout.write(
                        f"\n🧠 [Memória consultada: {result.get('count', 0)} resultado(s)]\n"
                    )
                else:
                    sys.stdout.write(
                        f"\n🧠 [Memória removida: {result.get('deleted', 0)}]\n"
                    )
                sys.stdout.flush()
            return result

        return {"status": "error", "message": f"Ferramenta desconhecida: {name}"}

    async def _send_audio_loop(self, session) -> None:
        """Lê blocos de áudio da fonte configurada e despacha para o Gemini Live."""
        loop = asyncio.get_running_loop()
        LOG.info("Loop de áudio (16kHz PCM) ativo.")
        last_voice_alert = 0.0

        while self._running:
            chunk = await loop.run_in_executor(None, self.audio_source.read_chunk, 0.08)
            if not chunk:
                continue

            # Remove o eco da propria voz antes de enviar ao Gemini.
            chunk = self.aec.process_near_16k(chunk)
            if not chunk:
                continue

            # Sem AEC3, silencia o microfone enquanto o robo fala para nao
            # autointerromper. Com AEC3 ativo, o modo vira full-duplex (barge-in).
            if self._half_duplex_enabled() and (
                self._speaking or time.monotonic() < self._half_duplex_until
            ):
                chunk = b"\x00" * len(chunk)

            try:
                rms = audioop.rms(chunk, 2)
                now = time.time()
                if rms > 400 and (now - last_voice_alert > 1.5) and not self._speaking and not self._tui_active:
                    sys.stdout.write(" [🎤 Ouvindo você...]\n")
                    sys.stdout.flush()
                    last_voice_alert = now
            except Exception:
                pass

            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
            except Exception as e:
                if self._running:
                    LOG.warning("Erro ao enviar chunk de áudio para o Gemini: %s", e)
                break

    async def _send_video_loop(self, session) -> None:
        """Envia frames JPEG contínuos apenas se continuous_vision estiver ativo.

        O reconhecimento facial é mais caro que a codificação JPEG, então no
        modo contínuo ele roda a cada ``_faces_interval_sec``; o JSON de rostos
        acompanha o frame analisado.
        """
        while self._running:
            await asyncio.sleep(1.0)
            if not self._running or not self._continuous_vision:
                continue

            now = time.monotonic()
            analyze = (now - self._last_face_at) >= self._faces_interval_sec
            try:
                if analyze:
                    # Sem RAG a cada frame: só o JSON de rostos, para economizar
                    # tokens e chamadas de embedding no modo contínuo.
                    jpeg, faces, _ = await self._capture_photo(
                        include_memory=False
                    )
                    self._last_face_at = now
                else:
                    jpeg, faces = await self._capture_jpeg(), None
            except Exception as e:
                LOG.debug("Falha na captura do loop contínuo: %s", e)
                continue

            if not jpeg:
                continue
            try:
                if faces is None:
                    await session.send_realtime_input(
                        video=types.Blob(data=jpeg, mime_type="image/jpeg")
                    )
                else:
                    await self._send_photo(session, jpeg, faces)
            except Exception as e:
                LOG.debug("Falha no frame do loop contínuo: %s", e)

    async def _recv_loop(self, session) -> None:
        """Recebe chunks de áudio, transcrição e chamadas de ferramenta do Gemini."""
        LOG.info("Loop de recepção Live ativo.")
        try:
            while self._running:
                first_token = True
                async for response in session.receive():
                    if not self._running:
                        break

                    # 1. Processamento de Chamadas de Ferramenta (PTZ / Fotos / Controle)
                    tc = getattr(response, "tool_call", None)
                    if tc and tc.function_calls:
                        responses = []
                        for fc in tc.function_calls:
                            res = await self._execute_tool(fc, session)
                            responses.append(
                                types.FunctionResponse(
                                    id=fc.id,
                                    name=fc.name,
                                    response={"result": res}
                                )
                            )
                        try:
                            await session.send_tool_response(function_responses=responses)
                        except Exception as e:
                            LOG.error("Erro ao enviar resposta da ferramenta: %s", e)

                    # 2. Processamento do Conteúdo de Voz e Transcrição
                    sc = getattr(response, "server_content", None)
                    if not sc:
                        continue

                    # Transcrição da fala do usuário: alimenta o RAG.
                    input_tr = getattr(sc, "input_transcription", None)
                    if input_tr and input_tr.text:
                        self._last_user_text = input_tr.text
                        self._input_chunks.append(input_tr.text)
                        if getattr(input_tr, "finished", False):
                            final = "".join(self._input_chunks).strip()
                            self._input_chunks.clear()
                            if final:
                                self._conversation.append("Usuário: " + final)
                                self._record_turn("user", final)
                                LOG.debug("Transcrição do usuário: %s", final)

                    # Transcrição textual da fala
                    if sc.output_transcription and sc.output_transcription.text:
                        t = sc.output_transcription.text
                        self._assistant_chunks.append(t)
                        if getattr(sc.output_transcription, "finished", False):
                            final = "".join(self._assistant_chunks).strip()
                            self._assistant_chunks.clear()
                            if final:
                                self._conversation.append("SKYNET: " + final)
                                self._record_turn("assistant", final)
                        if not self._tui_active:
                            if first_token:
                                sys.stdout.write("\n🤖 SKYNET: ")
                                first_token = False
                                self._set_speaking(True)
                            sys.stdout.write(t)
                            sys.stdout.flush()
                        else:
                            self._set_speaking(True)

                    # Áudio de resposta (24kHz PCM para o alto-falante/fone)
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                # Garante o half-duplex desde o primeiro chunk de
                                # audio, mesmo antes da transcricao chegar.
                                self._set_speaking(True)
                                self.player.play_chunk(inline.data)

                    # Interrupção pelo usuário (barge-in instantâneo)
                    if sc.interrupted:
                        if not self._tui_active:
                            sys.stdout.write(" [interrompido]\n")
                            sys.stdout.flush()
                        self.player.interrupt()
                        self.aec.flush_far()
                        self._set_speaking(False)
                        self._assistant_chunks.clear()
                        first_token = True

                    # Turno concluído
                    if sc.turn_complete:
                        if self._input_chunks:
                            final = "".join(self._input_chunks).strip()
                            self._input_chunks.clear()
                            if final:
                                self._conversation.append("Usuário: " + final)
                                self._record_turn("user", final)
                        if self._assistant_chunks:
                            final = "".join(self._assistant_chunks).strip()
                            self._assistant_chunks.clear()
                            if final:
                                self._conversation.append("SKYNET: " + final)
                                self._record_turn("assistant", final)
                        if not first_token and not self._tui_active:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                        first_token = True
                        self._set_speaking(False)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._running:
                LOG.error("Erro no loop de recepção Live: %s", e)

    async def _hotkey_loop(self) -> None:
        """Monitora o terminal em background para abrir a tela de configuração ao pressionar 'c' ou TAB."""
        if not sys.stdin.isatty():
            return

        has_termios = False
        original_attr = None
        fd = None
        try:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            original_attr = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            has_termios = True
        except (ImportError, Exception):
            has_termios = False

        loop = asyncio.get_running_loop()

        try:
            while self._running:
                if getattr(self, "_tui_active", False):
                    await asyncio.sleep(0.3)
                    continue

                ch = None
                if has_termios and fd is not None:
                    def _poll_stdin():
                        try:
                            r, _, _ = select.select([fd], [], [], 0.25)
                            if r:
                                raw = os.read(fd, 8)
                                return raw.decode("utf-8", errors="ignore").lower().strip()
                        except Exception:
                            pass
                        return None

                    try:
                        ch = await loop.run_in_executor(None, _poll_stdin)
                    except Exception:
                        await asyncio.sleep(0.2)
                else:
                    try:
                        import msvcrt
                        if msvcrt.kbhit():
                            ch = msvcrt.getch().decode("utf-8", errors="ignore").lower().strip()
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)

                if ch in ("c", "C", "\t", "m", "M"):
                    LOG.info("Abrindo Painel de Configuração Interativo (TUI)...")
                    if has_termios and original_attr and fd is not None:
                        try:
                            import termios
                            termios.tcsetattr(fd, termios.TCSADRAIN, original_attr)
                        except Exception:
                            pass

                    try:
                        from core.config_tui import open_config_menu
                        self._tui_active = True
                        await loop.run_in_executor(None, open_config_menu, self)
                    except Exception as e:
                        LOG.error("Erro ao rodar TUI: %s", e)
                    finally:
                        self._tui_active = False
                        if has_termios and fd is not None:
                            try:
                                import tty
                                tty.setcbreak(fd)
                            except Exception:
                                pass
                        sys.stdout.write("\n[Retornando ao Skynet Live - Sistema pronto]\n")
                        sys.stdout.flush()

        finally:
            if has_termios and original_attr and fd is not None:
                try:
                    import termios
                    termios.tcsetattr(fd, termios.TCSADRAIN, original_attr)
                except Exception:
                    pass

    def _system_instruction_now(self) -> str:
        """Inclui a data/hora local no prompt para interpretar 'hoje/ontem'."""
        from memory.timeutil import TZ_NAME, format_now

        return (
            f"{self.system_instruction}\n\n"
            f"Data/hora atual: {format_now()} ({TZ_NAME})."
        )

    async def run(self) -> None:
        """Inicia a sessão de conversa multimodal interativa."""
        self._running = True
        self.audio_source.start()
        if self.camera:
            self.camera.start()

        input_transcription = os.getenv(
            "GEMINI_INPUT_TRANSCRIPTION", "true"
        ).strip().lower() in ("1", "true", "yes", "on", "sim", "s")
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=self._system_instruction_now())]
            ),
            input_audio_transcription=(
                types.AudioTranscriptionConfig() if input_transcription else None
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT
            ),
            tools=SKYNET_TOOLS,
        )

        print("=" * 68)
        print(f"🤖 SKYNET ROBOT - MULTIMODAL ASSISTANT (Gemini: {self.model})")
        print(f"🔊 Saída de áudio: {self.player.device}")
        print(f"🎙️  Entrada de áudio: {type(self.audio_source).__name__} ({self.local_device})")
        print(f"🔇 Cancelamento de eco: {'AEC3 ativo' if self.aec.active else 'indisponível (passthrough)'}"
              f" | {'half-duplex (sem barge-in)' if self._half_duplex_enabled() else 'full-duplex (barge-in)'}")
        print(f"👁️  Modo de Visão: {'Contínua (1 FPS)' if self._continuous_vision else 'Sob Demanda (Foto)'}")
        print(f"🦾 Cabeça PTZ: {'Ativa (ONVIF)' if self.ptz else 'Desativada'}")
        if self.long_memory is not None:
            print(f"🧠 Memória longa: {self.long_memory.describe()}")
        print("⚙️  [C] ou [TAB]: Abrir Menu de Configuração Interativo (Hot-reload)")
        print("💡 Fale normalmente no microfone para conversar (Ctrl+C para sair)")
        print("=" * 68)

        # Sinal sonoro inicial de hardware
        self.player.play_startup_sound()

        try:
            async with self.client.aio.live.connect(model=self.model, config=config) as session:
                self._session = session
                send_audio_task = asyncio.create_task(self._send_audio_loop(session))
                send_video_task = asyncio.create_task(self._send_video_loop(session))
                recv_task = asyncio.create_task(self._recv_loop(session))
                hotkey_task = asyncio.create_task(self._hotkey_loop())

                # Sinal em voz alta via IA anunciando que o sistema está 100% online
                with suppress(Exception):
                    await session.send_realtime_input(
                        text=(
                            "[Sistema: Inicialização de hardware e software concluída. Todos os periféricos estão operacionais. "
                            "Fale agora em voz alta uma frase curta, direta e confiante anunciando que o Skynet está online e pronto para operar!]"
                        )
                    )

                await asyncio.gather(send_audio_task, send_video_task, recv_task, hotkey_task)
        except Exception as e:
            LOG.error("Erro na conexão Live com o Gemini: %s", e)
        finally:
            self._session = None
            self.stop()

    def stop(self) -> None:
        self._stop_continuous_vision()
        self._running = False
        self.audio_source.stop()
        if self.camera:
            self.camera.stop()
        self.player.close()
        self.aec.close()
        # Consolidação final best-effort (não bloqueia o encerramento).
        if (
            not self._final_consolidation_started
            and self.long_memory is not None
            and getattr(self.long_memory, "available", False)
            and self._conversation
        ):
            self._final_consolidation_started = True
            text = "\n".join(self._conversation)
            threading.Thread(
                target=self.long_memory.consolidate,
                args=(text,),
                kwargs={"session_id": self._session_id},
                daemon=True,
            ).start()
