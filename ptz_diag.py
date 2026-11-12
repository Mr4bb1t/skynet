#!/usr/bin/env python3
# ==============================================================================
# PTZ_DIAG - descobre QUAL forma de enviar os pulsos a câmera realmente aceita
#
# Não usa vídeo: você observa a câmera e responde. Cada variante dispara um
# trem de pulsos, imprime o que a câmera respondeu e pergunta se ela girou.
# Depois de cada pergunta a câmera volta sozinha (mesma variante, sentido oposto).
#
# Uso:  python3 ptz_diag.py            (câmera visível, sem outro processo no RTSP)
#       python3 ptz_diag.py --pulses 60
# Respostas: s = girou bem | p = girou pouco | n = não mexeu | ENTER = pular
# ==============================================================================
import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import config  # noqa: E402
from camera.ptz import PTZController  # noqa: E402


def probe(ptz: PTZController):
    """1 pulso, conexão normal, imprime a resposta CRUA da câmera."""
    pkt = ptz._build_packet(1.0, 0.0)
    s = socket.socket()
    s.settimeout(3.0)
    t0 = time.perf_counter()
    try:
        s.connect((ptz.host, ptz.port))
        t1 = time.perf_counter()
        s.sendall(pkt)
        data = b""
        while True:
            try:
                chunk = s.recv(512)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        t2 = time.perf_counter()
        print(f"   conexão: {(t1 - t0) * 1000:.1f} ms | resposta em: {(t2 - t1) * 1000:.1f} ms")
        print(f"   resposta crua: {data[:160]!r}")
        if not data:
            print("   ⚠️  a câmera fechou/ficou muda sem responder (normal em alguns firmwares, mas anote)")
    except Exception as e:
        print(f"   ❌ erro: {e}")
    finally:
        s.close()


def legacy(ptz: PTZController, direction: float, n: int):
    """Comportamento da versão que você já tinha: conecta, envia, ESPERA resposta, repete."""
    pkt = ptz._build_packet(direction, 0.0)
    ok = 0
    t0 = time.perf_counter()
    for _ in range(n):
        s = socket.socket()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(1.5)
        try:
            s.connect((ptz.host, ptz.port))
            s.sendall(pkt)
            if b"200 OK" in s.recv(256):
                ok += 1
        except Exception:
            pass
        finally:
            s.close()
    dt = time.perf_counter() - t0
    return f"enviados={n} acks={ok} tempo={dt:.2f}s (~{dt / n * 1000:.0f} ms/pulso)"


def train(ptz: PTZController, direction: float, n: int, mode: str, preopen: bool, iv: float):
    ptz.close_mode, ptz.preopen = mode, preopen
    ptz.stream_pulses(direction, 0.0, n, interval=iv)
    s = ptz.last_stats.summary()
    return (f"enviados={s['sent']} acks={s['acked']} erros={s['errors']} atrasos={s['late']} "
            f"gap={s['gap_mean_ms']:.1f}±{s['gap_std_ms']:.1f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulses", type=int, default=40)
    ap.add_argument("--host", default=config.CAMERA_IP)
    ap.add_argument("--port", type=int, default=config.CAMERA_ONVIF_PORT)
    a = ap.parse_args()
    n = a.pulses
    ptz = PTZController(host=a.host, port=a.port)

    print("=" * 70)
    print("DIAGNÓSTICO PTZ - observe a câmera. Respostas: s=girou bem | p=pouco | n=nada | ENTER=pular")
    print("=" * 70)
    print("\n[0] Resposta crua da câmera a 1 pulso:")
    probe(ptz)

    variants = [
        ("A  legado: espera resposta de cada pulso", ("legacy",)),
        ("B  ack      sem pré-conexão 25ms", ("train", "ack", False, 0.025)),
        ("C  read     sem pré-conexão 25ms", ("train", "read", False, 0.025)),
        ("D  fin      sem pré-conexão 25ms", ("train", "fin", False, 0.025)),
        ("E  rst      sem pré-conexão 25ms", ("train", "rst", False, 0.025)),
        ("F  read     com pré-conexão 25ms", ("train", "read", True, 0.025)),
        ("G  rst      com pré-conexão 25ms", ("train", "rst", True, 0.025)),
        ("H  ack      sem pré-conexão 50ms", ("train", "ack", False, 0.050)),
        ("I  ack      sem pré-conexão 100ms", ("train", "ack", False, 0.100)),
    ]
    answers = []
    direction = 1.0
    for name, spec in variants:
        print(f"\n--- {name}  ({n} pulsos) ---")
        if input("ENTER para disparar (ou 'x' para sair): ").strip().lower() == "x":
            break
        run = (lambda d: legacy(ptz, d, n)) if spec[0] == "legacy" else \
              (lambda d, sp=spec: train(ptz, d, n, sp[1], sp[2], sp[3]))
        print("   " + run(direction))
        time.sleep(0.6)
        ans = input("A câmera girou? [s/p/n/ENTER pular]: ").strip().lower() or "-"
        answers.append((name, ans))
        print("   voltando...")
        run(-direction)
        time.sleep(1.0)
        direction = 1.0  # sempre volta ao ponto inicial

    ptz.close_mode, ptz.preopen = "read", True
    print("\n" + "=" * 70)
    print("RESUMO (cole isto de volta):")
    for name, ans in answers:
        print(f"  {name:<44} -> {ans}")


if __name__ == "__main__":
    main()
