# ==============================================================================
# SKYNET ROBOT - CONFIGURAÇÕES GERAIS DO SISTEMA
# ==============================================================================
import os

# --- Rede e Conexão da Câmera ---
CAMERA_IP = os.getenv("CAMERA_IP", "192.168.10.14")
CAMERA_ONVIF_PORT = int(os.getenv("CAMERA_ONVIF_PORT", "5000"))
CAMERA_RTSP_PORT = int(os.getenv("CAMERA_RTSP_PORT", "554"))
CAMERA_USER = os.getenv("CAMERA_USER", "admin")
CAMERA_PASS = os.getenv("CAMERA_PASS", "E33426244b")

# --- URLs de Serviço ---
ONVIF_PTZ_URL = f"http://{CAMERA_IP}:{CAMERA_ONVIF_PORT}/onvif/ptz_service"
ONVIF_MEDIA_URL = f"http://{CAMERA_IP}:{CAMERA_ONVIF_PORT}/onvif/media_service"
ONVIF_DEVICE_URL = f"http://{CAMERA_IP}:{CAMERA_ONVIF_PORT}/onvif/device_service"
ONVIF_PROFILE_TOKEN = "IPCProfilesToken0"

RTSP_STREAM_URL = f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@{CAMERA_IP}:{CAMERA_RTSP_PORT}/onvif1"
RTSP_SUB_STREAM_URL = f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@{CAMERA_IP}:{CAMERA_RTSP_PORT}/onvif2"

# --- Calibração de Movimento PTZ ---
# Heartbeat: intervalo entre pacotes ContinuousMove encadeados p/ mov. suave
PTZ_HEARTBEAT_SEC = 0.150           # 150ms entre pulsos (motor nunca para)
PTZ_STEP_DURATION_SEC = 0.22        # Duração de 1 passo discreto (controle manual)
# Velocidade angular medida em movimento contínuo encadeado
# Calibração original: 160 passos = 180° em ~4s → ~45°/s horizontal
PTZ_PAN_DEG_PER_SEC = 45.0          # Graus por segundo horizontal
PTZ_TILT_DEG_PER_SEC = 35.0         # Graus por segundo vertical
# Compatibilidade com tracker.py e move_steps()
DEGREES_PER_STEP_PAN = 5.0          # Graus por passo discreto (horizontal)
DEGREES_PER_STEP_TILT = 4.0         # Graus por passo discreto (vertical)
PTZ_STEP_INTERVAL_SEC = 0.150       # Compatibilidade com tracker.py

# --- Constantes de Compatibilidade ---
DEFAULT_CRUISE_SPEED = 1.0         # Velocidade padrão
DEFAULT_STEP_TIME = 0.15           # Toque fino manual

# Diretório para snapshots e dados temporários
STORAGE_DIR = "/tmp/skynet"
os.makedirs(STORAGE_DIR, exist_ok=True)
