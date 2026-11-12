@echo off
setlocal
echo ============================================================
echo   SINCRONIZADOR SKYNET ROBOT - LOCAL PARA TV BOX
echo ============================================================
echo.

set "SERVER_IP=192.168.1.13"
set "SERVER_USER=root"
set "SERVER_PASS=@E33426244b"
set "HOSTKEY=ssh-ed25519 255 SHA256:2h3oK++R/FYLb/Vj1K1hUxHibLcq3BLI5qIBxFmebUc"
set "PSCP=C:\Program Files\PuTTY\pscp.exe"
set "PLINK=C:\Program Files\PuTTY\plink.exe"

set "LOCAL_DIR=%~dp0skynet"
set "REMOTE_DIR=/root/skynet"

echo [1/3] Enviando arquivos raiz...
"%PSCP%" -pw "%SERVER_PASS%" -hostkey "%HOSTKEY%" -batch "%LOCAL_DIR%\config.py" "%LOCAL_DIR%\main.py" "%LOCAL_DIR%\voice_chat.py" "%LOCAL_DIR%\ptz_console.py" "%LOCAL_DIR%\calibrate.py" "%LOCAL_DIR%\tune_cadence.py" "%LOCAL_DIR%\requirements.txt" "%LOCAL_DIR%\README.md" "%LOCAL_DIR%\.env.example" %SERVER_USER%@%SERVER_IP%:%REMOTE_DIR%/

echo [2/3] Enviando modulos camera, audio, core, memory, tests, vision e scripts...
"%PSCP%" -pw "%SERVER_PASS%" -hostkey "%HOSTKEY%" -batch -r "%LOCAL_DIR%\camera" "%LOCAL_DIR%\audio" "%LOCAL_DIR%\core" "%LOCAL_DIR%\memory" "%LOCAL_DIR%\tests" "%LOCAL_DIR%\vision" "%LOCAL_DIR%\scripts" %SERVER_USER%@%SERVER_IP%:%REMOTE_DIR%/

echo [3/3] Validando sintaxe Python e instalando servico no servidor...
"%PLINK%" -pw "%SERVER_PASS%" -hostkey "%HOSTKEY%" -batch %SERVER_USER%@%SERVER_IP% "[ -f /root/skynet/.env ] || cp /root/skynet/.env.example /root/skynet/.env; chmod 600 /root/skynet/.env && python3 -m py_compile /root/skynet/config.py /root/skynet/main.py /root/skynet/voice_chat.py /root/skynet/camera/ptz.py /root/skynet/audio/sources.py /root/skynet/audio/player.py /root/skynet/audio/live_agent.py /root/skynet/audio/aec.py /root/skynet/core/alsa_detector.py /root/skynet/core/env_manager.py /root/skynet/core/config_tui.py /root/skynet/core/time_sync.py && python3 -m compileall -q /root/skynet/memory /root/skynet/vision && cp -f /root/skynet/scripts/sync_time.py /root/sync_time.py && chmod 755 /root/sync_time.py && rm -rf /root/skynet/scripts/__pycache__ && find /root/skynet/scripts /root/skynet/audio/native -maxdepth 1 -type f -exec sed -i 's/\r$//' {} + && chmod +x /root/skynet/scripts/*.sh /root/skynet/scripts/skynet* /root/skynet/audio/native/*.sh && bash /root/skynet/scripts/install_service.sh"

echo.
echo ============================================================
echo   SUCESSO! Servidor TV Box atualizado com exito.
echo ============================================================
echo.
endlocal
