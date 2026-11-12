#!/usr/bin/env python3
# ==============================================================================
# TESTE DE TIMING DO ENCADEAMENTO PTZ (não precisa da câmera)
#
# Sobe uma "câmera falsa" local que registra o instante em que cada pacote
# chega e responde com latência aleatória (como o firmware real faz), e compara:
#   - ANTIGO: conecta, envia, ESPERA a resposta, repete (período = latência)
#   - NOVO:   trem de pulsos com relógio absoluto + pré-conexão
#
# Isto valida o AGENDADOR (regularidade do que chega na câmera). Se a câmera
# real aceita esse ritmo é outra pergunta: use tune_cadence.py para medir.
# ==============================================================================
import os
import random
import socket
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import config  # noqa: E402
from camera.ptz import PTZController  # noqa: E402


class FakeCamera:
    """Registra a chegada de cada ContinuousMove; descarta se vier colado demais."""

    def __init__(self, lat=(0.005, 0.060), min_gap=0.020):
        self.lat, self.min_gap = lat, min_gap
        self.arrivals, self.accepted, self.dropped = [], 0, 0
        self._last_ok = -1e9
        self._lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        try:
            c.settimeout(2.0)
            buf = b""
            while b"</soap:Envelope>" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            t = time.perf_counter()
            with self._lock:
                self.arrivals.append(t)
                if t - self._last_ok >= self.min_gap:
                    self._last_ok, self.accepted = t, self.accepted + 1
                else:
                    self.dropped += 1
            time.sleep(random.uniform(*self.lat))
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
        finally:
            c.close()

    def stats(self):
        a = sorted(self.arrivals)
        g = [(y - x) * 1000 for x, y in zip(a, a[1:])]
        if not g:
            return {}
        gs = sorted(g)
        return {"n": len(a), "accepted": self.accepted, "dropped": self.dropped,
                "gap_mean_ms": statistics.mean(g), "gap_std_ms": statistics.pstdev(g),
                "gap_p99_ms": gs[int(len(gs) * 0.99)], "gap_max_ms": gs[-1],
                "total_s": a[-1] - a[0]}


class OldSequentialPTZ:
    """Comportamento da versão anterior: connect -> send -> recv (bloqueia) -> próximo."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        body = (b'<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>'
                b'<ptz:ContinuousMove xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl">'
                b'<ptz:ProfileToken>x</ptz:ProfileToken></ptz:ContinuousMove></soap:Body></soap:Envelope>')
        self.pkt = (b"POST /onvif/ptz_service HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n"
                    b"Connection: close\r\n\r\n" % len(body)) + body

    def stream_pulses(self, count):
        for _ in range(count):
            s = socket.socket()
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(1.5)
            try:
                s.connect((self.host, self.port))
                s.sendall(self.pkt)
                s.recv(256)
            except Exception:
                pass
            finally:
                s.close()


class FakeCameraProc:
    """Câmera falsa em OUTRO processo (a medição não sofre o GIL do processo de teste)."""

    def __init__(self, lat=(0.005, 0.060), min_gap=0.020):
        import subprocess
        self.p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", str(lat[0]), str(lat[1]), str(min_gap)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.port = int(self.p.stdout.readline())

    def stats(self):
        import json
        self.p.stdin.write("STATS\n")
        self.p.stdin.flush()
        s = json.loads(self.p.stdout.readline())
        self.p.stdin.close()
        self.p.wait(timeout=2)
        return s


def serve(lo, hi, min_gap):
    cam = FakeCamera(lat=(lo, hi), min_gap=min_gap)
    print(cam.port, flush=True)
    sys.stdin.readline()
    import json
    print(json.dumps(cam.stats()), flush=True)


def fmt(name, s):
    return (f"{name:<22} n={s['n']:<4} aceitos={s['accepted']:<4} descartados={s['dropped']:<4} "
            f"gap médio={s['gap_mean_ms']:6.2f}ms  desvio={s['gap_std_ms']:6.2f}ms  "
            f"p99={s['gap_p99_ms']:6.2f}ms  máx={s['gap_max_ms']:6.2f}ms  total={s['total_s']:.2f}s")


def run(n=160, interval=0.025):
    random.seed(7)
    results = {}

    cam = FakeCameraProc()
    OldSequentialPTZ("127.0.0.1", cam.port).stream_pulses(n)
    time.sleep(0.2)
    results["antigo"] = cam.stats()

    for mode in ("rst", "fin", "read"):
        cam = FakeCameraProc()
        ptz = PTZController(host="127.0.0.1", port=cam.port, interval=interval, close_mode=mode)
        ptz.stream_pulses(1.0, 0.0, n)
        time.sleep(0.2)
        results[f"novo ({mode})"] = cam.stats()
        results[f"novo ({mode})"]["_tx"] = ptz.last_stats.summary()
    return results


def main():
    n, iv = 160, 0.025
    print(f"{n} passos, alvo = {iv * 1000:.0f} ms entre pacotes "
          f"(câmera falsa: latência 5-60 ms, descarta pacote colado < 20 ms)\n")
    res = run(n, iv)
    for k, v in res.items():
        print(fmt(k, v))
    print()
    for k, v in res.items():
        if "_tx" in v:
            t = v["_tx"]
            print(f"{k:<14} lado do envio: jitter p50={t['jitter_p50_us']:.0f}µs "
                  f"p99={t['jitter_p99_us']:.0f}µs máx={t['jitter_max_us']:.0f}µs "
                  f"atrasos={t['late']} erros={t['errors']} acks={t['acked']}")

    # Critérios: o que chega na câmera tem que ser regular e completo.
    ok = True
    for mode in ("rst", "fin", "read"):
        s = res[f"novo ({mode})"]
        good = (s["n"] == n and s["dropped"] == 0 and abs(s["gap_mean_ms"] - iv * 1000) < 1.0
                and s["gap_std_ms"] < 3.0)
        ok &= good
        print(f"[{'OK' if good else 'FALHOU'}] novo ({mode})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]))
    else:
        main()
