"""Testes de redimensionamento e compressao JPEG (Teste 2)."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from vision.camera_capture import Frame
from vision.image_processor import ImageProcessor, preset_size


def _image(width: int, height: int, color=(120, 80, 40)) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def _decode(jpeg: bytes) -> Image.Image:
    with Image.open(io.BytesIO(jpeg)) as img:
        img.load()
        return img.copy()


class ImageProcessorTest(unittest.TestCase):
    def test_presets_from_640x360(self) -> None:
        source = _image(640, 360)
        expected = {
            "LOW": (320, 180),
            "BALANCED": (640, 360),
            "DETAIL": (640, 360),  # nao amplia e respeita max_height=480
        }
        for preset, size in expected.items():
            with self.subTest(preset=preset):
                processor = ImageProcessor(
                    max_width=preset_size(preset), max_height=480, preset=preset
                )
                result = processor.process_image(source)
                self.assertEqual((result.width, result.height), size)
                self.assertEqual(result.orig_width, 640)
                self.assertEqual(result.orig_height, 360)
                self.assertTrue(result.jpeg.startswith(b"\xff\xd8"))

    def test_presets_from_1280x720(self) -> None:
        source = _image(1280, 720)
        expected = {
            "LOW": (320, 180),
            "BALANCED": (640, 360),
            "DETAIL": (853, 480),
        }
        for preset, size in expected.items():
            with self.subTest(preset=preset):
                processor = ImageProcessor(
                    max_width=preset_size(preset), max_height=480, preset=preset
                )
                result = processor.process_image(source)
                self.assertEqual((result.width, result.height), size)

    def test_never_upscales(self) -> None:
        source = _image(200, 100)
        processor = ImageProcessor(max_width=640, max_height=480)
        result = processor.process_image(source)
        self.assertEqual((result.width, result.height), (200, 100))

    def test_preserves_aspect_ratio_without_crop(self) -> None:
        source = _image(1000, 333)
        processor = ImageProcessor(max_width=320, max_height=480)
        result = processor.process_image(source)
        self.assertLessEqual(result.width, 320)
        self.assertLessEqual(result.height, 480)
        original_ratio = 1000 / 333
        output_ratio = result.width / result.height
        self.assertAlmostEqual(original_ratio, output_ratio, delta=0.02)
        self.assertGreater(result.width * result.height, 0)

    def test_jpeg_quality_changes_size(self) -> None:
        source = _image(640, 360, (30, 140, 210))
        low = ImageProcessor(max_width=640, max_height=480, jpeg_quality=30).process_image(
            source
        )
        high = ImageProcessor(
            max_width=640, max_height=480, jpeg_quality=90
        ).process_image(source)
        self.assertTrue(low.jpeg.startswith(b"\xff\xd8"))
        self.assertTrue(high.jpeg.startswith(b"\xff\xd8"))
        self.assertGreater(len(high.jpeg), len(low.jpeg))

    def test_bgr_frame_is_converted_to_rgb(self) -> None:
        # BGR24: vermelho puro = (0, 0, 255). Quadrantes 16x16 evitam que o
        # subsampling de croma do JPEG misture cores de blocos vizinhos.
        colors = {
            "red": bytes([0, 0, 255]),
            "blue": bytes([255, 0, 0]),
            "green": bytes([0, 255, 0]),
            "white": bytes([255, 255, 255]),
        }
        width = height = 32
        rows = []
        for y in range(height):
            top = y < height // 2
            row = bytearray()
            for x in range(width):
                left = x < width // 2
                key = (
                    "red"
                    if top and left
                    else "blue"
                    if top
                    else "green"
                    if left
                    else "white"
                )
                row.extend(colors[key])
            rows.append(bytes(row))
        frame = Frame(
            data=b"".join(rows), width=width, height=height, seq=1, captured_at=0.0
        )
        processor = ImageProcessor(max_width=width, max_height=height, jpeg_quality=95)
        result = processor.process(frame)
        decoded = _decode(result.jpeg).convert("RGB")
        self.assertEqual(decoded.size, (width, height))
        red = decoded.getpixel((8, 8))
        blue = decoded.getpixel((24, 8))
        green = decoded.getpixel((8, 24))
        white = decoded.getpixel((24, 24))
        self.assertGreater(red[0], 200)
        self.assertLess(red[1], 80)
        self.assertGreater(blue[2], 200)
        self.assertLess(blue[0], 80)
        self.assertGreater(green[1], 200)
        self.assertGreater(min(white), 200)

    def test_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            ImageProcessor(max_width=0, max_height=480)
        with self.assertRaises(ValueError):
            ImageProcessor(max_width=640, max_height=480, jpeg_quality=0)
        with self.assertRaises(ValueError):
            ImageProcessor(max_width=640, max_height=480, jpeg_quality=101)
        with self.assertRaises(ValueError):
            preset_size("HUGE")

    def test_metadata_and_timing_are_recorded(self) -> None:
        processor = ImageProcessor(max_width=320, max_height=240, jpeg_quality=70)
        result = processor.process_image(_image(800, 600))
        self.assertEqual(result.quality, 70)
        self.assertGreaterEqual(result.total_ms, 0.0)
        self.assertGreaterEqual(result.resize_ms, 0.0)
        self.assertGreaterEqual(result.encode_ms, 0.0)
        self.assertGreater(result.jpeg_kb, 0.0)


if __name__ == "__main__":
    unittest.main()
