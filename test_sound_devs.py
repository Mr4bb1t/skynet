import subprocess
import math
import struct

sample_rate = 24000
duration = 1.0
samples = [int(20000 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(int(sample_rate * duration))]
pcm_data = struct.pack(f"{len(samples)}h", *samples)

for dev in ["default", "plughw:0,0", "plughw:2,0", "hw:0,0"]:
    print(f"\n--- Testando dispositivo: {dev} ---")
    proc = subprocess.Popen(["aplay", "-v", "-D", dev, "-r", "24000", "-f", "S16_LE", "-c", "1", "-t", "raw"],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    out, err = proc.communicate(input=pcm_data)
    print("Return code:", proc.returncode)
    if err:
        print("Stderr:\n", err.decode(errors='replace'))

