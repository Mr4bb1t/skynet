"""Garante persona de família e ajuda natural, sem tom robótico de atendente."""

from __future__ import annotations

import unittest
from pathlib import Path

from audio.live_agent import DEFAULT_EMBODIED_INSTRUCTION


class PersonaPromptTest(unittest.TestCase):
    def test_voice_prompt_is_family_like_and_allows_help(self) -> None:
        text = DEFAULT_EMBODIED_INSTRUCTION
        self.assertIn("FAMÍLIA, NÃO ATENDENTE", text)
        self.assertIn("pode oferecer ajuda", text.lower())
        self.assertIn("sem script", text.lower())
        self.assertNotIn("É PROIBIDO", text)

    def test_chat_prompt_is_family_like_and_allows_help(self) -> None:
        path = Path(__file__).resolve().parents[1] / "vision" / "chat_prompt.txt"
        text = path.read_text(encoding="utf-8")
        self.assertIn("família", text)
        self.assertIn("pode oferecer ajuda", text.lower())
        self.assertIn("sem script", text.lower())
        self.assertNotIn("É proibido", text)


if __name__ == "__main__":
    unittest.main()
