"""Testes da memória temporal: quatro blocos, datas, supersessão e working memory."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from memory import (
    HashingEmbedder,
    MemoryConfig,
    MemoryConsolidator,
    MemoryManager,
    WorkingMemory,
    format_memory_line,
)
from memory.timeutil import detect_temporal_intent, local_tz, parse_when

NOW = datetime(2026, 11, 11, 22, 0, tzinfo=local_tz())
YESTERDAY = "2026-11-10"
TOMORROW = "2026-11-12"
NEXT_WEEK = "2026-11-18"


def make_manager(db_path: str) -> MemoryManager:
    config = MemoryConfig(
        enabled=True,
        db_path=db_path,
        embeddings_enabled=False,
        embedding_model="hash",
        embedding_dim=256,
        top_k=5,
        candidates=30,
        token_budget=380,
        photo_token_budget=120,
        min_score=0.0,
        half_life_days=45.0,
        max_content_chars=700,
        working_ttl_sec=1800,
    )
    return MemoryManager(
        config=config,
        source="chat",
        embedder=HashingEmbedder(dim=256),
    )


class TemporalMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "memory.db")
        self.manager = make_manager(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def remember(self, content: str, **kwargs):
        result = self.manager.remember(content, **kwargs)
        self.assertEqual(result["status"], "ok", result)
        return result

    # 1. Pessoa identificada ontem não é considerada presente hoje.
    def test_person_seen_yesterday_is_not_present_now(self) -> None:
        self.remember(
            "João esteve na sala ontem",
            memory_type="event",
            event_time=YESTERDAY,
            subject="João",
            tags="sala",
        )
        context = self.manager.build_context(
            "o que aconteceu com o João ontem?",
            people=[],
            now=NOW,
        )
        current_block = context.split("=== EPISODIC MEMORIES ===")[0]
        self.assertIn("ninguém identificado agora", current_block)
        self.assertNotIn("João", current_block)
        self.assertIn("João", context)
        self.assertIn("10/11/2026", context)
        self.assertIn("ontem", context)

    # 2. Pessoa visível agora é reconhecida como presente.
    def test_person_visible_now_is_present(self) -> None:
        context = self.manager.build_context(
            "", people=[{"name": "João"}], now=NOW
        )
        current_block = context.split("=== EPISODIC MEMORIES ===")[0]
        self.assertIn("Pessoas visíveis agora: João", current_block)

    # 3. Fatos antigos não são tratados como atuais.
    def test_expired_fact_is_not_returned_as_current(self) -> None:
        old = self.remember(
            "João trabalhava na empresa X",
            memory_type="fact",
            subject="João",
            valid_from="2026-01-01",
            valid_until="2026-11-01",
        )
        current = self.remember(
            "João trabalha na empresa Y",
            memory_type="fact",
            subject="João",
            valid_from="2026-11-02",
        )
        stored = self.manager.store.get(old["memory_id"])
        self.assertEqual(stored["status"], "expired")
        recalled = self.manager.store.recall(
            "João empresa",
            memory_types=["fact"],
            temporal_intent="any",
            now=NOW,
        )
        ids = [item["memory_id"] for item in recalled["memories"]]
        self.assertNotIn(old["memory_id"], ids)
        self.assertIn(current["memory_id"], ids)
        context = self.manager.build_context("João empresa", now=NOW)
        self.assertNotIn("empresa X", context)
        self.assertIn("empresa Y", context)

    # 4. Mudanças preservam o histórico (supersessão).
    def test_supersede_preserves_history(self) -> None:
        first = self.remember(
            "João trabalha na empresa X",
            memory_type="fact",
            subject="João",
            valid_from="2026-01-01",
        )
        second = self.remember(
            "João trabalha na empresa Y",
            memory_type="fact",
            subject="João",
            valid_from="2026-11-02",
            memory_id=first["memory_id"],
            supersede=True,
        )
        old = self.manager.store.get(first["memory_id"])
        new = self.manager.store.get(second["memory_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertTrue(old["valid_until"])
        self.assertEqual(new["status"], "active")
        self.assertEqual(new["supersedes_id"], first["memory_id"])
        current = self.manager.store.recall(
            "João empresa", memory_types=["fact"], now=NOW
        )
        contents = [item["content"] for item in current["memories"]]
        self.assertIn("João trabalha na empresa Y", contents)
        self.assertNotIn("João trabalha na empresa X", contents)
        history = self.manager.store.recall(
            "João empresa", memory_types=["fact"], include_history=True, now=NOW
        )
        history_contents = [item["content"] for item in history["memories"]]
        self.assertIn("João trabalha na empresa X", history_contents)
        self.assertIn("João trabalha na empresa Y", history_contents)

    # 5. Tarefas futuras não são consideradas concluídas.
    def test_future_plan_is_not_done(self) -> None:
        plan = self.remember(
            "Comprar café",
            memory_type="plan",
            event_time=NEXT_WEEK,
            subject="casa",
            status="active",
        )
        context = self.manager.build_context("o que vou fazer?", now=NOW)
        future_block = context.split("=== FUTURE PLANS ===")[-1]
        self.assertIn("Comprar café", future_block)
        self.assertIn("não realizado", future_block)
        self.assertNotIn("concluída", future_block)
        stored = self.manager.store.get(plan["memory_id"])
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["memory_type"], "plan")

    # 6. Memórias sem data não recebem datas inventadas.
    def test_undated_memory_keeps_unknown_event_time(self) -> None:
        item = self.remember(
            "Prefere café sem açúcar",
            memory_type="preference",
            subject="usuário",
        )
        stored = self.manager.store.get(item["memory_id"])
        self.assertEqual(stored["event_time"], "")
        self.assertEqual(stored["temporal_scope"], "unknown")
        line = format_memory_line(stored, now=NOW)
        self.assertNotIn("Em 11/11/2026", line)
        self.assertIn("Validade/data desconhecida", line)

    # 7. Inferências não são apresentadas como fatos confirmados.
    def test_inferred_memory_is_not_confirmed(self) -> None:
        item = self.remember(
            "Talvez o João goste de chá",
            memory_type="fact",
            subject="João",
            verification_status="inferred",
            confidence=0.6,
        )
        stored = self.manager.store.get(item["memory_id"])
        self.assertEqual(stored["verification_status"], "inferred")
        self.assertLessEqual(stored["confidence"], 0.7)
        line = format_memory_line(stored, now=NOW)
        self.assertIn("inferido", line)
        self.assertIn("confiança 0.60", line)
        self.assertNotIn("Confirmado em", line)

    # 8. Migração de banco antigo não inventa event_time.
    def test_legacy_migration_does_not_invent_event_time(self) -> None:
        legacy_db = str(Path(self.tmp.name) / "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE memories (
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
            CREATE TABLE memory_entities (
                memory_id INTEGER NOT NULL,
                entity TEXT NOT NULL,
                display TEXT NOT NULL DEFAULT '',
                entity_type TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(memory_id, entity)
            );
            CREATE TABLE memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO memories
                (fingerprint, kind, subject, content, importance, source,
                 created_at, updated_at)
            VALUES
                ('legacy', 'event', 'João', 'João esteve aqui',
                 0.5, 'voice', '2026-01-01T00:00:00+00:00',
                 '2026-01-02T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()
        manager = make_manager(legacy_db)
        item = manager.store.get(1)
        self.assertEqual(item["memory_type"], "event")
        self.assertEqual(item["event_time"], "")
        self.assertEqual(item["temporal_scope"], "unknown")
        self.assertEqual(item["source"], "audio")
        self.assertEqual(item["last_confirmed_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(item["created_at"], "2026-01-01T00:00:00+00:00")

    def test_temporal_intent_and_date_parsing(self) -> None:
        self.assertEqual(detect_temporal_intent("o que está acontecendo agora?"), "now")
        self.assertEqual(detect_temporal_intent("o que aconteceu ontem?"), "yesterday")
        self.assertEqual(detect_temporal_intent("o que vou fazer amanhã?"), "future")
        parsed = parse_when("hoje", now=NOW)
        self.assertTrue(parsed.startswith("2026-11-11"))
        self.assertEqual(parse_when("data inventada"), "")

    def test_relative_age_past_and_future_are_not_inverted(self) -> None:
        from memory.timeutil import format_when, relative_age

        self.assertEqual(relative_age(YESTERDAY, now=NOW), "ontem")
        self.assertEqual(relative_age(TOMORROW, now=NOW), "amanhã")
        self.assertEqual(relative_age("2026-11-08", now=NOW), "há 3 dias")
        self.assertEqual(relative_age(NEXT_WEEK, now=NOW), "em 7 dias")
        self.assertEqual(relative_age("2026-11-11", now=NOW), "hoje")
        self.assertEqual(
            format_when(YESTERDAY, now=NOW), "10/11/2026 (ontem)"
        )
        self.assertEqual(
            format_when(TOMORROW, now=NOW), "12/11/2026 (amanhã)"
        )

    def test_now_excludes_past_events(self) -> None:
        self.remember(
            "João esteve na sala",
            memory_type="event",
            event_time=YESTERDAY,
            subject="João",
        )
        result = self.manager.store.recall(
            "João",
            memory_types=["event"],
            temporal_intent="now",
            now=NOW,
        )
        self.assertEqual(result["count"], 0)

    def test_today_filters_events(self) -> None:
        yesterday = self.remember(
            "Evento de ontem",
            memory_type="event",
            event_time=YESTERDAY,
            subject="teste",
        )
        today = self.remember(
            "Evento de hoje",
            memory_type="event",
            event_time="2026-11-11",
            subject="teste",
        )
        result = self.manager.store.recall(
            "evento",
            memory_types=["event"],
            temporal_intent="today",
            now=NOW,
        )
        ids = [item["memory_id"] for item in result["memories"]]
        self.assertNotIn(yesterday["memory_id"], ids)
        self.assertIn(today["memory_id"], ids)


class ConsolidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "memory.db")
        self.manager = make_manager(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_consolidation_does_not_duplicate(self) -> None:
        def proposer(text, existing, max_items):
            return [
                {
                    "content": "João gosta de café",
                    "memory_type": "fact",
                    "subject": "João",
                    "confidence": 0.9,
                    "verification_status": "inferred",
                }
            ]

        consolidator = MemoryConsolidator(self.manager, proposer=proposer)
        first = consolidator.consolidate(
            "Usuário: João gosta de café", session_id="s1"
        )
        self.assertEqual(first["created"], 1)
        self.assertEqual(self.manager.store.count(), 1)
        second = consolidator.consolidate(
            "Usuário: João gosta de café", session_id="s1"
        )
        self.assertTrue(second["skipped"])
        third = consolidator.consolidate(
            "Usuário: João gosta de café", session_id="s1", force=True
        )
        self.assertEqual(third["updated"], 1)
        self.assertEqual(self.manager.store.count(), 1)

    def test_consolidation_marks_inferred(self) -> None:
        def proposer(text, existing, max_items):
            return [
                {
                    "content": "Talvez o João goste de chá",
                    "memory_type": "fact",
                    "subject": "João",
                    "confidence": 0.9,
                    "verification_status": "inferred",
                }
            ]

        consolidator = MemoryConsolidator(self.manager, proposer=proposer)
        consolidator.consolidate("Usuário: o João talvez goste de chá", session_id="s2")
        memories = self.manager.store.recall("chá", limit=1)["memories"]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["verification_status"], "inferred")
        self.assertLessEqual(memories[0]["confidence"], 0.7)

    def test_consolidation_rejects_invalid_date(self) -> None:
        def proposer(text, existing, max_items):
            return [
                {
                    "content": "Festa na casa do João",
                    "memory_type": "event",
                    "subject": "João",
                    "event_time": "data inventada",
                    "verification_status": "inferred",
                }
            ]

        consolidator = MemoryConsolidator(self.manager, proposer=proposer)
        consolidator.consolidate("Usuário: teve uma festa", session_id="s3")
        memories = self.manager.store.recall("festa", limit=1)["memories"]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["event_time"], "")
        self.assertEqual(memories[0]["temporal_scope"], "unknown")

    def test_consolidation_supersedes(self) -> None:
        old = self.manager.remember(
            "João trabalha na empresa X",
            memory_type="fact",
            subject="João",
        )
        self.assertEqual(old["status"], "ok")

        def proposer(text, existing, max_items):
            return [
                {
                    "content": "João trabalha na empresa Y",
                    "memory_type": "fact",
                    "subject": "João",
                    "supersedes_memory_id": old["memory_id"],
                    "verification_status": "inferred",
                }
            ]

        consolidator = MemoryConsolidator(self.manager, proposer=proposer)
        result = consolidator.consolidate(
            "Usuário: o João agora trabalha na empresa Y", session_id="s4"
        )
        self.assertEqual(result["superseded"], 1)
        old_stored = self.manager.store.get(old["memory_id"])
        self.assertEqual(old_stored["status"], "superseded")
        current = self.manager.store.recall(
            "João empresa", memory_types=["fact"], now=NOW
        )
        contents = [item["content"] for item in current["memories"]]
        self.assertIn("João trabalha na empresa Y", contents)
        self.assertNotIn("João trabalha na empresa X", contents)


class WorkingMemoryTest(unittest.TestCase):
    def test_turns_expire_with_ttl(self) -> None:
        working = WorkingMemory(ttl_seconds=1, max_turns=4)
        working.add_turn("user", "oi")
        self.assertEqual(len(working.snapshot()["turns"]), 1)
        time.sleep(1.1)
        self.assertEqual(len(working.snapshot()["turns"]), 0)

    def test_people_expire_and_state_is_available(self) -> None:
        working = WorkingMemory(people_ttl_seconds=1)
        working.set_people(["João"])
        working.set_state("active_task", "ligar a luz", ttl_seconds=60)
        snapshot = working.snapshot()
        self.assertEqual(snapshot["people"], ["João"])
        self.assertEqual(snapshot["active_task"], "ligar a luz")
        time.sleep(1.1)
        self.assertEqual(working.snapshot()["people"], [])
        self.assertEqual(working.get_state("active_task"), "ligar a luz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
