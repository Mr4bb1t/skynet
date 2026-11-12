# ==============================================================================
# MÓDULO DE CONTROLE DE MOVIMENTO PTZ (YOOSEE / XM ONVIF NATIVO)
#
# Firmware Yoosee/XM: cada pacote ContinuousMove dispara UM passo fixo do
# motor stepper. Para movimento suave e contínuo, pacotes são encadeados em
# cadência fixa (keepalive a cada ~150ms). Ao final, velocidade (0,0) freia.
#
# Modos de uso:
#   step("right")          → 1 passo discreto (controle manual WASD)
#   pan_degrees(45)        → giro suave de 45° com heartbeat contínuo
#   smooth_move(1,0, 2.0)  → movimento suave por 2 segundos
# ==============================================================================
import base64
import hashlib
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from xml.sax.saxutils import escape

import config

# Cadência do heartbeat: intervalo entre pacotes ContinuousMove encadeados.
# Valor original calibrado do trem de pulsos: 25ms funcionava para manter
# encadeamento, mas era frágil com TCP RST. Com conexão limpa, 150ms garante
# movimento suave sem sobrecarregar a câmera.
HEARTBEAT_INTERVAL = float(getattr(config, "PTZ_HEARTBEAT_SEC", 0.150))

# Duração de um passo discreto (para step() manual)
STEP_DURATION_SEC = float(getattr(config, "PTZ_STEP_DURATION_SEC", 0.22))

# Timeout de conexão TCP com a câmera
CONNECT_TIMEOUT = float(getattr(config, "PTZ_CONNECT_TIMEOUT_SEC", 1.5))

# Velocidade angular calibrada (graus por segundo de movimento contínuo)
# Calibração original: 160 passos = 180° em ~4s → ~45°/s
# Ajustar conforme medição real da câmera
PAN_DEG_PER_SEC = float(getattr(config, "PTZ_PAN_DEG_PER_SEC", 45.0))
TILT_DEG_PER_SEC = float(getattr(config, "PTZ_TILT_DEG_PER_SEC", 35.0))


class PTZController:
    """Controlador PTZ com heartbeat contínuo para câmeras Yoosee/XM ONVIF."""

    def __init__(
        self,
        host: str = config.CAMERA_IP,
        port: int = config.CAMERA_ONVIF_PORT,
        profile_token: str = config.ONVIF_PROFILE_TOKEN,
        username: Optional[str] = config.CAMERA_USER,
        password: Optional[str] = config.CAMERA_PASS,
        step_duration: float = STEP_DURATION_SEC,
    ):
        self.host = host
        self.port = port
        self.token = profile_token
        self.username = username
        self.password = password
        self.step_duration = step_duration

        self.last_steps = 0
        self.last_degrees = 0.0
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None

    @property
    def use_auth(self) -> bool:
        return bool(self.username and self.password)

    # ------------------------------------------------------------------ auth
    def _security_header(self) -> str:
        """Header WS-Security UsernameToken/PasswordDigest do ONVIF."""
        if not self.use_auth:
            return ""
        nonce_bytes = os.urandom(16)
        nonce = base64.b64encode(nonce_bytes).decode("utf-8")
        created = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        digest = base64.b64encode(
            hashlib.sha1(
                nonce_bytes + created.encode("utf-8") + self.password.encode("utf-8")
            ).digest()
        ).decode("utf-8")
        return f"""
  <soap:Header>
    <Security soap:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <UsernameToken>
        <Username>{escape(self.username)}</Username>
        <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
        <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</Nonce>
        <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
      </UsernameToken>
    </Security>
  </soap:Header>"""

    # ----------------------------------------------------------------- pacotes
    def _build_move_packet(self, x: float, y: float) -> bytes:
        security = self._security_header()
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl"
               xmlns:tt="http://www.onvif.org/ver10/schema">{security}
   <soap:Body>
      <ptz:ContinuousMove>
         <ptz:ProfileToken>{self.token}</ptz:ProfileToken>
         <ptz:Velocity><tt:PanTilt x="{x:.2f}" y="{y:.2f}"/></ptz:Velocity>
      </ptz:ContinuousMove>
   </soap:Body>
</soap:Envelope>""".encode("utf-8")

        head = (
            f"POST /onvif/ptz_service HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Content-Type: application/soap+xml; charset=utf-8; "
            f'action="http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove"\r\n'
            f"Content-Length: {len(envelope)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        return head + envelope

    # ------------------------------------------------------------------- rede
    def _send_packet(self, pkt: bytes) -> bool:
        """Envia um pacote SOAP via TCP e retorna True se recebeu resposta."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(CONNECT_TIMEOUT)
        try:
            s.connect((self.host, self.port))
            s.sendall(pkt)
            try:
                resp = s.recv(1024)
                return b"200" in resp or b"Response" in resp
            except Exception:
                return True  # enviou, timeout na resposta é OK
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    # --------------------------------------------------------- movimento suave
    def smooth_move(self, x: float, y: float, duration: float,
                    interval: float = HEARTBEAT_INTERVAL) -> bool:
        """Movimento suave contínuo: encadeia pacotes ContinuousMove como
        heartbeat a cada `interval` segundos por `duration` total.

        O firmware executa 1 passo por pacote. Encadeando pacotes a cada 150ms,
        o motor nunca para e o movimento é contínuo e fluido.
        Ao final, envia velocidade (0,0) para frear.
        """
        self._stop_evt.clear()
        pkt_move = self._build_move_packet(x, y)
        pkt_zero = self._build_move_packet(0.0, 0.0)

        t_start = time.perf_counter()
        t_end = t_start + duration
        sent = 0

        # Primeiro pacote imediato
        if not self._send_packet(pkt_move):
            return False
        sent += 1

        # Heartbeat: reenvia pacotes até completar a duração
        while time.perf_counter() < t_end:
            if self._stop_evt.is_set():
                break
            # Dormir até o próximo heartbeat ou até o fim, o que for menor
            remaining = t_end - time.perf_counter()
            sleep_time = min(interval, remaining)
            if sleep_time > 0:
                time.sleep(sleep_time)

            if self._stop_evt.is_set():
                break

            # Se ainda tem tempo, manda outro pulso
            if time.perf_counter() < t_end:
                self._send_packet(pkt_move)
                sent += 1

        # Freia: velocidade zero
        self._send_packet(pkt_zero)
        return sent > 0

    def smooth_move_async(self, x: float, y: float, duration: float,
                          interval: float = HEARTBEAT_INTERVAL):
        """Lança smooth_move em thread separada (não-bloqueante)."""
        self.halt()
        self._stop_evt.clear()
        th = threading.Thread(
            target=self.smooth_move,
            args=(x, y, duration, interval),
            name="ptz-smooth",
            daemon=True,
        )
        self._worker = th
        th.start()

    # --------------------------------------------------------- parada
    def stop(self) -> bool:
        """Para qualquer movimento em andamento."""
        self._stop_evt.set()
        pkt_zero = self._build_move_packet(0.0, 0.0)
        return self._send_packet(pkt_zero)

    def halt(self):
        """Para movimento e aguarda thread encerrar."""
        self._stop_evt.set()
        th = self._worker
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=2.0)
        self._worker = None

    release = halt

    # --------------------------------------------------------- passo discreto
    def single_step(self, x: float, y: float, duration: Optional[float] = None) -> bool:
        """Exatamente 1 passo mecânico: move → aguarda → para."""
        dur = duration if duration is not None else self.step_duration
        pkt_move = self._build_move_packet(x, y)
        pkt_zero = self._build_move_packet(0.0, 0.0)

        if not self._send_packet(pkt_move):
            return False

        t_end = time.perf_counter() + dur
        while time.perf_counter() < t_end:
            if self._stop_evt.is_set():
                break
            time.sleep(0.01)

        self._send_packet(pkt_zero)
        return True

    def step(self, direction: str, count: int = 1) -> bool:
        """Passos discretos (controle manual WASD). Para 1 passo, usa
        single_step. Para múltiplos, encadeia via smooth_move."""
        d = direction.lower().strip()
        table = {
            "left": (-1.0, 0.0), "esquerda": (-1.0, 0.0),
            "right": (1.0, 0.0), "direita": (1.0, 0.0),
            "up": (0.0, 1.0), "cima": (0.0, 1.0),
            "down": (0.0, -1.0), "baixo": (0.0, -1.0),
        }
        if d not in table:
            return False

        x, y = table[d]
        self._stop_evt.clear()

        if count <= 1:
            return self.single_step(x, y)
        else:
            # Múltiplos passos = movimento suave encadeado
            duration = count * HEARTBEAT_INTERVAL
            return self.smooth_move(x, y, duration=duration)

    # --------------------------------------------------------- graus (API principal)
    def pan_degrees(self, degrees: float, speed: float = config.DEFAULT_CRUISE_SPEED) -> bool:
        """Giro horizontal suave. Positivo = direita, negativo = esquerda.

        Calcula duração baseado na velocidade angular calibrada e executa
        movimento contínuo com heartbeat.
        """
        if abs(degrees) < 0.5:
            return True

        deg_abs = abs(degrees)
        vx = 1.0 if degrees > 0 else -1.0

        # Duração calibrada: graus / velocidade_angular
        duration = max(HEARTBEAT_INTERVAL, deg_abs / PAN_DEG_PER_SEC)

        self.last_degrees = degrees
        self.last_steps = max(1, int(round(duration / HEARTBEAT_INTERVAL)))

        return self.smooth_move(vx, 0.0, duration=duration)

    def tilt_degrees(self, degrees: float, speed: float = config.DEFAULT_CRUISE_SPEED) -> bool:
        """Inclinação vertical suave. Positivo = cima, negativo = baixo."""
        if abs(degrees) < 0.5:
            return True

        deg_abs = abs(degrees)
        vy = 1.0 if degrees > 0 else -1.0

        duration = max(HEARTBEAT_INTERVAL, deg_abs / TILT_DEG_PER_SEC)

        self.last_degrees = degrees
        self.last_steps = max(1, int(round(duration / HEARTBEAT_INTERVAL)))

        return self.smooth_move(0.0, vy, duration=duration)

    def move_steps(self, steps: float, axis: str = "pan",
                   speed: float = config.DEFAULT_CRUISE_SPEED) -> bool:
        """Compatível com tracker.py e interfaces existentes."""
        n = int(abs(round(steps)))
        if n == 0:
            return True
        sgn = 1.0 if steps > 0 else -1.0
        self.last_steps = n

        if axis.lower() == "pan":
            deg = sgn * n * config.DEGREES_PER_STEP_PAN
            return self.pan_degrees(deg, speed)
        else:
            deg = sgn * n * config.DEGREES_PER_STEP_TILT
            return self.tilt_degrees(deg, speed)

    # --------------------------------------------------------- atalhos
    def pan_left(self, degrees: Optional[float] = None) -> bool:
        return self.pan_degrees(-abs(degrees)) if degrees is not None else self.step("left")

    def pan_right(self, degrees: Optional[float] = None) -> bool:
        return self.pan_degrees(abs(degrees)) if degrees is not None else self.step("right")

    def tilt_up(self, degrees: Optional[float] = None) -> bool:
        return self.tilt_degrees(abs(degrees)) if degrees is not None else self.step("up")

    def tilt_down(self, degrees: Optional[float] = None) -> bool:
        return self.tilt_degrees(-abs(degrees)) if degrees is not None else self.step("down")

    # --------------------------------------------------------- bloqueante legado
    def stream_pulses(self, x: float, y: float, count: int,
                      interval: Optional[float] = None) -> bool:
        """Compatibilidade: encadeia `count` pulsos via smooth_move."""
        if count <= 0:
            return True
        iv = interval if interval else HEARTBEAT_INTERVAL
        return self.smooth_move(x, y, duration=count * iv, interval=iv)

    def run_for(self, x: float, y: float, seconds: float,
                interval: Optional[float] = None) -> bool:
        """Compatibilidade: movimento contínuo por N segundos."""
        iv = interval if interval else HEARTBEAT_INTERVAL
        return self.smooth_move(x, y, duration=seconds, interval=iv)

    def hold(self, x: float, y: float, interval: Optional[float] = None):
        """Movimento contínuo até halt(): lança em thread."""
        iv = interval if interval else HEARTBEAT_INTERVAL
        self.smooth_move_async(x, y, duration=3600.0, interval=iv)
