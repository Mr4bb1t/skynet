"""Interface de Configuração Interativa no Terminal (TUI) para o Skynet Robot.

Permite selecionar e alternar dispositivos de entrada/saída de áudio em tempo real,
ajustar ganho, modo de visão, testar som e movimento sem reiniciar o sistema.
Edita diretamente o arquivo .env e aplica as mudanças instantaneamente.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .alsa_detector import (
    AudioDevice,
    detect_capture_devices,
    detect_playback_devices,
)
from .env_manager import find_env_path, read_env_values, update_env_values

# Códigos ANSI para cores e tela
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[1;36m"
C_GREEN = "\033[1;32m"
C_YELLOW = "\033[1;33m"
C_BLUE = "\033[1;34m"
C_MAGENTA = "\033[1;35m"
C_RED = "\033[1;31m"
C_WHITE = "\033[1;37m"
C_BG_BLUE = "\033[44;1;37m"
C_BG_CYAN = "\033[46;1;30m"
C_BG_GREEN = "\033[42;1;30m"

GAIN_OPTIONS = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0]


def parse_key(data: bytes, fd: Optional[int] = None) -> Optional[str]:
    """Decodifica sequências brutas do terminal (incluindo escape ANSI) de forma robusta."""
    if not data:
        return None

    # Se recebeu só \x1b (byte ESC inicial), espera até 80ms para ver se chegam os bytes da seta
    if data == b"\x1b" and fd is not None:
        try:
            r, _, _ = select.select([fd], [], [], 0.08)
            if r:
                extra = os.read(fd, 31)
                data = data + extra
        except Exception:
            pass

    # 1. Setas Direcionais (suporta xterm, vt100, PuTTY, rxvt)
    if b"[A" in data or b"OA" in data:
        return "UP"
    if b"[B" in data or b"OB" in data:
        return "DOWN"
    if b"[C" in data or b"OC" in data:
        return "RIGHT"
    if b"[D" in data or b"OD" in data:
        return "LEFT"

    # 2. Teclas de navegação rápida
    if b"[H" in data or b"[1~" in data:
        return "HOME"
    if b"[F" in data or b"[4~" in data:
        return "END"
    if b"[5~" in data:
        return "PAGE_UP"
    if b"[6~" in data:
        return "PAGE_DOWN"

    # 3. Teclas de Confirmação e Controle
    if data in (b"\r", b"\n", b"\r\n"):
        return "ENTER"
    if data == b" ":
        return "SPACE"
    if data == b"\t":
        return "TAB"
    if data == b"\x03":
        return "CTRL_C"
    if data == b"\x1b":
        return "ESC"

    # 4. Teclas normais de texto (caractere único)
    try:
        ch = data.decode("utf-8", errors="ignore").strip().lower()
        if ch:
            return ch
    except Exception:
        pass

    return None


class ConfigTUI:
    """Painel TUI Interativo para Configuração em Tempo Real."""

    def __init__(self, agent: Optional[Any] = None):
        self.agent = agent
        self.env_path = find_env_path()
        self.env_vars = read_env_values(self.env_path)

        # Estado da Configuração
        # A TUI gerencia somente o microfone local. O backend "camera" e
        # experimental (abre uma segunda sessao RTSP e degrada o AEC3), entao
        # continua disponivel apenas editando o .env manualmente.
        env_backend = self.env_vars.get("AUDIO_INPUT_BACKEND", "local").lower()
        self.backend = "local"
        self._camera_backend_in_env = env_backend == "camera"
        self.local_device = self.env_vars.get("AUDIO_LOCAL_DEVICE", "plughw:Microphone,0")
        self.output_device = self.env_vars.get("AUDIO_OUTPUT_DEVICE", "plughw:Microphone,0")
        try:
            self.gain = float(self.env_vars.get("AUDIO_MIC_GAIN", "1.0"))
        except ValueError:
            self.gain = 1.0

        self.continuous_vision = getattr(agent, "_continuous_vision", False)
        self.camera_url = self.env_vars.get(
            "CAMERA_URL", "rtsp://admin:E33426244b@192.168.10.14:554/onvif1"
        )

        # Dispositivos detectados em tempo real
        self.capture_devs: List[AudioDevice] = []
        self.playback_devs: List[AudioDevice] = []
        self.refresh_devices()

        # Itens do Menu Navegável
        # Cada item: {"type": ..., "title": ..., "val": ...}
        self.selected_idx = 0
        self.status_msg = "Pronto. Use as setas para navegar e ENTER para selecionar."
        if self._camera_backend_in_env:
            self.status_msg = (
                "Backend 'camera' no .env é experimental; a TUI usa o mic local. "
                "Salve para persistir o local."
            )
        self.status_time = time.time()
        self._running = True

    def refresh_devices(self) -> None:
        """Atualiza a lista física de dispositivos ALSA/USB em tempo real."""
        self.capture_devs = detect_capture_devices()
        self.playback_devs = detect_playback_devices()

    def _build_menu_items(self) -> List[Dict[str, Any]]:
        """Gera a lista sequencial de itens interativos do menu."""
        items = []

        # 1. Fonte do Microfone (somente local; câmera é experimental via .env)
        items.append({
            "key": "backend_local",
            "section": "MICROFONE",
            "type": "radio",
            "label": "Microfone Local no TV Box (USB / P2) — recomendado (AEC3)",
            "selected": True,
        })

        # 2. Se backend for local, lista os dispositivos de captura
        if self.backend == "local":
            for d in self.capture_devs:
                items.append({
                    "key": f"cap_{d.device_id}",
                    "section": "DISPOSITIVO DE CAPTURA",
                    "type": "radio",
                    "label": f"{d.device_id:<22} - {d.description}",
                    "selected": self.local_device == d.device_id,
                    "device_id": d.device_id,
                })

        # 3. Ganho do microfone
        items.append({
            "key": "mic_gain",
            "section": "GANHO DO MICROFONE",
            "type": "stepper",
            "label": f"Sensibilidade Digital do Microfone: {self.gain:.1f}x",
            "value": f"{self.gain:.1f}x",
        })

        # 4. Saída de Áudio (Alto-falante / Fone)
        for d in self.playback_devs:
            items.append({
                "key": f"play_{d.device_id}",
                "section": "SAÍDA DE ÁUDIO",
                "type": "radio",
                "label": f"{d.device_id:<22} - {d.description}",
                "selected": self.output_device == d.device_id,
                "device_id": d.device_id,
            })

        # 5. Modo de Visão
        items.append({
            "key": "vision_mode",
            "section": "VISÃO E CÂMERA",
            "type": "stepper",
            "label": "Modo de Visão",
            "value": "Contínua (1 FPS Realtime)" if self.continuous_vision else "Sob Demanda (Fotos pontuais)",
        })

        # 6. Stream RTSP
        is_onvif1 = "onvif1" in self.camera_url
        items.append({
            "key": "stream_mode",
            "section": "VISÃO E CÂMERA",
            "type": "stepper",
            "label": "Stream da Câmera",
            "value": "onvif1 (1080p UDP - Recomendado)" if is_onvif1 else "onvif2 (Substream)",
        })

        # 7. Ações de Teste
        items.append({
            "key": "test_audio",
            "section": "AÇÕES E TESTES",
            "type": "button",
            "label": "► TESTAR SAÍDA DE ÁUDIO (Tocar som agora no fone/caixa)",
        })
        items.append({
            "key": "test_ptz",
            "section": "AÇÕES E TESTES",
            "type": "button",
            "label": "► TESTAR GIRO PTZ (Girar câmera 15° e retornar)",
        })
        items.append({
            "key": "refresh_devs",
            "section": "AÇÕES E TESTES",
            "type": "button",
            "label": "► RECARREGAR DISPOSITIVOS (Escanear USBs plugados)",
        })

        # 8. Botões Finais
        items.append({
            "key": "btn_save",
            "section": "AÇÕES",
            "type": "button",
            "label": "✔ SALVAR, APLICAR E RETORNAR",
        })
        items.append({
            "key": "btn_cancel",
            "section": "AÇÕES",
            "type": "button",
            "label": "✖ CANCELAR E FECHAR",
        })

        return items

    def _draw(self) -> None:
        """Renderiza a interface visual completa no terminal."""
        items = self._build_menu_items()
        if not items:
            return
        if self.selected_idx >= len(items):
            self.selected_idx = len(items) - 1
        if self.selected_idx < 0:
            self.selected_idx = 0

        buf = []
        # Move o cursor para o topo
        buf.append("\033[H")

        # Cabeçalho
        buf.append(f"\033[K{C_CYAN}╔════════════════════════════════════════════════════════════════════════════╗{C_RESET}\n")
        buf.append(f"\033[K{C_CYAN}║{C_BOLD}{C_WHITE}       ⚙️   SKYNET ROBOT - PAINEL DE CONFIGURAÇÃO E ÁUDIO AO VIVO        {C_CYAN}║{C_RESET}\n")
        buf.append(f"\033[K{C_CYAN}╚════════════════════════════════════════════════════════════════════════════╝{C_RESET}\n")

        current_sec = ""
        for idx, it in enumerate(items):
            sec = it.get("section", "")
            if sec != current_sec:
                current_sec = sec
                buf.append(f"\033[K\n{C_BOLD}{C_YELLOW}── [ {sec} ] ──────────────────────────────────────────────────────────{C_RESET}\n")

            is_sel = (idx == self.selected_idx)
            prefix = f"\033[K{C_BG_CYAN} ► " if is_sel else "\033[K   "
            suffix = f" {C_RESET}" if is_sel else ""

            t = it["type"]
            label = it["label"]

            if t == "radio":
                icon = f"{C_GREEN}(●){C_RESET}" if it.get("selected") else f"{C_DIM}( ){C_RESET}"
                buf.append(f"{prefix}{icon} {label}{suffix}\n")
            elif t == "stepper":
                val_str = it.get("value", "")
                buf.append(f"{prefix}◄ [ {C_BOLD}{val_str}{C_RESET} ] ►  {label}{suffix}\n")
            elif t == "button":
                if it["key"] == "btn_save":
                    buf.append(f"{prefix}{C_BOLD}{C_GREEN}[ {label} ]{C_RESET}{suffix}\n")
                elif it["key"] == "btn_cancel":
                    buf.append(f"{prefix}{C_RED}[ {label} ]{C_RESET}{suffix}\n")
                else:
                    buf.append(f"{prefix}{C_BOLD}{C_WHITE}[ {label} ]{C_RESET}{suffix}\n")

        # Rodapé e Status
        buf.append(f"\033[K\n{C_CYAN}──────────────────────────────────────────────────────────────────────────────{C_RESET}\n")
        if self.status_msg and (time.time() - self.status_time < 5.0):
            buf.append(f"\033[K{C_BOLD}{C_GREEN}ℹ️  {self.status_msg}{C_RESET}\n")
        else:
            buf.append(f"\033[K{C_DIM}⌨️  [▲/▼] Navegar  |  [◄/►] Ajustar  |  [ENTER/ESPAÇO] Selecionar  |  [ESC/Q] Sair{C_RESET}\n")

        # Limpa o restante da tela para baixo
        buf.append("\033[J")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def _test_audio_playback(self) -> None:
        """Toca um sinal sonoro de teste no dispositivo selecionado em segundo plano."""
        dev = self.output_device
        self.status_msg = f"Tocando teste sonoro em: {dev}..."
        self.status_time = time.time()

        def _play():
            try:
                # Garante desmute
                try:
                    subprocess.run(["amixer", "-c", "Microphone", "sset", "Speaker", "100%", "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                for sound in ["/usr/share/sounds/alsa/Front_Center.wav", "/usr/share/sounds/alsa/Noise.wav"]:
                    if os.path.exists(sound):
                        subprocess.run(
                            ["aplay", "-q", "-D", dev, sound],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4.0
                        )
                        break
            except Exception as e:
                pass

        threading.Thread(target=_play, daemon=True).start()

    def _test_ptz_movement(self) -> None:
        """Testa o movimento do PTZ por 15 graus e retorna."""
        self.status_msg = "Movendo cabeça PTZ 15° para teste..."
        self.status_time = time.time()

        def _move():
            try:
                ptz = getattr(self.agent, "ptz", None)
                if ptz is None:
                    from camera.ptz import PTZController
                    ptz = PTZController()
                ptz.pan_degrees(15)
                time.sleep(0.5)
                ptz.pan_degrees(-15)
            except Exception as e:
                pass

        threading.Thread(target=_move, daemon=True).start()

    def _apply_and_save(self) -> None:
        """Salva as configurações no .env e aplica imediatamente no robô."""
        updates = {
            "AUDIO_INPUT_BACKEND": self.backend,
            "AUDIO_LOCAL_DEVICE": self.local_device,
            "AUDIO_OUTPUT_DEVICE": self.output_device,
            "AUDIO_MIC_GAIN": f"{self.gain:.1f}",
            "CAMERA_URL": self.camera_url,
        }
        ok = update_env_values(updates, self.env_path)
        saved = read_env_values(self.env_path)
        persisted = ok and all(saved.get(k) == v for k, v in updates.items())

        # Aplica hot-reload se tiver o agente rodando
        if self.agent is not None and hasattr(self.agent, "reconfigure"):
            self.agent.reconfigure(
                output_device=self.output_device,
                audio_backend=self.backend,
                local_device=self.local_device,
                mic_gain=self.gain,
                continuous_vision=self.continuous_vision,
                camera_url=self.camera_url,
            )

        if persisted:
            self.status_msg = f"✅ Configurações salvas em {self.env_path} e aplicadas!"
        else:
            self.status_msg = (
                f"⚠️ Aplicado, mas não foi possível salvar em {self.env_path}. "
                "Verifique permissões/read-only."
            )
        self.status_time = time.time()
        self._running = False

    def handle_input(self, key: str) -> None:
        """Processa as teclas pressionadas pelo usuário."""
        items = self._build_menu_items()
        if not items:
            return

        if self.selected_idx >= len(items):
            self.selected_idx = len(items) - 1
        if self.selected_idx < 0:
            self.selected_idx = 0

        cur = items[self.selected_idx]

        # Navegação Vertical
        if key in ("UP", "k", "w"):
            self.selected_idx = (self.selected_idx - 1) % len(items)
        elif key in ("DOWN", "j", "s"):
            self.selected_idx = (self.selected_idx + 1) % len(items)
        elif key == "PAGE_UP":
            self.selected_idx = max(0, self.selected_idx - 5)
        elif key == "PAGE_DOWN":
            self.selected_idx = min(len(items) - 1, self.selected_idx + 5)
        elif key == "HOME":
            self.selected_idx = 0
        elif key == "END":
            self.selected_idx = len(items) - 1

        # Ajuste de Valores para a Esquerda
        elif key in ("LEFT", "h", "a"):
            if cur["key"] == "mic_gain":
                idx = GAIN_OPTIONS.index(self.gain) if self.gain in GAIN_OPTIONS else 0
                if idx > 0:
                    self.gain = GAIN_OPTIONS[idx - 1]
                    self.status_msg = f"Ganho do microfone ajustado: {self.gain:.1f}x"
                    self.status_time = time.time()
            elif cur["key"] == "vision_mode":
                self.continuous_vision = not self.continuous_vision
            elif cur["key"] == "stream_mode":
                if "onvif1" in self.camera_url:
                    self.camera_url = self.camera_url.replace("onvif1", "onvif2")
                else:
                    self.camera_url = self.camera_url.replace("onvif2", "onvif1")

        # Ajuste de Valores para a Direita
        elif key in ("RIGHT", "l", "d"):
            if cur["key"] == "mic_gain":
                idx = GAIN_OPTIONS.index(self.gain) if self.gain in GAIN_OPTIONS else 0
                if idx < len(GAIN_OPTIONS) - 1:
                    self.gain = GAIN_OPTIONS[idx + 1]
                    self.status_msg = f"Ganho do microfone ajustado: {self.gain:.1f}x"
                    self.status_time = time.time()
            elif cur["key"] == "vision_mode":
                self.continuous_vision = not self.continuous_vision
            elif cur["key"] == "stream_mode":
                if "onvif1" in self.camera_url:
                    self.camera_url = self.camera_url.replace("onvif1", "onvif2")
                else:
                    self.camera_url = self.camera_url.replace("onvif2", "onvif1")

        # Seleção / Enter / Espaço
        elif key in ("ENTER", "SPACE"):
            k = cur["key"]
            if k == "backend_local":
                self.backend = "local"
                self.status_msg = "Microfone selecionado: Local no TV Box (USB/P2)"
                self.status_time = time.time()
            elif k == "backend_camera":
                self.backend = "camera"
                self.status_msg = "Microfone selecionado: Câmera IP (RTSP onvif1)"
                self.status_time = time.time()
            elif k.startswith("cap_"):
                self.local_device = cur["device_id"]
                self.status_msg = f"Dispositivo de captura selecionado: {self.local_device}"
                self.status_time = time.time()
            elif k.startswith("play_"):
                self.output_device = cur["device_id"]
                self.status_msg = f"Dispositivo de saída selecionado: {self.output_device}"
                self.status_time = time.time()
            elif k == "vision_mode":
                self.continuous_vision = not self.continuous_vision
            elif k == "stream_mode":
                if "onvif1" in self.camera_url:
                    self.camera_url = self.camera_url.replace("onvif1", "onvif2")
                else:
                    self.camera_url = self.camera_url.replace("onvif2", "onvif1")
            elif k == "test_audio":
                self._test_audio_playback()
            elif k == "test_ptz":
                self._test_ptz_movement()
            elif k == "refresh_devs":
                self.refresh_devices()
                self.status_msg = f"Dispositivos recarregados: {len(self.playback_devs)} saídas, {len(self.capture_devs)} entradas."
                self.status_time = time.time()
            elif k == "btn_save":
                self._apply_and_save()
            elif k == "btn_cancel":
                self._running = False

        # Atalhos rápidos de teclado
        elif key in ("t", "T"):
            self._test_audio_playback()
        elif key in ("p", "P"):
            self._test_ptz_movement()
        elif key in ("r", "R"):
            self.refresh_devices()
            self.status_msg = "Dispositivos USB/ALSA recarregados com sucesso."
            self.status_time = time.time()
        elif key in ("ESC", "q", "CTRL_C"):
            # Sai sem aplicar alterações
            self._running = False

    def run(self) -> None:
        """Executa a TUI interativa em tela cheia usando o buffer alternativo."""
        # Marca que a interface está ativa para suprimir saídas concorrentes do agente
        if self.agent is not None:
            setattr(self.agent, "_tui_active", True)

        # Salva tela atual e muda para buffer alternativo, limpa e esconde cursor
        sys.stdout.write("\033[?1049h\033[?25l\033[2J")
        sys.stdout.flush()

        fd = sys.stdin.fileno() if hasattr(sys.stdin, "fileno") else None
        is_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
        old_settings = None

        has_termios = False
        if is_tty and fd is not None:
            try:
                import termios
                import tty
                old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                has_termios = True
            except (ImportError, Exception):
                has_termios = False

        needs_redraw = True
        had_status_msg = False

        try:
            while self._running:
                # Verifica expiração de mensagem de status (5 segundos)
                has_active_status = bool(self.status_msg and (time.time() - self.status_time < 5.0))
                if had_status_msg and not has_active_status:
                    needs_redraw = True
                had_status_msg = has_active_status

                # Redesenha a tela SOMENTE se houve alteração de estado
                if needs_redraw:
                    self._draw()
                    needs_redraw = False

                if not is_tty:
                    time.sleep(0.5)
                    break

                if has_termios and fd is not None:
                    # Espera não-bloqueante por dados brutos do descritor de arquivo
                    rlist, _, _ = select.select([fd], [], [], 0.15)
                    if not rlist:
                        continue

                    # Leitura direta do descritor sem buffering do TextIOWrapper
                    data = os.read(fd, 32)
                    if not data:
                        continue

                    key = parse_key(data, fd)
                    if key:
                        self.handle_input(key)
                        needs_redraw = True
                else:
                    # Fallback para Windows (msvcrt)
                    try:
                        import msvcrt
                        if msvcrt.kbhit():
                            ch = msvcrt.getch()
                            if ch in (b"\x00", b"\xe0"):
                                ch2 = msvcrt.getch()
                                win_map = {
                                    b"H": "UP",
                                    b"P": "DOWN",
                                    b"M": "RIGHT",
                                    b"K": "LEFT",
                                }
                                self.handle_input(win_map.get(ch2, "UNKNOWN"))
                                needs_redraw = True
                            elif ch == b"\r":
                                self.handle_input("ENTER")
                                needs_redraw = True
                            elif ch == b"\x1b":
                                self.handle_input("ESC")
                                needs_redraw = True
                            elif ch == b" ":
                                self.handle_input("SPACE")
                                needs_redraw = True
                            else:
                                key_char = ch.decode("utf-8", errors="ignore").lower()
                                self.handle_input(key_char)
                                needs_redraw = True
                        time.sleep(0.05)
                    except Exception:
                        time.sleep(0.2)

        finally:
            if has_termios and old_settings and fd is not None:
                try:
                    import termios
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass
            # Restaura cursor e buffer da tela original
            sys.stdout.write("\033[?25h\033[?1049l\n")
            sys.stdout.flush()

            if self.agent is not None:
                setattr(self.agent, "_tui_active", False)


def open_config_menu(agent: Optional[Any] = None) -> None:
    """Função de entrada para abrir o menu interativo."""
    tui = ConfigTUI(agent=agent)
    tui.run()


if __name__ == "__main__":
    open_config_menu()
