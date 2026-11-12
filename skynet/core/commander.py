# ==============================================================================
# CENTRAL DE COMANDO DO ROBÔ (ORQUESTRADOR CORE)
# Interface de alto nível para agentes e modelos de IA controlarem a cabeça e visão
# ==============================================================================
import time
from typing import Optional, List, Dict, Any
from camera.ptz import PTZController
from camera.vision import VisionModule
import config

class RobotCommander:
    """Central de comando que unifica movimento de cabeça fluido e visão para a IA."""

    def __init__(self):
        self.ptz = PTZController()
        self.vision = VisionModule()

    def turn(self, degrees: float, speed: float = config.DEFAULT_CRUISE_SPEED) -> bool:
        """
        Gira a cabeça horizontalmente por um ângulo em GRAUS num movimento contínuo de alta velocidade.
        :param degrees: Positivo (+) = Direita, Negativo (-) = Esquerda (ex: 180, -90, 45)
        :param speed: Fator de velocidade
        """
        return self.ptz.pan_degrees(degrees=degrees, speed=speed)

    def pitch(self, degrees: float, speed: float = config.DEFAULT_CRUISE_SPEED) -> bool:
        """
        Inclina a cabeça verticalmente por um ângulo em GRAUS num movimento contínuo de alta velocidade.
        :param degrees: Positivo (+) = Cima, Negativo (-) = Baixo (ex: +30, -20)
        :param speed: Fator de velocidade
        """
        return self.ptz.tilt_degrees(degrees=degrees, speed=speed)

    def look_at(self, pan_degrees: float = 0.0, tilt_degrees: float = 0.0) -> bool:
        """Move a cabeça sequencialmente nos eixos em graus."""
        ok_pan = self.turn(pan_degrees) if abs(pan_degrees) >= 0.5 else True
        ok_tilt = self.pitch(tilt_degrees) if abs(tilt_degrees) >= 0.5 else True
        return ok_pan and ok_tilt

    def nudge(self, direction: str, steps: int = 1) -> bool:
        """Micro-ajuste manual (toque fino) para centralização de alvo."""
        return self.ptz.step(direction, count=max(1, steps))

    def see(self, high_res: bool = False) -> Optional[str]:
        """
        Captura o que o robô está vendo no momento exato.
        Retorna o caminho do arquivo de imagem salvo em disco.
        """
        return self.vision.capture_snapshot(high_res=high_res)

    def get_vision_frame(self) -> Optional[bytes]:
        """
        Retorna instantaneamente (< 1ms) os bytes do frame atual na memória RAM.
        Usado para passar direto para modelos de visão (OpenCV, YOLO, Gemini Vision).
        """
        return self.vision.get_latest_frame_bytes()

    def turn_and_see(self, degrees: float, high_res: bool = False) -> Optional[str]:
        """Gira por N graus e imediatamente captura o que enxerga."""
        self.turn(degrees)
        time.sleep(0.15)
        return self.see(high_res=high_res)

    def scan_panorama(self, total_degrees: float = 180.0, stops: int = 3) -> List[Dict[str, Any]]:
        """
        Executa uma varredura panorâmica de N graus com paradas para captura visual.
        """
        results = []
        step_angle = total_degrees / max(1, (stops - 1))
        half = total_degrees / 2.0

        # Gira para o extremo esquerdo
        self.turn(-half)
        time.sleep(0.2)
        results.append({"angle_offset": -half, "image": self.see()})

        # Percorre o arco em passos de graus contínuos
        current = -half
        for _ in range(stops - 1):
            self.turn(step_angle)
            current += step_angle
            time.sleep(0.2)
            results.append({"angle_offset": current, "image": self.see()})

        # Retorna para o centro
        self.turn(-current)
        return results
