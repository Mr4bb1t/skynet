"""Heartbeat visual: envia o frame mais recente ~1x por segundo.

Nao confundir com ping de rede. O heartbeat transmite imagens reais para
atualizar a percepcao do Gemini. Ele:
  * usa relogio monotonico;
  * respeita o limite de 1 FPS da API;
  * nunca acumula frames (slot unico no GeminiLiveClient);
  * nao cria tarefas de envio concorrentes;
  * continua lendo a camera mesmo durante uma reconexao do Gemini.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from .camera_capture import CameraCapture
from .config import Settings
from .diagnostics import CsvRecorder, Diagnostics
from .image_processor import ImageProcessor, ProcessedFrame
from .session_manager import SessionManager

LOG = logging.getLogger("vision.heartbeat")

MAX_API_FPS = 1.0  # limite documentado para video no Gemini Live


class ImageHeartbeat:
    """Produtor periodico de frames para a sessao Live."""

    def __init__(
        self,
        camera: CameraCapture,
        processor: ImageProcessor,
        session: SessionManager,
        settings: Settings,
        diagnostics: Diagnostics,
        *,
        csv_recorder: Optional[CsvRecorder] = None,
    ) -> None:
        self.camera = camera
        self.processor = processor
        self.session = session
        self.settings = settings
        self.diag = diagnostics
        self.csv = csv_recorder

        self._send_fps = min(MAX_API_FPS, max(0.1, settings.image_send_fps))
        if settings.image_send_fps > MAX_API_FPS:
            LOG.warning(
                "IMAGE_SEND_FPS=%.2f excede o limite da API; usando %.1f FPS.",
                settings.image_send_fps,
                MAX_API_FPS,
            )
        self._interval = 1.0 / self._send_fps
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_sent_seq = -1
        self._ticks = 0
        self._processed = 0

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def last_sent_seq(self) -> int:
        return self._last_sent_seq

    @property
    def effective_fps(self) -> float:
        return self._send_fps

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="image-heartbeat")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None

    # ------------------------------------------------------------------
    async def run(self) -> None:
        LOG.info(
            "Heartbeat visual iniciado: %.2f FPS (intervalo %.0fms).",
            self._send_fps,
            self._interval * 1000.0,
        )
        next_tick = time.monotonic()
        try:
            while not self._stop.is_set():
                delay = next_tick - time.monotonic()
                if delay > 0:
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                if self._stop.is_set():
                    break

                now = time.monotonic()
                if now - next_tick > self._interval:
                    # Perdeu um ou mais ciclos (envio/processamento lento):
                    # nao faz rajada de recuperacao.
                    next_tick = now
                next_tick += self._interval

                self._ticks += 1
                await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensivo
            LOG.error("Heartbeat visual encerrado por erro: %s", exc)
            self.diag.record_error("heartbeat", str(exc))
        finally:
            LOG.info(
                "Heartbeat visual parado (ticks=%d, processados=%d, enviados=%d).",
                self._ticks,
                self._processed,
                self.diag.sent_frames,
            )

    async def _tick(self) -> None:
        if not self.session.is_connected:
            self.diag.record_heartbeat_skip("disconnected")
            return

        frame = self.camera.get_latest(max_age=self.settings.frame_stale_sec)
        if frame is None:
            self.diag.record_heartbeat_skip("no_frame")
            LOG.debug("Sem frame valido da camera neste ciclo.")
            return

        if frame.seq == self._last_sent_seq:
            self.diag.record_heartbeat_skip("same_frame")
            return

        try:
            processed: ProcessedFrame = await asyncio.to_thread(
                self.processor.process, frame
            )
        except Exception as exc:
            self.diag.record_error("process", str(exc))
            LOG.warning("Falha ao processar frame %d: %s", frame.seq, exc)
            return

        self._processed += 1
        age_ms = frame.age() * 1000.0
        if self.settings.log_each_frame:
            LOG.info(
                "Frame %d: %dx%d -> %dx%d %.1fKB resize=%.2fms jpeg=%.2fms idade=%.0fms",
                frame.seq,
                processed.orig_width,
                processed.orig_height,
                processed.width,
                processed.height,
                processed.jpeg_kb,
                processed.resize_ms,
                processed.encode_ms,
                age_ms,
            )

        submitted = self.session.send_frame(processed.jpeg)
        if submitted:
            self._last_sent_seq = frame.seq
        else:
            self.diag.record_heartbeat_skip("not_submitted")
            LOG.debug("Frame %d nao entrou no slot de envio.", frame.seq)

        if self.csv is not None:
            if self._processed % 10 == 0:
                self.diag.update_system()
            self.csv.write(
                timestamp=datetime.now(timezone.utc).isoformat(),
                frame_seq=frame.seq,
                orig_w=processed.orig_width,
                orig_h=processed.orig_height,
                out_w=processed.width,
                out_h=processed.height,
                jpeg_kb=round(processed.jpeg_kb, 2),
                resize_ms=round(processed.resize_ms, 2),
                encode_ms=round(processed.encode_ms, 2),
                frame_age_ms=round(age_ms, 1),
                submitted=int(submitted),
                connected=int(self.session.is_connected),
                capture_fps=round(self.diag.capture_rate(), 2),
                send_fps=round(self.diag.send_rate(), 2),
                sent_total=self.diag.sent_frames,
                dropped_total=self.diag.dropped_frames,
                cpu_pct=round(self.diag.cpu_pct, 1) if self.diag.cpu_pct is not None else "",
                rss_mb=round(self.diag.rss_mb, 1) if self.diag.rss_mb is not None else "",
                tokens_total=self.diag.usage_total,
            )
