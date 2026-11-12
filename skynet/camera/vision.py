# ==============================================================================
# MÓDULO DE VISÃO (CAPTURA DE IMAGEM / RTSP COM BUFFER EM RAM)
# Suporta leitura instantânea (< 1ms) via RAM Drive (/dev/shm) para IA
# ==============================================================================
import subprocess
import os
import time
import shutil
from typing import Optional
import config

class VisionModule:
    """Módulo de captura de vídeo e fotos da câmera para processamento visual."""

    def __init__(self, 
                 stream_url: str = config.RTSP_SUB_STREAM_URL,
                 hd_stream_url: str = config.RTSP_STREAM_URL):
        self.stream_url = stream_url          # 640x360 para visão rápida / IA
        self.hd_stream_url = hd_stream_url    # 1080p para fotos detalhadas
        self.ram_frame_path = "/dev/shm/skynet_latest.jpg"
        self._worker_process: Optional[subprocess.Popen] = None

    def start_background_stream(self):
        """
        Inicia um worker em segundo plano que mantém o frame mais recente
        SEMPRE atualizado na memória RAM (/dev/shm) com latência zero para a IA.
        """
        if self._worker_process and self._worker_process.poll() is None:
            return  # Já está rodando

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-rtsp_transport", "udp",
            "-i", self.stream_url,
            "-vf", "fps=5",             # 5 frames por segundo na RAM é ideal para robô
            "-update", "1",
            self.ram_frame_path
        ]
        self._worker_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    def stop_background_stream(self):
        """Para o worker de atualização contínua em RAM."""
        if self._worker_process:
            self._worker_process.terminate()
            try:
                self._worker_process.wait(timeout=2.0)
            except Exception:
                self._worker_process.kill()
            self._worker_process = None

    def get_latest_frame_bytes(self) -> Optional[bytes]:
        """
        Retorna instantaneamente (< 1ms) os bytes JPEG do último frame em RAM.
        Se o stream contínuo não estiver ativo, captura sob demanda.
        """
        if os.path.exists(self.ram_frame_path) and os.path.getsize(self.ram_frame_path) > 0:
            try:
                with open(self.ram_frame_path, "rb") as f:
                    return f.read()
            except Exception:
                pass
        return self.capture_frame_bytes(high_res=False)

    def capture_frame_bytes(self, high_res: bool = False) -> Optional[bytes]:
        """Captura sob demanda um frame JPEG diretamente na memória RAM."""
        url = self.hd_stream_url if high_res else self.stream_url
        cmd = [
            "ffmpeg",
            "-y",
            "-rtsp_transport", "udp",
            "-i", url,
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-"
        ]
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            stdout_data, _ = process.communicate(timeout=8.0)
            if process.returncode == 0 and len(stdout_data) > 0:
                return stdout_data
        except Exception as e:
            print(f"[Vision] Erro ao capturar frame na RAM: {e}")
        return None

    def capture_snapshot(self, output_path: Optional[str] = None, high_res: bool = False) -> Optional[str]:
        """
        Captura um frame e salva em disco como arquivo JPEG.
        :param output_path: Caminho de saída. Se omitido, salva em /tmp/skynet/
        :param high_res: True para 1080p, False para 640x360 (mais rápido)
        """
        # Se temos frame recente em RAM e não pediram HD, aproveita instantaneamente
        if not high_res and os.path.exists(self.ram_frame_path) and os.path.getsize(self.ram_frame_path) > 0:
            if not output_path:
                output_path = os.path.join(config.STORAGE_DIR, f"frame_{int(time.time() * 1000)}.jpg")
            shutil.copyfile(self.ram_frame_path, output_path)
            return output_path

        if not output_path:
            output_path = os.path.join(config.STORAGE_DIR, f"frame_{int(time.time() * 1000)}.jpg")

        url = self.hd_stream_url if high_res else self.stream_url
        cmd = [
            "ffmpeg",
            "-y",
            "-rtsp_transport", "udp",
            "-i", url,
            "-vframes", "1",
            output_path
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=9.0
            )
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
        except Exception as e:
            print(f"[Vision] Erro ao capturar snapshot: {e}")
        return None
