"""Testes das ferramentas de câmera e PTZ usadas pelo chat."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from vision.tools.camera_tool import CameraTool
from vision.tools.ptz_tool import PTZTool


class FakePTZ:
    def __init__(self) -> None:
        self.calls = []
        self.last_steps = 0
        self.last_degrees = 0.0
        self.stopped = 0

    def pan_degrees(self, degrees: float) -> bool:
        self.calls.append(("pan", float(degrees)))
        self.last_steps = abs(round(degrees / 1.125))
        self.last_degrees = float(degrees)
        return True

    def tilt_degrees(self, degrees: float) -> bool:
        self.calls.append(("tilt", float(degrees)))
        self.last_steps = abs(round(degrees / 1.0))
        self.last_degrees = float(degrees)
        return True

    def stop(self) -> bool:
        self.stopped += 1
        return True


class PTZToolTest(unittest.TestCase):
    def test_move_pan_and_tilt(self) -> None:
        fake = FakePTZ()
        tool = PTZTool(fake)
        ok, info = tool.move(10, -5)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [("pan", 10.0), ("tilt", -5.0)])
        self.assertIn("pan=+10.0", info)

    def test_clamps_angles(self) -> None:
        fake = FakePTZ()
        tool = PTZTool(fake)
        ok, _ = tool.move(999, -999)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [("pan", 180.0), ("tilt", -90.0)])

    def test_zero_does_nothing(self) -> None:
        fake = FakePTZ()
        tool = PTZTool(fake)
        ok, info = tool.move(0, 0)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [])
        self.assertIn("nenhum movimento", info)

    def test_stop(self) -> None:
        fake = FakePTZ()
        tool = PTZTool(fake)
        ok, info = tool.stop()
        self.assertTrue(ok)
        self.assertEqual(fake.stopped, 1)
        self.assertIn("interrompido", info)

    def test_disabled(self) -> None:
        tool = PTZTool(enabled=False)
        ok, info = tool.move(10, 0)
        self.assertFalse(ok)
        self.assertIn("desativado", info)

    def test_controller_failure(self) -> None:
        class Failing(FakePTZ):
            def pan_degrees(self, degrees: float) -> bool:
                raise RuntimeError("falha simulada")

        tool = PTZTool(Failing())
        ok, info = tool.move(10, 0)
        self.assertFalse(ok)
        self.assertIn("falha simulada", info)

    def test_refused_move_is_reported(self) -> None:
        class Refusing(FakePTZ):
            def pan_degrees(self, degrees: float) -> bool:
                return False

        tool = PTZTool(Refusing())
        ok, info = tool.move(10, 0)
        self.assertFalse(ok)
        self.assertIn("falha ao enviar", info)


class CameraToolDisabledTest(unittest.TestCase):
    def _settings(self, url: str = ""):
        return SimpleNamespace(
            image_max_width=640,
            image_max_height=480,
            jpeg_quality=75,
            image_preset="BALANCED",
            camera_url=url,
            capture_fps=10,
            capture_max_width=0,
            capture_width=640,
            capture_height=360,
            camera_transport="udp",
            camera_probe_timeout_sec=10,
            camera_read_timeout_sec=5,
            capture_reconnect_base_sec=1,
            capture_reconnect_max_sec=30,
            frame_stale_sec=3,
            person_memory_enabled=False,
            person_db_path="/tmp/skynet_people_test.db",
            person_photo_dir="/tmp/skynet_people_test",
            face_match_threshold=0.6,
        )

    def test_disabled_capture_returns_error(self) -> None:
        tool = CameraTool(self._settings(), disabled=True)
        jpeg, info, payload = tool.capture()
        self.assertIsNone(jpeg)
        self.assertEqual(payload["status"], "error")
        self.assertIn("desativada", info)

    def test_camera_can_be_lazy_for_tests(self) -> None:
        tool = CameraTool(self._settings("rtsp://exemplo/camera"), prewarm=False)
        self.assertIsNone(tool.camera)
        tool.close()


if __name__ == "__main__":
    unittest.main()
