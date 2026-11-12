echo "=== USB DEVICES (SYSFS) ==="
for d in /sys/bus/usb/devices/*; do
  if [ -f "$d/idVendor" ]; then
    v=$(cat "$d/idVendor")
    p=$(cat "$d/idProduct")
    prod=$(cat "$d/product" 2>/dev/null || echo "")
    echo "$d: $v:$p $prod"
  fi
done

echo "=== DMESG USB / BT ==="
dmesg | grep -iE "usb|bluetooth|hci" | tail -n 25

echo "=== HCICONFIG ==="
hciconfig -a 2>&1 || true

echo "=== RFKILL ==="
rfkill list 2>&1 || true

