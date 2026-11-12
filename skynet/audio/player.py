"""Módulo de Saída de Áudio para o Skynet (Alto-falante do TV Box).

Reproduz a voz do Gemini através da saída de áudio física do TV Box
(P2/AV analógico, HDMI ou caixinha de som USB conectada ao Box).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Callable, Optional

LOG = logging.getLogger("skynet.audio.player")


class AudioPlayer:
    """Reprodutor contínuo de áudio PCM de baixa latência usando aplay."""

    def __init__(
        self,
        device: str = "default",
        sample_rate: int = 24000,
        reference_sink: Optional[Callable[[bytes], None]] = None,
    ):
        self.device = device
        self.sample_rate = sample_rate
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        # Recebe uma copia do PCM enviado ao alto-falante (referencia do AEC).
        self._reference_sink = reference_sink

        # Garante que os volumes de hardware do TV Box estão desmutados
        self._unmute_alsa()

    def set_reference_sink(self, sink: Optional[Callable[[bytes], None]]) -> None:
        """Define quem recebe a copia do audio reproduzido (far-end do AEC)."""
        self._reference_sink = sink

    def _notify_reference(self, pcm_bytes: bytes) -> None:
        sink = self._reference_sink
        if sink is None:
            return
        try:
            sink(pcm_bytes)
        except Exception as exc:  # noqa: BLE001 - AEC nunca pode quebrar o audio
            LOG.debug("Falha ao entregar referencia ao AEC: %s", exc)


    def _unmute_alsa(self) -> None:
        """Garante que o volume digital do codec Allwinner (card 0) e USB estejam ativos."""
        try:
            # Codec analógico Allwinner (card 0)
            subprocess.run(["amixer", "-c", "0", "cset", "numid=2", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["amixer", "-c", "0", "cset", "numid=6", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["amixer", "-c", "0", "cset", "numid=5", "31"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass

        try:
            # Dispositivo USB Fifine / USB-Audio (se conectado)
            subprocess.run(["amixer", "-c", "Microphone", "sset", "Speaker", "100%", "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["amixer", "-c", "Microphone", "sset", "Mic", "100%", "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass

    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        cmd = [
            "aplay",
            "-q",
            "-D", self.device,
            "-r", str(self.sample_rate),
            "-f", "S16_LE",
            "-c", "1",
            "-t", "raw",
            "--buffer-time=150000"
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return True
        except Exception as e:
            LOG.error("Falha ao abrir processo aplay para reprodução: %s", e)
            self._proc = None
            return False

    def play_chunk(self, pcm_bytes: bytes) -> None:
        """Escreve um bloco de áudio PCM de 24kHz para ser reproduzido imediatamente."""
        if not pcm_bytes:
            return
        with self._lock:
            if not self._ensure_proc():
                return
            # A referencia precisa ser exatamente o que vai para o alto-falante.
            self._notify_reference(pcm_bytes)
            try:
                if self._proc and self._proc.stdin:
                    self._proc.stdin.write(pcm_bytes)
                    self._proc.stdin.flush()
            except BrokenPipeError:
                LOG.warning("Canal de áudio aplay fechou; reiniciando...")
                self._proc = None
            except Exception as e:
                LOG.error("Erro ao enviar chunk de áudio para aplay: %s", e)

    def play_startup_sound(self) -> None:
        """Emite um sinal sonoro inicial curto e agradável indicando hardware de áudio ativo."""
        import math
        import struct
        notes = [(523.25, 0.08), (659.25, 0.08), (1046.50, 0.20)]
        pcm = bytearray()
        for freq, dur in notes:
            n_samples = int(self.sample_rate * dur)
            for i in range(n_samples):
                t = i / self.sample_rate
                fade = math.sin((i / n_samples) * math.pi) if dur < 0.1 else (1.0 - (i / n_samples) ** 0.6)
                val = int(math.sin(2 * math.pi * freq * t) * 0.35 * fade * 32767)
                pcm.extend(struct.pack("<h", max(-32768, min(32767, val))))
        self.play_chunk(bytes(pcm))

    def interrupt(self) -> None:
        """Interrompe a fala do robô instantaneamente (barge-in do usuário)."""
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=0.2)
                except Exception:
                    self._proc.kill()
                self._proc = None

    def close(self) -> None:
        """Fecha o player e drena o áudio pendente."""
        with self._lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    self._proc.kill()
                self._proc = None
