"""Teste de integracao da captura continua (Teste 1).

Gera um video sintetico com FFmpeg e valida que a captura mantem uma unica
conexao, entrega frames recentes e nao reinicia o decoder em regime normal.
Requer ffmpeg/ffprobe; caso contrario o teste e ignorado.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from vision.camera_capture import CameraCapture
from vision.diagnostics import Diagnostics


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe nao disponiveis",
)
class CameraCaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls._tmp.name) / "synthetic.avi"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=15",
                "-t",
                "4",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-y",
                str(cls.video),
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_capture_keeps_single_connection_and_fresh_frames(self) -> None:
        diag = Diagnostics()
        camera = CameraCapture(
            str(self.video),
            fps=10,
            fallback_width=320,
            fallback_height=180,
            diagnostics=diag,
        )
        try:
            camera.start()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and camera.get_latest() is None:
                time.sleep(0.05)
            self.assertIsNotNone(camera.get_latest(), camera.last_stderr)

            time.sleep(2.5)
            frame = camera.get_latest()
            self.assertIsNotNone(frame)
            self.assertEqual((frame.width, frame.height), (320, 180))
            self.assertLess(frame.age(), 1.0)
            self.assertGreaterEqual(diag.capture_frames, 10)
            self.assertEqual(diag.capture_restarts, 0)
            self.assertGreater(diag.capture_rate(), 5.0)
        finally:
            camera.stop()

    def test_reconnects_after_decoder_exit(self) -> None:
        diag = Diagnostics()
        camera = CameraCapture(
            str(self.video),
            fps=10,
            fallback_width=320,
            fallback_height=180,
            reconnect_base_sec=0.2,
            reconnect_max_sec=0.5,
            diagnostics=diag,
        )
        try:
            camera.start()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and camera.get_latest() is None:
                time.sleep(0.05)
            first = camera.get_latest()
            self.assertIsNotNone(first, camera.last_stderr)
            seq_before = first.seq

            proc = camera._proc
            self.assertIsNotNone(proc)
            proc.terminate()

            restarted = False
            while time.monotonic() < deadline:
                frame = camera.get_latest()
                if (
                    diag.capture_restarts >= 1
                    and frame is not None
                    and frame.seq > seq_before + 2
                ):
                    restarted = True
                    break
                time.sleep(0.1)
            self.assertTrue(restarted, "captura nao reiniciou apos matar o FFmpeg")
            self.assertGreaterEqual(diag.capture_restarts, 1)
        finally:
            camera.stop()


if __name__ == "__main__":
    unittest.main()
