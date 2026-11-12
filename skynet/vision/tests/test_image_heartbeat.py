"""Testes do heartbeat visual: limite de 1 FPS e ausencia de backlog."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from vision.camera_capture import Frame
from vision.diagnostics import Diagnostics
from vision.image_heartbeat import ImageHeartbeat
from vision.image_processor import ProcessedFrame


def _frame(seq: int, *, age: float = 0.0) -> Frame:
    return Frame(
        data=b"\x00" * 12,
        width=2,
        height=2,
        seq=seq,
        captured_at=time.monotonic() - age,
    )


def _processed(seq: int) -> ProcessedFrame:
    return ProcessedFrame(
        jpeg=b"\xff\xd8fake",
        orig_width=2,
        orig_height=2,
        width=2,
        height=2,
        quality=75,
        seq=seq,
        captured_at=time.monotonic(),
        convert_ms=0.0,
        resize_ms=0.0,
        encode_ms=0.0,
        total_ms=0.0,
    )


class FakeCamera:
    def __init__(self, frame=None) -> None:
        self.frame = frame

    def get_latest(self, max_age=None):
        if self.frame is None:
            return None
        if max_age is not None and self.frame.age() > max_age:
            return None
        return self.frame


class FakeProcessor:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def process(self, frame):
        self.calls += 1
        if self.fail:
            raise RuntimeError("falha simulada")
        return _processed(frame.seq)


class FakeSession:
    def __init__(self, connected: bool = True, accept: bool = True) -> None:
        self.is_connected = connected
        self.accept = accept
        self.frames: list[bytes] = []

    def send_frame(self, jpeg: bytes) -> bool:
        if not self.accept:
            return False
        self.frames.append(jpeg)
        return True


class ImageHeartbeatTest(unittest.IsolatedAsyncioTestCase):
    def _heartbeat(self, *, fps: float, camera, processor=None, session=None):
        settings = SimpleNamespace(
            image_send_fps=fps,
            frame_stale_sec=3.0,
            log_each_frame=False,
        )
        diag = Diagnostics()
        heartbeat = ImageHeartbeat(
            camera,
            processor or FakeProcessor(),
            session or FakeSession(),
            settings,
            diag,
        )
        return heartbeat, diag

    async def test_caps_send_rate_at_one_fps(self) -> None:
        heartbeat, _ = self._heartbeat(fps=5.0, camera=FakeCamera())
        self.assertEqual(heartbeat.effective_fps, 1.0)
        heartbeat, _ = self._heartbeat(fps=0.25, camera=FakeCamera())
        self.assertAlmostEqual(heartbeat.effective_fps, 0.25)

    async def test_skips_when_disconnected(self) -> None:
        heartbeat, diag = self._heartbeat(
            fps=1.0, camera=FakeCamera(_frame(1)), session=FakeSession(connected=False)
        )
        await heartbeat._tick()
        self.assertEqual(diag.heartbeat_skips["disconnected"], 1)
        self.assertEqual(diag.sent_frames, 0)

    async def test_skips_when_no_frame_or_stale(self) -> None:
        heartbeat, diag = self._heartbeat(fps=1.0, camera=FakeCamera(None))
        await heartbeat._tick()
        self.assertEqual(diag.heartbeat_skips["no_frame"], 1)

        stale = FakeCamera(_frame(2, age=10.0))
        heartbeat, diag = self._heartbeat(fps=1.0, camera=stale)
        await heartbeat._tick()
        self.assertEqual(diag.heartbeat_skips["no_frame"], 1)

    async def test_sends_only_new_frames(self) -> None:
        camera = FakeCamera(_frame(7))
        processor = FakeProcessor()
        session = FakeSession()
        heartbeat, diag = self._heartbeat(
            fps=1.0, camera=camera, processor=processor, session=session
        )
        await heartbeat._tick()
        self.assertEqual(processor.calls, 1)
        self.assertEqual(len(session.frames), 1)
        self.assertEqual(heartbeat.last_sent_seq, 7)
        # submitted_frames e contabilizado pelo GeminiLiveClient real; aqui
        # validamos o contrato do heartbeat (frame novo -> um envio).
        self.assertEqual(len(session.frames), 1)

        # Mesmo frame no proximo ciclo: nao reprocessa nem reenvia.
        await heartbeat._tick()
        self.assertEqual(processor.calls, 1)
        self.assertEqual(len(session.frames), 1)
        self.assertEqual(diag.heartbeat_skips["same_frame"], 1)

        # Frame novo volta a ser enviado.
        camera.frame = _frame(8)
        await heartbeat._tick()
        self.assertEqual(processor.calls, 2)
        self.assertEqual(len(session.frames), 2)
        self.assertEqual(heartbeat.last_sent_seq, 8)

    async def test_not_submitted_does_not_mark_frame_as_sent(self) -> None:
        camera = FakeCamera(_frame(3))
        session = FakeSession(accept=False)
        heartbeat, diag = self._heartbeat(fps=1.0, camera=camera, session=session)
        await heartbeat._tick()
        self.assertEqual(heartbeat.last_sent_seq, -1)
        self.assertEqual(diag.heartbeat_skips["not_submitted"], 1)

    async def test_processor_error_is_counted_and_does_not_crash(self) -> None:
        heartbeat, diag = self._heartbeat(
            fps=1.0, camera=FakeCamera(_frame(4)), processor=FakeProcessor(fail=True)
        )
        await heartbeat._tick()
        self.assertEqual(diag.errors["process"], 1)
        self.assertEqual(diag.sent_frames, 0)


if __name__ == "__main__":
    unittest.main()
