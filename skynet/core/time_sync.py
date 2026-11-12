#!/usr/bin/env python3
"""Garante que o relogio esteja certo antes de abrir conexoes TLS.

O wrapper /usr/local/bin/skynet ja sincroniza antes de chamar o main.py, mas
este modulo cobre tambem a execucao direta (`python3 main.py`) e protege quem
inicia o Skynet por outro caminho. O script real fica em /root/sync_time.py
(copiado de scripts/sync_time.py na instalacao).

Desative com SKYNET_SKIP_TIME_SYNC=1.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKIP_VALUES = {"1", "true", "yes", "on"}
CANDIDATES = (
    Path("/root/sync_time.py"),
    Path(__file__).resolve().parent.parent / "scripts" / "sync_time.py",
)


def ensure_clock(timeout: float = 30.0) -> bool:
    """Executa o sincronizador. Retorna True se o relogio esta confiavel."""
    if os.name != "posix":
        # Desenvolvimento fora do box (Windows): nao ha o que ajustar.
        return True
    if os.getenv("SKYNET_SKIP_TIME_SYNC", "").strip().lower() in SKIP_VALUES:
        return True

    syncer = next((path for path in CANDIDATES if path.is_file()), None)
    if syncer is None:
        return True

    try:
        result = subprocess.run(
            [sys.executable, str(syncer)],
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - nunca impedir o boot por isso
        print(f"[hora] Falha ao sincronizar o relogio: {exc}")
        return False

    if result.returncode != 0:
        print("[hora] Aviso: relogio nao sincronizado; o TLS pode falhar.")
        return False
    return True
