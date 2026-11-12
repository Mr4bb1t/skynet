"""Ferramenta de PTZ: move a cabeça da câmera em graus.

O controlador real fica em `camera/ptz.py` (legado), que envia pulsos ONVIF
calibrados. Esta ferramenta apenas valida os ângulos, chama o controlador e
devolve um resultado textual para o modelo.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from google.genai import types

LOG = logging.getLogger("vision.tool.ptz")

MOVE_DECLARATION = types.FunctionDeclaration(
    name="move_camera",
    description=(
        "Move sua cabeça e o campo de visão. Use quando quiser olhar em outra "
        "direção, explorar o ambiente, procurar uma pessoa ou objeto, "
        "acompanhar algo, confirmar um movimento ou por iniciativa própria. "
        "O movimento é relativo (não há posição absoluta) e lento (~1 passo/s); "
        "para voltar, use o ângulo oposto. Você pode chamar várias vezes. "
        "Após mover, uma imagem da nova visão é devolvida automaticamente; "
        "use-a para responder. Só chame capture_image se quiser outro ângulo."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "pan_degrees": types.Schema(
                type=types.Type.NUMBER,
                description=(
                    "Giro horizontal em graus. Positivo = direita, negativo = "
                    "esquerda. Faixa: -180 a 180."
                ),
            ),
            "tilt_degrees": types.Schema(
                type=types.Type.NUMBER,
                description=(
                    "Inclinação vertical em graus. Positivo = cima, negativo = "
                    "baixo. Faixa: -90 a 90."
                ),
            ),
        },
    ),
)

STOP_DECLARATION = types.FunctionDeclaration(
    name="stop_camera",
    description=(
        "Interrompe o movimento PTZ atual. Use quando quiser parar, mudar de "
        "direção ou encerrar uma exploração."
    ),
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)

PAN_LIMIT = 180.0
TILT_LIMIT = 90.0


class PTZTool:
    """Wrapper seguro em torno do controlador PTZ legado."""

    def __init__(
        self,
        controller=None,
        *,
        enabled: bool = True,
        settings=None,
    ) -> None:
        self.controller = controller
        self.error: Optional[str] = None
        self.enabled = enabled
        if not enabled:
            self.error = "PTZ desativado por --no-ptz"
            return
        if controller is not None:
            return

        try:
            # camera/ptz.py é um pacote legado na raiz do projeto.
            project_root = Path(__file__).resolve().parents[2]
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            import config as legacy_config
            from camera.ptz import PTZController

            host = legacy_config.CAMERA_IP
            port = legacy_config.CAMERA_ONVIF_PORT
            username = getattr(legacy_config, "CAMERA_USER", "admin")
            password = getattr(legacy_config, "CAMERA_PASS", "")
            interval = 0.8

            if settings is not None:
                interval = float(
                    getattr(settings, "ptz_interval_sec", interval) or interval
                )
                if settings.camera_url:
                    parsed = urlparse(settings.camera_url)
                    host = parsed.hostname or host
                    username = parsed.username or username
                    password = parsed.password or password

            # Permite senha ONVIF separada da senha RTSP.
            username = os.getenv("CAMERA_ONVIF_USER", username)
            password = os.getenv("CAMERA_ONVIF_PASS", password)
            self.controller = PTZController(
                host=host,
                port=port,
                interval=interval,
                username=username,
                password=password,
            )
        except Exception as exc:  # pragma: no cover - depende do hardware/SDK
            self.error = str(exc)
            LOG.warning("PTZ indisponível: %s", exc)

    @property
    def available(self) -> bool:
        return self.controller is not None

    def move(
        self, pan_degrees: float = 0.0, tilt_degrees: float = 0.0
    ) -> tuple[bool, str]:
        if self.controller is None:
            return False, self.error or "PTZ indisponível"

        try:
            pan = max(-PAN_LIMIT, min(PAN_LIMIT, float(pan_degrees or 0.0)))
            tilt = max(-TILT_LIMIT, min(TILT_LIMIT, float(tilt_degrees or 0.0)))
        except (TypeError, ValueError):
            return False, "ângulos inválidos para o PTZ"

        if abs(pan) < 0.5 and abs(tilt) < 0.5:
            return True, "nenhum movimento solicitado"

        try:
            ok_pan = self.controller.pan_degrees(pan) if abs(pan) >= 0.5 else True
            ok_tilt = self.controller.tilt_degrees(tilt) if abs(tilt) >= 0.5 else True
            ok = bool(ok_pan and ok_tilt)
            steps = getattr(self.controller, "last_steps", 0)
            last_degrees = getattr(self.controller, "last_degrees", 0.0)
            stats = getattr(self.controller, "last_stats", None)
            sent = acked = 0
            if stats is not None:
                try:
                    summary = stats.summary()
                    sent = int(summary.get("sent", 0))
                    acked = int(summary.get("acked", 0))
                except Exception:
                    pass
                # Se o controlador enviou pulsos mas a câmera não confirmou
                # nenhum, não reporte sucesso falso.
                if sent > 0 and acked < max(1, int(sent * 0.8)):
                    ok = False

            if ok:
                detail = (
                    f"pan={pan:+.1f}°, tilt={tilt:+.1f}°; "
                    f"último eixo: {steps} passos ({last_degrees:+.1f}°); "
                    f"pulsos={sent} confirmados={acked}"
                )
            elif sent > 0:
                detail = (
                    f"pan={pan:+.1f}°, tilt={tilt:+.1f}°; "
                    f"a câmera não confirmou os pulsos "
                    f"(enviados={sent}, confirmados={acked})"
                )
            else:
                detail = (
                    f"pan={pan:+.1f}°, tilt={tilt:+.1f}°; "
                    "falha ao enviar os pulsos PTZ (verifique a câmera/ONVIF)"
                )
            return ok, detail
        except Exception as exc:  # pragma: no cover - hardware
            LOG.warning("Falha ao mover PTZ: %s", exc)
            return False, f"falha no PTZ: {exc}"

    def stop(self) -> tuple[bool, str]:
        if self.controller is None:
            return False, self.error or "PTZ indisponível"
        try:
            self.controller.stop()
            return True, "movimento PTZ interrompido"
        except Exception as exc:  # pragma: no cover - hardware
            LOG.warning("Falha ao parar PTZ: %s", exc)
            return False, f"falha ao parar PTZ: {exc}"
