"""Configuração do subsistema de memória de longo prazo (RAG local).

Lida do ambiente/.env e não depende de `vision.config` para poder ser usada
tanto pelo chat de texto quanto pelo agente de voz.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "sim", "s")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def resolve_path(value: str, default: str) -> str:
    """Resolve caminhos relativos a partir da raiz do projeto (skynet/)."""
    path = Path(value or default).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path)


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    db_path: str = ""
    embeddings_enabled: bool = True
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    top_k: int = 5
    candidates: int = 30
    token_budget: int = 380
    photo_token_budget: int = 160
    min_score: float = 0.20
    half_life_days: float = 45.0
    max_content_chars: int = 700
    working_ttl_sec: int = 1800
    consolidate_every_turns: int = 10
    consolidation_model: str = "gemini-3.1-flash-lite"

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        top_k = max(1, min(12, _env_int("MEMORY_RAG_TOP_K", 5)))
        candidates = max(top_k, min(200, _env_int("MEMORY_RAG_CANDIDATES", 30)))
        token_budget = max(80, min(2000, _env_int("MEMORY_RAG_TOKEN_BUDGET", 380)))
        photo_budget = max(
            60, min(token_budget, _env_int("MEMORY_PHOTO_TOKEN_BUDGET", 160))
        )
        return cls(
            enabled=_env_bool("MEMORY_ENABLED", True),
            db_path=resolve_path(
                _env("MEMORY_DB_PATH", ""), "data/memory.db"
            ),
            embeddings_enabled=_env_bool("MEMORY_EMBEDDINGS_ENABLED", True),
            embedding_model=_env(
                "MEMORY_EMBEDDING_MODEL", "gemini-embedding-001"
            ),
            embedding_dim=max(
                128, min(3072, _env_int("MEMORY_EMBEDDING_DIM", 768))
            ),
            top_k=top_k,
            candidates=candidates,
            token_budget=token_budget,
            photo_token_budget=photo_budget,
            min_score=max(
                0.0, min(1.0, _env_float("MEMORY_RAG_MIN_SCORE", 0.20))
            ),
            half_life_days=max(
                1.0, _env_float("MEMORY_RECENCY_HALF_LIFE_DAYS", 45.0)
            ),
            max_content_chars=max(
                120, min(4000, _env_int("MEMORY_MAX_CONTENT_CHARS", 700))
            ),
            working_ttl_sec=max(
                60, min(7200, _env_int("MEMORY_WORKING_TTL_SEC", 1800))
            ),
            consolidate_every_turns=max(
                2, min(100, _env_int("MEMORY_CONSOLIDATE_EVERY_TURNS", 10))
            ),
            consolidation_model=_env(
                "MEMORY_CONSOLIDATION_MODEL",
                _env("GEMINI_TEXT_MODEL", "gemini-3.1-flash-lite"),
            ),
        )
