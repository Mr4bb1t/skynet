"""Consolidação automática da conversa em memórias temporais.

Um modelo econômico propõe memórias; uma camada determinística valida datas,
origem, confiança, duplicidade e consistência antes de gravar. Inferências nunca
entram como fatos confirmados. O controle de turnos evita reprocessar a mesma
conversa.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Optional

from .store import MEMORY_STATUS, MEMORY_TYPES, VERIFICATION_STATUS
from .timeutil import format_now, parse_when

LOG = logging.getLogger("memory.consolidate")

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MIN_CONTENT_CHARS = 8
_TRIVIAL = {
    "oi", "olá", "ola", "oi tudo bem", "bom dia", "boa tarde", "boa noite",
    "tudo bem", "ok", "certo", "sim", "não", "nao", "obrigado", "valeu",
}

PromptProposer = Callable[[str, list[dict[str, Any]], int], list[dict[str, Any]]]


def _conversation_text(conversation: Any) -> str:
    if isinstance(conversation, (list, tuple)):
        return "\n".join(str(item) for item in conversation if str(item).strip())
    return str(conversation or "")


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class MemoryConsolidator:
    """Extrai memórias da conversa e grava com validação determinística."""

    def __init__(
        self,
        manager,
        *,
        model: str = "",
        api_key: Optional[str] = None,
        proposer: Optional[PromptProposer] = None,
        max_items: int = 6,
    ) -> None:
        self.manager = manager
        self.model = model or getattr(
            getattr(manager, "config", None), "consolidation_model", DEFAULT_MODEL
        )
        self.api_key = api_key or getattr(manager, "api_key", None)
        self.proposer = proposer
        self.max_items = max(1, min(12, int(max_items)))
        self._client = None

    # ------------------------------------------------------------------
    # Entrada principal
    # ------------------------------------------------------------------
    def consolidate(
        self,
        conversation: Any,
        *,
        session_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        if not getattr(self.manager, "available", False) or self.manager.store is None:
            return {"status": "error", "error": "memória indisponível"}

        text = _conversation_text(conversation)
        if not text.strip():
            return {"status": "ok", "skipped": True, "reason": "conversa vazia"}

        store = self.manager.store
        key = f"consolidated:{session_id or 'default'}"
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if not force and store.get_meta(key) == digest:
            return {"status": "ok", "skipped": True, "reason": "já consolidado"}

        existing = self._existing(text)
        try:
            proposals = self._propose(text, existing)
        except Exception as exc:  # pragma: no cover - depende da rede
            LOG.warning("Consolidação falhou ao propor memórias: %s", exc)
            return {"status": "error", "error": str(exc)}

        created = updated = superseded = rejected = 0
        valid_ids = {int(item["memory_id"]) for item in existing if item.get("memory_id")}
        for proposal in proposals[: self.max_items]:
            normalized = self._validate(proposal, session_id, valid_ids)
            if normalized is None:
                rejected += 1
                continue
            try:
                result = self.manager.remember(**normalized)
            except Exception as exc:  # pragma: no cover - defensivo
                LOG.warning("Falha ao gravar memória consolidada: %s", exc)
                rejected += 1
                continue
            if result.get("status") != "ok":
                rejected += 1
                continue
            if normalized.get("supersede") and normalized.get("memory_id"):
                superseded += 1
            elif result.get("updated"):
                updated += 1
            else:
                created += 1

        store.set_meta(key, digest)
        summary = {
            "status": "ok",
            "skipped": False,
            "proposed": len(proposals),
            "created": created,
            "updated": updated,
            "superseded": superseded,
            "rejected": rejected,
        }
        LOG.info(
            "Consolidação: %d criadas, %d atualizadas, %d substituídas, %d rejeitadas",
            created,
            updated,
            superseded,
            rejected,
        )
        return summary

    # ------------------------------------------------------------------
    # Proposta (modelo)
    # ------------------------------------------------------------------
    def _existing(self, text: str) -> list[dict[str, Any]]:
        try:
            result = self.manager.recall(text[:300], limit=5)
            return result.get("memories", []) if result.get("status") == "ok" else []
        except Exception:
            return []

    def _propose(self, text: str, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.proposer is not None:
            return list(
                self.proposer(text, existing, self.max_items) or []
            )
        prompt = self._build_prompt(text, existing)
        client = self._get_client()
        from google.genai import types

        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1200,
                response_mime_type="application/json",
            ),
        )
        raw = getattr(response, "text", "") or ""
        data = json.loads(_strip_json_fence(raw))
        if isinstance(data, dict):
            proposals = data.get("memories", [])
        elif isinstance(data, list):
            proposals = data
        else:
            proposals = []
        return [item for item in proposals if isinstance(item, dict)]

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _build_prompt(self, text: str, existing: list[dict[str, Any]]) -> str:
        existing_json = json.dumps(
            [
                {
                    "memory_id": item.get("memory_id"),
                    "memory_type": item.get("memory_type"),
                    "subject": item.get("subject"),
                    "content": item.get("content"),
                }
                for item in existing
            ],
            ensure_ascii=False,
        )
        return f"""Você é o consolidador de memória do Skynet.
Data/hora atual: {format_now()}.
Analise a conversa e proponha no máximo {self.max_items} memórias duráveis.

Regras:
- Não invente datas. Se não souber quando aconteceu, use "".
- event = acontecimento passado (informe event_time).
- plan/task = intenção futura (informe event_time/due date).
- fact/preference/relationship/routine = conhecimento.
- verification_status: "confirmed" só se o usuário afirmou explicitamente;
  "inferred" se foi dedução; "uncertain" se houver dúvida.
- Não guarde trivialidades, saudações, opiniões vazias nem o JSON de rostos.
- Se uma memória substitui uma existente, use o memory_id em supersedes_memory_id.
- Para fatos que mudam, o novo registro deve ter valid_from.

Memórias existentes relevantes (JSON):
{existing_json}

Conversa:
{text[:7000]}

Responda SOMENTE JSON:
{{"memories": [{{"content": "...", "memory_type": "fact|event|plan|task|preference|relationship|routine|state", "subject": "...", "tags": "...", "event_time": "ISO ou ''", "event_end": "", "valid_from": "", "valid_until": "", "expires_at": "", "status": "active", "importance": 0.5, "confidence": 0.6, "verification_status": "inferred", "supersedes_memory_id": null}}]}}
"""

    # ------------------------------------------------------------------
    # Validação determinística
    # ------------------------------------------------------------------
    def _validate(
        self,
        proposal: dict[str, Any],
        session_id: str,
        valid_ids: set[int],
    ) -> Optional[dict[str, Any]]:
        content = " ".join(str(proposal.get("content", "")).split())
        if len(content) < MIN_CONTENT_CHARS or content.lower() in _TRIVIAL:
            return None

        memory_type = str(proposal.get("memory_type", "fact") or "fact").lower()
        if memory_type not in MEMORY_TYPES:
            memory_type = "fact"

        verification = str(
            proposal.get("verification_status", "inferred") or "inferred"
        ).lower()
        if verification not in VERIFICATION_STATUS:
            verification = "inferred"
        if verification == "confirmed":
            # O consolidador é inferência por natureza. Só aceitamos confirmed
            # se houver uma citação textual curta marcada como confirmation.
            quote = str(proposal.get("quote", "") or "").strip()
            if len(quote) < 8:
                verification = "inferred"

        try:
            confidence = max(0.0, min(1.0, float(proposal.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        if verification == "inferred":
            confidence = min(confidence, 0.7)

        status = str(proposal.get("status", "active") or "active").lower()
        if status not in MEMORY_STATUS:
            status = "active"

        event_time = parse_when(str(proposal.get("event_time", "") or ""))
        event_end = parse_when(str(proposal.get("event_end", "") or ""))
        valid_from = parse_when(str(proposal.get("valid_from", "") or ""))
        valid_until = parse_when(str(proposal.get("valid_until", "") or ""))
        expires_at = parse_when(str(proposal.get("expires_at", "") or ""))

        supersedes_id = proposal.get("supersedes_memory_id")
        try:
            supersedes_id = int(supersedes_id) if supersedes_id else None
        except (TypeError, ValueError):
            supersedes_id = None
        if supersedes_id is not None:
            target = (
                self.manager.store.get(supersedes_id)
                if getattr(self.manager, "store", None) is not None
                else None
            )
            if not target or str(target.get("status", "active")) in (
                "superseded",
                "expired",
            ):
                supersedes_id = None

        importance = proposal.get("importance", 0.5)
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5

        return {
            "content": content,
            "kind": str(proposal.get("kind", "fact") or "fact"),
            "subject": str(proposal.get("subject", "") or ""),
            "tags": str(proposal.get("tags", "") or ""),
            "importance": importance,
            "memory_type": memory_type,
            "event_time": event_time,
            "event_end": event_end,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "expires_at": expires_at,
            "status": status,
            "confidence": confidence,
            "verification_status": verification,
            "session_id": session_id,
            "source": "inferred" if verification == "inferred" else "system",
            "supersede": bool(supersedes_id),
            "memory_id": supersedes_id,
            "metadata": {
                "consolidated": True,
                "session_id": session_id,
                "quote": str(proposal.get("quote", "") or "")[:240],
            },
        }
