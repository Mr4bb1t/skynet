#!/usr/bin/env python3
# ==============================================================================
# SKYNET ROBOT - CONSOLE MANUAL DE PTZ (LEGADO)
#
# O ponto de entrada principal agora é main.py (chat de texto com ferramentas).
# Este arquivo mantém o controle manual por graus/teclas para calibração e
# operação direta, sem passar pela IA.
# ==============================================================================
import sys
import os
import argparse
from core.commander import RobotCommander
import config

def interactive_mode(commander: RobotCommander):
    print("=" * 65)
    print("🤖 SKYNET ROBOT - CONTROLE POR GRAUS (PASSOS CALIBRADOS) & VISÃO")
    print("=" * 65)
    print("Comandos disponíveis:")
    print("  [Número] -> Gira em GRAUS (arredondado para o passo do motor)")
    print("              Ex: '180' (gira 180° à dir), '-90' (gira 90° à esq)")
    print("  [t<num>] -> Inclina em GRAUS verticalmente")
    print("              Ex: 't30' (sobe 30°), 't-20' (desce 20°)")
    print("  [W/S]    -> Micro-passo Cima / Baixo (1 passo)")
    print("  [A/D]    -> Micro-passo Esquerda / Direita (1 passo)")
    print("  [F]      -> Capturar Foto (Snapshot)")
    print("  [SCAN]   -> Varredura panorâmica de 180°")
    print("  [Q]      -> Sair")
    print("=" * 65)

    while True:
        try:
            raw = input("\n[Skynet-Box] Digite o comando ou graus > ").strip()
            if not raw:
                continue

            cmd = raw.lower()

            if cmd in ["q", "quit", "exit"]:
                print("Encerrando central de comando.")
                break
            
            # Comando de Tilt por graus (ex: t30, t-15)
            if cmd.startswith("t") and (cmd[1:].lstrip("-").isdigit() or cmd[1:].lstrip("-").replace(".", "", 1).isdigit()):
                deg = float(cmd[1:])
                print(f"📐 Inclinando verticalmente {deg:+.1f}° em passos calibrados...")
                commander.pitch(deg)
                print(f"✅ Concluído ({commander.ptz.last_steps} passos).")
                continue

            # Comando de Pan por graus (ex: 180, -90, 45, -45)
            try:
                deg = float(cmd)
                print(f"🔄 Girando {deg:+.1f}° horizontalmente em passos calibrados...")
                commander.turn(deg)
                print(f"✅ Concluído ({commander.ptz.last_steps} passos, "
                      f"~{commander.ptz.last_degrees:+.1f}°).")
                continue
            except ValueError:
                pass

            # Comandos de teclado tradicionais
            if cmd == "w":
                print("⬆️  Micro-passo CIMA...")
                commander.nudge("up")
            elif cmd == "s":
                print("⬇️  Micro-passo BAIXO...")
                commander.nudge("down")
            elif cmd == "a":
                print("⬅️  Micro-passo ESQUERDA...")
                commander.nudge("left")
            elif cmd == "d":
                print("➡️  Micro-passo DIREITA...")
                commander.nudge("right")
            elif cmd == "f":
                print("📸 Capturando imagem...")
                path = commander.see()
                print(f"✅ Foto salva em: {path}")
            elif cmd == "scan":
                print("🔄 Executando varredura panorâmica fluida de 180°...")
                frames = commander.scan_panorama(total_degrees=180, stops=3)
                for f in frames:
                    print(f" - Ângulo [{f['angle_offset']:+.1f}°]: {f['image']}")
                print("✅ Varredura concluída!")
            else:
                print(f"Comando '{raw}' inválido. Digite um número de graus (ex: 180, -90) ou W,A,S,D,F,Q.")
        except KeyboardInterrupt:
            print("\nOperação interrompida pelo usuário.")
            break

def main():
    parser = argparse.ArgumentParser(description="Central de Controle do Robô Skynet")
    parser.add_argument("--degrees", type=float, help="Gira a cabeça horizontalmente por N graus (passos calibrados, ex: 180, -90)")
    parser.add_argument("--tilt", type=float, help="Inclina a cabeça verticalmente por N graus (passos calibrados, ex: 30, -20)")
    parser.add_argument("--snapshot", action="store_true", help="Captura uma imagem imediatamente")
    parser.add_argument("--scan", action="store_true", help="Executa varredura panorâmica de 180°")
    args = parser.parse_args()

    commander = RobotCommander()

    if args.snapshot:
        path = commander.see()
        print(f"SNAPSHOT_PATH: {path}")
        return

    if args.degrees is not None or args.tilt is not None:
        pan = args.degrees if args.degrees is not None else 0.0
        tilt = args.tilt if args.tilt is not None else 0.0
        commander.look_at(pan_degrees=pan, tilt_degrees=tilt)
        print(f"MOVED: Pan {pan:+.1f}°, Tilt {tilt:+.1f}° ({commander.ptz.last_steps} passos, {commander.ptz.last_degrees:+.1f}°)")
        return

    if args.scan:
        frames = commander.scan_panorama()
        print(f"SCAN_COMPLETE: {len(frames)} frames capturados")
        return

    # Se nenhum argumento for passado, entra no modo interativo
    interactive_mode(commander)

if __name__ == "__main__":
    main()
