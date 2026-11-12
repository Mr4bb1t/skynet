import sys
import config
from camera.ptz import PTZController

def test():
    print("Iniciando PTZController...")
    ptz = PTZController()
    print("Movendo pan 15 graus...")
    r1 = ptz.pan_degrees(15)
    print(f"Resultado pan +15: {r1}")
    print("Movendo pan -15 graus...")
    r2 = ptz.pan_degrees(-15)
    print(f"Resultado pan -15: {r2}")

if __name__ == "__main__":
    test()

