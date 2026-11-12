#!/usr/bin/env python3
"""SKYNET VOICE - Conversação por Voz em Tempo Real com Gemini Live.

Uso:
    python3 voice_chat.py                   # Usa microfone da câmera e áudio no TV Box
    python3 voice_chat.py --backend local   # Usa microfone conectado direto no TV Box
    python3 voice_chat.py --device plughw:1,0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Carrega variáveis do .env
load_dotenv("/root/skynet/.env", override=True)

from audio.live_agent import VoiceAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


def main():
    parser = argparse.ArgumentParser(description="Skynet Voice Assistant (Gemini Live)")
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
    args = parser.parse_args()

    from core.time_sync import ensure_clock
    ensure_clock()

    agent = VoiceAgent(
        model=args.model,
        audio_backend=args.backend,
        local_device=args.local_device,
        output_device=args.output_device,
    )

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n[Encerrado pelo usuário]")
    finally:
        agent.stop()


if __name__ == "__main__":
    main()

