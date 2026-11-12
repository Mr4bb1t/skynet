#!/bin/sh
/system/bin/setprop service.adb.tcp.port 5555
/system/bin/stop adbd
/system/bin/start adbd
sleep 1
/system/bin/cmd media_session volume --stream 3 --set 15 2>/dev/null
/system/bin/media volume --stream 3 --set 15 2>/dev/null
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    /system/bin/input keyevent 24 2>/dev/null
done
echo "VOLUME_UP_DONE"

