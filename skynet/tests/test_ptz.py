#!/usr/bin/env python3
# Teste unitário para movimento e micro-passos PTZ
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from camera.ptz import PTZController

def test_ptz():
    print("Iniciando teste de controle PTZ...")
    ptz = PTZController()

    print("1. Teste de micro-passos (step)...")
    assert ptz.step("left", count=2), "Falha no micro-passo esquerda"
    time.sleep(0.3)
    assert ptz.step("right", count=2), "Falha no micro-passo direita"
    time.sleep(0.3)

    print("2. Teste de giro por graus (20°)...")
    assert ptz.pan_degrees(20), "Falha no giro 20° direita"
    time.sleep(0.5)
    assert ptz.pan_degrees(-20), "Falha no retorno 20° esquerda"
    
    print("\n✅ Todos os testes de movimento PTZ foram bem-sucedidos!")

if __name__ == "__main__":
    test_ptz()
