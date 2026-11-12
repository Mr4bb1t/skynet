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


def normalize_kind(value: str) -> str:
    cleaned = normalize_entity(value).replace(" ", "_")
    if cleaned in VALID_KINDS:
        return cleaned
    return _KIND_ALIASES.get(cleaned, "other")


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


def _fingerprint(kind: str, subject: str, content: str) -> str:
    raw = f"{kind}|{normalize_entity(subject)}|{normalize_entity(content)}"
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
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
                ("schema_version", "1"),
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
    ) -> dict[str, Any]:
        content = truncate(compact_whitespace(content), self.max_content_chars)
        if not content:
            return {"status": "error", "error": "conteúdo vazio"}
        kind = normalize_kind(kind)
        subject = truncate(compact_whitespace(subject), 160)
        tags = truncate(compact_whitespace(tags), 200)
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5
        now = _now_iso()
        fingerprint = _fingerprint(kind, subject, content)
        embedding, provider = self._embed(
            f"{subject}. {content} {tags}".strip(), query=False
        )
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._lock, self._db() as conn:
            target_id = None
            updated = False
            if memory_id:
                row = conn.execute(
                    "SELECT id FROM memories WHERE id=?", (int(memory_id),)
                ).fetchone()
                if row is not None:
                    target_id = int(row["id"])
                    updated = True
            if target_id is None:
                row = conn.execute(
                    "SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if row is not None:
                    target_id = int(row["id"])
                    updated = True

            if target_id is not None:
                try:
                    conn.execute(
                        """
                        UPDATE memories
                           SET fingerprint=?, kind=?, subject=?, content=?,
                               tags=?, importance=?, source=?, metadata=?,
                               updated_at=?, embedding=?, embedding_model=?
                         WHERE id=?
                        """,
                        (
                            fingerprint,
                            kind,
                            subject,
                            content,
                            tags,
                            importance,
                            source,
                            metadata_json,
                            now,
                            _pack_vector(embedding) if embedding else None,
                            provider,
                            target_id,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # O novo fingerprint já pertence a outra linha: funde nela
                    # e remove a linha antiga para não deixar duplicata.
                    row = conn.execute(
                        "SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)
                    ).fetchone()
                    if row is None:
                        raise
                    duplicate_id = int(row["id"])
                    if duplicate_id != target_id:
                        conn.execute(
                            """
                            UPDATE memories
                               SET fingerprint=?, kind=?, subject=?, content=?,
                                   tags=?, importance=?, source=?, metadata=?,
                                   updated_at=?, embedding=?, embedding_model=?
                             WHERE id=?
                            """,
                            (
                                fingerprint,
                                kind,
                                subject,
                                content,
                                tags,
                                importance,
                                source,
                                metadata_json,
                                now,
                                _pack_vector(embedding) if embedding else None,
                                provider,
                                duplicate_id,
                            ),
                        )
                        conn.execute(
                            "DELETE FROM memories WHERE id=?", (target_id,)
                        )
                        target_id = duplicate_id
                    updated = True
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO memories
                        (fingerprint, kind, subject, content, tags, importance,
                         source, metadata, created_at, updated_at, embedding,
                         embedding_model)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        kind,
                        subject,
                        content,
                        tags,
                        importance,
                        source,
                        metadata_json,
                        now,
                        now,
                        _pack_vector(embedding) if embedding else None,
                        provider,
                    ),
                )
                target_id = int(cursor.lastrowid)

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
            "subject": row["subject"] or "",
            "content": row["content"],
            "tags": row["tags"] or "",
            "importance": round(float(row["importance"]), 2),
            "source": row["source"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"] or "",
            "use_count": int(row["use_count"]),
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

    def recall(
        self,
        query: str = "",
        *,
        kinds: Optional[list[str]] = None,
        entities: Optional[list[str]] = None,
        limit: int = 5,
        min_score: Optional[float] = None,
    ) -> dict[str, Any]:
        """Retorna somente as memórias mais relevantes para a consulta.

        Sem consulta e sem entidades, não devolve nada: a memória nunca é
        despejada inteira para o modelo.
        """
        query = compact_whitespace(query)
        if not query and not entities:
            return {"status": "ok", "query": "", "count": 0, "memories": []}

        limit = max(1, min(12, int(limit)))
        threshold = self.min_score if min_score is None else float(min_score)
        with self._db() as conn:
            rows, fts_ranks, entity_ids = self._candidate_rows(
                conn,
                query,
                kinds=kinds or [],
                entities=entities or [],
            )
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
                        if not allowed or row["kind"] in allowed:
                            rows[int(row["id"])] = row

        if not rows:
            return {"status": "ok", "query": query, "count": 0, "memories": []}

        query_tokens = token_set(query)
        best_rank = min(fts_ranks.values()) if fts_ranks else 0.0
        worst_rank = max(fts_ranks.values()) if fts_ranks else 0.0
        rank_span = max(1e-9, abs(worst_rank - best_rank))
        now = datetime.now(timezone.utc)

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
            age_days = max(0.0, (now - updated).total_seconds() / 86400.0) if updated else 365.0
            recency = 0.5 ** (age_days / self.half_life_days)
            entity_boost = 1.0 if memory_id in entity_ids else 0.0

            if query:
                score = (
                    0.42 * semantic
                    + 0.33 * lexical
                    + 0.10 * importance
                    + 0.08 * recency
                    + 0.17 * entity_boost
                )
            else:
                score = (
                    0.55 * entity_boost
                    + 0.20 * importance
                    + 0.15 * recency
                    + 0.10 * lexical
                )
            # Evita ruído de similaridade lexical/semântica fraca: sem nenhuma
            # palavra em comum, só entra se a semelhança vetorial for forte.
            if query and lexical <= 0.0 and semantic < 0.40 and entity_boost <= 0.0:
                continue
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
