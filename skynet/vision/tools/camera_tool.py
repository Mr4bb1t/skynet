"""Ferramenta de câmera: captura uma foto atual sob demanda."""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from PIL import Image
from google.genai import types

from ..camera_capture import CameraCapture, CameraError
from ..config import Settings
from ..diagnostics import Diagnostics
from ..image_processor import ImageProcessor
from ..person_memory import FaceAnalysis, PersonMemory

LOG = logging.getLogger("vision.tool.camera")

CAPTURE_DECLARATION = types.FunctionDeclaration(
    name="capture_image",
    description=(
        "Abre seus olhos e captura a cena atual. Use sempre que quiser ou "
        "precisar ver algo: para responder, confirmar, explorar o ambiente, "
        "acompanhar uma mudança, verificar o resultado de um movimento ou "
        "quando o usuário pedir. Você decide quando usar."
    ),
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)


class CameraTool:
    """Câmera mantida aquecida em segundo plano; fotos sob demanda."""

    def __init__(
        self,
        settings: Settings,
        *,
        source: Optional[str] = None,
        disabled: bool = False,
        save_photo: Optional[str] = None,
        prewarm: bool = True,
    ) -> None:
        self.settings = settings
        self.save_photo = save_photo
        self.disabled = disabled
        self.camera: Optional[CameraCapture] = None
        self.last_seq = -1
        self.diag = Diagnostics()
        self.processor = ImageProcessor.from_settings(settings, self.diag)
        self.memory = PersonMemory(
            settings.person_db_path,
            settings.person_photo_dir,
            threshold=settings.face_match_threshold,
            enabled=settings.person_memory_enabled,
        )
        self.last_faces: list = []
        self.last_jpeg: Optional[bytes] = None
        self.last_capture_at = 0.0
        self.url = source or settings.camera_url
        self._start_error = ""
        self._retry_after = 0.0

        if disabled:
            self._start_error = "câmera desativada por --no-camera"
            LOG.info("Câmera desativada por --no-camera.")
            return
        if not self.url:
            self._start_error = "CAMERA_URL não definida"
            LOG.warning(
                "CAMERA_URL não definida; o chat funcionará, mas capture_image "
                "retornará erro."
            )
            self.disabled = True
            return

        # Aquece o encoder/resampler do Pillow para a primeira foto.
        try:
            self.processor.process_image(Image.new("RGB", (320, 180), (0, 0, 0)))
        except Exception:
            pass
        # Deixa a câmera abrindo enquanto o usuário conversa; a primeira
        # captura já encontra a câmera aquecida.
        if prewarm:
            self._create_camera()

    def _create_camera(self) -> bool:
        if self.disabled:
            return False
        if self.camera is not None and self.camera.is_running:
            return True
        if time.monotonic() < self._retry_after:
            return False
        if self.camera is not None:
            self.camera.stop()
            self.camera = None

        try:
            self.camera = CameraCapture(
                self.url,
                transport=self.settings.camera_transport,
                fps=self.settings.capture_fps,
                max_width=self.settings.capture_max_width,
                fallback_width=self.settings.capture_width,
                fallback_height=self.settings.capture_height,
                probe_timeout_sec=self.settings.camera_probe_timeout_sec,
                read_timeout_sec=self.settings.camera_read_timeout_sec,
                reconnect_base_sec=self.settings.capture_reconnect_base_sec,
                reconnect_max_sec=self.settings.capture_reconnect_max_sec,
                skip_probe=self.settings.camera_skip_probe,
                diagnostics=self.diag,
            )
            self.camera.start()
            self._start_error = ""
            LOG.info("Câmera iniciada em segundo plano (prewarm).")
            return True
        except CameraError as exc:
            LOG.warning("Não foi possível iniciar a câmera: %s", exc)
            self.camera = None
            self._start_error = f"falha ao iniciar a câmera: {exc}"
            self._retry_after = time.monotonic() + 10.0
            return False

    def _ensure_camera(self) -> bool:
        """Garante a câmera e espera o primeiro frame, sem bloquear o chat."""
        if not self._create_camera():
            return False
        assert self.camera is not None

        # Esta câmera às vezes falha na primeira conexão RTSP e só entrega
        # frame na tentativa seguinte do supervisor; por isso a margem é maior.
        started = time.monotonic()
        deadline = started + 30.0
        while time.monotonic() < deadline:
            if self.camera.get_latest() is not None:
                LOG.info("Câmera pronta para capturas sob demanda.")
                self._start_error = ""
                return True
            # 405/400 persiste = outro cliente (VLC/app) ou RTSP travado.
            # Não faça o usuário esperar 30 s por um erro conhecido.
            if time.monotonic() - started > 8.0:
                stderr = (self.camera.last_stderr or "").lower()
                if "405" in stderr or "400 bad request" in stderr:
                    LOG.warning(
                        "Câmera recusou RTSP (405/400); interrompendo a espera."
                    )
                    break
            time.sleep(0.05)

        stderr = (self.camera.last_stderr or "").lower() if self.camera else ""
        LOG.warning(
            "A câmera não entregou frame; capture_image retornará erro."
        )
        self.camera.stop()
        self.camera = None
        if "405" in stderr or "400 bad request" in stderr:
            self._start_error = (
                "câmera recusou RTSP (405/400): feche o VLC/app que estiver "
                "usando a câmera, aguarde alguns segundos ou reinicie a câmera"
            )
        else:
            self._start_error = (
                "câmera não entregou frame (RTSP/ffmpeg indisponível)"
            )
        self._retry_after = time.monotonic() + 10.0
        return False

    def capture(self) -> tuple[Optional[bytes], str, dict]:
        """Captura o frame mais recente e devolve (JPEG, descrição, payload)."""
        if not self._ensure_camera() or self.camera is None:
            error = self._start_error or "câmera indisponível"
            return (
                None,
                error,
                {"status": "error", "error": error},
            )

        start_seq = self.last_seq
        deadline = time.monotonic() + 3.0
        frame = None
        while time.monotonic() < deadline:
            candidate = self.camera.get_latest()
            if (
                candidate is not None
                and candidate.seq > start_seq
                and candidate.age() < self.settings.frame_stale_sec
            ):
                frame = candidate
                break
            time.sleep(0.05)

        if frame is None:
            # Aceita um frame recente mesmo que o seq não tenha avançado.
            frame = self.camera.get_latest(max_age=self.settings.frame_stale_sec)

        if frame is None:
            return (
                None,
                "sem frame válido da câmera",
                {"status": "error", "error": "sem frame válido da câmera"},
            )

        self.last_seq = frame.seq
        frame_age_ms = frame.age() * 1000.0
        try:
            processed = self.processor.process(frame)
        except Exception as exc:
            LOG.warning("Falha ao processar o frame da câmera: %s", exc)
            return (
                None,
                f"falha ao processar imagem: {exc}",
                {"status": "error", "error": "falha ao processar imagem"},
            )

        if self.save_photo:
            try:
                parent = os.path.dirname(os.path.abspath(self.save_photo))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.save_photo, "wb") as handle:
                    handle.write(processed.jpeg)
            except OSError as exc:
                LOG.warning("Não foi possível salvar a foto: %s", exc)

        self.last_jpeg = processed.jpeg
        self.last_capture_at = time.monotonic()
        analysis: FaceAnalysis = self.memory.analyze_jpeg(processed.jpeg)
        self.last_faces = analysis.faces

        info = (
            f"{processed.width}x{processed.height}, "
            f"{processed.jpeg_kb:.1f} KB, idade={frame_age_ms:.0f} ms, "
            f"rostos={len(analysis.faces)}"
        )
        payload = {
            "status": "ok",
            "width": processed.width,
            "height": processed.height,
            "jpeg_kb": round(processed.jpeg_kb, 1),
            "frame_age_ms": round(frame_age_ms, 1),
            "people": analysis.public.get("people", []),
            "unknown_faces": analysis.public.get("unknown_faces", 0),
            "people_json": analysis.json_text,
        }
        return processed.jpeg, info, payload

    # ------------------------------------------------------------------
    # Memória de pessoas
    # ------------------------------------------------------------------
    def remember_person(
        self,
        name: str,
        nickname: str = "",
        notes: str = "",
        face_index: int = 0,
    ) -> dict:
        if not self.memory.available:
            return {
                "status": "error",
                "error": self.memory.reason or "memória de pessoas indisponível",
            }
        if not self.last_faces or self.last_jpeg is None:
            return {
                "status": "error",
                "error": "capture uma foto com o rosto antes de lembrar",
            }
        if time.monotonic() - self.last_capture_at > 120.0:
            return {
                "status": "error",
                "error": "a última foto é antiga; capture novamente",
            }
        return self.memory.remember(
            name,
            nickname=nickname,
            notes=notes,
            face_index=face_index,
            faces=self.last_faces,
            jpeg_bytes=self.last_jpeg,
        )

    def recall_people(self, query: str) -> dict:
        return self.memory.recall(query)

    def list_people(self) -> dict:
        return self.memory.list_people()

    def forget_person(self, name: str) -> dict:
        return self.memory.forget(name)

    def close(self) -> None:
        if self.camera is not None:
            self.camera.stop()
            self.camera = None
