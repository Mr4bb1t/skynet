@echo off
C:\adb\adb.exe connect 192.168.1.13:5555
C:\adb\adb.exe -s 192.168.1.13:5555 shell "media volume --stream 3 --set 15"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "cmd media_session volume --stream 3 --set 15"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "input keyevent 24"
C:\adb\adb.exe -s 192.168.1.13:5555 shell "dumpsys audio | grep -E 'STREAM_MUSIC|mVolumeSteps|Current:'"
echo Volume maximizado com sucesso!

