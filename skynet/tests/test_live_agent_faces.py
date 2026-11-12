"""Garante que o agente de voz entregue o JSON do reconhecimento facial à IA."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from types import SimpleNamespace

from audio.live_agent import VoiceAgent


FACES = {
    "people": [
        {
            "face_index": 0,
            "known": True,
            "distance": 0.41,
            "bbox": [210, 60, 120, 120],
            "person": {
                "person_id": 1,
                "name": "João",
                "nickname": "Jota",
                "notes": "amigo do usuário",
            },
        }
    ],
    "unknown_faces": 0,
    "faces": 1,
    "memory": "ok",
}


class FakeMemory:
    available = True
    reason = ""

    def __init__(self, public: dict) -> None:
        self.public = public

    def analyze_jpeg(self, jpeg: bytes):
        return SimpleNamespace(public=self.public)


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_realtime_input(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FakeLongMemory:
    available = True
    reason = ""

    def __init__(self) -> None:
        self.config = SimpleNamespace(photo_token_budget=120)

    def build_context(self, query="", **kwargs) -> str:
        people = kwargs.get("people") or []
        if not people:
            return ""
        return "Memórias relevantes:\n- [pessoa] João: gosta de café"


class LiveAgentFacesTest(unittest.TestCase):
    def _agent(self, memory: FakeMemory, *, long_memory=None) -> VoiceAgent:
        agent = object.__new__(VoiceAgent)
        agent._tui_active = True
        agent.memory = memory
        agent.long_memory = long_memory
        agent.memory_tool = (
            SimpleNamespace(available=True) if long_memory is not None else None
        )
        agent._conversation = deque(["Usuário: oi, João!"])
        agent._last_user_text = "oi, João!"
        agent._last_face_at = 0.0
        agent._faces_interval_sec = 5.0
        agent.ptz = None
        return agent

    def test_capture_image_sends_json_with_the_photo(self) -> None:
        agent = self._agent(FakeMemory(FACES))

        async def fake_capture() -> bytes:
            return b"jpeg-de-teste"

        agent._capture_jpeg = fake_capture
        session = FakeSession()

        result = asyncio.run(
            agent._execute_tool(
                SimpleNamespace(name="capture_image", args={}), session
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["faces"], FACES)
        self.assertEqual(json.loads(result["people_json"]), FACES)

        videos = [call for call in session.calls if "video" in call]
        texts = [call for call in session.calls if "text" in call]
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["video"].mime_type, "image/jpeg")
        self.assertEqual(len(texts), 1)
        self.assertIn("Pessoas reconhecidas nesta foto (JSON)", texts[0]["text"])

        public = json.loads(texts[0]["text"].split("(JSON): ", 1)[1])
        self.assertEqual(public["people"][0]["person"]["name"], "João")

    def test_capture_image_includes_relevant_memory(self) -> None:
        agent = self._agent(FakeMemory(FACES), long_memory=FakeLongMemory())

        async def fake_capture() -> bytes:
            return b"jpeg-de-teste"

        agent._capture_jpeg = fake_capture
        session = FakeSession()

        result = asyncio.run(
            agent._execute_tool(
                SimpleNamespace(name="capture_image", args={}), session
            )
        )

        self.assertIn("Memórias relevantes", result["memory_context"])
        texts = [call["text"] for call in session.calls if "text" in call]
        self.assertTrue(any("Memórias relevantes" in text for text in texts))

    def test_move_camera_returns_json_for_the_new_photo(self) -> None:
        agent = self._agent(FakeMemory(FACES))

        async def fake_capture() -> bytes:
            return b"jpeg-de-teste"

        agent._capture_jpeg = fake_capture
        session = FakeSession()

        result = asyncio.run(
            agent._execute_tool(
                SimpleNamespace(
                    name="move_camera",
                    args={"pan_degrees": 10, "tilt_degrees": 0},
                ),
                session,
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["photo_sent"])
        self.assertEqual(result["faces"], FACES)
        self.assertEqual(json.loads(result["people_json"]), FACES)
        self.assertTrue(any("text" in call for call in session.calls))


if __name__ == "__main__":
    unittest.main()
