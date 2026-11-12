"""Metricas e diagnostico do subsistema de visao.

Mede cada etapa separadamente:
  captura -> redimensionamento -> compressao JPEG -> envio -> resposta

Nao confunde tempo de envio com tempo de interpretacao do modelo.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Deque, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - opcional
    psutil = None  # type: ignore


class RateMeter:
    """Taxa calculada sobre uma janela de eventos (thread-safe via caller)."""

    __slots__ = ("_times",)

    def __init__(self, window: int = 120) -> None:
        self._times: Deque[float] = deque(maxlen=window)

    def add(self, when: Optional[float] = None) -> None:
        self._times.append(time.monotonic() if when is None else when)

    def clear(self) -> None:
        self._times.clear()

    def rate(self, now: Optional[float] = None) -> float:
        if len(self._times) < 2:
            return 0.0
        now = time.monotonic() if now is None else now
        span = now - self._times[0]
        if span <= 0:
            return 0.0
        return (len(self._times) - 1) / span


def _fmt(value: Optional[float], suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


class Diagnostics:
    """Contadores e medias de todo o pipeline."""

    def __init__(self, process_name: str = "vision") -> None:
        self._lock = threading.Lock()
        self._process_name = process_name

        # Captura
        self.capture_frames = 0
        self.capture_restarts = 0
        self.capture_errors = 0
        self.last_frame_ts: Optional[float] = None
        self.last_frame_seq = -1
        self.last_frame_w = 0
        self.last_frame_h = 0
        self._capture_rate = RateMeter(240)

        # Processamento
        self.processed_frames = 0
        self.last_orig_w = 0
        self.last_orig_h = 0
        self.last_out_w = 0
        self.last_out_h = 0
        self.last_jpeg_bytes = 0
        self.last_resize_ms: Optional[float] = None
        self.last_encode_ms: Optional[float] = None
        self._resize_sum_ms = 0.0
        self._encode_sum_ms = 0.0
        self._jpeg_bytes_sum = 0

        # Envio / heartbeat
        self.submitted_frames = 0
        self.sent_frames = 0
        self.dropped_frames = 0
        self.send_errors = 0
        self.send_timeouts = 0
        self.last_send_ms: Optional[float] = None
        self._send_sum_ms = 0.0
        self._send_count_ms = 0
        self.last_send_ts: Optional[float] = None
        self._send_rate = RateMeter(120)
        self.heartbeat_skips = Counter()

        # Sessao Gemini
        self.ws_state = "closed"
        self.connect_count = 0
        self.connect_failures = 0
        self.disconnect_count = 0
        self.reconnect_count = 0
        self.last_disconnect_reason = ""
        self.resumption_updates = 0
        self.resumption_used = 0
        self.goaway_count = 0
        self.last_goaway_sec: Optional[float] = None

        # Respostas
        self.questions = 0
        self.responses = 0
        self.spontaneous_responses = 0
        self.transcription_chars = 0
        self.last_first_token_ms: Optional[float] = None
        self.last_turn_complete_ms: Optional[float] = None
        self.audio_bytes = 0
        self.usage_total = 0
        self.usage_prompt = 0
        self.usage_response = 0

        # Sistema
        self.cpu_pct: Optional[float] = None
        self.rss_mb: Optional[float] = None
        self._psproc = None
        if psutil is not None:
            try:
                self._psproc = psutil.Process(os.getpid())
                self._psproc.cpu_percent(None)  # inicializa a medicao
            except Exception:
                self._psproc = None

        # Erros por categoria
        self.errors: Counter = Counter()

    # ------------------------------------------------------------------
    # Captura
    # ------------------------------------------------------------------
    def record_capture(self, seq: int, width: int, height: int) -> None:
        with self._lock:
            self.capture_frames += 1
            self.last_frame_seq = seq
            self.last_frame_ts = time.monotonic()
            self.last_frame_w = width
            self.last_frame_h = height
            self._capture_rate.add(self.last_frame_ts)

    def record_capture_restart(self) -> None:
        with self._lock:
            self.capture_restarts += 1

    def capture_rate(self, now: Optional[float] = None) -> float:
        with self._lock:
            return self._capture_rate.rate(now)

    def send_rate(self, now: Optional[float] = None) -> float:
        with self._lock:
            return self._send_rate.rate(now)

    def record_error(self, category: str, message: str = "") -> None:
        with self._lock:
            self.errors[category] += 1
            if category.startswith("capture"):
                self.capture_errors += 1

    # ------------------------------------------------------------------
    # Processamento
    # ------------------------------------------------------------------
    def record_processed(
        self,
        orig_w: int,
        orig_h: int,
        out_w: int,
        out_h: int,
        jpeg_bytes: int,
        resize_ms: float,
        encode_ms: float,
    ) -> None:
        with self._lock:
            self.processed_frames += 1
            self.last_orig_w = orig_w
            self.last_orig_h = orig_h
            self.last_out_w = out_w
            self.last_out_h = out_h
            self.last_jpeg_bytes = jpeg_bytes
            self.last_resize_ms = resize_ms
            self.last_encode_ms = encode_ms
            self._resize_sum_ms += resize_ms
            self._encode_sum_ms += encode_ms
            self._jpeg_bytes_sum += jpeg_bytes

    # ------------------------------------------------------------------
    # Envio
    # ------------------------------------------------------------------
    def record_submitted(self) -> None:
        with self._lock:
            self.submitted_frames += 1

    def record_dropped(self, reason: str = "queue_full") -> None:
        with self._lock:
            self.dropped_frames += 1
            self.heartbeat_skips[reason] += 1

    def record_heartbeat_skip(self, reason: str) -> None:
        with self._lock:
            self.heartbeat_skips[reason] += 1

    def record_sent(self, send_ms: float) -> None:
        with self._lock:
            self.sent_frames += 1
            self.last_send_ms = send_ms
            self._send_sum_ms += send_ms
            self._send_count_ms += 1
            self.last_send_ts = time.monotonic()
            self._send_rate.add(self.last_send_ts)

    def record_send_failure(self, timeout: bool = False) -> None:
        with self._lock:
            if timeout:
                self.send_timeouts += 1
            else:
                self.send_errors += 1

    # ------------------------------------------------------------------
    # Sessao
    # ------------------------------------------------------------------
    def record_connecting(self) -> None:
        with self._lock:
            self.ws_state = "connecting"

    def record_connected(self, used_handle: bool) -> None:
        with self._lock:
            self.connect_count += 1
            self.ws_state = "open"
            if used_handle:
                self.resumption_used += 1
            if self.disconnect_count > 0:
                self.reconnect_count += 1

    def record_connect_failure(self, reason: str) -> None:
        with self._lock:
            self.connect_failures += 1
            self.ws_state = "closed"
            self.last_disconnect_reason = reason

    def record_disconnected(self, reason: str) -> None:
        with self._lock:
            self.disconnect_count += 1
            self.ws_state = "closed"
            self.last_disconnect_reason = reason

    def record_resumption_update(self) -> None:
        with self._lock:
            self.resumption_updates += 1

    def record_goaway(self, time_left_sec: Optional[float]) -> None:
        with self._lock:
            self.goaway_count += 1
            self.last_goaway_sec = time_left_sec

    # ------------------------------------------------------------------
    # Respostas
    # ------------------------------------------------------------------
    def record_question(self) -> None:
        with self._lock:
            self.questions += 1

    def record_first_token(self, elapsed_ms: float) -> None:
        with self._lock:
            self.last_first_token_ms = elapsed_ms
            self.responses += 1

    def record_turn_complete(self, elapsed_ms: Optional[float]) -> None:
        with self._lock:
            self.last_turn_complete_ms = elapsed_ms

    def record_spontaneous_response(self) -> None:
        """Resposta iniciada pelo modelo sem pergunta (deve permanecer 0)."""
        with self._lock:
            self.spontaneous_responses += 1

    def record_transcription(self, chars: int) -> None:
        with self._lock:
            self.transcription_chars += chars

    def record_audio_bytes(self, count: int) -> None:
        with self._lock:
            self.audio_bytes += count

    def record_usage(self, total: int, prompt: int, response: int) -> None:
        with self._lock:
            self.usage_total = total
            self.usage_prompt = prompt
            self.usage_response = response

    # ------------------------------------------------------------------
    # Sistema
    # ------------------------------------------------------------------
    def update_system(self) -> None:
        if self._psproc is None:
            return
        try:
            cpu = self._psproc.cpu_percent(None)
            rss = self._psproc.memory_info().rss / (1024 * 1024)
        except Exception:
            return
        with self._lock:
            self.cpu_pct = cpu
            self.rss_mb = rss

    # ------------------------------------------------------------------
    # Resumo
    # ------------------------------------------------------------------
    def summary(self) -> str:
        now = time.monotonic()
        with self._lock:
            age = (now - self.last_frame_ts) if self.last_frame_ts else None
            cap_rate = self._capture_rate.rate(now)
            send_rate = self._send_rate.rate(now)
            avg_resize = (
                self._resize_sum_ms / self.processed_frames
                if self.processed_frames
                else None
            )
            avg_encode = (
                self._encode_sum_ms / self.processed_frames
                if self.processed_frames
                else None
            )
            avg_jpeg = (
                self._jpeg_bytes_sum / self.processed_frames / 1024
                if self.processed_frames
                else None
            )
            avg_send = (
                self._send_sum_ms / self._send_count_ms
                if self._send_count_ms
                else None
            )
            skips = ",".join(f"{k}={v}" for k, v in sorted(self.heartbeat_skips.items()))
            parts = [
                f"cap={_fmt(cap_rate, 'fps')}",
                f"age={_fmt(age, 's', 2)}",
                f"orig={self.last_frame_w}x{self.last_frame_h}",
                f"proc={self.processed_frames}",
                f"out={self.last_out_w}x{self.last_out_h}",
                f"resize={_fmt(avg_resize, 'ms', 2)}",
                f"encode={_fmt(avg_encode, 'ms', 2)}",
                f"jpeg={_fmt(avg_jpeg, 'KB', 1)}",
                f"sub={self.submitted_frames}",
                f"sent={self.sent_frames}",
                f"send_rate={_fmt(send_rate, '/s', 2)}",
                f"drops={self.dropped_frames}",
                f"send={_fmt(avg_send, 'ms', 2)}",
                f"ttf={_fmt(self.last_first_token_ms, 'ms', 0)}",
                f"turn={_fmt(self.last_turn_complete_ms, 'ms', 0)}",
                f"answers={self.responses}",
                f"spont={self.spontaneous_responses}",
                f"ws={self.ws_state}",
                f"conn={self.connect_count}",
                f"reconn={self.reconnect_count}",
                f"resump={self.resumption_updates}",
                f"goaway={self.goaway_count}",
                f"tokens={self.usage_total}",
                f"cpu={_fmt(self.cpu_pct, '%', 0)}",
                f"ram={_fmt(self.rss_mb, 'MB', 1)}",
            ]
            if skips:
                parts.append(f"skips={skips}")
            if self.errors:
                parts.append("errors=" + ",".join(f"{k}={v}" for k, v in sorted(self.errors.items())))
            return " ".join(parts)

    def log_summary(self, logger: logging.Logger, level: int = logging.INFO) -> None:
        self.update_system()
        logger.log(level, "DIAG %s", self.summary())


class CsvRecorder:
    """Registra uma linha por frame processado (para testes prolongados)."""

    FIELDS = [
        "timestamp",
        "frame_seq",
        "orig_w",
        "orig_h",
        "out_w",
        "out_h",
        "jpeg_kb",
        "resize_ms",
        "encode_ms",
        "frame_age_ms",
        "submitted",
        "connected",
        "capture_fps",
        "send_fps",
        "sent_total",
        "dropped_total",
        "cpu_pct",
        "rss_mb",
        "tokens_total",
    ]

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        if new_file:
            self._writer.writeheader()
            self._file.flush()

    def write(self, **row) -> None:
        clean = {key: row.get(key, "") for key in self.FIELDS}
        clean["timestamp"] = clean.get("timestamp") or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._writer.writerow(clean)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass
