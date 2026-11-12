#!/usr/bin/env python3
"""SKYNET ROBOT - Entrada Principal do Sistema.

Inicia a conversação por voz em tempo real através do Gemini Live.
- Entrada de áudio: Microfone da câmera (padrão) ou microfone local no TV Box.
- Saída de áudio: Alto-falante conectado ao TV Box (P2/AV, HDMI ou USB).

Uso:
    python3 main.py                   # Inicia conversa por voz (padrão: mic da câmera)
    python3 main.py --backend local   # Inicia usando microfone conectado direto no TV Box
    python3 main.py --chat            # Modo texto legado (chat)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Carregamento de variáveis de ambiente do .env
def _load_env():
    for candidate in [
        Path("/root/skynet/.env"),
        Path(__file__).parent / ".env",
        Path.cwd() / ".env"
    ]:
        if candidate.is_file():
            try:
                from dotenv import load_dotenv
                load_dotenv(candidate, override=True)
            except Exception:
                pass
            break

_load_env()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


def main():
    parser = argparse.ArgumentParser(description="SKYNET ROBOT - Conversação por Voz (Gemini Live)")
    parser.add_argument(
        "--backend",
        choices=["camera", "local"],
        default=os.getenv("AUDIO_INPUT_BACKEND", "camera"),
        help="Fonte do microfone ('camera' via RTSP ou 'local' via TV Box)",
    )
    parser.add_argument(
        "--local-device",
        default=os.getenv("AUDIO_LOCAL_DEVICE", "default"),
        help="Dispositivo ALSA de entrada quando --backend local (ex: default, hw:0,0, plughw:1,0)",
    )
    parser.add_argument(
        "--output-device",
        default=os.getenv("AUDIO_OUTPUT_DEVICE", "default"),
        help="Dispositivo ALSA de saída para o TV Box (ex: default, hw:0,0)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live"),
        help="Modelo do Gemini Live (padrão: gemini-3.8-live)",
    )
    parser.add_argument(
        "--mic-gain",
        type=float,
        default=float(os.getenv("AUDIO_MIC_GAIN", "5.0")),
        help="Multiplicador de ganho digital do microfone (padrão: 5.0)",
    )
    parser.add_argument(
        "--continuous-vision",
        action="store_true",
        help="Inicia já com streaming contínuo de vídeo 1 FPS (padrão: visão sob demanda por foto)",
    )
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="Desativa totalmente o módulo de câmera",
    )
    parser.add_argument(
        "--camera-url",
        default=os.getenv("CAMERA_URL"),
        help="URL RTSP da câmera (padrão: CAMERA_URL no .env)",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Abre o chat de texto legado em vez da voz",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Abre diretamente a tela de configuração interativa de áudio/visão no terminal",
    )
    args = parser.parse_args()

    if args.config:
        from core.config_tui import open_config_menu
        open_config_menu()
        return 0

    # Relogio certo antes de qualquer conexao TLS (Gemini/RTSP/TLS).
    from core.time_sync import ensure_clock
    ensure_clock()

    if args.chat:
        from vision.chat import main as chat_main
        return chat_main()

    from audio.live_agent import VoiceAgent

    agent = VoiceAgent(
        model=args.model,
        audio_backend=args.backend,
        local_device=args.local_device,
        output_device=args.output_device,
        mic_gain=args.mic_gain,
        camera_url=args.camera_url,
        enable_vision=not args.no_vision,
        continuous_vision=args.continuous_vision,
    )

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n[Encerrado pelo usuário]")
    finally:
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
