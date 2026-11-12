# ==============================================================================
# Script PowerShell para Sincronização Automática com o TV Box
# Execução: .\sync_to_server.ps1
# ==============================================================================
$ErrorActionPreference = "Stop"

$SERVER_IP   = "192.168.1.13"
$SERVER_USER = "root"
$SERVER_PASS = "@E33426244b"
$HOSTKEY     = "ssh-ed25519 255 SHA256:2h3oK++R/FYLb/Vj1K1hUxHibLcq3BLI5qIBxFmebUc"
$PSCP        = "C:\Program Files\PuTTY\pscp.exe"
$PLINK       = "C:\Program Files\PuTTY\plink.exe"

$LocalSkynet = Join-Path $PSScriptRoot "skynet"
$RemoteDir   = "/root/skynet"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  🚀 SINCRONIZADOR SKYNET - LOCAL -> SERVIDOR DEBIAN" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

if (-not (Test-Path $LocalSkynet)) {
    Write-Host "[ERRO] Diretório local '$LocalSkynet' não encontrado!" -ForegroundColor Red
    exit 1
}

try {
    Write-Host "[1/2] Enviando arquivos atualizados via PSCP..." -ForegroundColor Yellow
    
    # Arquivos raiz (o .env fica no box e NAO e sobrescrito: a TUI salva nele)
    & $PSCP -pw $SERVER_PASS -hostkey $HOSTKEY -batch "$LocalSkynet\config.py" "$LocalSkynet\main.py" "$LocalSkynet\voice_chat.py" "$LocalSkynet\ptz_console.py" "$LocalSkynet\calibrate.py" "$LocalSkynet\tune_cadence.py" "$LocalSkynet\requirements.txt" "$LocalSkynet\README.md" "$LocalSkynet\.env.example" "$($SERVER_USER)@$($SERVER_IP):$RemoteDir/"
    
    # Subpastas
    & $PSCP -pw $SERVER_PASS -hostkey $HOSTKEY -batch -r "$LocalSkynet\camera" "$LocalSkynet\audio" "$LocalSkynet\core" "$LocalSkynet\memory" "$LocalSkynet\tests" "$LocalSkynet\vision" "$LocalSkynet\scripts" "$($SERVER_USER)@$($SERVER_IP):$RemoteDir/"
    
    Write-Host "[2/2] Validando sintaxe Python no servidor..." -ForegroundColor Yellow
    $compileCmd = "[ -f /root/skynet/.env ] || cp /root/skynet/.env.example /root/skynet/.env; chmod 600 /root/skynet/.env && python3 -m py_compile /root/skynet/config.py /root/skynet/main.py /root/skynet/voice_chat.py /root/skynet/ptz_console.py /root/skynet/camera/ptz.py /root/skynet/camera/vision.py /root/skynet/core/commander.py /root/skynet/core/time_sync.py /root/skynet/audio/sources.py /root/skynet/audio/aec.py /root/skynet/audio/player.py /root/skynet/audio/live_agent.py && python3 -m compileall -q /root/skynet/vision /root/skynet/audio /root/skynet/memory && cp -f /root/skynet/scripts/sync_time.py /root/sync_time.py && chmod 755 /root/sync_time.py && rm -rf /root/skynet/scripts/__pycache__ && find /root/skynet/scripts /root/skynet/audio/native -maxdepth 1 -type f -exec sed -i 's/\r$//' {} + && chmod +x /root/skynet/scripts/*.sh /root/skynet/scripts/skynet* /root/skynet/audio/native/*.sh && bash /root/skynet/scripts/install_service.sh"
    & $PLINK -pw $SERVER_PASS -hostkey $HOSTKEY -batch "$SERVER_USER@$SERVER_IP" $compileCmd
    
    Write-Host "`n✅ Sincronização concluída com sucesso! Servidor atualizado." -ForegroundColor Green
}
catch {
    Write-Host "`n❌ Erro durante a sincronização: $_" -ForegroundColor Red
    exit 1
}

