echo "=== USB DEVICES (SYSFS) ==="
for d in /sys/bus/usb/devices/*; do
  if [ -f "$d/idVendor" ]; then
    v=$(cat "$d/idVendor")
    p=$(cat "$d/idProduct")
    prod=$(cat "$d/product" 2>/dev/null || echo "")
    manuf=$(cat "$d/manufacturer" 2>/dev/null || echo "")
    echo "$d: $v:$p [$manuf] $prod"
  fi
done

echo "=== DMESG USB / BT ==="
dmesg | grep -iE "usb|bluetooth|hci" | tail -n 25

echo "=== HCICONFIG ==="
hciconfig -a 2>&1

echo "=== RFKILL ==="
rfkill list 2>&1

echo "=== PACKAGES & TOOLS ==="
which bluetoothctl bluez-alsa bluealsa pulseaudio aplay arecord 2>&1
dpkg -l | grep -iE "bluez|bluetooth|alsa" | head -n 20

