"""Cancelamento de eco acústico (AEC3) como camada independente do Skynet.

O player informa exatamente o áudio que está indo para o alto-falante
(referência / far-end). O microfone entrega o áudio do ambiente (near-end).
O AEC3 subtrai da captura o eco da própria voz do Skynet antes de o chunk
seguir para o Gemini Live — permitindo barge-in (ouvir o usuário enquanto fala).

O backend nativo é `libskynet_aec.so`, uma fachada C sobre o WebRTC APM 1.3
(AEC3). Se a biblioteca não estiver instalada, esta camada vira passthrough:
o áudio continua funcionando exatamente como antes.

Configuração (.env):
    AEC_ENABLED=true          # liga/desliga a camada
    AEC_DELAY_MS=150          # atraso alto-falante -> microfone (dica ao AEC3)
    AEC_FAR_MAX_MS=1000       # limite do buffer de referência

Notas de alinhamento: o AEC3 tem estimador de atraso próprio; `AEC_DELAY_MS`
é só uma dica para convergir mais rápido. O `aplay` usa buffer de 150 ms, então
esse é o valor inicial razoável neste box.
"""

from __future__ import annotations

import audioop
import ctypes
import ctypes.util
import logging
import os
import platform
import threading
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("skynet.audio.aec")

MIC_RATE = 16000
PLAYBACK_RATE = 24000
SAMPLE_WIDTH = 2  # int16
FRAME_MS = 10
MIC_FRAME_SAMPLES = MIC_RATE // (1000 // FRAME_MS)  # 160
MIC_FRAME_BYTES = MIC_FRAME_SAMPLES * SAMPLE_WIDTH  # 320


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _library_candidates() -> list[Path]:
    candidates: list[Path] = []
    override = os.getenv("AEC_LIBRARY_PATH")
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).resolve().parent / "native" / "libskynet_aec.so")
    candidates.append(Path("/usr/local/lib/libskynet_aec.so"))
    candidates.append(Path("/usr/lib/libskynet_aec.so"))
    return candidates


def _load_library() -> Optional[ctypes.CDLL]:
    """Carrega libskynet_aec.so, se existir. Retorna None caso contrário."""
    if os.name != "posix" or platform.system() == "Windows":
        return None

    for path in _library_candidates():
        if not path.is_file():
            continue
        try:
            return ctypes.CDLL(str(path))
        except OSError as exc:
            LOG.warning("AEC3: falha ao carregar %s: %s", path, exc)

    found = ctypes.util.find_library("skynet_aec")
    if found:
        try:
            return ctypes.CDLL(found)
        except OSError as exc:
            LOG.warning("AEC3: falha ao carregar %s: %s", found, exc)
    return None


class NativeAec:
    """Fachada Python para a AEC3 nativa (frames de 10 ms, 16 kHz, mono)."""

    FRAME_SAMPLES = MIC_FRAME_SAMPLES
    FRAME_BYTES = MIC_FRAME_BYTES

    def __init__(self, delay_ms: int = 0):
        library = _load_library()
        if library is None:
            raise RuntimeError("libskynet_aec.so não encontrada")

        library.skynet_aec_create.argtypes = [ctypes.c_int, ctypes.c_int]
        library.skynet_aec_create.restype = ctypes.c_void_p
        library.skynet_aec_destroy.argtypes = [ctypes.c_void_p]
        library.skynet_aec_destroy.restype = None
        library.skynet_aec_process.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.skynet_aec_process.restype = ctypes.c_int
        library.skynet_aec_far.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        library.skynet_aec_far.restype = ctypes.c_int
        library.skynet_aec_near.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.skynet_aec_near.restype = ctypes.c_int

        handle = library.skynet_aec_create(MIC_RATE, int(delay_ms))
        if not handle:
            raise RuntimeError("skynet_aec_create falhou (AEC3 indisponível)")

        self._library = library
        self._handle = handle
        self._near = (ctypes.c_int16 * self.FRAME_SAMPLES)()
        self._far = (ctypes.c_int16 * self.FRAME_SAMPLES)()
        self._out = (ctypes.c_int16 * self.FRAME_SAMPLES)()
        self._lock = threading.Lock()
        self.delay_ms = int(delay_ms)
        LOG.info("AEC3 ativo (16 kHz, delay=%d ms).", self.delay_ms)

    def process_far(self, far: bytes) -> None:
        """Alimenta a referência do alto-falante (render/far-end)."""
        if len(far) != self.FRAME_BYTES:
            raise ValueError("AEC3 far exige frames de 10 ms (320 bytes) a 16 kHz")
        ctypes.memmove(self._far, far, self.FRAME_BYTES)
        with self._lock:
            rc = self._library.skynet_aec_far(self._handle, self._far, self.FRAME_SAMPLES)
        if rc != 0:
            raise RuntimeError(f"skynet_aec_far retornou {rc}")

    def process_near(self, near: bytes) -> bytes:
        """Processa o microfone (capture/near-end) de um frame de 10 ms."""
        if len(near) != self.FRAME_BYTES:
            raise ValueError("AEC3 near exige frames de 10 ms (320 bytes) a 16 kHz")
        ctypes.memmove(self._near, near, self.FRAME_BYTES)
        with self._lock:
            rc = self._library.skynet_aec_near(
                self._handle, self._near, self._out, self.FRAME_SAMPLES
            )
        if rc != 0:
            raise RuntimeError(f"skynet_aec_near retornou {rc}")
        return ctypes.string_at(self._out, self.FRAME_BYTES)

    def process(self, near: bytes, far: bytes) -> bytes:
        if len(near) != self.FRAME_BYTES or len(far) != self.FRAME_BYTES:
            raise ValueError("AEC3 exige frames de 10 ms (320 bytes) a 16 kHz")

        ctypes.memmove(self._near, near, self.FRAME_BYTES)
        ctypes.memmove(self._far, far, self.FRAME_BYTES)
        with self._lock:
            rc = self._library.skynet_aec_process(
                self._handle, self._near, self._far, self._out, self.FRAME_SAMPLES
            )
        if rc != 0:
            raise RuntimeError(f"skynet_aec_process retornou {rc}")
        return ctypes.string_at(self._out, self.FRAME_BYTES)

    def set_delay(self, delay_ms: int) -> None:
        setter = getattr(self._library, "skynet_aec_set_delay", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p, ctypes.c_int]
            setter.restype = ctypes.c_int
            setter(self._handle, int(delay_ms))
        self.delay_ms = int(delay_ms)

    def close(self) -> None:
        if self._handle:
            with self._lock:
                self._library.skynet_aec_destroy(self._handle)
                self._handle = None


class AudioEchoCanceller:
    """Mantém referência de reprodução + captura e aplica AEC3 frame a frame."""

    def __init__(
        self,
        enabled: Optional[bool] = None,
        delay_ms: Optional[int] = None,
        playback_rate: int = PLAYBACK_RATE,
        mic_rate: int = MIC_RATE,
    ):
        if enabled is None:
            enabled = _env_bool("AEC_ENABLED", True)
        if delay_ms is None:
            delay_ms = _env_int("AEC_DELAY_MS", 150)

        self.playback_rate = int(playback_rate)
        self.mic_rate = int(mic_rate)
        self.frame_bytes = int(self.mic_rate / 100) * SAMPLE_WIDTH

        self._far_buffer = bytearray()
        self._near_buffer = bytearray()
        self._resample_state = None
        self._lock = threading.Lock()
        self._frames_processed = 0
        self._backend: Optional[NativeAec] = None

        if enabled:
            try:
                self._backend = NativeAec(delay_ms)
            except Exception as exc:  # noqa: BLE001 - áudio nunca deve parar por isso
                LOG.warning(
                    "AEC3 indisponível (%s). O áudio segue sem cancelamento de eco. "
                    "Rode: bash /root/skynet/audio/native/build_aec.sh",
                    exc,
                )

    @property
    def active(self) -> bool:
        return self._backend is not None

    def push_far(self, pcm_bytes: bytes) -> None:
        """Referência do que está sendo enviado ao alto-falante."""
        if not self._backend or not pcm_bytes:
            return

        try:
            if self.playback_rate != self.mic_rate:
                pcm_bytes, self._resample_state = audioop.ratecv(
                    pcm_bytes,
                    SAMPLE_WIDTH,
                    1,
                    self.playback_rate,
                    self.mic_rate,
                    self._resample_state,
                )
            with self._lock:
                self._far_buffer.extend(pcm_bytes)
                while len(self._far_buffer) >= self.frame_bytes:
                    frame = bytes(self._far_buffer[: self.frame_bytes])
                    del self._far_buffer[: self.frame_bytes]
                    # Alimenta o render imediatamente: o AEC3 guarda o
                    # historico e estima o atraso ate o eco no microfone.
                    self._backend.process_far(frame)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("AEC3: falha ao processar referência de áudio: %s", exc)

    def process_near_16k(self, pcm_bytes: bytes) -> bytes:
        """Remove o eco do microfone. Retorna o chunk limpo (ou o original)."""
        if not self._backend or not pcm_bytes:
            return pcm_bytes

        try:
            with self._lock:
                self._near_buffer.extend(pcm_bytes)
                output = bytearray()
                while len(self._near_buffer) >= self.frame_bytes:
                    near = bytes(self._near_buffer[: self.frame_bytes])
                    del self._near_buffer[: self.frame_bytes]
                    output.extend(self._backend.process_near(near))
                    self._frames_processed += 1
                return bytes(output) if output else b""
        except Exception as exc:  # noqa: BLE001
            LOG.error("AEC3: falha no processamento; seguindo sem AEC: %s", exc)
            self._backend = None
            return pcm_bytes

    def flush_far(self) -> None:
        """Descarta a referência parcial (chamado quando a fala é interrompida)."""
        with self._lock:
            self._far_buffer.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "active": self.active,
                "frames": self._frames_processed,
                "delay_ms": getattr(self._backend, "delay_ms", 0),
            }

    def close(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
