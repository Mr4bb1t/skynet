"""Testes de configuracao, mascaramento de URL e utilidades do cliente Live."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vision.config import (
    ConfigError,
    IMAGE_PRESETS,
    load_settings,
    mask_url,
)
from vision.gemini_live_client import ResponseCollector, _parse_duration_seconds


class ConfigTest(unittest.TestCase):
    def _load(self, content: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".env"
        path.write_text(content, encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            return load_settings(path)

    def test_preset_low_defines_width(self) -> None:
        settings = self._load(
            "GEMINI_API_KEY=test\n"
            "IMAGE_PRESET=LOW\n"
            "JPEG_QUALITY=50\n"
            "MEDIA_RESOLUTION=HIGH\n"
            "CAPTURE_FPS=12\n"
            f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
        )
        self.assertEqual(settings.image_max_width, IMAGE_PRESETS["LOW"])
        self.assertEqual(settings.image_max_height, 480)
        self.assertEqual(settings.image_preset, "LOW")
        self.assertEqual(settings.jpeg_quality, 50)
        self.assertEqual(settings.media_resolution, "HIGH")
        self.assertEqual(settings.capture_fps, 12.0)

    def test_invalid_preset_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load(
                "IMAGE_PRESET=HUGE\n"
                f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
            )

    def test_invalid_quality_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load(
                "JPEG_QUALITY=0\n"
                f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
            )

    def test_invalid_media_resolution_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._load(
                "MEDIA_RESOLUTION=ULTRA\n"
                f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
            )

    def test_heartbeat_can_be_disabled(self) -> None:
        settings = self._load(
            "IMAGE_HEARTBEAT_ENABLED=false\n"
            f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
        )
        self.assertFalse(settings.heartbeat_enabled)
        self.assertIn("heartbeat=off", settings.describe())

    def test_heartbeat_enabled_by_default(self) -> None:
        settings = self._load(
            f"VISION_STORAGE_DIR={tempfile.gettempdir()}/skynet_test_vision\n"
        )
        self.assertTrue(settings.heartbeat_enabled)
        self.assertIn("heartbeat=on", settings.describe())

    def test_mask_url_hides_credentials(self) -> None:
        masked = mask_url("rtsp://admin:segredo@192.168.10.14:554/onvif2")
        self.assertNotIn("segredo", masked)
        self.assertIn("admin:***@192.168.10.14", masked)
        self.assertEqual(mask_url(""), "")
        self.assertEqual(mask_url("arquivo.mp4"), "arquivo.mp4")


class LiveClientUtilTest(unittest.TestCase):
    def test_parse_duration(self) -> None:
        self.assertEqual(_parse_duration_seconds("9.5s"), 9.5)
        self.assertEqual(_parse_duration_seconds("10"), 10.0)
        self.assertEqual(_parse_duration_seconds(3), 3.0)
        self.assertIsNone(_parse_duration_seconds(None))
        self.assertIsNone(_parse_duration_seconds("invalido"))

    def test_response_collector_measures_latency(self) -> None:
        start = time.monotonic() - 0.5
        collector = ResponseCollector(question_at=start)
        collector.add("ola ")
        collector.add("mundo")
        collector.finish()
        self.assertEqual(collector.text, "ola mundo")
        self.assertIsNotNone(collector.first_token_ms)
        self.assertGreaterEqual(collector.first_token_ms, 450.0)
        self.assertIsNotNone(collector.turn_complete_ms)
        self.assertGreaterEqual(collector.turn_complete_ms, 450.0)
        self.assertFalse(collector.interrupted)

    def test_response_collector_interrupted(self) -> None:
        collector = ResponseCollector()
        collector.add("parcial")
        collector.finish(interrupted=True)
        self.assertTrue(collector.interrupted)
        self.assertEqual(collector.text, "parcial")


if __name__ == "__main__":
    unittest.main()
