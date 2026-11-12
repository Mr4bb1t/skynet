#!/usr/bin/env python3
"""Teste offline do AEC3 com eco sintético (não precisa de microfone).

Gera uma "voz" de referência (ruído em banda de voz), simula o eco atrasado e
com reverberação, adiciona a voz do usuário no final (double-talk) e mede a
atenuação do eco (ERLE) depois da convergência.

Uso:
    cd /root/skynet && python3 tests/test_aec_offline.py
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np  # noqa: E402

from audio.aec import MIC_FRAME_BYTES, MIC_RATE, AudioEchoCanceller  # noqa: E402

SECONDS = 5.0
DELAY_MS = 100
FRAME_SAMPLES = MIC_RATE // 100


def speech_like(seconds: float, seed: int, amplitude: float = 0.22) -> np.ndarray:
    """Ruído filtrado em 300-3400 Hz, parecido com fala, em int16."""
    rng = np.random.default_rng(seed)
    samples = int(MIC_RATE * seconds)
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    freqs = np.fft.rfftfreq(samples, 1.0 / MIC_RATE)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0
    signal = np.fft.irfft(spectrum, samples)
    signal = signal / (np.max(np.abs(signal)) or 1.0)
    # Envelope lento para não ser um tom constante.
    envelope = 0.65 + 0.35 * np.sin(2 * np.pi * 0.7 * np.arange(samples) / MIC_RATE)
    return np.clip(signal * envelope * amplitude * 32767, -32768, 32767).astype(np.int16)


def room_echo(far: np.ndarray, delay_ms: int = DELAY_MS) -> np.ndarray:
    """Eco com atraso principal + duas reflexões, em float."""
    echo = np.zeros(len(far), dtype=np.float64)
    reflections = [
        (delay_ms / 1000.0, 0.55),
        (delay_ms / 1000.0 + 0.021, 0.22),
        (delay_ms / 1000.0 + 0.047, 0.12),
    ]
    for delay_s, gain in reflections:
        shift = int(delay_s * MIC_RATE)
        if shift < len(far):
            echo[shift:] += far[:-shift].astype(np.float64) * gain
    return echo


def rms(signal: np.ndarray) -> float:
    if len(signal) == 0:
        return 0.0
    return float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))


class AecOfflineTest(unittest.TestCase):
    def test_echo_is_cancelled(self) -> None:
        aec = AudioEchoCanceller(enabled=True, delay_ms=DELAY_MS, playback_rate=MIC_RATE)
        if not aec.active:
            self.skipTest("AEC3 nativo indisponível (libskynet_aec.so)")

        far = speech_like(SECONDS, seed=1, amplitude=0.24)
        user = speech_like(SECONDS, seed=2, amplitude=0.20)
        echo = room_echo(far)
        near = echo + np.random.default_rng(3).standard_normal(len(far)) * 25
        # Usuário só fala nos últimos 2 s (double-talk).
        double_talk_at = int(3.0 * MIC_RATE)
        near[double_talk_at:] += user[double_talk_at:]
        near = np.clip(near, -32768, 32767).astype(np.int16)

        cleaned_frames: list[bytes] = []
        for start in range(0, len(near) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            far_frame = far[start : start + FRAME_SAMPLES].tobytes()
            near_frame = near[start : start + FRAME_SAMPLES].tobytes()
            self.assertEqual(len(near_frame), MIC_FRAME_BYTES)
            aec.push_far(far_frame)
            cleaned_frames.append(aec.process_near_16k(near_frame))

        cleaned = np.frombuffer(b"".join(cleaned_frames), dtype=np.int16)
        # Janela só de eco (antes do double-talk e depois da convergência).
        start = int(1.5 * MIC_RATE)
        stop = int(2.5 * MIC_RATE)
        erle_db = 20 * math.log10(max(rms(near[start:stop]), 1e-9) / max(rms(cleaned[start:stop]), 1e-9))
        stats = aec.stats()
        print(
            f"\nERLE na janela de eco: {erle_db:.1f} dB "
            f"(frames={stats['frames']}, delay={stats['delay_ms']} ms)"
        )
        self.assertGreater(erle_db, 6.0, "AEC3 não atenuou o eco o suficiente")
        aec.close()

    def test_bypass_returns_input(self) -> None:
        aec = AudioEchoCanceller(enabled=False)
        self.assertFalse(aec.active)
        sample = b"\x01\x02" * FRAME_SAMPLES
        self.assertEqual(aec.process_near_16k(sample), sample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
