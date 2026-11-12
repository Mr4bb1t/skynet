"""Ferramentas de memória expostas ao modelo (Function Calling).

A IA decide o que guardar, quando consultar e quando esquecer. As respostas são
curtas e sempre limitadas, para não inflar o contexto com a memória inteira.
"""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

from .store import VALID_KINDS

LOG = logging.getLogger("memory.tools")

_KIND_ENUM = list(VALID_KINDS)

REMEMBER_DECLARATION = types.FunctionDeclaration(
    name="remember_memory",
    description=(
        "Guarda uma memória durável na memória de longo prazo. Use quando o "
        "usuário contar algo importante ou durável: pessoas, animais, projetos, "
        "ideias, decisões, acontecimentos, preferências, lugares, objetos ou "
        "tarefas. Seja específico e conciso (uma frase). Não guarde trivialidades, "
        "saudações nem o JSON de rostos. Para atualizar algo existente, passe "
        "memory_id."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "content": types.Schema(
                type=types.Type.STRING,
                description="O fato em uma frase curta e completa.",
            ),
            "kind": types.Schema(
                type=types.Type.STRING,
                description="Categoria da memória.",
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
            "importance": types.Schema(
                type=types.Type.NUMBER,
                description="Importância de 0 a 1 (padrão 0.5).",
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
        "todas."
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
        result = self.manager.remember(
            content,
            kind=str(args.get("kind", "fact") or "fact"),
            subject=str(args.get("subject", "") or ""),
            tags=str(args.get("tags", "") or ""),
            importance=importance,
            memory_id=memory_id,
        )
        if result.get("status") == "ok":
            LOG.info(
                "Memória %s: #%s [%s] %s",
                "atualizada" if result.get("updated") else "salva",
                result.get("memory_id"),
                result.get("kind"),
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
        result = self.manager.recall(query, kinds=kinds, limit=limit)
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
