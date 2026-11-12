#!/usr/bin/env python3
# ==============================================================================
# TUNE_CADENCE - descobre o ritmo real em que a SUA câmera encadeia os passos
#
# Para cada (modo de fechamento, intervalo) dispara um trem de N pulsos e mede,
# pelo vídeo RTSP (deslocamento horizontal da imagem quadro a quadro):
#   - velocidade: px/s médios durante o trem (mostra se a câmera perde pulso)
#   - fluidez   : fração dos quadros, durante o trem, em que a imagem se move
#   - cv        : variação da velocidade quadro a quadro (menor = mais liso)
#   - cauda     : por quantos ms a câmera continua girando DEPOIS do último pacote
# e recomenda o MAIOR intervalo ainda contínuo (menos pacotes) com folga.
# ATENÇÃO: graus por pulso dependem do intervalo. Se mudar PULSE_INTERVAL_SEC,
# rode calibrate.py de novo.
#
# Uso (no TV Box, com a câmera apontada para uma cena com textura, não parede lisa):
#   python3 tune_cadence.py                       # varredura completa
#   python3 tune_cadence.py --modes rst,read --steps 40   # >= 40 pulsos: um pulso isolado quase não move
#   python3 tune_cadence.py --apply               # grava a recomendação no config.py
#   python3 tune_cadence.py --selftest            # testa só a análise (sem câmera)
# O pan alterna direita/esquerda a cada rodada: a câmera volta ao ponto inicial.
# ==============================================================================
import argparse
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from operator import sub
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import config  # noqa: E402

W, H, FPS = 160, 90, 15
MAXSHIFT = 40   # câmera real gira rápido: ~20 px/quadro a 15 fps
_now = time.perf_counter


# ------------------------------------------------------------------------------
# Análise de imagem (Python puro: roda no TV Box sem numpy)
# ------------------------------------------------------------------------------
def col_profile(frame: bytes, w: int = W, h: int = H) -> List[float]:
    """Média de cada coluna (perfil horizontal da imagem em tons de cinza)."""
    return [sum(frame[c::w]) / h for c in range(w)]


def best_shift(old: List[float], new: List[float], maxs: int = MAXSHIFT) -> Tuple[float, float]:
    """
    Deslocamento horizontal (px, sub-pixel) entre dois perfis. s > 0 = o conteúdo
    andou para a ESQUERDA (a câmera girou para a direita). Retorna (shift, qualidade).
    """
    n = len(old)
    sads = []
    for s in range(-maxs, maxs + 1):
        lo, hi = max(0, -s), min(n, n - s)
        sads.append(sum(map(abs, map(sub, new[lo:hi], old[lo + s:hi + s]))) / (hi - lo))
    i = min(range(len(sads)), key=sads.__getitem__)
    shift = float(i - maxs)
    if 0 < i < len(sads) - 1:  # parábola nos 3 pontos -> sub-pixel
        a, b, c = sads[i - 1], sads[i], sads[i + 1]
        den = a - 2 * b + c
        if den > 1e-9:
            shift += 0.5 * (a - c) / den
    mean_sad = sum(sads) / len(sads)
    quality = 0.0 if mean_sad <= 1e-9 else 1.0 - sads[i] / mean_sad
    return shift, quality


class Motion:
    """Linha do tempo de deslocamentos: lista de (instante_do_quadro, dx_px)."""

    def __init__(self):
        self.ev: List[Tuple[float, float]] = []
        self.lock = threading.Lock()

    def add(self, t: float, dx: float):
        with self.lock:
            self.ev.append((t, dx))

    def window(self, a: float, b: float) -> List[Tuple[float, float]]:
        with self.lock:
            return [(t, d) for t, d in self.ev if a <= t <= b]


class FrameGrabber:
    """ffmpeg -> quadros cinza 160x90 -> deslocamento por quadro (sem guardar imagens)."""

    def __init__(self, url: str, motion: Motion, transport: str = "udp"):
        self.motion, self.stopped = motion, False
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", transport, "-i", url,
             "-vf", f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.frames = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        size, prev = W * H, None
        fd = self.proc.stdout.fileno()
        buf = b""
        while not self.stopped:
            chunk = os.read(fd, size - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) < size:
                continue
            t, frame, buf = _now(), buf, b""
            prof = col_profile(frame)
            self.frames += 1
            if prev is not None:
                dx, q = best_shift(prev, prof)
                self.motion.add(t, dx if q > 0.05 else 0.0)
            prev = prof

    def stop(self):
        self.stopped = True
        try:
            self.proc.kill()
        except Exception:
            pass


# ------------------------------------------------------------------------------
# Métricas de um trem de pulsos
# ------------------------------------------------------------------------------
def analyze(motion: Motion, t0: float, t1: float, lag: float, tail_s: float,
            sign: float, pps: float, n: int) -> Dict[str, float]:
    """t0/t1 = início/fim do envio; lag = atraso envio->imagem."""
    main = motion.window(t0 + lag - 0.1, t1 + lag)
    tail = motion.window(t1 + lag, t1 + lag + tail_s)
    net_main = sign * sum(d for _, d in main)
    net_tail = sign * sum(d for _, d in tail)
    speed = net_main / max(1e-6, (t1 - t0))
    inside = [abs(d) for t, d in motion.window(t0 + lag, t1 + lag)]
    duty = (sum(1 for d in inside if d >= 0.6) / len(inside)) if inside else 0.0
    core = inside[2:-2] if len(inside) >= 8 else inside
    cv = (statistics.pstdev(core) / statistics.mean(core)) if core and statistics.mean(core) > 0 else 9.9
    tail_ms = (net_tail / speed * 1000.0) if speed > 1e-6 else 0.0
    return {"speed": speed, "px_per_pulse": net_main / n, "tail_ms": tail_ms, "duty": duty, "cv": cv}


def choose(rows: List[Dict], frame_period: float) -> Optional[Dict]:
    """Maior intervalo que ainda é CONTÍNUO (fluidez >= 0.9 e velocidade regular)."""
    cont = [r for r in rows if r["duty"] >= 0.9 and r["cv"] <= 0.6 and r["speed"] > 0]
    return max(cont, key=lambda r: r["interval"]) if cont else None


# ------------------------------------------------------------------------------
# Execução
# ------------------------------------------------------------------------------
def apply_to_config(values: Dict[str, object]):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    shutil.copyfile(path, path + ".bak")
    text = open(path, encoding="utf-8").read()
    for k, v in values.items():
        line = f"{k} = {v!r}"
        if re.search(rf"^{k}\s*=", text, flags=re.M):
            text = re.sub(rf"^{k}\s*=.*$", line, text, flags=re.M)
        else:
            text += ("\n" if not text.endswith("\n") else "") + line + "\n"
    open(path, "w", encoding="utf-8").write(text)
    print(f"💾 gravado em {path} (backup: config.py.bak)")


def run_tune(ptz, motion: Motion, modes: List[str], intervals: List[float], n: int,
             settle: float = 2.0, tail_s: float = 1.5, log=print):
    # --- calibração em RAJADA encadeada: um pulso isolado quase não move esta câmera ---
    CAL_N, CAL_IV = 24, 0.025
    log(f"\n[calibração] rajada de {CAL_N} pulsos a {CAL_IV * 1000:.0f} ms para medir o sentido, "
        "a escala e o atraso do vídeo...")
    ptz.close_mode = "read"
    t0 = _now()
    ptz.stream_pulses(1.0, 0.0, CAL_N, interval=CAL_IV)
    t1 = _now()
    time.sleep(settle + 1.0)
    ev = motion.window(t0, t1 + 4.0)
    net = sum(d for _, d in ev)
    if abs(net) < 4.0:
        log("❌ Quase nenhum movimento detectado na imagem mesmo com a rajada. Aponte a câmera para uma "
            "cena com textura (não parede lisa) e confira o RTSP (a imagem precisa estar mudando).")
        return None
    sign = 1.0 if net > 0 else -1.0
    pps = abs(net) / CAL_N
    first = next((t for t, d in ev if abs(d) >= 1.0), t0)
    lag = max(0.0, first - t0)
    log(f"   deslocamento da rajada = {abs(net):.0f} px | sinal direita = {'+' if sign > 0 else '-'} "
        f"| atraso envio->imagem = {lag * 1000:.0f} ms")
    ptz.stream_pulses(-1.0, 0.0, CAL_N, interval=CAL_IV)
    time.sleep(settle + 1.0)

    results: Dict[str, List[Dict]] = {}
    direction = 1.0
    for mode in modes:
        ptz.close_mode = mode
        rows: List[Dict] = []
        log(f"\n=== modo de fechamento: {mode} ===")
        log(f"{'intervalo':>10} {'px/s':>8} {'px/pulso':>9} {'fluidez':>8} {'cv':>6} {'cauda ms':>9} {'jitter p99':>11}")
        for iv in intervals:
            ta = _now()
            ptz.stream_pulses(direction, 0.0, n, interval=iv)
            tb = _now()
            time.sleep(tail_s + settle)
            m = analyze(motion, ta, tb, lag, tail_s, sign * direction, pps, n)
            jit = ptz.last_stats.summary()["jitter_p99_us"] if ptz.last_stats else 0.0
            m.update(interval=iv)
            rows.append(m)
            log(f"{iv * 1000:8.0f}ms {m['speed']:8.1f} {m['px_per_pulse']:9.2f} {m['duty']:8.2f} {m['cv']:6.2f} {m['tail_ms']:9.0f} {jit:9.0f}µs")
            direction = -direction
        results[mode] = rows
    return results


def recommend(results: Dict[str, List[Dict]], log=print) -> Optional[Dict[str, object]]:
    best = None
    for mode, rows in results.items():
        pick = choose(rows, 1.0 / FPS)
        if pick and (best is None or (pick["duty"], pick["interval"]) > (best[1]["duty"], best[1]["interval"])):
            best = (mode, pick)
    if not best:
        log("\n❌ Nenhum intervalo ficou contínuo. Veja a tabela: se a fluidez é baixa até no intervalo mais "
            "curto, o firmware tem tempo morto entre pulsos; se a velocidade é ~0, a cena é lisa demais.")
        return None
    mode, pick = best
    rec = round(pick["interval"] * 0.8, 3)    # 20% mais denso que o limite contínuo
    log(f"\n✅ Maior intervalo contínuo: {pick['interval'] * 1000:.0f} ms (modo {mode}). "
        f"Recomendado com folga: {rec * 1000:.0f} ms | a câmera ainda gira ~{pick['tail_ms']:.0f} ms após o último pacote")
    log(f"   PULSE_INTERVAL_SEC = {rec}\n   PULSE_CLOSE_MODE   = {mode!r}")
    log("   (se mudar o intervalo, rode calibrate.py: graus por pulso dependem dele)")
    return {"PULSE_INTERVAL_SEC": rec, "PULSE_CLOSE_MODE": mode}


# ------------------------------------------------------------------------------
# Autoteste da análise (sem câmera): imagem sintética + câmera simulada
# ------------------------------------------------------------------------------
def _selftest() -> bool:
    import random
    random.seed(1)
    base, v = [], 0.0
    for _ in range(3000):
        v += random.uniform(-9, 9)
        v = max(20.0, min(235.0, v))
        base.append(v)

    def prof(off: float) -> List[float]:
        i, f = int(off // 1), off - int(off // 1)
        return [base[i + c] * (1 - f) + base[i + c + 1] * f for c in range(W)]

    ok = True
    for true in (0.0, 1.0, 2.0, 3.5, -4.25, 7.8, -12.3):
        got, q = best_shift(prof(100.0), prof(100.0 + true))
        good = abs(got - true) < 0.35
        ok &= good
        print(f"  shift real={true:+6.2f}  medido={got:+6.2f}  [{'OK' if good else 'FALHOU'}]")

    # quadro bruto -> perfil de colunas
    frame = bytes(int(base[c]) for _ in range(H) for c in range(W))
    pr = col_profile(frame)
    good = all(abs(pr[c] - int(base[c])) < 1e-6 for c in range(W))
    ok &= good
    print(f"  col_profile em quadro bruto [{'OK' if good else 'FALHOU'}]")

    # câmera simulada: firmware aceita passo só se passaram >= 0.12 s do último aceito;
    # cada passo aceito move 2 px em 0.10 s; vídeo com 0.25 s de atraso a 15 fps.
    class SimPTZ:
        close_mode, last_stats = "read", None

        def __init__(self, motion):
            self.m = motion

        def stream_pulses(self, x, y, n, interval=0.025):
            # firmware simulado: cada pulso mantém o giro por 0.2 s; vídeo atrasa 0.25 s
            sgn = 1.0 if x > 0 else -1.0
            t = _now()
            spans = [(t + k * interval, t + k * interval + 0.2) for k in range(n)]
            time.sleep(n * interval)
            f, end = t, t + (n - 1) * interval + 0.2 + 0.6
            while f < end:
                moving = any(a <= f < b for a, b in spans)
                self.m.add(f + 0.25, sgn * 3.0 if moving else 0.0)   # quadros parados também contam
                f += 1.0 / FPS
            return True

    motion = Motion()
    sim = SimPTZ(motion)
    res = run_tune(sim, motion, ["read"], [0.5, 0.25, 0.15, 0.10, 0.06], n=8, settle=0.3, tail_s=0.6,
                   log=lambda *_: None)
    pick = choose(res["read"], 1.0 / FPS) if res else None
    good = bool(pick) and abs(pick["interval"] - 0.15) < 1e-9
    ok &= good
    print(f"  maior intervalo contínuo esperado = 150 ms (cada pulso estende o giro por 200 ms) | achado = "
          f"{(pick['interval'] * 1000 if pick else float('nan')):.0f} ms [{'OK' if good else 'FALHOU'}]")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Mede o ritmo real de encadeamento de passos da câmera")
    ap.add_argument("--modes", default="rst,fin,read")
    ap.add_argument("--intervals", default="0.25,0.15,0.10,0.06,0.04,0.025,0.015")
    ap.add_argument("--steps", type=int, default=24)
    ap.add_argument("--rtsp", default=getattr(config, "RTSP_SUB_STREAM_URL", ""))
    ap.add_argument("--transport", default="udp", help="udp (padrão, como o vision.py) ou tcp")
    ap.add_argument("--apply", action="store_true", help="grava a recomendação no config.py")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        print("Autoteste da análise (sem câmera):")
        ok = _selftest()
        print("\n✅ tudo certo" if ok else "\n❌ falhou")
        sys.exit(0 if ok else 1)

    from camera.ptz import PTZController
    motion = Motion()
    grab = FrameGrabber(a.rtsp, motion, a.transport)
    print("Aguardando vídeo estável (até 10 s)...")
    t = _now()
    while grab.frames < 20 and _now() - t < 10:
        time.sleep(0.2)
    if grab.frames < 20:
        print("❌ RTSP não entregou quadros. Pare o stream em segundo plano do vision.py (a câmera aceita poucos clientes), confira ffmpeg/URL/senha e tente --transport tcp.")
        grab.stop()
        sys.exit(1)

    ptz = PTZController()
    try:
        res = run_tune(ptz, motion, a.modes.split(","), [float(x) for x in a.intervals.split(",")], a.steps)
        if res:
            rec = recommend(res)
            if rec and a.apply:
                apply_to_config(rec)
    except KeyboardInterrupt:
        print("\ninterrompido")
    finally:
        ptz.halt()
        grab.stop()


if __name__ == "__main__":
    main()