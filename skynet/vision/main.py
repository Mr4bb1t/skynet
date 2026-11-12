"""Ponto de entrada e testes em etapas do subsistema de visao SKYNET.

Uso (a partir de /root/skynet):
    python3 -m vision.main --test 1          # camera
    python3 -m vision.main --test 2          # processamento
    python3 -m vision.main --test 3          # conexao Gemini
    python3 -m vision.main --test 4          # imagem unica
    python3 -m vision.main --test 5          # heartbeat
    python3 -m vision.main --test 6          # recebimento
    python3 -m vision.main --test 7          # reconexao
    python3 -m vision.main --test 8 --stability-minutes 30
    python3 -m vision.main --run             # pipeline completo

Nada aqui implementa deteccao local, audio, motores ou navegacao.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PIL import Image

from .camera_capture import CameraCapture, Frame
from .config import (
    IMAGE_PRESETS,
    VALID_MEDIA_RESOLUTIONS,
    ConfigError,
    Settings,
    load_settings,
    mask_url,
)
from .diagnostics import CsvRecorder, Diagnostics
from .image_heartbeat import ImageHeartbeat
from .image_processor import ImageProcessor, ProcessedFrame
from .session_manager import SessionManager

LOG = logging.getLogger("vision.main")

DEFAULT_QUESTION = (
    "Descreva brevemente, em uma frase, o que voce ve na imagem mais recente. "
    "Nao invente informacoes que nao estejam visiveis."
)

TEST_STAGES = {
    1: "camera",
    2: "process",
    3: "connect",
    4: "image",
    5: "heartbeat",
    6: "receive",
    7: "reconnect",
    8: "stability",
}

STAGE_DESCRIPTIONS = {
    "camera": "TESTE 1 - captura continua da camera",
    "process": "TESTE 2 - redimensionamento + JPEG",
    "connect": "TESTE 3 - conexao Gemini Live",
    "image": "TESTE 4 - imagem unica interpretada",
    "heartbeat": "TESTE 5 - heartbeat visual ~1 FPS",
    "receive": "TESTE 6 - recebimento durante o envio",
    "reconnect": "TESTE 7 - reconexao sem parar a camera",
    "stability": "TESTE 8 - estabilidade prolongada",
    "run": "PIPELINE COMPLETO",
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _load_image(path: str) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        return image.copy()


def _write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def _result(stage: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    LOG.info("RESULT %s: %s%s", status, STAGE_DESCRIPTIONS.get(stage, stage), f" - {detail}" if detail else "")
    return passed


def _aspect_ok(orig_w: int, orig_h: int, out_w: int, out_h: int) -> bool:
    if not orig_w or not orig_h or not out_w or not out_h:
        return False
    return abs((out_w / out_h) - (orig_w / orig_h)) <= 0.02


def _require_heartbeat(settings: Settings, stage: str) -> bool:
    if settings.heartbeat_enabled:
        return True
    LOG.error(
        "Heartbeat desativado (IMAGE_HEARTBEAT_ENABLED=false). "
        "Ative no .env para executar este teste."
    )
    return _result(stage, False, "heartbeat desativado por configuracao")


# ---------------------------------------------------------------------------
# Pipeline compartilhado
# ---------------------------------------------------------------------------
class Pipeline:
    """Sobe camera, processador, sessao e heartbeat na ordem correta."""

    def __init__(
        self,
        settings: Settings,
        diagnostics: Diagnostics,
        *,
        need_camera: bool = True,
        need_gemini: bool = True,
        need_heartbeat: bool = False,
        csv_recorder: Optional[CsvRecorder] = None,
        wait_first_frame: bool = True,
        wait_gemini: bool = True,
    ) -> None:
        self.settings = settings
        self.diag = diagnostics
        self.need_camera = need_camera
        self.need_gemini = need_gemini
        self.need_heartbeat = need_heartbeat
        self.csv = csv_recorder
        self.wait_first_frame = wait_first_frame
        self.wait_gemini = wait_gemini

        self.camera: Optional[CameraCapture] = None
        self.processor: Optional[ImageProcessor] = None
        self.session: Optional[SessionManager] = None
        self.heartbeat: Optional[ImageHeartbeat] = None
        self._reporter: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "Pipeline":
        try:
            if self.need_camera:
                if not self.settings.camera_url:
                    raise ConfigError(
                        "CAMERA_URL nao definida. Preencha o .env ou use --source."
                    )
                self.camera = CameraCapture(
                    self.settings.camera_url,
                    transport=self.settings.camera_transport,
                    fps=self.settings.capture_fps,
                    max_width=self.settings.capture_max_width,
                    fallback_width=self.settings.capture_width,
                    fallback_height=self.settings.capture_height,
                    probe_timeout_sec=self.settings.camera_probe_timeout_sec,
                    read_timeout_sec=self.settings.camera_read_timeout_sec,
                    reconnect_base_sec=self.settings.capture_reconnect_base_sec,
                    reconnect_max_sec=self.settings.capture_reconnect_max_sec,
                    skip_probe=self.settings.camera_skip_probe,
                    diagnostics=self.diag,
                )
                self.camera.start()
                if self.wait_first_frame:
                    await self._wait_first_frame(timeout=20.0)
                    LOG.info(
                        "Camera OK: %s (frame %dx%d).",
                        mask_url(self.settings.camera_url),
                        self.camera.get_latest().width,
                        self.camera.get_latest().height,
                    )
                else:
                    LOG.info(
                        "Camera iniciada em segundo plano (%s); o primeiro frame "
                        "sera aguardado pelo heartbeat.",
                        mask_url(self.settings.camera_url),
                    )

            self.processor = ImageProcessor.from_settings(self.settings, self.diag)
            LOG.info("ImageProcessor: %s", self.processor.describe())

            if self.need_gemini:
                self.session = SessionManager(self.settings, self.diag, LOG)
                await self.session.start()
                if self.wait_gemini:
                    await self.session.wait_connected(timeout=25.0)
                    LOG.info(
                        "Gemini Live conectado (geracao=%d).", self.session.generation
                    )
                else:
                    LOG.info(
                        "Gemini Live iniciando em segundo plano; o supervisor "
                        "reconecta automaticamente."
                    )

            if self.need_heartbeat:
                if not self.camera or not self.processor or not self.session:
                    raise RuntimeError("Heartbeat exige camera, processador e sessao.")
                if self.settings.heartbeat_enabled:
                    self.heartbeat = ImageHeartbeat(
                        self.camera,
                        self.processor,
                        self.session,
                        self.settings,
                        self.diag,
                        csv_recorder=self.csv,
                    )
                    await self.heartbeat.start()
                else:
                    LOG.info(
                        "Heartbeat visual DESATIVADO (IMAGE_HEARTBEAT_ENABLED=false); "
                        "nenhuma imagem sera enviada automaticamente."
                    )

            self._reporter = asyncio.create_task(
                self._report_loop(), name="vision-diagnostics"
            )
            return self
        except Exception:
            await self._shutdown()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self._reporter and not self._reporter.done():
            self._reporter.cancel()
            with suppress(asyncio.CancelledError):
                await self._reporter
        self._reporter = None
        if self.heartbeat is not None:
            with suppress(Exception):
                await self.heartbeat.stop()
            self.heartbeat = None
        if self.session is not None:
            with suppress(Exception):
                await self.session.stop()
            self.session = None
        if self.camera is not None:
            with suppress(Exception):
                self.camera.stop()
            self.camera = None
        if self.csv is not None:
            with suppress(Exception):
                self.csv.close()
        self.diag.log_summary(LOG)

    async def _wait_first_frame(self, timeout: float) -> Frame:
        assert self.camera is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.camera.get_latest()
            if frame is not None:
                return frame
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"Camera nao entregou frame em {timeout:.0f}s. "
            f"Ultimo erro: {self.camera.last_error or 'desconhecido'} "
            f"ffmpeg: {self.camera.last_stderr or 'sem stderr'}"
        )

    async def _report_loop(self) -> None:
        interval = max(5.0, self.settings.diagnostics_interval_sec)
        try:
            while True:
                await asyncio.sleep(interval)
                self.diag.log_summary(LOG)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------
async def stage_camera(args, settings: Settings, diag: Diagnostics) -> bool:
    async with Pipeline(settings, diag, need_camera=True, need_gemini=False) as p:
        await asyncio.sleep(max(2.0, args.seconds))
        frame = p.camera.get_latest()
        age = p.camera.frame_age()
        rate = diag.capture_rate()
        diag.log_summary(LOG)
        if args.save_frame:
            assert p.processor is not None and frame is not None
            processed = p.processor.process(frame)
            _write_bytes(args.save_frame, processed.jpeg)
            LOG.info("Frame salvo em %s (%.1fKB).", args.save_frame, processed.jpeg_kb)
        passed = (
            frame is not None
            and diag.capture_frames >= 3
            and rate >= settings.capture_fps * 0.5
            and (age or 0.0) < settings.frame_stale_sec
        )
        detail = (
            f"{diag.capture_frames} frames, {rate:.2f} FPS, "
            f"idade={age:.3f}s, {frame.width}x{frame.height}"
            if frame
            else "sem frame"
        )
        return _result("camera", passed, detail)


async def stage_process(args, settings: Settings, diag: Diagnostics) -> bool:
    need_camera = not bool(args.image)
    async with Pipeline(settings, diag, need_camera=need_camera, need_gemini=False) as p:
        assert p.processor is not None
        if args.image:
            processed = p.processor.process_image(_load_image(args.image))
        else:
            frame = p.camera.get_latest()
            assert frame is not None
            processed = p.processor.process(frame)

        output_path = args.save_frame or os.path.join(
            settings.storage_dir, "vision_test.jpg"
        )
        _write_bytes(output_path, processed.jpeg)
        passed = (
            processed.jpeg[:2] == b"\xff\xd8"
            and processed.width <= settings.image_max_width
            and processed.height <= settings.image_max_height
            and processed.width <= processed.orig_width
            and processed.height <= processed.orig_height
            and _aspect_ok(
                processed.orig_width,
                processed.orig_height,
                processed.width,
                processed.height,
            )
            and processed.jpeg_kb > 0
        )
        detail = (
            f"{processed.orig_width}x{processed.orig_height} -> "
            f"{processed.width}x{processed.height}, {processed.jpeg_kb:.1f}KB, "
            f"resize={processed.resize_ms:.2f}ms jpeg={processed.encode_ms:.2f}ms, "
            f"arquivo={output_path}"
        )
        return _result("process", passed, detail)


async def stage_connect(args, settings: Settings, diag: Diagnostics) -> bool:
    async with Pipeline(settings, diag, need_camera=False, need_gemini=True) as p:
        assert p.session is not None
        opened = p.session.is_connected
        LOG.info("Sessao aberta por %.0fs para validacao.", args.seconds)
        await asyncio.sleep(max(1.0, args.seconds))
        still_connected = p.session.is_connected
        detail = (
            f"geracao={p.session.generation} "
            f"handle={'retomado' if p.session.connected_handle_used else 'novo'} "
            f"aberta={opened} ativa_apos_{args.seconds:.0f}s={still_connected}"
        )
        return _result("connect", opened and still_connected, detail)


async def _collect_answer(session: SessionManager, question: str, timeout: float):
    collector = await session.ask(question, timeout=timeout)
    if collector.text:
        LOG.info(
            "Resposta recebida (primeiro token=%s, turno=%s): %s",
            f"{collector.first_token_ms:.0f}ms" if collector.first_token_ms else "n/a",
            f"{collector.turn_complete_ms:.0f}ms" if collector.turn_complete_ms else "n/a",
            collector.text,
        )
    else:
        LOG.warning("Nenhuma transcricao recebida do Gemini.")
    return collector


async def stage_image(args, settings: Settings, diag: Diagnostics) -> bool:
    need_camera = not bool(args.image)
    async with Pipeline(settings, diag, need_camera=need_camera, need_gemini=True) as p:
        assert p.processor is not None and p.session is not None
        if args.image:
            processed = p.processor.process_image(_load_image(args.image))
        else:
            frame = p.camera.get_latest()
            assert frame is not None
            processed = p.processor.process(frame)

        LOG.info(
            "Enviando 1 frame (%dx%d, %.1fKB) e aguardando interpretacao...",
            processed.width,
            processed.height,
            processed.jpeg_kb,
        )
        p.session.send_frame(processed.jpeg)
        await asyncio.sleep(max(0.5, args.pre_ask_delay))
        collector = await _collect_answer(
            p.session, args.ask or DEFAULT_QUESTION, args.answer_timeout
        )
        passed = bool(collector.text)
        return _result("image", passed, f"{len(collector.text)} chars transcritos")


async def stage_heartbeat(args, settings: Settings, diag: Diagnostics) -> bool:
    if not _require_heartbeat(settings, "heartbeat"):
        return False
    async with Pipeline(
        settings, diag, need_camera=True, need_gemini=True, need_heartbeat=True
    ) as p:
        seconds = max(3.0, args.seconds)
        await asyncio.sleep(seconds)
        diag.log_summary(LOG)
        expected = seconds * p.heartbeat.effective_fps
        sent = diag.sent_frames
        passed = sent >= expected * 0.6
        detail = (
            f"enviados={sent} esperado~{expected:.0f} "
            f"taxa={diag.send_rate():.2f}/s drops={diag.dropped_frames} "
            f"respostas_espontaneas={diag.spontaneous_responses}"
        )
        if diag.spontaneous_responses > 0:
            LOG.warning(
                "O modelo respondeu sem pergunta (%d respostas). "
                "Verifique proatividade/instrucao do sistema.",
                diag.spontaneous_responses,
            )
        return _result("heartbeat", passed, detail)


async def stage_receive(args, settings: Settings, diag: Diagnostics) -> bool:
    if not _require_heartbeat(settings, "receive"):
        return False
    async with Pipeline(
        settings, diag, need_camera=True, need_gemini=True, need_heartbeat=True
    ) as p:
        assert p.session is not None
        await asyncio.sleep(max(1.0, args.pre_ask_delay))
        sent_before = diag.sent_frames
        collector = await _collect_answer(
            p.session, args.ask or DEFAULT_QUESTION, args.answer_timeout
        )
        sent_after = diag.sent_frames
        await asyncio.sleep(max(1.0, args.seconds))
        passed = bool(collector.text) and diag.sent_frames > sent_before
        detail = (
            f"resposta={len(collector.text)} chars, "
            f"frames enviados durante o teste={diag.sent_frames - sent_before} "
            f"(antes={sent_before}, depois={sent_after}->{diag.sent_frames})"
        )
        return _result("receive", passed, detail)


async def stage_reconnect(args, settings: Settings, diag: Diagnostics) -> bool:
    if not _require_heartbeat(settings, "reconnect"):
        return False
    async with Pipeline(
        settings, diag, need_camera=True, need_gemini=True, need_heartbeat=True
    ) as p:
        assert p.session is not None and p.camera is not None
        await asyncio.sleep(2.0)
        frame_before = p.camera.get_latest()
        seq_before = frame_before.seq if frame_before else -1
        generation_before = p.session.generation
        sent_before = diag.sent_frames
        handle_before = p.session.handle

        LOG.warning("Forcando queda da conexao Gemini para testar reconexao...")
        await p.session.force_reconnect()
        await p.session.wait_generation(generation_before + 1, timeout=30.0)
        await asyncio.sleep(3.0)

        frame_after = p.camera.get_latest()
        seq_after = frame_after.seq if frame_after else -1
        sent_after = diag.sent_frames
        handle_after = p.session.handle

        collector = await _collect_answer(
            p.session, args.ask or DEFAULT_QUESTION, args.answer_timeout
        )
        camera_continued = seq_after > seq_before
        sending_resumed = sent_after > sent_before
        connected = p.session.is_connected
        passed = camera_continued and sending_resumed and connected
        detail = (
            f"camera_seq {seq_before}->{seq_after} (captura continua), "
            f"envios {sent_before}->{sent_after}, "
            f"geracao {generation_before}->{p.session.generation}, "
            f"handle_antes={'sim' if handle_before else 'nao'} "
            f"handle_depois={'sim' if handle_after else 'nao'}, "
            f"resposta_apos_reconexao={bool(collector.text)}"
        )
        return _result("reconnect", passed, detail)


async def stage_stability(args, settings: Settings, diag: Diagnostics) -> bool:
    if not _require_heartbeat(settings, "stability"):
        return False
    minutes = max(0.2, args.stability_minutes)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = args.csv or os.path.join(
        settings.storage_dir, f"stability_{timestamp}.csv"
    )
    csv_recorder = CsvRecorder(csv_path)
    LOG.info("CSV de estabilidade: %s", csv_path)

    async with Pipeline(
        settings,
        diag,
        need_camera=True,
        need_gemini=True,
        need_heartbeat=True,
        csv_recorder=csv_recorder,
    ) as p:
        end = time.monotonic() + minutes * 60.0
        rss_start: Optional[float] = None
        sent_start = diag.sent_frames
        initial_frame = p.camera.get_latest() if p.camera else None
        seq_start = initial_frame.seq if initial_frame else -1
        while time.monotonic() < end:
            await asyncio.sleep(min(10.0, max(0.5, end - time.monotonic())))
            diag.update_system()
            if rss_start is None and diag.rss_mb is not None:
                rss_start = diag.rss_mb
            LOG.info(
                "estabilidade: restante=%.0fs %s",
                max(0.0, end - time.monotonic()),
                diag.summary(),
            )

        seq_end_frame = p.camera.get_latest() if p.camera else None
        seq_end = seq_end_frame.seq if seq_end_frame else -1
        rss_end = diag.rss_mb
        sent_total = diag.sent_frames - sent_start
        capture_ok = seq_end > seq_start
        rss_ok = (
            rss_start is None
            or rss_end is None
            or (rss_end - rss_start) < 64.0
        )
        passed = sent_total > 0 and capture_ok and rss_ok
        detail = (
            f"{minutes:.1f}min, enviados={sent_total}, "
            f"camera_seq {seq_start}->{seq_end}, "
            f"RAM {rss_start if rss_start is not None else 'n/a'}MB -> "
            f"{rss_end if rss_end is not None else 'n/a'}MB, "
            f"csv={csv_path}"
        )
        return _result("stability", passed, detail)


async def stage_run(args, settings: Settings, diag: Diagnostics) -> bool:
    # Em producao o servico nao deve morrer se a camera ou a internet estiverem
    # momentaneamente fora: ambos os supervisores reconectam sozinhos.
    async with Pipeline(
        settings,
        diag,
        need_camera=True,
        need_gemini=True,
        need_heartbeat=True,
        wait_first_frame=False,
        wait_gemini=False,
    ) as p:
        LOG.info("Pipeline completo ativo. Pressione Ctrl+C para encerrar.")
        while True:
            await asyncio.sleep(1.0)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SKYNET - visao para Gemini Live (captura + JPEG + envio)."
    )
    parser.add_argument(
        "--stage",
        choices=sorted(STAGE_DESCRIPTIONS),
        help="Etapa a executar.",
    )
    parser.add_argument(
        "--test",
        type=int,
        choices=sorted(TEST_STAGES),
        help="Atalho para os testes obrigatorios 1..8.",
    )
    parser.add_argument("--run", action="store_true", help="Pipeline completo.")
    parser.add_argument("--source", help="Sobrescreve CAMERA_URL (RTSP ou arquivo de teste).")
    parser.add_argument("--image", help="Usa um JPEG local em vez da camera (teste 2/4).")
    parser.add_argument("--save-frame", help="Caminho para salvar o JPEG de teste.")
    parser.add_argument("--preset", choices=sorted(IMAGE_PRESETS), help="LOW/BALANCED/DETAIL.")
    parser.add_argument("--max-width", type=int)
    parser.add_argument("--max-height", type=int)
    parser.add_argument("--quality", type=int)
    parser.add_argument("--send-fps", type=float)
    parser.add_argument("--capture-fps", type=float)
    parser.add_argument("--media-resolution", choices=list(VALID_MEDIA_RESOLUTIONS))
    parser.add_argument("--model")
    parser.add_argument("--ask", help="Pergunta enviada ao modelo.")
    parser.add_argument("--answer-timeout", type=float, default=30.0)
    parser.add_argument("--pre-ask-delay", type=float, default=1.0)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--stability-minutes", type=float, default=5.0)
    parser.add_argument("--csv", help="Arquivo CSV para o teste de estabilidade.")
    parser.add_argument("--verbose", action="store_true", help="Ativa LOG_LEVEL=DEBUG.")
    return parser


def apply_overrides(settings: Settings, args) -> Settings:
    overrides = {}
    if args.source:
        overrides["camera_url"] = args.source
    if args.preset:
        overrides["image_preset"] = args.preset
        overrides["image_max_width"] = IMAGE_PRESETS[args.preset]
    if args.max_width:
        overrides["image_max_width"] = args.max_width
        if not args.preset:
            overrides["image_preset"] = "CUSTOM"
    if args.max_height:
        overrides["image_max_height"] = args.max_height
    if args.quality:
        overrides["jpeg_quality"] = args.quality
    if args.send_fps:
        overrides["image_send_fps"] = args.send_fps
    if args.capture_fps:
        overrides["capture_fps"] = args.capture_fps
    if args.media_resolution:
        overrides["media_resolution"] = args.media_resolution
    if args.model:
        overrides["gemini_model"] = args.model
    if args.verbose:
        overrides["log_level"] = "DEBUG"
        overrides["log_each_frame"] = True
    return replace(settings, **overrides) if overrides else settings


def resolve_stage(args) -> str:
    if args.run:
        return "run"
    if args.stage:
        return args.stage
    if args.test:
        return TEST_STAGES[args.test]
    return "run"


async def async_main(args, settings: Settings, stage: str) -> int:
    diag = Diagnostics()
    LOG.info("Configuracao: %s", settings.describe())
    LOG.info(
        "API key: %s | .env: %s",
        "definida" if settings.gemini_api_key else "AUSENTE",
        os.getenv("VISION_ENV_FILE_LOADED", "nao encontrado"),
    )
    LOG.info("Etapa: %s", STAGE_DESCRIPTIONS.get(stage, stage))

    handlers = {
        "camera": stage_camera,
        "process": stage_process,
        "connect": stage_connect,
        "image": stage_image,
        "heartbeat": stage_heartbeat,
        "receive": stage_receive,
        "reconnect": stage_reconnect,
        "stability": stage_stability,
        "run": stage_run,
    }
    passed = await handlers[stage](args, settings, diag)
    return 0 if passed else 1


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    stage = resolve_stage(args)

    try:
        base = load_settings()
        settings = apply_overrides(base, args)
    except ConfigError as exc:
        print(f"ERRO DE CONFIGURACAO: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level)
    if not args.image and stage in ("camera", "process", "heartbeat", "receive", "reconnect", "stability", "run"):
        if not settings.camera_url:
            LOG.error("CAMERA_URL nao definida. Preencha o .env ou use --source.")
            return 2
    if stage in ("connect", "image", "heartbeat", "receive", "reconnect", "stability", "run"):
        if not settings.gemini_api_key:
            LOG.error("GEMINI_API_KEY nao definida. Preencha o .env.")
            return 2

    try:
        return asyncio.run(async_main(args, settings, stage))
    except KeyboardInterrupt:
        LOG.info("Encerrado pelo usuario.")
        return 130
    except Exception as exc:
        LOG.exception("Falha na etapa %s: %s", stage, exc)
        _result(stage, False, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
