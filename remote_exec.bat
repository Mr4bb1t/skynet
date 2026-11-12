@echo off
set "SERVER_IP=192.168.1.13"
set "SERVER_USER=root"
set "SERVER_PASS=@E33426244b"
set "HOSTKEY=ssh-ed25519 255 SHA256:2h3oK++R/FYLb/Vj1K1hUxHibLcq3BLI5qIBxFmebUc"
set "PLINK=C:\Program Files\PuTTY\plink.exe"

"%PLINK%" -pw "%SERVER_PASS%" -hostkey "%HOSTKEY%" -batch %SERVER_USER%@%SERVER_IP% %*

