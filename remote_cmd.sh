export PAGER=cat
export DEBIAN_FRONTEND=noninteractive

echo "=== RFKILL / DEV ==="
ls -l /dev/rfkill 2>/dev/null || echo "No /dev/rfkill"
ls -l /dev/bluetooth 2>/dev/null || echo "No /dev/bluetooth"

echo "=== PACKAGES ==="
dpkg --no-pager -l | grep -iE 'bluez|bluetooth' || echo "Nenhum pacote bluetooth encontrado"

echo "=== BINARIES ==="
which bluetoothctl bluez-alsa bluealsa pulseaudio rfkill hciconfig aplay arecord || true

echo "=== SYSFS BLUETOOTH ==="
ls -la /sys/class/bluetooth/ 2>/dev/null || echo "Sem /sys/class/bluetooth"
