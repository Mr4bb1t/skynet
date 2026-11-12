import sys
import subprocess

PLINK = r"C:\Program Files\PuTTY\plink.exe"
HOSTKEY = "ssh-ed25519 255 SHA256:2h3oK++R/FYLb/Vj1K1hUxHibLcq3BLI5qIBxFmebUc"
PASS = "@E33426244b"
TARGET = "root@192.168.1.13"

def run_bash(script_str):
    res = subprocess.run([
        PLINK, "-pw", PASS, "-hostkey", HOSTKEY, "-batch",
        TARGET, "bash -s"
    ], input=script_str, capture_output=True, text=True, timeout=60)
    print("STDOUT:\n" + res.stdout)
    if res.stderr:
        print("STDERR:\n" + res.stderr)
    return res.returncode

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            code = f.read()
    elif len(sys.argv) > 1:
        code = sys.argv[1]
    else:
        code = "uname -a"
    sys.exit(run_bash(code))
