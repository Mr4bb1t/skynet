"""Módulo de Fontes de Áudio para o Skynet.

Permite alternar entre o microfone da câmera (RTSP/ONVIF) e um microfone local
(USB ou P2 conectado diretamente no TV Box). Para uso com AEC3, o microfone
local é o recomendado: o da câmera adiciona latência/jitter de rede e, em vários
firmwares, abre uma segunda sessão RTSP que pode travar o vídeo.
"""

from __future__ import annotations

import abc
import audioop
import hashlib
import logging
import os
import re
import socket
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlparse

LOG = logging.getLogger("skynet.audio.source")


class BaseAudioSource(abc.ABC):
    """Interface padrão para qualquer fonte de áudio (16kHz, 16-bit, Mono PCM)."""

    @abc.abstractmethod
    def start(self) -> None:
        """Inicia a captura de áudio."""
        pass

    @abc.abstractmethod
    def read_chunk(self, timeout: float = 1.0) -> Optional[bytes]:
        """Lê um bloco de áudio PCM (16kHz, 16-bit, Mono). Retorna None se timeout."""
        pass

    @abc.abstractmethod
    def stop(self) -> None:
        """Encerra a captura de áudio."""
        pass


class CameraAudioSource(BaseAudioSource):
    """Áudio do microfone da câmera via RTSP/RTP (G.711 A-law/u-law ou L16).

    Esta fonte abre uma sessão RTSP própria. Ela é experimental: em câmeras
    cujo RTSP só aceita um cliente, isso pode conflitar com o vídeo. Por isso:
      * autentica TODAS as requisições (DESCRIBE/SETUP/PLAY/GET_PARAMETER/TEARDOWN);
      * lê a resposta RTSP inteira (headers + Content-Length);
      * descobre o track de áudio e o codec pelo SDP (não chuta "track2");
      * faz backoff e para de martelar a câmera quando ela recusa RTSP;
      * reamostra G.711 (8 kHz) para os 16 kHz exigidos pelo Gemini.
    """

    def __init__(
        self,
        camera_ip: str = "192.168.10.14",
        camera_port: int = 554,
        username: str = "admin",
        password: str = "E33426244b",
        stream_path: str = "/onvif1",
        local_bind_ip: str = "192.168.10.1",
        local_bind_port: int = 0,
        gain: float = 5.0,
    ):
        self.camera_ip = camera_ip
        self.camera_port = camera_port
        self.username = username
        self.password = password
        self.stream_path = stream_path
        self.local_bind_ip = local_bind_ip
        self.local_bind_port = int(local_bind_port)
        self.gain = float(gain)

        self._running = False
        self._rtsp_sock: Optional[socket.socket] = None
        self._udp_sock: Optional[socket.socket] = None
        self._session_id: Optional[str] = None
        self._realm: str = ""
        self._nonce: str = ""
        self._cseq = 0
        self._codec: str = "PCMA"
        self._codec_rate: int = 8000
        self._rate_state = None
        self._queue: list[bytes] = []
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # RTSP
    # ------------------------------------------------------------------
    def _next_cseq(self) -> int:
        self._cseq += 1
        return self._cseq

    @staticmethod
    def _status(response: str) -> int:
        match = re.match(r"RTSP/\d\.\d\s+(\d+)", response or "")
        return int(match.group(1)) if match else 0

    def _recv_response(self, sock: socket.socket, timeout: float = 4.0) -> str:
        """Lê uma resposta RTSP completa (cabeçalho + corpo por Content-Length)."""
        sock.settimeout(timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        header, _, body = data.partition(b"\r\n\r\n")
        header_text = header.decode("utf-8", "replace")
        match = re.search(r"(?im)^Content-Length:\s*(\d+)", header_text)
        length = int(match.group(1)) if match else 0
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                break
            body += chunk
        return header_text + "\r\n\r\n" + body.decode("utf-8", "replace")

    def _digest_auth(self, method: str, uri: str, realm: str, nonce: str) -> str:
        ha1 = hashlib.md5(f"{self.username}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"'
        )

    def _request(
        self,
        sock: socket.socket,
        method: str,
        uri: str,
        *,
        auth: Optional[str] = None,
        session: Optional[str] = None,
        extra: Optional[list[str]] = None,
    ) -> str:
        lines = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {self._next_cseq()}",
            "User-Agent: Skynet/1.0",
        ]
        if session:
            lines.append(f"Session: {session}")
        if auth:
            lines.append(f"Authorization: {auth}")
        if extra:
            lines.extend(extra)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._recv_response(sock)

    def _authorization(self, method: str, uri: str) -> Optional[str]:
        if not (self._realm and self._nonce):
            return None
        return self._digest_auth(method, uri, self._realm, self._nonce)

    @staticmethod
    def _parse_sdp_audio(sdp: str, base_url: str, camera_ip: str, camera_port: int):
        """Retorna (track_url, codec, sample_rate) do primeiro m=audio do SDP."""
        in_audio = False
        saw_audio = False
        control = None
        codec = None
        rate = 8000
        payload_type = None

        for raw in sdp.splitlines():
            line = raw.strip()
            if line.startswith("m=audio"):
                in_audio = True
                saw_audio = True
                parts = line.split()
                if len(parts) >= 4 and parts[3].isdigit():
                    payload_type = int(parts[3])
                continue
            if line.startswith("m=") and in_audio:
                break
            if not in_audio:
                continue
            if line.startswith("a=control:"):
                control = line.split(":", 1)[1].strip()
            elif line.startswith("a=rtpmap:"):
                match = re.match(r"a=rtpmap:(\d+)\s+([^/]+)/(\d+)", line)
                if match and (payload_type is None or int(match.group(1)) == payload_type):
                    codec = match.group(2).upper()
                    rate = int(match.group(3))

        if not saw_audio:
            raise RuntimeError("SDP da câmera não tem track de áudio")
        if codec is None:
            # Fallback por payload type estático do RTP.
            codec = {8: "PCMA", 0: "PCMU", 11: "L16"}.get(payload_type or -1)
        if control is None:
            control = "track2"
        if control.startswith("rtsp://"):
            track_url = control
        elif control.startswith("/"):
            track_url = f"rtsp://{camera_ip}:{camera_port}{control}"
        else:
            track_url = f"{base_url.rstrip('/')}/{control}"
        return track_url, (codec or ""), rate

    def _close_session(self) -> None:
        if self._rtsp_sock:
            try:
                base = f"rtsp://{self.camera_ip}:{self.camera_port}{self.stream_path}"
                if self._session_id:
                    self._request(
                        self._rtsp_sock,
                        "TEARDOWN",
                        base,
                        auth=self._authorization("TEARDOWN", base),
                        session=self._session_id,
                    )
            except Exception:
                pass
        for sock in (self._udp_sock, self._rtsp_sock):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        self._udp_sock = None
        self._rtsp_sock = None
        self._session_id = None
        self._rate_state = None

    def _connect_rtsp(self) -> bool:
        try:
            self._close_session()
            self._realm = ""
            self._nonce = ""
            self._cseq = 0

            sock = socket.create_connection((self.camera_ip, self.camera_port), timeout=4.0)
            self._rtsp_sock = sock
            base_url = f"rtsp://{self.camera_ip}:{self.camera_port}{self.stream_path}"

            response = self._request(
                sock, "DESCRIBE", base_url, extra=["Accept: application/sdp"]
            )
            if self._status(response) == 401:
                match_realm = re.search(r'realm="([^"]+)"', response)
                match_nonce = re.search(r'nonce="([^"]+)"', response)
                if not (match_realm and match_nonce):
                    LOG.error("DESCRIBE 401 sem realm/nonce")
                    return False
                self._realm = match_realm.group(1)
                self._nonce = match_nonce.group(1)
                response = self._request(
                    sock,
                    "DESCRIBE",
                    base_url,
                    auth=self._authorization("DESCRIBE", base_url),
                    extra=["Accept: application/sdp"],
                )
            if self._status(response) != 200:
                LOG.error("DESCRIBE recusado pela câmera (%s)", self._status(response))
                return False

            track_url, codec, rate = self._parse_sdp_audio(
                response, base_url, self.camera_ip, self.camera_port
            )
            if codec not in {"PCMA", "PCMU", "L16"}:
                LOG.error("Codec de áudio da câmera não suportado: %s", codec or "desconhecido")
                return False
            self._codec = codec
            self._codec_rate = rate
            self._rate_state = None

            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind((self.local_bind_ip, self.local_bind_port))
            udp.settimeout(0.5)
            self._udp_sock = udp
            client_port = udp.getsockname()[1]
            transport = f"RTP/AVP;unicast;client_port={client_port}-{client_port + 1}"

            response = self._request(
                sock,
                "SETUP",
                track_url,
                auth=self._authorization("SETUP", track_url),
                extra=[f"Transport: {transport}"],
            )
            if self._status(response) != 200:
                LOG.error("SETUP recusado pela câmera (%s)", self._status(response))
                return False
            match_session = re.search(r"(?im)^Session:\s*([^;\r\n]+)", response)
            if not match_session:
                LOG.error("SETUP sem Session ID")
                return False
            self._session_id = match_session.group(1).strip()

            response = self._request(
                sock,
                "PLAY",
                base_url,
                auth=self._authorization("PLAY", base_url),
                session=self._session_id,
            )
            if self._status(response) != 200:
                LOG.error("PLAY recusado pela câmera (%s)", self._status(response))
                return False

            LOG.info(
                "Áudio RTSP da câmera iniciado (%s %d Hz -> 16 kHz, ganho %.1fx).",
                self._codec,
                self._codec_rate,
                self.gain,
            )
            return True
        except Exception as exc:
            LOG.error("Erro ao conectar RTSP da câmera: %s", exc)
            self._close_session()
            return False

    # ------------------------------------------------------------------
    # RTP / captura
    # ------------------------------------------------------------------
    @staticmethod
    def _rtp_payload(packet: bytes) -> Optional[bytes]:
        """Extrai o payload de um pacote RTP, respeitando CSRC/extensão/padding."""
        if len(packet) < 12 or (packet[0] >> 6) != 2:
            return None
        offset = 12 + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10:  # extension
            if len(packet) < offset + 4:
                return None
            extension_words = int.from_bytes(packet[offset + 2 : offset + 4], "big")
            offset += 4 + extension_words * 4
        end = len(packet)
        if packet[0] & 0x20:  # padding
            if end == 0:
                return None
            end -= packet[-1]
        if offset >= end:
            return None
        return packet[offset:end]

    def _decode(self, payload: bytes) -> Optional[bytes]:
        if self._codec == "PCMA":
            pcm = audioop.alaw2lin(payload, 2)
        elif self._codec == "PCMU":
            pcm = audioop.ulaw2lin(payload, 2)
        elif self._codec == "L16":
            pcm = audioop.byteswap(payload, 2)  # RTP L16 é big-endian
        else:
            return None

        if self._codec_rate != 16000:
            pcm, self._rate_state = audioop.ratecv(
                pcm, 2, 1, self._codec_rate, 16000, self._rate_state
            )
        if self.gain != 1.0 and pcm:
            pcm = audioop.mul(pcm, 2, self.gain)
        return pcm

    def _keepalive(self) -> None:
        if not self._rtsp_sock or not self._session_id:
            return
        base_url = f"rtsp://{self.camera_ip}:{self.camera_port}{self.stream_path}"
        try:
            self._request(
                self._rtsp_sock,
                "GET_PARAMETER",
                base_url,
                auth=self._authorization("GET_PARAMETER", base_url),
                session=self._session_id,
                extra=["Content-Length: 0"],
            )
        except Exception as exc:
            LOG.debug("Keepalive RTSP falhou: %s", exc)

    def _loop(self) -> None:
        last_keepalive = time.time()
        failures = 0
        while self._running:
            if not self._udp_sock or not self._rtsp_sock:
                if self._connect_rtsp():
                    failures = 0
                    last_keepalive = time.time()
                else:
                    failures += 1
                    wait = min(2 ** failures, 60)
                    if failures == 3:
                        LOG.error(
                            "Câmera recusando RTSP repetidamente. Desligue e ligue a "
                            "câmera (o RTSP pode ter travado). Nova tentativa em %ds.",
                            wait,
                        )
                    time.sleep(wait)
                    continue

            if time.time() - last_keepalive > 20.0:
                self._keepalive()
                last_keepalive = time.time()

            try:
                packet, _ = self._udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as exc:
                LOG.warning("Erro no socket de áudio RTSP: %s", exc)
                self._close_session()
                continue

            payload = self._rtp_payload(packet)
            if not payload:
                continue
            try:
                pcm = self._decode(payload)
            except Exception as exc:
                LOG.debug("Pacote de áudio inválido: %s", exc)
                continue
            if not pcm:
                continue
            with self._lock:
                if len(self._queue) > 100:
                    self._queue.pop(0)
                self._queue.append(pcm)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def read_chunk(self, timeout: float = 1.0) -> Optional[bytes]:
        """Drena todos os pacotes acumulados em um bloco contínuo."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._queue:
                    data = b"".join(self._queue)
                    self._queue.clear()
                    return data
            time.sleep(0.02)
        return None

    def stop(self) -> None:
        self._running = False
        self._close_session()


class LocalMicAudioSource(BaseAudioSource):
    """Captura áudio de um microfone conectado diretamente ao TV Box (USB ou P2/AV)."""

    def __init__(self, device: str = "default", gain: float = 1.0):
        self.device = device
        self.gain = float(gain)
        self._proc: Optional[subprocess.Popen] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # arecord: 16kHz, 16-bit Little Endian, Mono, raw PCM.
        # Buffer/period baixos reduzem a latencia do caminho do microfone,
        # o que ajuda o AEC3 a alinhar a referencia do alto-falante.
        cmd = [
            "arecord",
            "-D", self.device,
            "-r", "16000",
            "-f", "S16_LE",
            "-c", "1",
            "-t", "raw",
            "-q",
            "--period-time=20000",
            "--buffer-time=200000",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            LOG.info("Captura de microfone local iniciada (dispositivo: %s).", self.device)
        except Exception as e:
            LOG.error("Falha ao iniciar arecord no dispositivo %s: %s", self.device, e)

    def read_chunk(self, timeout: float = 1.0) -> Optional[bytes]:
        # 3200 bytes = 100ms de áudio a 16000Hz (16-bit Mono)
        if not self._proc or not self._proc.stdout:
            return None
        try:
            data = self._proc.stdout.read(3200)
            if data and self.gain != 1.0:
                data = audioop.mul(data, 2, self.gain)
            return data if data else None
        except Exception:
            return None

    def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                self._proc.kill()
            self._proc = None


def create_audio_source(backend: str = "camera", device: str = "default", gain: Optional[float] = None, **kwargs) -> BaseAudioSource:
    """Factory para instanciar a fonte de áudio conforme configuração.

    Args:
        backend: "camera" (microfone da câmera via RTSP) ou "local" (microfone no TV Box).
        device: Dispositivo ALSA caso backend seja "local".
        gain: Multiplicador de ganho digital (default: 5.0 para câmera, 1.0 para local).
    """
    backend = (backend or "camera").strip().lower()
    if backend == "local":
        effective_gain = float(gain if gain is not None else os.getenv("AUDIO_MIC_GAIN", "1.0"))
        LOG.info("Configurado para microfone LOCAL no TV Box (%s, Ganho: %.1fx).", device, effective_gain)
        return LocalMicAudioSource(device=device, gain=effective_gain)
    else:
        effective_gain = float(gain if gain is not None else os.getenv("AUDIO_MIC_GAIN", "5.0"))
        LOG.warning(
            "Microfone da CÂMERA selecionado: abre uma segunda sessão RTSP e pode "
            "conflitar com o vídeo. Para AEC3 e maior estabilidade, use "
            "AUDIO_INPUT_BACKEND=local (microfone USB)."
        )
        # Herda host/porta/credenciais da CAMERA_URL (fonte única de verdade).
        camera_url = os.getenv("CAMERA_URL", "")
        parsed = urlparse(camera_url) if camera_url else None
        audio_path = os.getenv("CAMERA_AUDIO_PATH") or (
            parsed.path if parsed and parsed.path else "/onvif1"
        )

        kwargs.setdefault("camera_ip", (parsed.hostname if parsed else None) or os.getenv("CAMERA_IP", "192.168.10.14"))
        kwargs.setdefault("camera_port", (parsed.port if parsed else None) or int(os.getenv("CAMERA_RTSP_PORT", "554")))
        kwargs.setdefault("username", (parsed.username if parsed else None) or os.getenv("CAMERA_USER", "admin"))
        kwargs.setdefault("password", (parsed.password if parsed else None) or os.getenv("CAMERA_PASS", "E33426244b"))
        kwargs.setdefault("stream_path", audio_path)
        return CameraAudioSource(gain=effective_gain, **kwargs)
