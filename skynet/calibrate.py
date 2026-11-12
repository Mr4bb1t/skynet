#!/usr/bin/env python3
# ==============================================================================
# SCRIPT DE CALIBRAÇÃO DE GRAUS POR PASSO - SKYNET ROBOT
# Mede exatamente a razão de graus por passo mecânico da câmera
# ==============================================================================
import time
import sys
import re
from camera.ptz import PTZController
import config

def update_config_file(param_name: str, new_value: float):
    config_path = "/root/skynet/config.py"
    try:
        with open(config_path, "r") as f:
            content = f.read()
        
        pattern = rf"{param_name}\s*=\s*[\d\.]+"
        replacement = f"{param_name} = {new_value:.3f}"
        new_content = re.sub(pattern, replacement, content)
        
        with open(config_path, "w") as f:
            f.write(new_content)
        print(f"💾 {param_name} atualizado para {new_value:.3f} no config.py!")
    except Exception as e:
        print(f"Não foi possível gravar no arquivo config.py: {e}")

def run_calibration():
    ptz = PTZController()
    print("=" * 65)
    print("🎯 CALIBRADOR DE GRAUS POR PASSO (CADÊNCIA ESTABILIZADA)")
    print("=" * 65)
    print(f"Valor atual Pan:  {config.DEGREES_PER_STEP_PAN:.3f}° por passo")
    print(f"Referência:       180° = {int(round(180 / config.DEGREES_PER_STEP_PAN))} passos | 360° = {int(round(360 / config.DEGREES_PER_STEP_PAN))} passos")
    print(f"Cadência fixa:    {config.PULSE_INTERVAL_SEC * 1000:.1f}ms por passo")
    print("=" * 65)
    
    num_steps = 80
    print(f"\n[Etapa 1] Vamos disparar uma rajada de {num_steps} passos para a DIREITA (~90°).")
    print("Observe visualmente a rotação da câmera.")
    input("\nPressione [ENTER] para iniciar a rajada...")
    
    print(f"Executando {num_steps} passos com compensação de jitter...")
    ptz.stream_pulses(1.0, 0.0, count=num_steps)
    print("Rajada concluída!")
    
    ans = input(f"\nAproximadamente quantos GRAUS a câmera girou no total nesses {num_steps} passos? (ex: 90, 85, 95): ").strip()
    try:
        total_deg = float(ans)
        deg_per_step = total_deg / float(num_steps)
        steps_180 = int(round(180.0 / deg_per_step))
        steps_360 = int(round(360.0 / deg_per_step))
        print(f"\n✅ Calibração calculada:")
        print(f"   -> 1 passo = {deg_per_step:.3f}°")
        print(f"   -> 180° = {steps_180} passos (~{steps_180 * config.PULSE_INTERVAL_SEC:.1f}s)")
        print(f"   -> 360° = {steps_360} passos (~{steps_360 * config.PULSE_INTERVAL_SEC:.1f}s)")
        
        apply = input("\nDeseja salvar essa calibração no config.py? [s/n]: ").strip().lower()
        if apply == "s":
            update_config_file("DEGREES_PER_STEP_PAN", deg_per_step)
            config.DEGREES_PER_STEP_PAN = deg_per_step
    except ValueError:
        print("Valor inválido inserido.")

    print("\nRetornando para a posição inicial...")
    ptz.stream_pulses(-1.0, 0.0, count=num_steps)
    print("✅ Posição inicial restaurada!")
    
    print("\n" + "=" * 65)
    test_180 = input("Deseja testar um giro completo de 180° contínuo agora? [s/n]: ").strip().lower()
    if test_180 == "s":
        print("Executando giro de 180°...")
        ptz.pan_degrees(180)
        print("180° concluído! Retornando...")
        time.sleep(1.0)
        ptz.pan_degrees(-180)
        print("✅ Giro de 180° e retorno concluídos!")

if __name__ == "__main__":
    run_calibration()
