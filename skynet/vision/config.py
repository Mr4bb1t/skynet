"""Configuracao externa do subsistema de visao.

Todas as configuracoes vem de variaveis de ambiente / arquivo .env.
Nenhuma chave de API ou senha deve ser escrita no codigo.

O arquivo .env e procurado, nesta ordem:
  1. VISION_ENV_FILE (caminho explicito)
  2. <raiz do projeto>/skynet/.env   (ex.: /root/skynet/.env)
  3. <raiz do projeto>/.env
  4. diretorio atual/.env

Se python-dotenv nao estiver instalado, um parser simples e usado.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Presets de resolucao local (largura maxima, em pixels)
# ---------------------------------------------------------------------------
IMAGE_PRESETS = {
    "LOW": 320,
    "BALANCED": 640,
    "DETAIL": 960,
}

VALID_MEDIA_RESOLUTIONS = ("LOW", "MEDIUM", "HIGH", "UNSPECIFIED")
VALID_RESPONSE_MODALITIES = ("AUDIO", "TEXT")


class ConfigError(RuntimeError):
    """Configuracao ausente ou invalida."""


# ---------------------------------------------------------------------------
# Carregamento do .env
# ---------------------------------------------------------------------------
def _candidate_env_files() -> list[Path]:
    here = Path(__file__).resolve()
    # .../skynet/vision/config.py -> parents: vision, skynet, projeto
    project_root = here.parents[2] if len(here.parents) > 2 else here.parent
    skynet_root = here.parents[1] if len(here.parents) > 1 else here.parent
    candidates = []
    explicit = os.getenv("VISION_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        [
            skynet_root / ".env",
            project_root / ".env",
            Path.cwd() / ".env",
        ]
    )
    return candidates


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parser minimo de .env (usado quando python-dotenv nao existe)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _load_env_file() -> Optional[Path]:
    for path in _candidate_env_files():
        if not path.is_file():
            continue
        try:
            from dotenv import load_dotenv  # type: ignore

            load_dotenv(path, override=False, encoding="utf-8")
        except Exception:
            for key, value in _parse_env_file(path).items():
                os.environ.setdefault(key, value)
        return path
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um inteiro (recebido: {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um numero (recebido: {raw!r})") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "sim", "s")


def _default_camera_url() -> str:
    """Reaproveita a URL RTSP ja validada no config.py existente.

    O projeto legado usa `import config` quando executado a partir de
    /root/skynet. Tambem tentamos `skynet.config` para execucao como pacote.
    """
    for module_name in ("config", "skynet.config"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        url = getattr(module, "RTSP_SUB_STREAM_URL", None)
        if url:
            return str(url)
    return ""


def _default_storage_dir() -> str:
    if os.name == "posix":
        return "/tmp/skynet/vision"
    return os.path.join(tempfile.gettempdir(), "skynet", "vision")


def _resolve_data_path(value: str, default: str) -> str:
    """Resolve caminhos relativos a partir da raiz do projeto (skynet/)."""
    path = Path(value or default)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return str(path)


def mask_url(url: str) -> str:
    """Esconde usuario/senha de uma URL para logs."""
    if not url:
        return ""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        credentials, host = rest.rsplit("@", 1)
        if ":" in credentials:
            user = credentials.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://***@{host}"
    return url


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # Gemini
    gemini_api_key: str
    gemini_model: str
    text_model: str
    text_fallback_model: str

    # Camera
    camera_url: str
    camera_transport: str
    camera_probe_timeout_sec: float
    camera_read_timeout_sec: float
    camera_skip_probe: bool
    capture_fps: float
    capture_max_width: int
    capture_width: int
    capture_height: int
    ptz_interval_sec: float

    # Envio
    heartbeat_enabled: bool
    image_send_fps: float
    image_preset: str
    image_max_width: int
    image_max_height: int
    jpeg_quality: int
    media_resolution: str

    # Live API
    response_modality: str
    output_transcription: bool
    system_instruction: str
    initial_prompt: str
    session_resumption: bool
    context_compression: bool
    context_trigger_tokens: Optional[int]
    send_timeout_sec: float

    # Reconexao
    reconnect_base_sec: float
    reconnect_max_sec: float
    capture_reconnect_base_sec: float
    capture_reconnect_max_sec: float
    frame_stale_sec: float

    # Diagnostico
    diagnostics_interval_sec: float
    log_level: str
    log_each_frame: bool
    storage_dir: str

    # Memória de pessoas / face recognition local
    person_memory_enabled: bool
    person_db_path: str
    person_photo_dir: str
    face_match_threshold: float

    def describe(self) -> str:
        """Resumo seguro (sem chave de API)."""
        return (
            f"model={self.gemini_model} text_model={self.text_model} "
            f"text_fallback={self.text_fallback_model or 'off'} "
            f"camera={mask_url(self.camera_url)} "
            f"transport={self.camera_transport} "
            f"probe={'off' if self.camera_skip_probe else 'on'} "
            f"capture_fps={self.capture_fps:g} "
            f"heartbeat={'on' if self.heartbeat_enabled else 'off'} "
            f"send_fps={self.image_send_fps:g} "
            f"ptz_interval={self.ptz_interval_sec:g}s "
            f"preset={self.image_preset} "
            f"max={self.image_max_width}x{self.image_max_height} "
            f"jpeg_q={self.jpeg_quality} media_res={self.media_resolution} "
            f"modality={self.response_modality} "
            f"resumption={self.session_resumption} "
            f"compression={self.context_compression} "
            f"people={'on' if self.person_memory_enabled else 'off'} "
            f"storage={self.storage_dir} log={self.log_level}"
        )


DEFAULT_SYSTEM_INSTRUCTION = (
    "Voce e o sistema de visao do robo SKYNET. Observe as imagens recebidas "
    "desta camera. Descreva apenas o que estiver visivel e nunca invente "
    "informacoes. Nao responda a cada imagem recebida; permaneca em silencio "
    "ate receber uma pergunta ou instrucao explicita."
)

DEFAULT_INITIAL_PROMPT = (
    "Observe as imagens recebidas desta camera. Descreva brevemente o ambiente "
    "quando solicitado. Nao invente informacoes que nao estejam visiveis."
)


def load_settings(env_file: Optional[Path] = None) -> Settings:
    """Le o .env e o ambiente e devolve Settings validado."""
    if env_file is not None:
        os.environ["VISION_ENV_FILE"] = str(env_file)
    loaded_from = _load_env_file()

    # --- presets de imagem ---
    preset_raw = (_env("IMAGE_PRESET", "") or "").upper()
    if preset_raw and preset_raw not in IMAGE_PRESETS:
        raise ConfigError(
            f"IMAGE_PRESET invalido: {preset_raw!r}. Use {sorted(IMAGE_PRESETS)}."
        )
    default_width = IMAGE_PRESETS.get(preset_raw, 640) if preset_raw else 640
    image_max_width = _env_int("IMAGE_MAX_WIDTH", default_width)
    image_max_height = _env_int("IMAGE_MAX_HEIGHT", 480)
    if image_max_width <= 0 or image_max_height <= 0:
        raise ConfigError("IMAGE_MAX_WIDTH e IMAGE_MAX_HEIGHT devem ser > 0.")
    effective_preset = preset_raw or "BALANCED"
    if not preset_raw and image_max_width != IMAGE_PRESETS["BALANCED"]:
        effective_preset = "CUSTOM"

    jpeg_quality = _env_int("JPEG_QUALITY", 75)
    if not 1 <= jpeg_quality <= 100:
        raise ConfigError("JPEG_QUALITY deve estar entre 1 e 100.")

    media_resolution = (_env("MEDIA_RESOLUTION", "LOW") or "LOW").upper()
    if media_resolution not in VALID_MEDIA_RESOLUTIONS:
        raise ConfigError(
            f"MEDIA_RESOLUTION invalido: {media_resolution!r}. "
            f"Use {VALID_MEDIA_RESOLUTIONS}."
        )

    response_modality = (_env("GEMINI_RESPONSE_MODALITY", "AUDIO") or "AUDIO").upper()
    if response_modality not in VALID_RESPONSE_MODALITIES:
        raise ConfigError(
            f"GEMINI_RESPONSE_MODALITY invalido: {response_modality!r}. "
            f"Use {VALID_RESPONSE_MODALITIES}."
        )

    camera_url = _env("CAMERA_URL", "") or _default_camera_url()
    # Nao falha aqui: etapas sem camera (ex.: teste de conexao) sao validas.
    # O CameraCapture levanta um erro claro quando a URL for realmente usada.

    capture_fps = _env_float("CAPTURE_FPS", 10.0)
    if capture_fps <= 0:
        raise ConfigError("CAPTURE_FPS deve ser > 0.")

    image_send_fps = _env_float("IMAGE_SEND_FPS", 1.0)
    if image_send_fps <= 0:
        raise ConfigError("IMAGE_SEND_FPS deve ser > 0.")

    ptz_interval_sec = _env_float("PTZ_INTERVAL_SEC", 0.8)
    if ptz_interval_sec <= 0:
        raise ConfigError("PTZ_INTERVAL_SEC deve ser > 0.")

    context_trigger_raw = _env("CONTEXT_TRIGGER_TOKENS")
    context_trigger_tokens = int(context_trigger_raw) if context_trigger_raw else None

    log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL invalido: {log_level!r}.")

    storage_dir = _env("VISION_STORAGE_DIR", "") or _default_storage_dir()
    try:
        os.makedirs(storage_dir, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Nao foi possivel criar VISION_STORAGE_DIR={storage_dir}: {exc}") from exc

    person_memory_enabled = _env_bool("PERSON_MEMORY_ENABLED", True)
    person_db_path = _resolve_data_path(
        _env("PERSON_DB_PATH", "") or "data/people.db", "data/people.db"
    )
    person_photo_dir = _resolve_data_path(
        _env("PERSON_PHOTO_DIR", "") or "data/people", "data/people"
    )
    face_match_threshold = _env_float("FACE_MATCH_THRESHOLD", 0.6)
    if not 0.1 <= face_match_threshold <= 1.0:
        raise ConfigError("FACE_MATCH_THRESHOLD deve estar entre 0.1 e 1.0.")

    settings = Settings(
        gemini_api_key=_env("GEMINI_API_KEY", "") or "",
        gemini_model=_env("GEMINI_MODEL", "gemini-3.8-live") or "gemini-3.8-live",
        text_model=_env("GEMINI_TEXT_MODEL", "gemini-3.1-flash-lite")
        or "gemini-3.1-flash-lite",
        text_fallback_model=_env("GEMINI_TEXT_FALLBACK_MODEL", "gemini-3.6-flash")
        or "",
        camera_url=camera_url,
        camera_transport=(_env("CAMERA_RTSP_TRANSPORT", "udp") or "udp").lower(),
        camera_probe_timeout_sec=_env_float("CAMERA_PROBE_TIMEOUT_SEC", 10.0),
        camera_read_timeout_sec=_env_float("CAMERA_READ_TIMEOUT_SEC", 5.0),
        camera_skip_probe=_env_bool("CAMERA_SKIP_PROBE", False),
        capture_fps=capture_fps,
        capture_max_width=_env_int("CAPTURE_MAX_WIDTH", 0),
        capture_width=_env_int("CAMERA_WIDTH", 640),
        capture_height=_env_int("CAMERA_HEIGHT", 360),
        ptz_interval_sec=ptz_interval_sec,
        heartbeat_enabled=_env_bool("IMAGE_HEARTBEAT_ENABLED", True),
        image_send_fps=image_send_fps,
        image_preset=effective_preset,
        image_max_width=image_max_width,
        image_max_height=image_max_height,
        jpeg_quality=jpeg_quality,
        media_resolution=media_resolution,
        response_modality=response_modality,
        output_transcription=_env_bool("GEMINI_OUTPUT_TRANSCRIPTION", True),
        system_instruction=_env("GEMINI_SYSTEM_INSTRUCTION", DEFAULT_SYSTEM_INSTRUCTION)
        or DEFAULT_SYSTEM_INSTRUCTION,
        initial_prompt=_env("GEMINI_INITIAL_PROMPT", DEFAULT_INITIAL_PROMPT)
        or DEFAULT_INITIAL_PROMPT,
        session_resumption=_env_bool("GEMINI_SESSION_RESUMPTION", True),
        context_compression=_env_bool("GEMINI_CONTEXT_COMPRESSION", True),
        context_trigger_tokens=context_trigger_tokens,
        send_timeout_sec=_env_float("GEMINI_SEND_TIMEOUT_SEC", 5.0),
        reconnect_base_sec=_env_float("GEMINI_RECONNECT_BASE_SEC", 1.0),
        reconnect_max_sec=_env_float("GEMINI_RECONNECT_MAX_SEC", 30.0),
        capture_reconnect_base_sec=_env_float("CAMERA_RECONNECT_BASE_SEC", 1.0),
        capture_reconnect_max_sec=_env_float("CAMERA_RECONNECT_MAX_SEC", 30.0),
        frame_stale_sec=_env_float("FRAME_STALE_SEC", 3.0),
        diagnostics_interval_sec=_env_float("DIAGNOSTICS_INTERVAL_SEC", 30.0),
        log_level=log_level,
        log_each_frame=_env_bool("LOG_EACH_FRAME", False),
        storage_dir=storage_dir,
        person_memory_enabled=person_memory_enabled,
        person_db_path=person_db_path,
        person_photo_dir=person_photo_dir,
        face_match_threshold=face_match_threshold,
    )
    if loaded_from is not None:
        os.environ.setdefault("VISION_ENV_FILE_LOADED", str(loaded_from))
    return settings


def with_overrides(settings: Settings, **overrides) -> Settings:
    """Aplica overrides de CLI mantendo imutabilidade."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(settings, **clean) if clean else settings


def env_file_loaded() -> Optional[str]:
    return os.getenv("VISION_ENV_FILE_LOADED") or None
