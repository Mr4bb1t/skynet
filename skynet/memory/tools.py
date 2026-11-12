"""Ferramentas de memória expostas ao modelo (Function Calling).

A IA decide o que guardar, quando consultar e quando esquecer. As respostas são
curtas e sempre limitadas, para não inflar o contexto com a memória inteira.
"""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

from .store import MEMORY_STATUS, MEMORY_TYPES, VALID_KINDS, VERIFICATION_STATUS

LOG = logging.getLogger("memory.tools")

_KIND_ENUM = list(VALID_KINDS)

REMEMBER_DECLARATION = types.FunctionDeclaration(
    name="remember_memory",
    description=(
        "Guarda uma memória durável na memória de longo prazo. Use quando o "
        "usuário contar algo importante ou durável: pessoas, animais, projetos, "
        "ideias, decisões, acontecimentos, preferências, lugares, objetos, "
        "tarefas e planos. Seja específico e conciso (uma frase). "
        "SEMPRE classifique o tipo temporal: event (aconteceu), plan/task "
        "(futuro), fact/preference/relationship/routine (conhecimento). "
        "Para eventos, informe `when` (ex.: 'ontem', '2026-11-10') — NUNCA "
        "invente uma data. Não guarde trivialidades, saudações nem o JSON de "
        "rostos. Para atualizar algo que mudou, passe memory_id e supersede=true."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "content": types.Schema(
                type=types.Type.STRING,
                description="O fato/acontecimento em uma frase curta e completa.",
            ),
            "memory_type": types.Schema(
                type=types.Type.STRING,
                description="Categoria temporal da memória.",
                enum=list(MEMORY_TYPES),
            ),
            "kind": types.Schema(
                type=types.Type.STRING,
                description="Categoria legada (pessoa, projeto, evento...).",
                enum=_KIND_ENUM,
            ),
            "subject": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Entidade principal (nome da pessoa, animal, projeto etc.). "
                    "Separe por vírgula se houver mais de uma."
                ),
            ),
            "tags": types.Schema(
                type=types.Type.STRING,
                description="Palavras-chave separadas por vírgula.",
            ),
            "when": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Quando o acontecimento ocorreu/começou. ISO 8601 ou "
                    "relativo ('hoje', 'ontem', 'há 3 dias', 'amanhã'). "
                    "Para fatos atemporais, deixe vazio."
                ),
            ),
            "event_end": types.Schema(
                type=types.Type.STRING,
                description="Fim do acontecimento, se durou um período.",
            ),
            "valid_from": types.Schema(
                type=types.Type.STRING,
                description="Início da validade de um fato/estado/preferência.",
            ),
            "valid_until": types.Schema(
                type=types.Type.STRING,
                description="Fim da validade (quando deixou de ser verdade).",
            ),
            "expires_at": types.Schema(
                type=types.Type.STRING,
                description="Expiração de um estado temporário.",
            ),
            "importance": types.Schema(
                type=types.Type.NUMBER,
                description="Importância de 0 a 1 (padrão 0.5).",
            ),
            "confidence": types.Schema(
                type=types.Type.NUMBER,
                description="Confiança de 0 a 1 (padrão 0.8).",
            ),
            "verification_status": types.Schema(
                type=types.Type.STRING,
                description=(
                    "confirmed = o usuário afirmou; inferred = você deduziu; "
                    "uncertain = dúvida. Inferências NÃO são fatos confirmados."
                ),
                enum=list(VERIFICATION_STATUS),
            ),
            "status": types.Schema(
                type=types.Type.STRING,
                description="Estado de planos/tarefas (active, done, cancelled...).",
                enum=["active", "done", "cancelled", "uncertain"],
            ),
            "supersede": types.Schema(
                type=types.Type.BOOLEAN,
                description=(
                    "true quando a informação mudou: preserva o registro antigo "
                    "como histórico e cria um novo. Use com memory_id."
                ),
            ),
            "memory_id": types.Schema(
                type=types.Type.INTEGER,
                description="ID de uma memória existente, se for atualização.",
            ),
        },
        required=["content"],
    ),
)

RECALL_DECLARATION = types.FunctionDeclaration(
    name="recall_memory",
    description=(
        "Busca memórias relevantes por consulta. Use antes de responder sobre "
        "algo do passado, ao ouvir o nome de alguém/algo já conhecido, ou quando "
        "precisar de contexto. Retorna só as memórias mais relevantes, nunca "
        "todas, e sempre com data/validade. Memórias antigas NÃO são o agora."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "query": types.Schema(
                type=types.Type.STRING,
                description="O que você quer lembrar (nome, assunto, projeto...).",
            ),
            "kind": types.Schema(
                type=types.Type.STRING,
                description="Filtra por categoria (opcional).",
                enum=_KIND_ENUM,
            ),
            "time_range": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Janela temporal: any, now, today, yesterday, week, "
                    "last_week, month, last_month, past, future."
                ),
                enum=[
                    "any", "now", "today", "yesterday", "week", "last_week",
                    "month", "last_month", "past", "future",
                ],
            ),
            "include_history": types.Schema(
                type=types.Type.BOOLEAN,
                description=(
                    "Inclui registros substituídos/expirados (para histórico). "
                    "Padrão false."
                ),
            ),
            "limit": types.Schema(
                type=types.Type.INTEGER,
                description="Máximo de memórias (1 a 8; padrão 5).",
            ),
        },
        required=["query"],
    ),
)

FORGET_DECLARATION = types.FunctionDeclaration(
    name="forget_memory",
    description=(
        "Remove memórias da memória de longo prazo. Use somente quando o "
        "usuário pedir explicitamente para esquecer/apagar algo. Prefira "
        "memory_id; use query apenas quando não souber o ID."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "memory_id": types.Schema(
                type=types.Type.INTEGER,
                description="ID exato da memória a remover.",
            ),
            "query": types.Schema(
                type=types.Type.STRING,
                description="Termo para localizar a memória, se não houver ID.",
            ),
        },
    ),
)

MEMORY_DECLARATIONS = [
    REMEMBER_DECLARATION,
    RECALL_DECLARATION,
    FORGET_DECLARATION,
]


class MemoryTool:
    """Adapta o MemoryManager ao formato de function calling."""

    def __init__(self, manager) -> None:
        self.manager = manager

    @property
    def available(self) -> bool:
        return bool(self.manager is not None and getattr(self.manager, "available", False))

    @property
    def reason(self) -> str:
        return str(getattr(self.manager, "reason", "") or "")

    def handle(self, name: str, args: Any) -> dict:
        if isinstance(args, dict):
            data = args
        else:
            try:
                data = dict(args or {})
            except Exception:
                data = {}

        if name == "remember_memory":
            return self.remember(data)
        if name == "recall_memory":
            return self.recall(data)
        if name == "forget_memory":
            return self.forget(data)
        return {"status": "error", "error": f"ferramenta de memória desconhecida: {name}"}

    def remember(self, args: dict) -> dict:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        content = str(args.get("content", "") or "").strip()
        if not content:
            return {"status": "error", "error": "content vazio"}
        memory_id = args.get("memory_id")
        try:
            memory_id = int(memory_id) if memory_id else None
        except (TypeError, ValueError):
            memory_id = None
        try:
            importance = float(args.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        try:
            confidence = float(args.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        supersede = args.get("supersede", False)
        if isinstance(supersede, str):
            supersede = supersede.strip().lower() in ("1", "true", "yes", "on")
        else:
            supersede = bool(supersede)
        result = self.manager.remember(
            content,
            kind=str(args.get("kind", "fact") or "fact"),
            subject=str(args.get("subject", "") or ""),
            tags=str(args.get("tags", "") or ""),
            importance=importance,
            memory_id=memory_id,
            memory_type=str(args.get("memory_type", "") or ""),
            event_time=str(args.get("when", "") or args.get("event_time", "") or ""),
            event_end=str(args.get("event_end", "") or ""),
            valid_from=str(args.get("valid_from", "") or ""),
            valid_until=str(args.get("valid_until", "") or ""),
            expires_at=str(args.get("expires_at", "") or ""),
            status=str(args.get("status", "active") or "active"),
            confidence=confidence,
            verification_status=str(args.get("verification_status", "") or ""),
            supersede=supersede,
        )
        if result.get("status") == "ok":
            LOG.info(
                "Memória %s: #%s [%s/%s] %s",
                "substituída" if supersede else ("atualizada" if result.get("updated") else "salva"),
                result.get("memory_id"),
                result.get("memory_type") or result.get("kind"),
                result.get("memory_status", "active"),
                result.get("subject", ""),
            )
        return result

    def recall(self, args: dict) -> dict:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        query = str(args.get("query", "") or "").strip()
        if not query:
            return {"status": "error", "error": "query vazia"}
        kind = str(args.get("kind", "") or "").strip()
        kinds = [part.strip() for part in kind.split(",") if part.strip()] or None
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(8, limit))
        time_range = str(args.get("time_range", "") or "").strip().lower() or None
        include_history = args.get("include_history", False)
        if isinstance(include_history, str):
            include_history = include_history.strip().lower() in ("1", "true", "yes", "on")
        else:
            include_history = bool(include_history)
        result = self.manager.recall(
            query,
            kinds=kinds,
            temporal_intent=time_range,
            include_history=include_history,
            limit=limit,
        )
        if result.get("status") != "ok":
            return result
        memories = result.get("memories", [])
        # Devolve o texto compacto com IDs; evita duplicar o JSON inteiro.
        return {
            "status": "ok",
            "count": len(memories),
            "text": self.manager.format_memories(memories, include_ids=True),
        }

    def forget(self, args: dict) -> dict:
        if not self.available:
            return {"status": "error", "error": self.reason or "memória indisponível"}
        memory_id = args.get("memory_id")
        try:
            memory_id = int(memory_id) if memory_id else None
        except (TypeError, ValueError):
            memory_id = None
        query = str(args.get("query", "") or "").strip()
        if not memory_id and not query:
            return {
                "status": "error",
                "error": "informe memory_id ou query para esquecer",
            }
        result = self.manager.forget(memory_id=memory_id, query=query)
        if result.get("deleted"):
            LOG.info("Memórias removidas: %d", result["deleted"])
        memories = result.get("memories", [])
        return {
            "status": result.get("status", "ok"),
            "deleted": result.get("deleted", 0),
            "memory_ids": [item.get("memory_id") for item in memories],
            "subjects": [item.get("subject", "") for item in memories],
        }
