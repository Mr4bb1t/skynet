import subprocess
import time
import math
import struct

def gen_tone(freq=440, dur=1.0, rate=48000):
    samples = [int(25000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * dur))]
    stereo = []
    for s in samples:
        stereo.append(s)
        stereo.append(s)
    return struct.pack(f"{len(stereo)}h", *stereo)

tone = gen_tone(440, 1.0, 48000)

devices = [
    "plughw:0,0",
    "plughw:1,0",
    "plughw:1,1",
    "plughw:1,2",
    "plughw:2,0",
    "plughw:3,0",
]

# Configura mixer de card 0 para LROMixer (ambos os canais)
subprocess.run(["amixer", "-c", "0", "cset", "numid=6", "on"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=5", "31"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=7", "on"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=8", "on"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=11", "on"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=12", "on"], check=False)
subprocess.run(["amixer", "-c", "0", "cset", "numid=15", "1"], check=False) # LROMixer
subprocess.run(["amixer", "-c", "0", "cset", "numid=16", "1"], check=False) # LROMixer

print("=== TESTANDO CARD 0 COM DIGITAL VOLUME = 0 ===")
subprocess.run(["amixer", "-c", "0", "cset", "numid=2", "0"], check=False)
p = subprocess.Popen(["aplay", "-D", "plughw:0,0", "-r", "48000", "-f", "S16_LE", "-c", "2", "-t", "raw"],
                     stdin=subprocess.PIPE, stderr=subprocess.PIPE)
p.communicate(input=tone)
print("plughw:0,0 (vol 0) return code:", p.returncode)

print("\n=== TESTANDO CARD 0 COM DIGITAL VOLUME = 60 ===")
subprocess.run(["amixer", "-c", "0", "cset", "numid=2", "60"], check=False)
p = subprocess.Popen(["aplay", "-D", "plughw:0,0", "-r", "48000", "-f", "S16_LE", "-c", "2", "-t", "raw"],
                     stdin=subprocess.PIPE, stderr=subprocess.PIPE)
p.communicate(input=tone)
print("plughw:0,0 (vol 60) return code:", p.returncode)

for dev in ["plughw:1,0", "plughw:1,1", "plughw:3,0"]:
    print(f"\n=== TESTANDO {dev} ===")
    p = subprocess.Popen(["aplay", "-D", dev, "-r", "48000", "-f", "S16_LE", "-c", "2", "-t", "raw"],
                         stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = p.communicate(input=tone)
    print(f"{dev} return code: {p.returncode}")
    if p.returncode != 0:
        print("Erro:", err.decode(errors='replace'))

