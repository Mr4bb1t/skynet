"""Preparacao local das imagens antes do envio ao Gemini Live.

Responsabilidades:
  1. receber o frame cru da camera;
  2. verificar dimensoes;
  3. preservar a proporcao;
  4. reduzir (nunca ampliar) para a resolucao alvo;
  5. converter para JPEG com qualidade configuravel;
  6. entregar bytes prontos para transmissao.

IMPORTANTE: o redimensionamento local e o `media_resolution` da API sao
controles independentes. Este modulo reduz bytes/CPU no TV Box; o
`media_resolution` controla tokens e resolucao de processamento no Gemini.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from .camera_capture import Frame
from .config import IMAGE_PRESETS
from .diagnostics import Diagnostics

LOG = logging.getLogger("vision.processor")

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:  # pragma: no cover - Pillow antigo
    _RESAMPLE = Image.BILINEAR


@dataclass(frozen=True)
class ProcessedFrame:
    jpeg: bytes
    orig_width: int
    orig_height: int
    width: int
    height: int
    quality: int
    seq: int
    captured_at: float
    convert_ms: float
    resize_ms: float
    encode_ms: float
    total_ms: float

    @property
    def jpeg_kb(self) -> float:
        return len(self.jpeg) / 1024.0

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.captured_at


class ImageProcessor:
    """Redimensiona e comprime frames preservando a proporcao original."""

    def __init__(
        self,
        *,
        max_width: int = 640,
        max_height: int = 480,
        jpeg_quality: int = 75,
        preset: str = "BALANCED",
        diagnostics: Optional[Diagnostics] = None,
    ) -> None:
        if max_width <= 0 or max_height <= 0:
            raise ValueError("max_width e max_height devem ser > 0.")
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality deve estar entre 1 e 100.")
        self.max_width = int(max_width)
        self.max_height = int(max_height)
        self.jpeg_quality = int(jpeg_quality)
        self.preset = preset.upper()
        self.diag = diagnostics or Diagnostics()

    @classmethod
    def from_settings(cls, settings, diagnostics: Optional[Diagnostics] = None) -> "ImageProcessor":
        return cls(
            max_width=settings.image_max_width,
            max_height=settings.image_max_height,
            jpeg_quality=settings.jpeg_quality,
            preset=settings.image_preset,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------
    def process(self, frame: Frame) -> ProcessedFrame:
        """Processa um Frame BGR24 cru vindo da camera."""
        t0 = time.perf_counter()
        image = Image.frombytes(
            "RGB", (frame.width, frame.height), frame.data, "raw", "BGR"
        )
        t_convert = time.perf_counter()
        return self._encode(
            image=image,
            seq=frame.seq,
            captured_at=frame.captured_at,
            t0=t0,
            t_convert=t_convert,
            orig_width=frame.width,
            orig_height=frame.height,
        )

    def process_image(
        self,
        image: Image.Image,
        *,
        seq: int = 0,
        captured_at: Optional[float] = None,
    ) -> ProcessedFrame:
        """Processa uma PIL.Image (usado em testes com arquivo JPEG)."""
        t0 = time.perf_counter()
        if image.mode != "RGB":
            image = image.convert("RGB")
        image = image.copy()
        orig_width, orig_height = image.size
        t_convert = time.perf_counter()
        return self._encode(
            image=image,
            seq=seq,
            captured_at=time.monotonic() if captured_at is None else captured_at,
            t0=t0,
            t_convert=t_convert,
            orig_width=orig_width,
            orig_height=orig_height,
        )

    def process_jpeg_bytes(self, jpeg_bytes: bytes, *, seq: int = 0) -> ProcessedFrame:
        """Conveniencia para testes: decodifica um JPEG e reprocessa."""
        with Image.open(io.BytesIO(jpeg_bytes)) as img:
            img.load()
            return self.process_image(img, seq=seq)

    # ------------------------------------------------------------------
    # Interno
    # ------------------------------------------------------------------
    def _encode(
        self,
        *,
        image: Image.Image,
        seq: int,
        captured_at: float,
        t0: float,
        t_convert: float,
        orig_width: int,
        orig_height: int,
    ) -> ProcessedFrame:
        # thumbnail: preserva proporcao, nunca faz upscale e nao corta.
        image.thumbnail((self.max_width, self.max_height), _RESAMPLE)
        t_resize = time.perf_counter()

        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=self.jpeg_quality,
            optimize=False,
            progressive=False,
        )
        jpeg = buffer.getvalue()
        t_encode = time.perf_counter()

        out_width, out_height = image.size
        result = ProcessedFrame(
            jpeg=jpeg,
            orig_width=orig_width,
            orig_height=orig_height,
            width=out_width,
            height=out_height,
            quality=self.jpeg_quality,
            seq=seq,
            captured_at=captured_at,
            convert_ms=(t_convert - t0) * 1000.0,
            resize_ms=(t_resize - t_convert) * 1000.0,
            encode_ms=(t_encode - t_resize) * 1000.0,
            total_ms=(t_encode - t0) * 1000.0,
        )
        self.diag.record_processed(
            orig_w=orig_width,
            orig_h=orig_height,
            out_w=out_width,
            out_h=out_height,
            jpeg_bytes=len(jpeg),
            resize_ms=result.resize_ms,
            encode_ms=result.encode_ms,
        )
        return result

    def describe(self) -> str:
        return (
            f"preset={self.preset} max={self.max_width}x{self.max_height} "
            f"jpeg_quality={self.jpeg_quality} upscale=off crop=off"
        )


def preset_size(preset: str) -> int:
    """Largura maxima de um preset (LOW/BALANCED/DETAIL)."""
    key = preset.upper()
    if key not in IMAGE_PRESETS:
        raise ValueError(f"Preset invalido: {preset!r}. Use {sorted(IMAGE_PRESETS)}.")
    return IMAGE_PRESETS[key]
