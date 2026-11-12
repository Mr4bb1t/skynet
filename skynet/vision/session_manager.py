"""Supervisor da sessao Gemini Live: reconexao, backoff e retomada.

Mantem a captura da camera totalmente independente. Quando o Gemini cai,
o SessionManager reconecta com backoff exponencial + jitter e reutiliza o
ultimo handle de SessionResumption. O heartbeat continua lendo a camera e,
ao reconectar, envia apenas o frame mais recente (nunca uma fila antiga).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from typing import Optional

from .config import ConfigError, Settings
from .diagnostics import Diagnostics
from .gemini_live_client import (
    GeminiLiveClient,
    GeminiLiveError,
    NotConnectedError,
    ResponseCollector,
    ensure_sdk,
)

LOG = logging.getLogger("vision.session")


class SessionManager:
    """Dono do ciclo de vida das conexoes Live."""

    def __init__(
        self,
        settings: Settings,
        diagnostics: Diagnostics,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        ensure_sdk()
        if not settings.gemini_api_key:
            raise ConfigError(
                "GEMINI_API_KEY nao definida. Preencha o arquivo .env."
            )
        from google import genai  # import local: camera/processo nao exigem SDK

        self._settings = settings
        self._diag = diagnostics
        self._log = logger or LOG
        self._genai = genai.Client(api_key=settings.gemini_api_key)

        self._handle: Optional[str] = None
        self._client: Optional[GeminiLiveClient] = None
        self._connected = asyncio.Event()
        self._generation = 0
        self._generation_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Propriedades
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        client = self._client
        return bool(client and client.is_open)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def handle(self) -> Optional[str]:
        client = self._client
        if client is not None and client.handle:
            return client.handle
        return self._handle

    @property
    def connected_handle_used(self) -> bool:
        client = self._client
        return bool(client and client.handle_used)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._supervisor(), name="gemini-session")

    async def stop(self) -> None:
        self._stop.set()
        # Fecha a conexao primeiro: o supervisor sai do wait_closed(), ve
        # _stop e encerra sem registrar uma reconexao desnecessaria.
        client = self._client
        self._client = None
        if client is not None:
            with suppress(Exception):
                await client.close("shutdown")
        task = self._task
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._connected.clear()
        with suppress(Exception):
            await self._genai.aio.aclose()

    # ------------------------------------------------------------------
    # Supervisor / reconexao
    # ------------------------------------------------------------------
    async def _supervisor(self) -> None:
        backoff = self._settings.reconnect_base_sec
        while not self._stop.is_set():
            handle = self._handle
            client = GeminiLiveClient(
                self._genai,
                self._settings,
                self._diag,
                self._log,
                handle=handle,
            )
            try:
                await client.connect()
            except asyncio.CancelledError:
                with suppress(Exception):
                    await client.close("cancelled")
                raise
            except Exception as exc:
                with suppress(Exception):
                    await client.close("connect_failed")
                self._diag.record_connect_failure(str(exc))
                if handle:
                    self._log.warning(
                        "Falha ao retomar a sessao (%s). Tentando uma conexao nova sem handle.",
                        exc,
                    )
                    self._handle = None
                    continue
                delay = self._jitter(backoff)
                self._log.warning(
                    "Falha ao conectar ao Gemini (%s). Nova tentativa em %.1fs.",
                    exc,
                    delay,
                )
                await self._wait_stop(delay)
                backoff = min(
                    self._settings.reconnect_max_sec, backoff * 2.0
                )
                continue

            # Conectado
            self._client = client
            self._generation += 1
            self._generation_event.set()
            self._connected.set()
            connected_at = time.monotonic()
            backoff = self._settings.reconnect_base_sec
            self._log.info(
                "Conectado ao Gemini Live (geracao=%d, handle=%s, modelo=%s).",
                self._generation,
                "retomado" if client.handle_used else "novo",
                self._settings.gemini_model,
            )

            reason = "unknown"
            try:
                reason = await client.wait_closed()
            except asyncio.CancelledError:
                with suppress(Exception):
                    await client.close("cancelled")
                raise
            finally:
                self._connected.clear()
                if self._client is client:
                    self._client = None
                if client.handle:
                    self._handle = client.handle
                with suppress(Exception):
                    await client.close(reason or client.closed_reason or "closed")

            if self._stop.is_set():
                break

            uptime = time.monotonic() - connected_at
            if reason in ("goaway", "forced"):
                delay = 0.2
            elif uptime > 30.0:
                delay = self._jitter(self._settings.reconnect_base_sec)
                backoff = self._settings.reconnect_base_sec
            else:
                delay = self._jitter(backoff)
                backoff = min(self._settings.reconnect_max_sec, backoff * 2.0)

            self._log.warning(
                "Sessao Gemini caiu (%s) apos %.1fs. Reconectando em %.1fs (handle=%s).",
                reason,
                uptime,
                delay,
                "sim" if self._handle else "nao",
            )
            await self._wait_stop(delay)

    def _jitter(self, delay: float) -> float:
        return max(0.2, delay * random.uniform(0.7, 1.3))

    async def _wait_stop(self, delay: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # ------------------------------------------------------------------
    # API de alto nivel
    # ------------------------------------------------------------------
    async def wait_connected(self, timeout: float = 20.0) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise GeminiLiveError(
                f"Timeout aguardando conexao com o Gemini ({timeout:.0f}s)."
            ) from exc

    async def wait_generation(self, generation: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while self._generation < generation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GeminiLiveError(
                    f"Timeout aguardando reconexao (geracao {generation})."
                )
            self._generation_event.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._generation_event.wait(), timeout=remaining)

    def send_frame(self, jpeg: bytes) -> bool:
        client = self._client
        if client is None or not client.is_open:
            self._diag.record_dropped("disconnected")
            return False
        return client.submit_frame(jpeg)

    async def ask(self, text: str, *, timeout: float = 30.0) -> ResponseCollector:
        await self.wait_connected(timeout=min(timeout, 20.0))
        client = self._client
        if client is None:
            raise NotConnectedError("Sessao Live indisponivel.")
        return await client.ask(text, timeout=timeout)

    async def force_reconnect(self) -> None:
        """Fecha a conexao atual; o supervisor reconecta com o handle."""
        client = self._client
        if client is not None:
            self._log.warning("Reconexao forcada solicitada (teste).")
            await client.close("forced")
