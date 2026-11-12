"""RAG de memória: recupera só o que é relevante e cabe no orçamento de tokens.

O fluxo é intencionalmente barato:

  1. a consulta é o texto atual do usuário + um trecho curto da conversa +
     nomes de rostos reconhecidos;
  2. o banco devolve poucos candidatos (FTS + embeddings + entidades);
  3. o ranking mistura semelhança, importância e recência;
  4. só as melhores memórias são formatadas, truncadas e limitadas por um
     orçamento de tokens.

A IA também pode decidir explicitamente o que guardar/consultar pelas
ferramentas de memória. Nada é despejado por inteiro no prompt.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .config import MemoryConfig
from .embeddings import BaseEmbedder, build_embedder
from .store import MemoryStore
from .text import compact_whitespace, tokens, truncate

LOG = logging.getLogger("memory.rag")

_KIND_LABELS = {
    "person": "pessoa",
    "animal": "animal",
    "event": "acontecimento",
    "project": "projeto",
    "idea": "ideia",
    "preference": "preferência",
    "fact": "fato",
    "place": "lugar",
    "object": "objeto",
    "task": "tarefa",
    "other": "outro",
}

_CONTEXT_HEADER = "Memórias relevantes (use só se ajudar; não recite):"


def _people_names(people: Optional[Iterable[Any]]) -> list[str]:
    names: list[str] = []
    for item in people or []:
        if isinstance(item, str):
            values = [item]
        elif isinstance(item, dict):
            values = [item.get("name") or "", item.get("nickname") or ""]
        else:
            values = [
                getattr(item, "name", "") or "",
                getattr(item, "nickname", "") or "",
            ]
        for value in values:
            value = compact_whitespace(str(value))
            if value and value not in names:
                names.append(value)
    return names


class MemoryManager:
    """Fachada única usada pelo chat, pelo agente de voz e pelas ferramentas."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        config: Optional[MemoryConfig] = None,
        source: str = "chat",
        embedder: Optional[BaseEmbedder] = None,
    ) -> None:
        self.config = config or MemoryConfig.from_env()
        self.source = source
        self.available = False
        self.reason = ""
        self.store: Optional[MemoryStore] = None
        self.embedder: Optional[BaseEmbedder] = None

        if not self.config.enabled:
            self.reason = "disabled"
            return
        try:
            if embedder is None:
                embedder = build_embedder(
                    api_key=api_key,
                    model=self.config.embedding_model,
                    dim=self.config.embedding_dim,
                    enabled=self.config.embeddings_enabled,
                )
            self.embedder = embedder
            self.store = MemoryStore(
                self.config.db_path,
                embedder,
                max_content_chars=self.config.max_content_chars,
                half_life_days=self.config.half_life_days,
                candidates=self.config.candidates,
                min_score=self.config.min_score,
            )
            self.available = True
        except Exception as exc:  # pragma: no cover - depende do ambiente
            self.reason = str(exc)
            LOG.warning("Memória de longo prazo desativada: %s", exc)

    # ------------------------------------------------------------------
    # Operações expostas às ferramentas
    # ------------------------------------------------------------------
    def remember(
        self,
        content: str,
        *,
        kind: str = "fact",
        subject: str = "",
        tags: str = "",
        importance: float = 0.5,
        memory_id: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        result = self.store.remember(
            content=content,
            kind=kind,
            subject=subject,
            tags=tags,
            importance=importance,
            source=self.source,
            metadata=metadata,
            memory_id=memory_id,
        )
        if result.get("status") == "ok":
            memory = result.get("memory", {})
            result = {
                "status": "ok",
                "memory_id": result.get("memory_id"),
                "updated": bool(result.get("updated")),
                "kind": memory.get("kind"),
                "subject": memory.get("subject", ""),
            }
        return result

    def recall(
        self,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        entities: Optional[list[str]] = None,
        limit: int = 5,
        threshold: Optional[float] = None,
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        result = self.store.recall(
            query,
            kinds=kinds,
            entities=entities,
            limit=limit,
            min_score=threshold,
        )
        if result.get("status") == "ok":
            result["text"] = self.format_memories(
                result.get("memories", []), include_ids=True
            )
        return result

    def forget(
        self,
        *,
        memory_id: Optional[int] = None,
        query: str = "",
        limit: int = 5,
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        return self.store.forget(memory_id=memory_id, query=query, limit=limit)

    # ------------------------------------------------------------------
    # Contexto automático (RAG)
    # ------------------------------------------------------------------
    def build_context(
        self,
        query: str = "",
        *,
        conversation: str = "",
        people: Optional[Iterable[Any]] = None,
        budget_tokens: Optional[int] = None,
        include_ids: bool = False,
        kinds: Optional[list[str]] = None,
    ) -> str:
        """Monta o bloco compacto de memórias relevantes (ou '' se nada)."""
        if not self.available or self.store is None:
            return ""
        if self.store.count() == 0:
            return ""

        names = _people_names(people)
        query = compact_whitespace(query)
        conversation = truncate(compact_whitespace(conversation), 500)
        # Mensagens triviais ("oi", "ok") não gastam embedding nem tokens.
        if not names and not tokens(query) and not tokens(conversation):
            return ""
        parts = [part for part in (query, conversation, ", ".join(names)) if part]
        retrieval_query = "\n".join(parts)
        if not retrieval_query:
            return ""

        result = self.store.recall(
            retrieval_query,
            kinds=kinds,
            entities=names,
            limit=self.config.top_k,
            min_score=self.config.min_score,
        )
        memories = result.get("memories", [])
        if not memories:
            return ""
        return self.format_memories(
            memories,
            budget_tokens=budget_tokens or self.config.token_budget,
            include_ids=include_ids,
        )

    def format_memories(
        self,
        memories: Iterable[dict[str, Any]],
        *,
        budget_tokens: Optional[int] = None,
        include_ids: bool = False,
    ) -> str:
        items = list(memories)
        if not items:
            return ""
        budget_chars = max(160, int((budget_tokens or self.config.token_budget) * 3.8))
        lines = [_CONTEXT_HEADER]
        used = len(lines[0]) + 1
        for memory in items:
            kind = _KIND_LABELS.get(str(memory.get("kind", "")), "outro")
            subject = compact_whitespace(str(memory.get("subject", "")))
            content = truncate(str(memory.get("content", "")), 240)
            tags = compact_whitespace(str(memory.get("tags", "")))
            memory_id = memory.get("memory_id")
            prefix = f"#{memory_id} " if include_ids and memory_id else ""
            subject_part = f"{subject}: " if subject else ""
            tags_part = f" [{tags}]" if tags else ""
            line = f"- {prefix}[{kind}] {subject_part}{content}{tags_part}"
            remaining = budget_chars - used
            if remaining < 32:
                break
            if len(line) > remaining:
                line = truncate(line, remaining)
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def describe(self) -> str:
        if not self.available:
            return f"memory=off ({self.reason})"
        count = self.store.count() if self.store is not None else 0
        provider = getattr(self.embedder, "name", "?")
        return (
            f"memory=on entries={count} embeddings={provider} "
            f"top_k={self.config.top_k} budget={self.config.token_budget}"
        )
