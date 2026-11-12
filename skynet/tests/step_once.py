import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from camera.ptz import PTZController

p = PTZController()
print("Executando 1 passo para a DIREITA...")
ok = p.step("right", count=1)
print(f"Resultado: {ok}")

