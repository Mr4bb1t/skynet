"""Banco SQLite da memória de longo prazo.

Guarda memórias de qualquer tipo (pessoa, animal, evento, projeto, ideia,
preferência, fato, lugar, objeto, tarefa) com:

  * FTS5 para busca lexical;
  * embeddings opcionais para busca semântica;
  * entidades normalizadas para filtrar/impulsionar pelo rosto reconhecido;
  * importância, recência e contagem de uso para ranking.

A API pública é intencionalmente pequena: remember, recall, forget e get.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .embeddings import BaseEmbedder
from .text import (
    compact_whitespace,
    normalize_entity,
    overlap_score,
    split_subjects,
    token_set,
    truncate,
)
from .timeutil import (
    detect_temporal_intent,
    format_when,
    now_local,
    parse_iso,
    parse_when,
    temporal_range,
    to_iso,
)

try:  # numpy já é dependência do projeto; opcional para vetores mais rápidos.
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover - ambiente mínimo
    _np = None  # type: ignore

LOG = logging.getLogger("memory.store")

VALID_KINDS = (
    "person",
    "animal",
    "event",
    "project",
    "idea",
    "preference",
    "fact",
    "place",
    "object",
    "task",
    "other",
)

# Categorias temporais do novo modelo (separação lógica, mesmo SQLite).
MEMORY_TYPES = (
    "fact",
    "event",
    "plan",
    "task",
    "preference",
    "relationship",
    "routine",
    "state",
)
MEMORY_STATUS = (
    "active",
    "done",
    "cancelled",
    "superseded",
    "expired",
    "uncertain",
)
TEMPORAL_SCOPES = ("instant", "interval", "timeless", "unknown")
VERIFICATION_STATUS = ("confirmed", "inferred", "uncertain")

_KIND_TO_TYPE = {
    "person": "relationship",
    "animal": "relationship",
    "event": "event",
    "project": "fact",
    "idea": "fact",
    "preference": "preference",
    "fact": "fact",
    "place": "fact",
    "object": "fact",
    "task": "task",
    "other": "fact",
}

_MEMORY_TYPE_ALIASES = {
    "fato": "fact",
    "evento": "event",
    "acontecimento": "event",
    "plano": "plan",
    "tarefa": "task",
    "preferencia": "preference",
    "relacao": "relationship",
    "relacionamento": "relationship",
    "rotina": "routine",
    "estado": "state",
}

_SOURCE_ALIASES = {
    "chat": "user",
    "text": "user",
    "user": "user",
    "voice": "audio",
    "audio": "audio",
    "vision": "vision",
    "sensor": "vision",
    "system": "system",
    "test": "system",
    "inferred": "inferred",
    "inferido": "inferred",
}

_KIND_ALIASES = {
    "pessoa": "person",
    "people": "person",
    "animal": "animal",
    "animal_estimacao": "animal",
    "pet": "animal",
    "evento": "event",
    "acontecimento": "event",
    "projeto": "project",
    "ideia": "idea",
    "preferencia": "preference",
    "gosto": "preference",
    "fato": "fact",
    "lugar": "place",
    "local": "place",
    "objeto": "object",
    "coisa": "object",
    "tarefa": "task",
    "outro": "other",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_date(value: str, field: str) -> str:
    """Normaliza uma data informada. Inválida vira '' (não inventa data)."""
    if not value:
        return ""
    normalized = parse_when(str(value))
    if not normalized:
        LOG.warning("Data inválida em %s: %r; mantendo desconhecida.", field, value)
    return normalized


def normalize_kind(value: str) -> str:
    cleaned = normalize_entity(value).replace(" ", "_")
    if cleaned in VALID_KINDS:
        return cleaned
    return _KIND_ALIASES.get(cleaned, "other")


def normalize_memory_type(value: str, *, kind: str = "fact") -> str:
    cleaned = normalize_entity(value or "").replace(" ", "_")
    if cleaned in MEMORY_TYPES:
        return cleaned
    cleaned = _MEMORY_TYPE_ALIASES.get(cleaned, cleaned)
    if cleaned in MEMORY_TYPES:
        return cleaned
    return _KIND_TO_TYPE.get(normalize_kind(kind), "fact")


def normalize_status(value: str, *, default: str = "active") -> str:
    cleaned = normalize_entity(value or "").replace(" ", "_")
    return cleaned if cleaned in MEMORY_STATUS else default


def normalize_source(value: str) -> str:
    cleaned = (value or "").strip().lower()
    return _SOURCE_ALIASES.get(cleaned, cleaned or "system")


def normalize_verification(value: str, *, source: str = "user") -> str:
    cleaned = normalize_entity(value or "").replace(" ", "_")
    if cleaned in VERIFICATION_STATUS:
        return cleaned
    return "inferred" if source == "inferred" else "confirmed"


def _pack_vector(vector: Iterable[float]) -> bytes:
    return array("f", (float(value) for value in vector)).tobytes()


def _unpack_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(left * right for left, right in zip(a, b)))


def _fingerprint(kind: str, subject: str, content: str, suffix: str = "") -> str:
    raw = f"{kind}|{normalize_entity(subject)}|{normalize_entity(content)}"
    if suffix:
        raw += f"|{suffix}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class MemoryStore:
    """Persistência e recuperação híbrida (FTS + vetores) em SQLite."""

    def __init__(
        self,
        db_path: str,
        embedder: BaseEmbedder,
        *,
        max_content_chars: int = 700,
        half_life_days: float = 45.0,
        candidates: int = 30,
        min_score: float = 0.18,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.embedder = embedder
        self.max_content_chars = int(max_content_chars)
        self.half_life_days = max(1.0, float(half_life_days))
        self.candidates = max(5, int(candidates))
        self.min_score = float(min_score)
        self._lock = threading.RLock()
        self.fts_available = False
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Conexão e schema
    # ------------------------------------------------------------------
    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'fact',
                    subject TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    importance REAL NOT NULL DEFAULT 0.5,
                    source TEXT NOT NULL DEFAULT 'chat',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    event_time TEXT NOT NULL DEFAULT '',
                    event_end TEXT NOT NULL DEFAULT '',
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_until TEXT NOT NULL DEFAULT '',
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    temporal_scope TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'active',
                    confidence REAL NOT NULL DEFAULT 0.8,
                    verification_status TEXT NOT NULL DEFAULT 'confirmed',
                    session_id TEXT NOT NULL DEFAULT '',
                    supersedes_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL DEFAULT '',
                    use_count INTEGER NOT NULL DEFAULT 0,
                    embedding BLOB,
                    embedding_model TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_fingerprint
                    ON memories(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_memories_kind
                    ON memories(kind);
                CREATE INDEX IF NOT EXISTS idx_memories_updated
                    ON memories(updated_at);
                CREATE INDEX IF NOT EXISTS idx_memories_embedding_model
                    ON memories(embedding_model);

                CREATE TABLE IF NOT EXISTS memory_entities (
                    memory_id INTEGER NOT NULL,
                    entity TEXT NOT NULL,
                    display TEXT NOT NULL DEFAULT '',
                    entity_type TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(memory_id, entity),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_memory_entities_entity
                    ON memory_entities(entity);

                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._migrate_schema(conn)
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content, subject, tags,
                        content='memories',
                        content_rowid='id',
                        tokenize='unicode61 remove_diacritics 2'
                    );
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content, subject, tags)
                        VALUES (new.id, new.content, new.subject, new.tags);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, subject, tags)
                        VALUES ('delete', old.id, old.content, old.subject, old.tags);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, subject, tags ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, subject, tags)
                        VALUES ('delete', old.id, old.content, old.subject, old.tags);
                        INSERT INTO memories_fts(rowid, content, subject, tags)
                        VALUES (new.id, new.content, new.subject, new.tags);
                    END;
                    """
                )
                self.fts_available = True
            except sqlite3.OperationalError as exc:
                # SQLite sem FTS5: a busca lexical cai para LIKE/tokens.
                self.fts_available = False
                LOG.warning("FTS5 indisponível; usando busca lexical simples: %s", exc)

            # Preenche o índice FTS uma única vez (bancos criados antes dele).
            if self.fts_available:
                rebuilt = conn.execute(
                    "SELECT value FROM memory_meta WHERE key='fts_rebuilt'"
                ).fetchone()
                if rebuilt is None:
                    try:
                        conn.execute(
                            "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')"
                        )
                    except sqlite3.OperationalError:
                        pass
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
                        ("fts_rebuilt", "1"),
                    )

    def _migrate_schema(self, conn) -> None:
        """Migra o schema v1 para o modelo temporal v2 sem perder dados."""
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        additions = {
            "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
            "event_time": "TEXT NOT NULL DEFAULT ''",
            "event_end": "TEXT NOT NULL DEFAULT ''",
            "valid_from": "TEXT NOT NULL DEFAULT ''",
            "valid_until": "TEXT NOT NULL DEFAULT ''",
            "last_confirmed_at": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT NOT NULL DEFAULT ''",
            "temporal_scope": "TEXT NOT NULL DEFAULT 'unknown'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "confidence": "REAL NOT NULL DEFAULT 0.8",
            "verification_status": "TEXT NOT NULL DEFAULT 'confirmed'",
            "session_id": "TEXT NOT NULL DEFAULT ''",
            "supersedes_id": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")

        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_memories_memory_type ON memories(memory_type)",
            "CREATE INDEX IF NOT EXISTS idx_memories_event_time ON memories(event_time)",
            "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)",
            "CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)",
        ):
            conn.execute(index_sql)

        row = conn.execute(
            "SELECT value FROM memory_meta WHERE key='schema_version'"
        ).fetchone()
        try:
            version = int(row["value"]) if row else 0
        except (TypeError, ValueError):
            version = 0
        if version >= 2:
            return

        total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        if total:
            legacy = conn.execute(
                "SELECT id, kind, source, updated_at FROM memories"
            ).fetchall()
            for item in legacy:
                memory_type = _KIND_TO_TYPE.get(normalize_kind(item["kind"]), "fact")
                source = normalize_source(item["source"])
                verification = "inferred" if source == "inferred" else "confirmed"
                # NÃO usar created_at como event_time: a data do acontecimento
                # é desconhecida nos registros antigos.
                conn.execute(
                    """
                    UPDATE memories
                       SET memory_type=?,
                           source=?,
                           temporal_scope='unknown',
                           last_confirmed_at=CASE
                               WHEN last_confirmed_at='' THEN updated_at
                               ELSE last_confirmed_at END,
                           verification_status=CASE
                               WHEN verification_status='' THEN ?
                               ELSE verification_status END
                     WHERE id=?
                    """,
                    (memory_type, source, verification, int(item["id"])),
                )
        conn.execute(
            "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
            ("schema_version", "2"),
        )

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------
    def _embed(self, text: str, *, query: bool = False):
        try:
            batch = self.embedder.embed([text], query=query)
            if not batch.vectors:
                return None, ""
            return batch.vectors[0], batch.provider
        except Exception as exc:
            LOG.warning("Não foi possível gerar embedding: %s", exc)
            return None, ""

    def remember(
        self,
        *,
        content: str,
        kind: str = "fact",
        subject: str = "",
        tags: str = "",
        importance: float = 0.5,
        source: str = "chat",
        metadata: Optional[dict[str, Any]] = None,
        memory_id: Optional[int] = None,
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
        content = truncate(compact_whitespace(content), self.max_content_chars)
        if not content:
            return {"status": "error", "error": "conteúdo vazio"}
        kind = normalize_kind(kind)
        memory_type = normalize_memory_type(memory_type, kind=kind)
        source = normalize_source(source)
        subject = truncate(compact_whitespace(subject), 160)
        tags = truncate(compact_whitespace(tags), 200)
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.8

        now = _now_iso()
        current = datetime.now(timezone.utc)
        event_time = _normalize_date(event_time, "event_time")
        event_end = _normalize_date(event_end, "event_end")
        valid_from = _normalize_date(valid_from, "valid_from")
        valid_until = _normalize_date(valid_until, "valid_until")
        last_confirmed_at = _normalize_date(last_confirmed_at, "last_confirmed_at") or now
        expires_at = _normalize_date(expires_at, "expires_at")

        scope = (temporal_scope or "").strip().lower()
        if scope not in TEMPORAL_SCOPES:
            if memory_type == "event":
                scope = "interval" if event_end else ("instant" if event_time else "unknown")
            elif memory_type in ("plan", "task"):
                scope = "instant" if event_time else "unknown"
            elif memory_type == "state":
                scope = "instant"
            else:
                scope = "unknown"

        state = normalize_status(status)
        verification = normalize_verification(verification_status, source=source)
        if source == "inferred":
            verification = "inferred"
            confidence = min(confidence, 0.7)

        expires_dt = _parse_iso(expires_at) if expires_at else None
        if expires_dt is not None and expires_dt <= current:
            state = "expired"
        valid_until_dt = _parse_iso(valid_until) if valid_until else None
        if valid_until_dt is not None and valid_until_dt <= current and state == "active":
            state = "expired"

        # Eventos idênticos em datas diferentes são registros diferentes.
        fp_suffix = event_time[:10] if memory_type == "event" and event_time else ""
        fingerprint = _fingerprint(kind, subject, content, fp_suffix)
        embedding, provider = self._embed(
            f"{subject}. {content} {tags}".strip(), query=False
        )
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._lock, self._db() as conn:
            target_id: Optional[int] = None
            updated = False
            supersede_old_id: Optional[int] = None

            if memory_id:
                row = conn.execute(
                    "SELECT id FROM memories WHERE id=?", (int(memory_id),)
                ).fetchone()
                if row is not None:
                    if supersede:
                        supersede_old_id = int(row["id"])
                    else:
                        target_id = int(row["id"])
                        updated = True

            if target_id is None:
                row = conn.execute(
                    "SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if row is not None:
                    target_id = int(row["id"])
                    updated = True

            # Conteúdo idêntico ao registro antigo: confirma/atualiza no lugar.
            if supersede_old_id is not None and target_id == supersede_old_id:
                supersede_old_id = None
                updated = True

            if supersede_old_id is not None and not valid_from:
                valid_from = now

            update_sql = """
                UPDATE memories
                   SET fingerprint=?, kind=?, memory_type=?, subject=?,
                       content=?, tags=?, importance=?, source=?, metadata=?,
                       event_time=?, event_end=?, valid_from=?, valid_until=?,
                       last_confirmed_at=?, expires_at=?, temporal_scope=?,
                       status=?, confidence=?, verification_status=?,
                       session_id=?, updated_at=?, embedding=?, embedding_model=?
                 WHERE id=?
            """
            update_params = (
                fingerprint,
                kind,
                memory_type,
                subject,
                content,
                tags,
                importance,
                source,
                metadata_json,
                event_time,
                event_end,
                valid_from,
                valid_until,
                last_confirmed_at,
                expires_at,
                scope,
                state,
                confidence,
                verification,
                session_id,
                now,
                _pack_vector(embedding) if embedding else None,
                provider,
            )

            if target_id is not None:
                try:
                    conn.execute(update_sql, (*update_params, target_id))
                except sqlite3.IntegrityError:
                    # O fingerprint já pertence a outra linha. Atualiza a linha
                    # existente em vez de criar duplicata; o histórico anterior
                    # permanece (não apagamos nada).
                    row = conn.execute(
                        "SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)
                    ).fetchone()
                    if row is None:
                        raise
                    duplicate_id = int(row["id"])
                    conn.execute(update_sql, (*update_params, duplicate_id))
                    target_id = duplicate_id
                    updated = True
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO memories
                        (fingerprint, kind, memory_type, subject, content, tags,
                         importance, source, metadata, event_time, event_end,
                         valid_from, valid_until, last_confirmed_at, expires_at,
                         temporal_scope, status, confidence, verification_status,
                         session_id, supersedes_id, created_at, updated_at,
                         embedding, embedding_model)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        kind,
                        memory_type,
                        subject,
                        content,
                        tags,
                        importance,
                        source,
                        metadata_json,
                        event_time,
                        event_end,
                        valid_from,
                        valid_until,
                        last_confirmed_at,
                        expires_at,
                        scope,
                        state,
                        confidence,
                        verification,
                        session_id,
                        0,
                        now,
                        now,
                        _pack_vector(embedding) if embedding else None,
                        provider,
                    ),
                )
                target_id = int(cursor.lastrowid)

            if supersede_old_id is not None and target_id != supersede_old_id:
                old = conn.execute(
                    "SELECT metadata FROM memories WHERE id=?", (supersede_old_id,)
                ).fetchone()
                old_meta: dict[str, Any] = {}
                if old is not None:
                    try:
                        old_meta = json.loads(old["metadata"] or "{}")
                    except (TypeError, ValueError):
                        old_meta = {}
                old_meta["superseded_by"] = target_id
                conn.execute(
                    """
                    UPDATE memories
                       SET status='superseded', valid_until=?, updated_at=?,
                           metadata=?
                     WHERE id=?
                    """,
                    (now, now, json.dumps(old_meta, ensure_ascii=False), supersede_old_id),
                )
                conn.execute(
                    "UPDATE memories SET supersedes_id=? WHERE id=?",
                    (supersede_old_id, target_id),
                )

            self._replace_entities(conn, target_id, subject, kind)
            row = conn.execute(
                "SELECT * FROM memories WHERE id=?", (target_id,)
            ).fetchone()

        return {
            "status": "ok",
            "memory_id": target_id,
            "updated": updated,
            "memory": self._row_to_dict(row),
        }

    @staticmethod
    def _replace_entities(conn, memory_id: int, subject: str, kind: str) -> None:
        conn.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
        seen: set[str] = set()
        for display in split_subjects(subject):
            key = normalize_entity(display)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_entities
                    (memory_id, entity, display, entity_type)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, key, display, kind),
            )

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        if row is None:
            return {}
        data = {
            "memory_id": int(row["id"]),
            "kind": row["kind"],
            "memory_type": row["memory_type"] or "fact",
            "subject": row["subject"] or "",
            "content": row["content"],
            "tags": row["tags"] or "",
            "importance": round(float(row["importance"]), 2),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"] or "",
            "use_count": int(row["use_count"]),
            "event_time": row["event_time"] or "",
            "event_end": row["event_end"] or "",
            "valid_from": row["valid_from"] or "",
            "valid_until": row["valid_until"] or "",
            "last_confirmed_at": row["last_confirmed_at"] or "",
            "expires_at": row["expires_at"] or "",
            "temporal_scope": row["temporal_scope"] or "unknown",
            "status": row["status"] or "active",
            "confidence": round(float(row["confidence"]), 2),
            "verification_status": row["verification_status"] or "confirmed",
            "session_id": row["session_id"] or "",
            "supersedes_id": int(row["supersedes_id"] or 0),
        }
        try:
            data["metadata"] = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            data["metadata"] = {}
        return data

    def get(self, memory_id: int) -> Optional[dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id=?", (int(memory_id),)
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _fts_query(self, query: str) -> str:
        """Monta uma query FTS segura (OR entre tokens entre aspas)."""
        cleaned = []
        for token in token_set(query):
            token = token.replace('"', "")
            if len(token) >= 2:
                cleaned.append(f'"{token}"')
            if len(cleaned) >= 12:
                break
        return " OR ".join(cleaned)

    def _candidate_rows(
        self,
        conn,
        query: str,
        *,
        kinds: list[str],
        entities: list[str],
    ) -> tuple[dict[int, sqlite3.Row], dict[int, float], set[int]]:
        rows: dict[int, sqlite3.Row] = {}
        fts_ranks: dict[int, float] = {}
        entity_ids: set[int] = set()
        limit = self.candidates

        def add(row) -> None:
            try:
                rows[int(row["id"])] = row
            except (KeyError, TypeError):
                return

        if query:
            fts_query = self._fts_query(query)
            if fts_query and self.fts_available:
                try:
                    found = conn.execute(
                        """
                        SELECT m.*, bm25(memories_fts) AS rank
                          FROM memories_fts
                          JOIN memories m ON m.id = memories_fts.rowid
                         WHERE memories_fts MATCH ?
                         ORDER BY rank
                         LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                    for row in found:
                        add(row)
                        fts_ranks[int(row["id"])] = float(row["rank"])
                except sqlite3.OperationalError as exc:
                    LOG.debug("FTS falhou, usando LIKE: %s", exc)
            if not fts_ranks:
                like = f"%{query.strip()}%"
                for row in conn.execute(
                    """
                    SELECT * FROM memories
                     WHERE content LIKE ? OR subject LIKE ? OR tags LIKE ?
                     ORDER BY updated_at DESC LIMIT ?
                    """,
                    (like, like, like, limit),
                ).fetchall():
                    add(row)

        entity_keys = []
        for entity in entities:
            key = normalize_entity(entity)
            if len(key) >= 2 and key not in entity_keys:
                entity_keys.append(key)
        if entity_keys:
            placeholders = ",".join("?" for _ in entity_keys)
            try:
                found = conn.execute(
                    f"""
                    SELECT m.*
                      FROM memories m
                      JOIN memory_entities e ON e.memory_id = m.id
                     WHERE e.entity IN ({placeholders})
                     ORDER BY m.updated_at DESC
                     LIMIT ?
                    """,
                    (*entity_keys, limit),
                ).fetchall()
                for row in found:
                    add(row)
                    entity_ids.add(int(row["id"]))
            except sqlite3.OperationalError as exc:
                LOG.debug("Busca por entidade falhou: %s", exc)

        if kinds:
            allowed = {normalize_kind(kind) for kind in kinds}
            rows = {
                memory_id: row
                for memory_id, row in rows.items()
                if row["kind"] in allowed
            }
            entity_ids &= set(rows)
        return rows, fts_ranks, entity_ids

    def _vector_scores(self, query: str) -> dict[int, float]:
        if not query:
            return {}
        vector, provider = self._embed(query, query=True)
        if not vector or not provider:
            return {}
        scores: dict[int, float] = {}
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, embedding FROM memories
                 WHERE embedding IS NOT NULL AND embedding_model=?
                """,
                (provider,),
            ).fetchall()

        if _np is not None and rows:
            ids = []
            vectors = []
            for row in rows:
                try:
                    item = _np.frombuffer(row["embedding"], dtype=_np.float32)
                except Exception:
                    continue
                if item.size != len(vector):
                    continue
                ids.append(int(row["id"]))
                vectors.append(item)
            if vectors:
                matrix = _np.vstack(vectors)
                query = _np.asarray(vector, dtype=_np.float32)
                similarities = matrix @ query
                for memory_id, score in zip(ids, similarities):
                    if score > 0.0:
                        scores[memory_id] = float(score)
                return scores

        for row in rows:
            try:
                stored = _unpack_vector(row["embedding"])
            except Exception:
                continue
            score = _cosine(vector, stored)
            if score > 0.0:
                scores[int(row["id"])] = score
        return scores

    @staticmethod
    def _row_is_current(row, now: datetime, include_history: bool = False) -> bool:
        status = (row["status"] or "active").lower()
        if not include_history and status in ("superseded", "expired"):
            return False
        expires = _parse_iso(row["expires_at"] or "")
        if not include_history and expires is not None and expires <= now:
            return False
        return True

    @staticmethod
    def _row_matches_temporal(row, intent: str, now: datetime) -> bool:
        """Aplica a intenção temporal sem transformar passado em presente."""
        if not intent or intent == "any":
            return True
        memory_type = (row["memory_type"] or "fact").lower()
        event = _parse_iso(row["event_time"] or "")
        event_end = _parse_iso(row["event_end"] or "")

        if intent == "now":
            if memory_type == "event":
                return bool(event_end and event_end >= now) or bool(
                    event and event_end and event <= now <= event_end
                )
            return memory_type in (
                "state",
                "fact",
                "preference",
                "relationship",
                "routine",
            )

        if intent in ("today", "yesterday", "week", "last_week", "month", "last_month"):
            if memory_type != "event":
                return True
            bounds = temporal_range(intent, now=now)
            if not bounds:
                return True
            start_s, end_s = bounds
            start = _parse_iso(start_s) or now
            end = _parse_iso(end_s) if end_s else now
            if event is None:
                return False
            effective_end = event_end or event
            return event < end and effective_end >= start

        if intent == "future":
            if memory_type not in ("plan", "task"):
                return False
            if event is None:
                return True
            return event >= now

        if intent == "past":
            if memory_type in ("plan", "task"):
                return False
            if memory_type == "event":
                return event is None or event <= now
            return True

        return True

    def recall(
        self,
        query: str = "",
        *,
        kinds: Optional[list[str]] = None,
        entities: Optional[list[str]] = None,
        memory_types: Optional[list[str]] = None,
        temporal_intent: Optional[str] = None,
        include_history: bool = False,
        limit: int = 5,
        min_score: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Retorna somente as memórias mais relevantes para a consulta.

        Sem consulta e sem entidades, não devolve nada: a memória nunca é
        despejada inteira para o modelo. Por padrão, memórias substituídas e
        expiradas não são retornadas como se fossem atuais.
        """
        query = compact_whitespace(query)
        if not query and not entities:
            return {"status": "ok", "query": "", "count": 0, "memories": []}

        limit = max(1, min(12, int(limit)))
        threshold = self.min_score if min_score is None else float(min_score)
        current = now or now_local()
        intent = (temporal_intent or detect_temporal_intent(query) or "any").lower()
        type_filter = {
            normalize_memory_type(item) for item in (memory_types or []) if item
        }
        with self._db() as conn:
            rows, fts_ranks, entity_ids = self._candidate_rows(
                conn,
                query,
                kinds=kinds or [],
                entities=entities or [],
            )

        rows = {
            memory_id: row
            for memory_id, row in rows.items()
            if self._row_is_current(row, current, include_history)
            and (not type_filter or row["memory_type"] in type_filter)
            and self._row_matches_temporal(row, intent, current)
        }
        entity_ids &= set(rows)
        fts_ranks = {key: value for key, value in fts_ranks.items() if key in rows}

        vector_scores = self._vector_scores(query)
        if query and vector_scores:
            # A busca semântica pode trazer memórias que o FTS não achou.
            allowed = {normalize_kind(kind) for kind in (kinds or [])}
            missing = [
                memory_id
                for memory_id, _ in sorted(
                    vector_scores.items(), key=lambda item: item[1], reverse=True
                )
                if memory_id not in rows
            ][: self.candidates]
            if missing:
                placeholders = ",".join("?" for _ in missing)
                with self._db() as conn:
                    for row in conn.execute(
                        f"SELECT * FROM memories WHERE id IN ({placeholders})",
                        tuple(missing),
                    ).fetchall():
                        if allowed and row["kind"] not in allowed:
                            continue
                        if not self._row_is_current(row, current, include_history):
                            continue
                        if type_filter and row["memory_type"] not in type_filter:
                            continue
                        if not self._row_matches_temporal(row, intent, current):
                            continue
                        rows[int(row["id"])] = row

        if not rows:
            return {
                "status": "ok",
                "query": query,
                "count": 0,
                "memories": [],
                "temporal_intent": intent,
            }

        query_tokens = token_set(query)
        best_rank = min(fts_ranks.values()) if fts_ranks else 0.0
        worst_rank = max(fts_ranks.values()) if fts_ranks else 0.0
        rank_span = max(1e-9, abs(worst_rank - best_rank))

        scored: list[tuple[float, sqlite3.Row]] = []
        for memory_id, row in rows.items():
            semantic = vector_scores.get(memory_id, 0.0)
            lexical = overlap_score(
                query_tokens,
                f"{row['subject']} {row['content']} {row['tags']}",
            )
            if memory_id in fts_ranks:
                # bm25: menor é melhor; normaliza para 0..1.
                lexical = 0.55 * lexical + 0.45 * (
                    1.0 - abs(fts_ranks[memory_id] - best_rank) / rank_span
                )
            try:
                importance = float(row["importance"])
            except (TypeError, ValueError):
                importance = 0.5
            updated = _parse_iso(row["updated_at"])
            age_days = max(0.0, (current - updated).total_seconds() / 86400.0) if updated else 365.0
            recency = 0.5 ** (age_days / self.half_life_days)
            entity_boost = 1.0 if memory_id in entity_ids else 0.0

            memory_type = (row["memory_type"] or "fact").lower()
            if intent == "now" and memory_type == "state":
                temporal_fit = 1.0
            elif intent == "future" and memory_type in ("plan", "task"):
                temporal_fit = 1.0
            elif intent in ("today", "yesterday", "week", "last_week", "month", "last_month") and memory_type == "event":
                temporal_fit = 1.0
            elif intent in ("past", "now") and memory_type in (
                "fact",
                "preference",
                "relationship",
                "routine",
            ):
                temporal_fit = 0.5
            else:
                temporal_fit = 0.0

            try:
                confidence = max(0.0, min(1.0, float(row["confidence"])))
            except (TypeError, ValueError):
                confidence = 0.8
            verification = (row["verification_status"] or "confirmed").lower()

            if query:
                score = (
                    0.42 * semantic
                    + 0.33 * lexical
                    + 0.10 * importance
                    + 0.08 * recency
                    + 0.17 * entity_boost
                    + 0.10 * temporal_fit
                )
            else:
                score = (
                    0.50 * entity_boost
                    + 0.20 * importance
                    + 0.15 * recency
                    + 0.10 * lexical
                    + 0.10 * temporal_fit
                )
            # Evita ruído de similaridade lexical/semântica fraca: sem nenhuma
            # palavra em comum, só entra se a semelhança vetorial for forte.
            # Planos/tarefas futuros entram pela intenção temporal, mesmo sem
            # sobreposição lexical com a pergunta ("o que vou fazer?").
            weak_match = query and lexical <= 0.0 and semantic < 0.40 and entity_boost <= 0.0
            if weak_match and not (intent == "future" and memory_type in ("plan", "task")):
                continue
            score *= 0.75 + 0.25 * confidence
            if verification == "inferred":
                score *= 0.90
            if entity_boost > 0.0 or score >= threshold:
                scored.append((score, row))

        scored.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
        selected = scored[:limit]

        if selected:
            ids = [int(row["id"]) for _, row in selected]
            now_iso = _now_iso()
            with self._lock, self._db() as conn:
                conn.executemany(
                    """
                    UPDATE memories
                       SET last_used_at=?, use_count=use_count+1
                     WHERE id=?
                    """,
                    [(now_iso, memory_id) for memory_id in ids],
                )

        memories = []
        for score, row in selected:
            data = self._row_to_dict(row)
            data["score"] = round(float(score), 3)
            memories.append(data)
        return {
            "status": "ok",
            "query": query,
            "count": len(memories),
            "memories": memories,
            "temporal_intent": intent,
        }

    # ------------------------------------------------------------------
    # Remoção
    # ------------------------------------------------------------------
    def forget(
        self,
        *,
        memory_id: Optional[int] = None,
        query: str = "",
        limit: int = 5,
    ) -> dict[str, Any]:
        removed: list[dict[str, Any]] = []
        with self._lock, self._db() as conn:
            if memory_id:
                row = conn.execute(
                    "SELECT * FROM memories WHERE id=?", (int(memory_id),)
                ).fetchone()
                if row is not None:
                    removed.append(self._row_to_dict(row))
                    conn.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
            elif query.strip():
                query = compact_whitespace(query)
                fts_query = self._fts_query(query)
                rows = []
                if fts_query and self.fts_available:
                    try:
                        rows = conn.execute(
                            """
                            SELECT m.* FROM memories_fts
                              JOIN memories m ON m.id = memories_fts.rowid
                             WHERE memories_fts MATCH ?
                             ORDER BY bm25(memories_fts)
                             LIMIT ?
                            """,
                            (fts_query, max(1, min(20, int(limit)))),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                if not rows:
                    like = f"%{query}%"
                    rows = conn.execute(
                        """
                        SELECT * FROM memories
                         WHERE content LIKE ? OR subject LIKE ? OR tags LIKE ?
                         ORDER BY updated_at DESC LIMIT ?
                        """,
                        (like, like, like, max(1, min(20, int(limit)))),
                    ).fetchall()
                for row in rows:
                    removed.append(self._row_to_dict(row))
                    conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
        return {
            "status": "ok" if removed else "not_found",
            "deleted": len(removed),
            "memories": removed,
        }

    def count(self) -> int:
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM memories").fetchone()
        return int(row["total"]) if row else 0

    def get_meta(self, key: str, default: str = "") -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    def stats(self) -> dict[str, Any]:
        """Contagens por tipo/status para diagnóstico e métricas."""
        with self._db() as conn:
            by_type = {
                row["memory_type"] or "fact": int(row["total"])
                for row in conn.execute(
                    "SELECT memory_type, COUNT(*) AS total FROM memories GROUP BY memory_type"
                ).fetchall()
            }
            by_status = {
                row["status"] or "active": int(row["total"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS total FROM memories GROUP BY status"
                ).fetchall()
            }
        return {"total": self.count(), "by_type": by_type, "by_status": by_status}
