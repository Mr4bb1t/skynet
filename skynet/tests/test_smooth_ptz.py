#!/usr/bin/env python3
"""Teste de movimento suave PTZ com heartbeat encadeado."""
import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from camera.ptz import PTZController

p = PTZController()

print("=== Teste de Movimento Suave PTZ ===\n")

print("1. Giro suave 30° para DIREITA...")
t0 = time.perf_counter()
ok = p.pan_degrees(30)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s  (esperado ~{30/45:.2f}s)\n")
time.sleep(0.5)

print("2. Giro suave 30° para ESQUERDA (volta)...")
t0 = time.perf_counter()
ok = p.pan_degrees(-30)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s\n")
time.sleep(0.5)

print("3. Inclinação suave 15° para CIMA...")
t0 = time.perf_counter()
ok = p.tilt_degrees(15)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s  (esperado ~{15/35:.2f}s)\n")
time.sleep(0.5)

print("4. Inclinação suave 15° para BAIXO (volta)...")
t0 = time.perf_counter()
ok = p.tilt_degrees(-15)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s\n")
time.sleep(0.5)

print("5. Giro longo 90° para DIREITA...")
t0 = time.perf_counter()
ok = p.pan_degrees(90)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s  (esperado ~{90/45:.2f}s)\n")
time.sleep(0.5)

print("6. Volta 90° para ESQUERDA...")
t0 = time.perf_counter()
ok = p.pan_degrees(-90)
dt = time.perf_counter() - t0
print(f"   OK={ok}  Duração={dt:.2f}s\n")

print("\n=== Teste concluído! ===")

