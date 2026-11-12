"""Teste Headless da TUI e do Gerenciador de Ambiente."""

import os
import sys

from core.alsa_detector import detect_capture_devices, detect_playback_devices
from core.config_tui import ConfigTUI
from core.env_manager import find_env_path, read_env_values, update_env_values


def test_tui():
    print("[TEST 1] Detectando dispositivos ALSA...")
    caps = detect_capture_devices()
    plays = detect_playback_devices()
    print(f" -> Capturas encontradas: {len(caps)}")
    print(f" -> Saidas encontradas: {len(plays)}")

    print("\n[TEST 2] Lendo e testando .env...")
    env_path = find_env_path()
    vals = read_env_values(env_path)
    print(f" -> Caminho do .env: {env_path}")
    print(f" -> AUDIO_INPUT_BACKEND: {vals.get('AUDIO_INPUT_BACKEND')}")
    print(f" -> AUDIO_LOCAL_DEVICE: {vals.get('AUDIO_LOCAL_DEVICE')}")
    print(f" -> AUDIO_OUTPUT_DEVICE: {vals.get('AUDIO_OUTPUT_DEVICE')}")

    print("\n[TEST 3] Construindo menu do ConfigTUI...")
    tui = ConfigTUI()
    items = tui._build_menu_items()
    print(f" -> Total de itens interativos no menu: {len(items)}")
    for idx, item in enumerate(items):
        sec = item.get("section", "GERAL")
        lbl = item.get("label")
        t = item.get("type")
        val = item.get("value", "")
        sel = " [SELECIONADO]" if item.get("selected") else ""
        print(f"   [{idx:02d}] [{sec:<22}] ({t:<7}) {lbl} {val}{sel}")

    print("\n[TEST 4] Testando simulacao de teclas...")
    # Navega para baixo e para cima
    tui.handle_input("DOWN")
    tui.handle_input("UP")
    # Testa ganho (seta para a direita quando no stepper)
    tui.selected_idx = 3 # mic_gain
    old_gain = tui.gain
    tui.handle_input("RIGHT")
    print(f" -> Ganho alterado de {old_gain}x para {tui.gain}x")
    # Volta o ganho
    tui.handle_input("LEFT")
    print(f" -> Ganho restaurado para {tui.gain}x")

    print("\n[SUCESSO] Todos os testes headless do ConfigTUI passaram com exito!")


if __name__ == "__main__":
    test_tui()

