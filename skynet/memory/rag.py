"""RAG de memória com separação temporal explícita.

O contexto entregue ao modelo tem quatro blocos:

  * CURRENT STATE       — data/hora, sessão, pessoas visíveis, conversa recente;
  * EPISODIC MEMORIES   — acontecimentos passados, sempre datados;
  * SEMANTIC KNOWLEDGE  — fatos/preferências com validade e confirmação;
  * FUTURE PLANS        — planos/tarefas futuras, marcados como não realizados.

A recuperação usa similaridade + importância + recência + intenção temporal.
Memórias substituídas/expiradas não entram como se fossem atuais.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Optional

from .config import MemoryConfig
from .embeddings import BaseEmbedder, build_embedder
from .store import MemoryStore
from .text import compact_whitespace, tokens, truncate
from .timeutil import (
    TZ_NAME,
    detect_temporal_intent,
    format_now,
    format_when,
    now_local,
    parse_iso,
)
from .working import WorkingMemory

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

_TYPE_LABELS = {
    "fact": "fato",
    "event": "evento",
    "plan": "plano",
    "task": "tarefa",
    "preference": "preferência",
    "relationship": "relação",
    "routine": "rotina",
    "state": "estado",
}

_CONTEXT_HEADER = "MEMÓRIAS DE LONGO PRAZO (histórico datado; NÃO é o agora):"


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


def format_memory_line(
    memory: dict[str, Any],
    *,
    include_ids: bool = False,
    now: Optional[datetime] = None,
) -> str:
    """Formata UMA memória com tipo, data/validade e sinais de confiança."""
    current = now or now_local()
    memory_type = str(memory.get("memory_type") or "fact")
    label = _TYPE_LABELS.get(
        memory_type, _KIND_LABELS.get(str(memory.get("kind", "")), "outro")
    )
    subject = compact_whitespace(str(memory.get("subject", "") or ""))
    content = truncate(str(memory.get("content", "") or ""), 220)
    tags = compact_whitespace(str(memory.get("tags", "") or ""))
    created_at = str(memory.get("created_at", "") or "")
    event_time = str(memory.get("event_time", "") or "")
    event_end = str(memory.get("event_end", "") or "")
    valid_from = str(memory.get("valid_from", "") or "")
    valid_until = str(memory.get("valid_until", "") or "")
    last_confirmed = str(memory.get("last_confirmed_at", "") or "")
    expires_at = str(memory.get("expires_at", "") or "")
    scope = str(memory.get("temporal_scope", "") or "unknown")
    status = str(memory.get("status", "") or "active").lower()
    verification = str(memory.get("verification_status", "") or "confirmed").lower()
    confidence = memory.get("confidence", 0.8)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.8

    when = ""
    if memory_type == "event":
        if event_time:
            when = f"Em {format_when(event_time, now=current)}"
            if event_end:
                when += f" até {format_when(event_end, now=current)}"
        else:
            when = (
                "Data desconhecida "
                f"(registrado em {format_when(created_at, now=current)})"
            )
    elif memory_type in ("plan", "task"):
        if event_time:
            event_dt = parse_iso(event_time)
            if event_dt is not None and event_dt < current:
                when = f"Para {format_when(event_time, now=current)} (data já passou)"
            else:
                when = f"Para {format_when(event_time, now=current)}"
        else:
            when = (
                "Sem data definida "
                f"(registrado em {format_when(created_at, now=current)})"
            )
    elif memory_type == "state":
        when = (
            f"Estado desde {format_when(valid_from, now=current)}"
            if valid_from
            else "Estado atual"
        )
        if expires_at:
            when += f"; expira em {format_when(expires_at, now=current)}"
    else:
        if scope == "timeless":
            when = "Atemporal"
        elif valid_from:
            when = f"Vigente desde {format_when(valid_from, now=current)}"
        elif last_confirmed and scope != "unknown":
            when = f"Confirmado em {format_when(last_confirmed, now=current)}"
        else:
            when = (
                "Validade/data desconhecida "
                f"(registrado em {format_when(created_at, now=current)})"
            )
        if valid_until:
            when += f"; válido até {format_when(valid_until, now=current)}"

    flags: list[str] = []
    if memory_type in ("plan", "task") and status == "active":
        flags.append("não realizado")
    if status == "superseded":
        flags.append("substituída")
    elif status == "expired":
        flags.append("expirada")
    elif status == "done":
        flags.append("concluída")
    elif status == "cancelled":
        flags.append("cancelada")
    elif status == "uncertain":
        flags.append("incerta")
    if verification == "inferred":
        flags.append(f"inferido, confiança {confidence:.2f}")
    elif verification == "uncertain":
        flags.append("não confirmado")

    prefix = ""
    if include_ids and memory.get("memory_id"):
        prefix = f"#{memory['memory_id']} "
    subject_part = f"{subject}: " if subject else ""
    tags_part = f" [{tags}]" if tags else ""
    flags_part = f" ({'; '.join(flags)})" if flags else ""
    return (
        f"- {prefix}[{label}] {when}: {subject_part}{content}{tags_part}{flags_part}"
    )


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
        self.api_key = api_key
        self.available = False
        self.reason = ""
        self.store: Optional[MemoryStore] = None
        self.embedder: Optional[BaseEmbedder] = None
        self.consolidator = None
        self.working = WorkingMemory(
            ttl_seconds=getattr(self.config, "working_ttl_sec", 1800)
        )

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
        source: Optional[str] = None,
        memory_type: str = "",
        event_time: str = "",
        event_end: str = "",
        valid_from: str = "",
        valid_until: str = "",
        last_confirmed_at: str = "",
        expires_at: str = "",
        temporal_scope: str = "",
        status: str = "active",
        confidence: float = 0.8,
        verification_status: str = "",
        session_id: str = "",
        supersede: bool = False,
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        result = self.store.remember(
            content=content,
            kind=kind,
            subject=subject,
            tags=tags,
            importance=importance,
            source=source or self.source,
            metadata=metadata,
            memory_id=memory_id,
            memory_type=memory_type,
            event_time=event_time,
            event_end=event_end,
            valid_from=valid_from,
            valid_until=valid_until,
            last_confirmed_at=last_confirmed_at,
            expires_at=expires_at,
            temporal_scope=temporal_scope,
            status=status,
            confidence=confidence,
            verification_status=verification_status,
            session_id=session_id,
            supersede=supersede,
        )
        if result.get("status") == "ok":
            memory = result.get("memory", {})
            result = {
                "status": "ok",
                "memory_id": result.get("memory_id"),
                "updated": bool(result.get("updated")),
                "kind": memory.get("kind"),
                "memory_type": memory.get("memory_type"),
                "subject": memory.get("subject", ""),
                "event_time": memory.get("event_time", ""),
                "memory_status": memory.get("status", "active"),
                "verification_status": memory.get("verification_status", "confirmed"),
            }
        return result

    def recall(
        self,
        query: str,
        *,
        kinds: Optional[list[str]] = None,
        entities: Optional[list[str]] = None,
        memory_types: Optional[list[str]] = None,
        temporal_intent: Optional[str] = None,
        include_history: bool = False,
        limit: int = 5,
        threshold: Optional[float] = None,
    ) -> dict[str, Any]:
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        result = self.store.recall(
            query,
            kinds=kinds,
            entities=entities,
            memory_types=memory_types,
            temporal_intent=temporal_intent,
            include_history=include_history,
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

    def consolidate(
        self,
        conversation: Any,
        *,
        session_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Consolida a conversa em memórias duráveis (Fase 3)."""
        if not self.available or self.store is None:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        if self.consolidator is None:
            from .consolidate import MemoryConsolidator

            self.consolidator = MemoryConsolidator(self, api_key=self.api_key)
        return self.consolidator.consolidate(
            conversation, session_id=session_id, force=force
        )

    # ------------------------------------------------------------------
    # Contexto automático (RAG) — quatro blocos explícitos
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
        now: Optional[datetime] = None,
    ) -> str:
        """Monta o bloco temporal de contexto (também sem consulta)."""
        current = now or now_local()

        # Presença: se `people` foi passado, ele é a percepção ATUAL (mesmo []).
        if people is not None:
            names = _people_names(people)
            self.working.set_people(names)
        else:
            names = self.working.current_people()

        snapshot = self.working.snapshot()
        turns = snapshot.get("turns", [])
        if turns:
            recent_text = " | ".join(
                f"{item['role']}: {item['text']}" for item in turns[-4:]
            )
        else:
            recent_text = conversation or ""
        recent_text = truncate(compact_whitespace(recent_text), 500)
        state = dict(snapshot.get("state") or {})
        active_task = compact_whitespace(str(state.pop("active_task", "") or ""))

        lines: list[str] = ["=== CURRENT STATE ==="]
        lines.append(f"Data/hora atual: {format_now(current)} ({TZ_NAME})")
        lines.append(
            "Pessoas visíveis agora: "
            + (", ".join(names) if names else "ninguém identificado agora")
        )
        if recent_text:
            lines.append(f"Conversa recente (sessão atual): {recent_text}")
        lines.append(f"Tarefa ativa: {active_task or 'nenhuma'}")
        for key, value in state.items():
            text = compact_whitespace(str(value))
            if text:
                lines.append(f"{key}: {text}")

        # Se não há memória de longo prazo, ainda devolvemos o estado atual.
        if self.store is None or not self.available:
            return "\n".join(lines)

        retrieval_query = truncate(
            compact_whitespace(
                " ".join(
                    part
                    for part in (query, conversation, ", ".join(names))
                    if part
                )
            ),
            500,
        )
        intent = detect_temporal_intent(query or conversation)

        episodic: list[dict[str, Any]] = []
        semantic: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        if retrieval_query:
            if intent != "future":
                episodic = self.store.recall(
                    retrieval_query,
                    entities=names,
                    memory_types=["event"],
                    temporal_intent=intent,
                    limit=3,
                    min_score=self.config.min_score,
                    now=current,
                ).get("memories", [])
            semantic = self.store.recall(
                retrieval_query,
                entities=names,
                memory_types=["fact", "preference", "relationship", "routine"],
                temporal_intent="any",
                limit=4,
                min_score=self.config.min_score,
                now=current,
            ).get("memories", [])
            plans = self.store.recall(
                retrieval_query,
                entities=names,
                memory_types=["plan", "task"],
                temporal_intent="future",
                limit=3,
                min_score=self.config.min_score,
                now=current,
            ).get("memories", [])

        def append_block(header: str, memories: list[dict[str, Any]], empty: str) -> None:
            lines.append("")
            lines.append(header)
            if memories:
                for memory in memories:
                    lines.append(
                        format_memory_line(
                            memory, include_ids=include_ids, now=current
                        )
                    )
            else:
                lines.append(empty)

        append_block(
            "=== EPISODIC MEMORIES ===",
            episodic,
            "(nenhuma memória episódica relevante)",
        )
        append_block(
            "=== SEMANTIC KNOWLEDGE ===",
            semantic,
            "(nenhum fato ou preferência relevante)",
        )
        append_block(
            "=== FUTURE PLANS ===",
            plans,
            "(nenhum plano ou tarefa futura)",
        )

        text = "\n".join(lines)
        budget_chars = max(360, int((budget_tokens or self.config.token_budget) * 3.8))
        if len(text) > budget_chars:
            text = text[: max(0, budget_chars - 2)].rstrip() + "\n…"
        return text

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
        now = now_local()
        budget_chars = max(200, int((budget_tokens or self.config.token_budget) * 3.8))
        lines = [_CONTEXT_HEADER]
        used = len(lines[0]) + 1
        for memory in items:
            line = format_memory_line(memory, include_ids=include_ids, now=now)
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

    def stats(self) -> dict[str, Any]:
        if self.store is None:
            return {"total": 0, "by_type": {}, "by_status": {}}
        return self.store.stats()
