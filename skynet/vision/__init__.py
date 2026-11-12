"""SKYNET vision subsystem.

Captura continua da camera IP, preparacao de frames JPEG e transmissao
para a Gemini Live API.

Este pacote NAO implementa deteccao local, navegacao, motores, audio ou
conversacao. Ele apenas entrega percepcao visual para a nuvem.
"""

from .config import Settings, load_settings, ConfigError

__all__ = ["Settings", "load_settings", "ConfigError"]
