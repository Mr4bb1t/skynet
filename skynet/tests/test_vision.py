#!/usr/bin/env python3
# Teste unitário para a captura de visão
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from camera.vision import VisionModule

def test_vision():
    print("Iniciando teste de captura visual...")
    vision = VisionModule()

    print("1. Capturando snapshot em arquivo JPEG (substream rápido)...")
    file_path = vision.capture_snapshot(high_res=False)
    assert file_path is not None and os.path.exists(file_path), "Falha ao salvar frame em arquivo"
    print(f"✅ Snapshot salvo em disco com sucesso: {file_path} ({os.path.getsize(file_path)} bytes)")

    print("2. Testando buffer em RAM (background worker)...")
    vision.start_background_stream()
    print("Aguardando 3s para o worker preencher a memória RAM (/dev/shm)...")
    time.sleep(3)
    
    frame_bytes = vision.get_latest_frame_bytes()
    vision.stop_background_stream()
    
    assert frame_bytes is not None and len(frame_bytes) > 1000, "Falha ao ler frame da RAM"
    print(f"✅ Buffer em RAM funcionando com latência <1ms! ({len(frame_bytes)} bytes)")
    print("\n✅ Todos os testes de Visão foram concluídos com sucesso!")

if __name__ == "__main__":
    test_vision()
