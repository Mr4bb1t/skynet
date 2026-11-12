"""Detector Dinâmico de Dispositivos de Áudio ALSA / USB para o Skynet Robot.

Lista em tempo real os dispositivos físicos disponíveis de captura (microfone)
e reprodução (alto-falantes, HDMI, fones de ouvido USB).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AudioDevice:
    device_id: str          # Identificador ALSA: 'hw:0,0', 'plughw:Microphone,0', 'default'
    card_num: int           # Número da placa ALSA
    card_name: str          # ID do cartão ('Microphone', 'audiocodec', 'sndhdmi')
    device_num: int         # Número do subdispositivo
    description: str        # Descrição amigável para exibição na interface
    is_usb: bool            # Se é dispositivo USB (plug and play)


def _parse_alsa_list(cmd: List[str]) -> List[AudioDevice]:
    """Executa 'aplay -l' ou 'arecord -l' e retorna a lista de dispositivos detectados."""
    devices: List[AudioDevice] = []
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True)
        output = res.stdout
    except Exception:
        return devices

    # Linhas típicas:
    # card 4: Microphone [Fifine K669 Microphone], device 0: USB Audio [USB Audio]
    # card 0: audiocodec [audiocodec], device 0: CDC PCM Codec-0 []
    pattern = re.compile(
        r"^card\s+(\d+):\s+([A-Za-z0-9_-]+)\s+\[([^\]]+)\],\s+device\s+(\d+):\s+([^\n,]+)",
        re.MULTILINE
    )

    for m in pattern.finditer(output):
        c_num = int(m.group(1))
        card_id = m.group(2).strip()
        card_desc = m.group(3).strip()
        dev_num = int(m.group(4))
        dev_desc = m.group(5).strip()

        is_usb = "usb" in card_desc.lower() or "usb" in dev_desc.lower() or "microphone" in card_id.lower()

        # Monta identificador ALSA seguro:
        # Se tiver nome estável (ex: Microphone), usa 'plughw:Microphone,0' para não quebrar se o USB mudar de porta
        if is_usb and card_id not in ("audiocodec", "sndhdmi", "sndahub", "snddaudio0"):
            alsa_id = f"plughw:{card_id},{dev_num}"
            desc = f"USB: {card_desc} ({dev_desc})"
        elif card_id == "audiocodec":
            alsa_id = f"hw:{c_num},{dev_num}"
            desc = "Codec Analógico Allwinner (Conector P2/AV do Box)"
        elif card_id == "sndhdmi":
            alsa_id = f"hw:{c_num},{dev_num}"
            desc = "Saída de Vídeo/Áudio HDMI da TV"
        else:
            alsa_id = f"hw:{c_num},{dev_num}"
            desc = f"{card_id} ({card_desc} {dev_desc})".strip()

        devices.append(AudioDevice(
            device_id=alsa_id,
            card_num=c_num,
            card_name=card_id,
            device_num=dev_num,
            description=desc,
            is_usb=is_usb
        ))

    return devices


def detect_playback_devices() -> List[AudioDevice]:
    """Retorna lista de dispositivos de saída de áudio disponíveis."""
    devs = _parse_alsa_list(["aplay", "-l"])
    res = [
        AudioDevice(
            device_id="default",
            card_num=-1,
            card_name="default",
            device_num=0,
            description="Padrão do Sistema (ALSA default)",
            is_usb=False
        )
    ]
    res.extend(devs)
    return res


def detect_capture_devices() -> List[AudioDevice]:
    """Retorna lista de dispositivos de entrada de áudio (microfones) disponíveis."""
    devs = _parse_alsa_list(["arecord", "-l"])
    res = [
        AudioDevice(
            device_id="default",
            card_num=-1,
            card_name="default",
            device_num=0,
            description="Padrão do Sistema (ALSA default)",
            is_usb=False
        )
    ]
    res.extend(devs)
    return res


if __name__ == "__main__":
    print("--- Dispositivos de Reprodução (Saída) ---")
    for d in detect_playback_devices():
        print(f"ID: {d.device_id:<24} Nome: {d.card_name:<16} USB: {d.is_usb}  Desc: {d.description}")
    print("\n--- Dispositivos de Captura (Microfone) ---")
    for d in detect_capture_devices():
        print(f"ID: {d.device_id:<24} Nome: {d.card_name:<16} USB: {d.is_usb}  Desc: {d.description}")
