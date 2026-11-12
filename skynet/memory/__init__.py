"""Memória de longo prazo com RAG local para o SKYNET.

Uso típico:

    from memory import MemoryManager, MemoryTool, MEMORY_DECLARATIONS

    manager = MemoryManager(api_key=settings.gemini_api_key, source="chat")
    tool = MemoryTool(manager)
    context = manager.build_context("quem é o João?", conversation=...)

O banco fica em `data/memory.db` (configurável por `MEMORY_DB_PATH`).
"""

from .config import MemoryConfig
from .consolidate import MemoryConsolidator
from .embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    GeminiEmbedder,
    HashingEmbedder,
    ResilientEmbedder,
    build_embedder,
)
from .rag import MemoryManager, format_memory_line
from .store import MEMORY_TYPES, VALID_KINDS, MemoryStore, normalize_kind
from .working import WorkingMemory
from .tools import (
    FORGET_DECLARATION,
    MEMORY_DECLARATIONS,
    RECALL_DECLARATION,
    REMEMBER_DECLARATION,
    MemoryTool,
)

__all__ = [
    "MemoryConfig",
    "MemoryConsolidator",
    "MemoryManager",
    "MemoryStore",
    "MemoryTool",
    "MEMORY_DECLARATIONS",
    "REMEMBER_DECLARATION",
    "RECALL_DECLARATION",
    "FORGET_DECLARATION",
    "VALID_KINDS",
    "MEMORY_TYPES",
    "WorkingMemory",
    "format_memory_line",
    "normalize_kind",
    "build_embedder",
    "HashingEmbedder",
    "GeminiEmbedder",
    "ResilientEmbedder",
    "EmbeddingBatch",
    "EmbeddingError",
]
