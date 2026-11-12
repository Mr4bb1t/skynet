"""Captura continua da camera IP via FFmpeg, com buffer de tamanho 1.

Regras de projeto:
  * uma unica conexao persistente com a camera (FFmpeg reinicia so se cair);
  * o decoder NAO e reiniciado a cada frame;
  * a captura e independente da conexao com o Gemini;
  * o buffer guarda SEMPRE apenas o frame mais recente (sem fila crescente);
  * reconexao automatica com backoff exponencial + jitter.

O transporte usado e o mesmo ja validado no projeto legado: RTSP/UDP para
o substream 640x360 (rtsp://.../onvif2).
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from .diagnostics import Diagnostics

LOG = logging.getLogger("vision.camera")


class CameraError(RuntimeError):
    """Falha de configuracao ou de captura da camera."""


@dataclass(frozen=True)
class Frame:
    """Frame BGR24 cru, imutavel, com carimbo monotonicamente crescente."""

    data: bytes
    width: int
    height: int
    seq: int
    captured_at: float  # time.monotonic()

    @property
    def nbytes(self) -> int:
        return len(self.data)

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.captured_at


@dataclass(frozen=True)
class StreamInfo:
    width: int
    height: int
    fps: float


def _is_network_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith(
        ("rtsp://", "rtsps://", "http://", "https://", "udp://", "tcp://")
    )


def _is_rtsp_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith(("rtsp://", "rtsps://"))


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


class CameraCapture:
    """Supervisor de captura com thread dedicada e slot unico de frame."""

    def __init__(
        self,
        url: str,
        *,
        transport: str = "udp",
        fps: float = 10.0,
        max_width: int = 0,
        fallback_width: int = 640,
        fallback_height: int = 360,
        probe_timeout_sec: float = 10.0,
        read_timeout_sec: float = 5.0,
        reconnect_base_sec: float = 1.0,
        reconnect_max_sec: float = 30.0,
        skip_probe: bool = False,
        diagnostics: Optional[Diagnostics] = None,
    ) -> None:
        if not url:
            raise CameraError("URL da camera vazia.")
        self.url = url
        self.transport = transport.lower()
        self.fps = max(1.0, float(fps))
        self.max_width = max(0, int(max_width))
        self.fallback_width = max(2, int(fallback_width))
        self.fallback_height = max(2, int(fallback_height))
        self.probe_timeout_sec = float(probe_timeout_sec)
        self.read_timeout_sec = float(read_timeout_sec)
        self.reconnect_base_sec = float(reconnect_base_sec)
        self.reconnect_max_sec = float(reconnect_max_sec)
        self.skip_probe = bool(skip_probe)
        self.diag = diagnostics or Diagnostics()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._latest: Optional[Frame] = None
        self._seq = 0
        self._stream_info: Optional[StreamInfo] = None
        self._capture_size: Optional[tuple[int, int]] = None
        self._last_stderr: Deque[str] = deque(maxlen=20)
        self._stderr_lock = threading.Lock()
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if shutil.which("ffmpeg") is None:
            raise CameraError(
                "ffmpeg nao encontrado no PATH. Instale com: apt-get install -y ffmpeg"
            )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervisor, name="camera-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_proc()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def stream_info(self) -> Optional[StreamInfo]:
        return self._stream_info

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_stderr(self) -> str:
        with self._stderr_lock:
            return " | ".join(self._last_stderr)

    # ------------------------------------------------------------------
    # Leitura do frame mais recente
    # ------------------------------------------------------------------
    def get_latest(self, max_age: Optional[float] = None) -> Optional[Frame]:
        with self._frame_lock:
            frame = self._latest
        if frame is None:
            return None
        if max_age is not None and max_age > 0 and frame.age() > max_age:
            return None
        return frame

    def frame_age(self) -> Optional[float]:
        with self._frame_lock:
            frame = self._latest
        return None if frame is None else frame.age()

    def _store_frame(self, data: bytes, width: int, height: int) -> None:
        self._seq += 1
        frame = Frame(
            data=data,
            width=width,
            height=height,
            seq=self._seq,
            captured_at=time.monotonic(),
        )
        with self._frame_lock:
            self._latest = frame
        self.diag.record_capture(self._seq, width, height)

    # ------------------------------------------------------------------
    # Supervisor / reconexao
    # ------------------------------------------------------------------
    def _supervisor(self) -> None:
        backoff = self.reconnect_base_sec
        while not self._stop.is_set():
            if self._capture_size is None:
                try:
                    self._resolve_capture_size()
                except Exception as exc:
                    self._last_error = f"probe: {exc}"
                    self.diag.record_error("capture_probe", str(exc))
                    LOG.warning(
                        "Falha ao inspecionar a camera (%s). Tentando de novo em %.1fs.",
                        exc,
                        backoff,
                    )
                    self._stop.wait(self._jitter(backoff))
                    backoff = min(self.reconnect_max_sec, backoff * 2)
                    continue
                # Camera respondeu: volta ao backoff inicial para a proxima
                # eventual queda de captura.
                backoff = self.reconnect_base_sec

            width, height = self._capture_size
            proc = self._spawn(width, height)
            frames_this_run = 0
            run_started = time.monotonic()
            try:
                frames_this_run = self._read_loop(proc, width, height)
            except Exception as exc:  # pragma: no cover - defensivo
                self._last_error = str(exc)
                self.diag.record_error("capture_read", str(exc))
                LOG.warning("Erro na leitura da camera: %s", exc)
            finally:
                self._terminate_proc(proc)

            if self._stop.is_set():
                break

            stderr_tail = self.last_stderr
            self.diag.record_capture_restart()
            LOG.warning(
                "Captura interrompida apos %.1fs (%d frames). Reiniciando em %.1fs.%s",
                time.monotonic() - run_started,
                frames_this_run,
                backoff,
                f" ffmpeg: {stderr_tail}" if stderr_tail else "",
            )
            if frames_this_run > 0 and (time.monotonic() - run_started) > 10.0:
                backoff = self.reconnect_base_sec
            self._stop.wait(self._jitter(backoff))
            backoff = min(self.reconnect_max_sec, backoff * 2)

    def _read_loop(self, proc: subprocess.Popen, width: int, height: int) -> int:
        assert proc.stdout is not None
        frame_size = width * height * 3
        frames = 0
        last_log = time.monotonic()
        while not self._stop.is_set():
            data = self._read_exact(proc.stdout, frame_size)
            if data is None:
                break
            self._store_frame(data, width, height)
            frames += 1
            now = time.monotonic()
            if now - last_log >= 10.0:
                LOG.debug(
                    "captura ativa: %d frames, %.1ffps, idade=%.2fs",
                    frames,
                    self.diag.capture_rate(now),
                    self.frame_age() or 0.0,
                )
                last_log = now
        return frames

    @staticmethod
    def _read_exact(stream, size: int) -> Optional[bytes]:
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if len(chunks) == 1:
            return chunks[0]
        return b"".join(chunks)

    def _jitter(self, delay: float) -> float:
        return max(0.1, delay * random.uniform(0.75, 1.25))

    # ------------------------------------------------------------------
    # FFmpeg
    # ------------------------------------------------------------------
    def _resolve_capture_size(self) -> None:
        info: Optional[StreamInfo] = None
        if self.skip_probe:
            # Evita uma sessão RTSP extra de ffprobe antes do FFmpeg. A
            # resolução alvo vem do .env; o FFmpeg faz o scale.
            width, height = self.fallback_width, self.fallback_height
            LOG.info(
                "Probe desativado (CAMERA_SKIP_PROBE=true); usando %dx%d.",
                width,
                height,
            )
        else:
            try:
                info = self.probe()
            except Exception as exc:
                LOG.warning(
                    "ffprobe falhou (%s). Usando fallback %dx%d do .env.",
                    exc,
                    self.fallback_width,
                    self.fallback_height,
                )
            if info is None:
                width, height = self.fallback_width, self.fallback_height
            else:
                width, height = info.width, info.height

        if width <= 0 or height <= 0:
            raise CameraError("Dimensoes invalidas detectadas na camera.")

        if self.max_width and width > self.max_width:
            scale = self.max_width / float(width)
            width = _even(round(width * scale))
            height = _even(round(height * scale))

        self._stream_info = info
        self._capture_size = (_even(width), _even(height))
        LOG.info(
            "Camera pronta: nativo=%s captura=%dx%d @ %.1ffps transporte=%s",
            f"{info.width}x{info.height}@{info.fps:.1f}" if info else "desconhecido",
            self._capture_size[0],
            self._capture_size[1],
            self.fps,
            self.transport,
        )

    def probe(self) -> StreamInfo:
        if shutil.which("ffprobe") is None:
            raise CameraError("ffprobe nao encontrado no PATH.")
        cmd = ["ffprobe", "-v", "error"]
        if _is_rtsp_url(self.url):
            cmd += ["-rtsp_transport", self.transport]
            cmd += ["-timeout", str(int(self.read_timeout_sec * 1_000_000))]
        cmd += [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate",
            "-of",
            "json",
            self.url,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.probe_timeout_sec,
                creationflags=self._creation_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            raise CameraError(f"timeout no ffprobe ({self.probe_timeout_sec:.0f}s)") from exc
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise CameraError(detail or f"ffprobe retornou {result.returncode}")
        try:
            payload = json.loads((result.stdout or b"{}").decode("utf-8", "replace"))
            stream = (payload.get("streams") or [])[0]
            width = int(stream["width"])
            height = int(stream["height"])
            rate = stream.get("avg_frame_rate") or "0/1"
            if "/" in str(rate):
                num, den = str(rate).split("/", 1)
                fps = float(num) / float(den) if float(den) else 0.0
            else:
                fps = float(rate)
        except Exception as exc:
            raise CameraError(f"resposta invalida do ffprobe: {exc}") from exc
        return StreamInfo(width=width, height=height, fps=fps)

    def _build_ffmpeg_cmd(self, width: int, height: int) -> list[str]:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats"]
        network = _is_network_url(self.url)
        if _is_rtsp_url(self.url):
            cmd += ["-rtsp_transport", self.transport]
            # `-rw_timeout` não existe neste FFmpeg (Debian 12/5.1); o
            # demuxer RTSP usa `-timeout` em microssegundos.
            cmd += ["-timeout", str(int(self.read_timeout_sec * 1_000_000))]
        if not network:
            # Fonte local (usada apenas em testes/benchmark): loop em tempo real.
            cmd += ["-stream_loop", "-1", "-re"]
        # NÃO usar `-fflags nobuffer`/`-flags low_delay` nesta câmera: o
        # decoder H.264 recebe slices incompletos e gera "no frame!".
        cmd += ["-i", self.url, "-an", "-sn", "-dn"]
        # O fps e o scale sao feitos pelo FFmpeg: menos CPU e menos copias.
        cmd += [
            "-vf",
            f"fps={self.fps:g},scale={width}:{height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
        return cmd

    def _spawn(self, width: int, height: int) -> subprocess.Popen:
        cmd = self._build_ffmpeg_cmd(width, height)
        LOG.info(
            "Iniciando captura FFmpeg (%dx%d @ %.1ffps, transporte=%s).",
            width,
            height,
            self.fps,
            self.transport,
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=self._creation_flags(),
        )
        with self._proc_lock:
            self._proc = proc
        threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name="camera-stderr",
            daemon=True,
        ).start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    with self._stderr_lock:
                        self._last_stderr.append(line)
        except Exception:
            pass

    def _terminate_proc(self, proc: Optional[subprocess.Popen] = None) -> None:
        with self._proc_lock:
            target = proc or self._proc
            self._proc = None
        if target is None:
            return
        if target.poll() is None:
            try:
                target.terminate()
            except Exception:
                pass
        try:
            target.wait(timeout=2.0)
        except Exception:
            try:
                target.kill()
                target.wait(timeout=1.0)
            except Exception:
                pass
        for stream in (target.stdout, target.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    @staticmethod
    def _creation_flags() -> int:
        if os.name == "nt":
            return getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return 0
