"""Testes da memória de longo prazo (SQLite + FTS + RAG por embeddings)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory import (
    HashingEmbedder,
    MemoryConfig,
    MemoryManager,
    MemoryTool,
    ResilientEmbedder,
)


def make_manager(db_path: str, *, token_budget: int = 380) -> MemoryManager:
    config = MemoryConfig(
        enabled=True,
        db_path=db_path,
        embeddings_enabled=False,
        embedding_model="hash",
        embedding_dim=256,
        top_k=5,
        candidates=30,
        token_budget=token_budget,
        photo_token_budget=min(120, token_budget),
        min_score=0.18,
        half_life_days=45.0,
        max_content_chars=700,
    )
    return MemoryManager(
        config=config,
        source="test",
        embedder=HashingEmbedder(dim=256),
    )


class MemoryRagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "memory.db")
        self.manager = make_manager(self.db)
        self.tool = MemoryTool(self.manager)
        self.tool.remember(
            {
                "content": "João é amigo do usuário e gosta de café",
                "kind": "person",
                "subject": "João",
                "tags": "café, amigo",
                "importance": 0.8,
            }
        )
        self.tool.remember(
            {
                "content": "O projeto Skynet usa Gemini Live e uma câmera PTZ",
                "kind": "project",
                "subject": "Skynet",
                "tags": "gemini, câmera",
                "importance": 0.9,
            }
        )
        self.tool.remember(
            {
                "content": "Rex é o cachorro do João e toma vacina em janeiro",
                "kind": "animal",
                "subject": "Rex, João",
                "tags": "cachorro, vacina",
                "importance": 0.7,
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recall_finds_relevant_memory(self) -> None:
        result = self.tool.recall({"query": "café", "limit": 3})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertIn("João", result["text"])
        self.assertIn("café", result["text"])

    def test_recall_by_project(self) -> None:
        result = self.tool.recall({"query": "projeto de visão", "limit": 3})
        self.assertEqual(result["status"], "ok")
        self.assertIn("Skynet", result["text"])
        self.assertNotIn("café", result["text"])

    def test_dedupe_and_update(self) -> None:
        result = self.tool.remember(
            {
                "content": "João é amigo do usuário e gosta de café",
                "kind": "person",
                "subject": "João",
            }
        )
        self.assertTrue(result["updated"])
        self.assertEqual(self.manager.store.count(), 3)

        result = self.tool.remember(
            {
                "content": "João é amigo do usuário e prefere chá",
                "kind": "person",
                "subject": "João",
                "memory_id": result["memory_id"],
            }
        )
        self.assertEqual(result["status"], "ok")
        recalled = self.tool.recall({"query": "chá", "limit": 2})
        self.assertIn("chá", recalled["text"])

    def test_entity_context_is_scoped_to_recognized_person(self) -> None:
        context = self.manager.build_context(
            "", people=[{"name": "João"}], budget_tokens=120
        )
        self.assertIn("João", context)
        self.assertIn("Rex", context)  # memória de Rex também cita João
        self.assertNotIn("Skynet", context)

    def test_context_budget_never_dumps_everything(self) -> None:
        for index in range(12):
            self.tool.remember(
                {
                    "content": (
                        f"Detalhe longo do projeto Skynet número {index}: "
                        "descrição extensa para testar o orçamento de tokens."
                    ),
                    "kind": "project",
                    "subject": "Skynet",
                    "tags": "teste",
                }
            )
        context = self.manager.build_context("projeto Skynet", budget_tokens=60)
        self.assertLessEqual(len(context), max(360, int(60 * 3.8) + 10))
        self.assertNotEqual(context.count("Detalhe longo"), 12)
        self.assertIn("CURRENT STATE", context)

    def test_forget_by_query(self) -> None:
        result = self.tool.forget({"query": "vacina"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.manager.store.count(), 2)
        recalled = self.tool.recall({"query": "cachorro", "limit": 3})
        self.assertEqual(recalled["count"], 0)

    def test_persists_across_managers(self) -> None:
        other = make_manager(self.db)
        result = MemoryTool(other).recall({"query": "café", "limit": 3})
        self.assertIn("João", result["text"])

    def test_empty_query_is_refused(self) -> None:
        result = self.tool.recall({"query": ""})
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.manager.store.count(), 3)

    def test_rag_combines_conversation_and_face(self) -> None:
        context = self.manager.build_context(
            "o que o João gosta de beber?",
            conversation="Usuário: e o João? SKYNET: ele é seu amigo.",
            people=[{"name": "João"}],
            budget_tokens=160,
        )
        self.assertIn("João", context)
        self.assertIn("café", context)
        self.assertLessEqual(len(context), int(160 * 3.8) + 10)


class ChatMemoryInjectionTest(unittest.TestCase):
    def test_context_is_injected_but_not_saved_in_history(self) -> None:
        from vision.chat import TextChat

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager = make_manager(str(Path(tmp.name) / "memory.db"))
        manager.remember(
            "João gosta de café",
            kind="person",
            subject="João",
            tags="café",
        )

        chat = object.__new__(TextChat)
        chat.memory = manager
        chat.memory_tool = MemoryTool(manager)
        chat.history = []
        chat.camera = SimpleNamespace(
            last_faces=[
                SimpleNamespace(
                    known=True,
                    name="João",
                    nickname="Jota",
                    person_id=1,
                )
            ]
        )
        chat.max_tool_rounds = 1
        chat.model = "fake"
        chat.fallback_model = ""
        captured = []

        def fake_generate(contents, model=None):
            captured.append(contents)
            part = SimpleNamespace(text="ok", function_call=None)
            content = SimpleNamespace(parts=[part])
            return SimpleNamespace(
                candidates=[SimpleNamespace(content=content)],
                prompt_feedback=None,
            )

        chat._generate = fake_generate
        answer = chat.ask("o que o João gosta de beber?")

        self.assertEqual(answer, "ok")
        sent_parts = captured[0][-1].parts
        self.assertEqual(len(sent_parts), 2)
        self.assertIn("CURRENT STATE", sent_parts[0].text)
        self.assertIn("SEMANTIC KNOWLEDGE", sent_parts[0].text)
        self.assertEqual(sent_parts[1].text, "o que o João gosta de beber?")

        # O contexto recuperado não é persistido no histórico.
        user_history = chat.history[0]
        self.assertEqual(len(user_history.parts), 1)
        self.assertEqual(user_history.parts[0].text, "o que o João gosta de beber?")


class ResilientEmbedderTest(unittest.TestCase):
    def test_falls_back_to_local_when_primary_fails(self) -> None:
        class Broken:
            name = "broken"

            def embed(self, texts, *, query=False):
                raise RuntimeError("sem rede")

        embedder = ResilientEmbedder(Broken(), HashingEmbedder(dim=128))
        batch = embedder.embed(["João gosta de café"], query=True)
        self.assertEqual(batch.provider, "hash:128")
        self.assertEqual(len(batch.vectors[0]), 128)


if __name__ == "__main__":
    unittest.main()
