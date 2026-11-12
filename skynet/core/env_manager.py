"""Gerenciador e editor persistente do arquivo .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


def find_env_path() -> Path:
    """Encontra o caminho do arquivo .env ativo."""
    candidates = [
        Path("/root/skynet/.env"),
        Path(__file__).resolve().parents[1] / ".env",
        Path.cwd() / ".env",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[1]


def read_env_values(env_path: Optional[Path] = None) -> Dict[str, str]:
    """Lê todas as variáveis chave=valor do .env."""
    path = env_path or find_env_path()
    values: Dict[str, str] = {}
    if not path.is_file():
        return values

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    values[k] = v
    except Exception:
        pass
    return values


def update_env_values(updates: Dict[str, str], env_path: Optional[Path] = None) -> bool:
    """Atualiza chaves no arquivo .env preservando comentários e formatação original."""
    path = env_path or find_env_path()
    lines = []
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            lines = []

    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                new_val = updates[k]
                new_lines.append(f"{k}={new_val}\n")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    # Chaves novas que não existiam no arquivo
    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}\n")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True
    except Exception as e:
        return False

