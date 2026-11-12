"""Ferramentas que o modelo de chat pode chamar.

Cada ferramenta encapsula um subsistema do robô:

  * camera_tool: captura uma foto atual da câmera;
  * ptz_tool: move a cabeça PTZ em graus.

O chat não executa essas ações sozinho; ele só as executa quando o modelo
pede por meio de function calling.
"""

from .camera_tool import CameraTool, CAPTURE_DECLARATION
from .person_tool import (
    FORGET_DECLARATION,
    LIST_DECLARATION,
    PersonTool,
    RECALL_DECLARATION,
    REMEMBER_DECLARATION,
)
from .ptz_tool import PTZTool, MOVE_DECLARATION, STOP_DECLARATION

__all__ = [
    "CameraTool",
    "CAPTURE_DECLARATION",
    "PTZTool",
    "MOVE_DECLARATION",
    "STOP_DECLARATION",
    "PersonTool",
    "REMEMBER_DECLARATION",
    "RECALL_DECLARATION",
    "LIST_DECLARATION",
    "FORGET_DECLARATION",
]
