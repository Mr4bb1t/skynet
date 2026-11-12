#!/usr/bin/env bash
# Script de instalacao do servico e comando global 'skynet'

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Instalando comando global /usr/local/bin/skynet..."
cp "$DIR/scripts/skynet" /usr/local/bin/skynet
chmod 755 /usr/local/bin/skynet

# Sincronizador de hora usado pelo wrapper antes de subir o Skynet.
echo "Instalando sincronizador /root/sync_time.py..."
cp "$DIR/scripts/sync_time.py" /root/sync_time.py
chmod 755 /root/sync_time.py

echo "Instalando servico SysVinit /etc/init.d/skynet..."
cp "$DIR/scripts/skynet.init" /etc/init.d/skynet
chmod 755 /etc/init.d/skynet

if which update-rc.d > /dev/null 2>&1; then
    echo "Habilitando inicializacao automatica no boot (SysVinit)..."
    update-rc.d skynet defaults 98 02 >/dev/null 2>&1 || true
fi

if [ -d /etc/systemd/system ]; then
    echo "Instalando unidade systemd /etc/systemd/system/skynet.service..."
    cp "$DIR/scripts/skynet.service" /etc/systemd/system/skynet.service
    if which systemctl > /dev/null 2>&1; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl enable skynet.service >/dev/null 2>&1 || true
    fi
fi

# Cria/atualiza o /root/eth0_watchdog.sh para auto-iniciar o skynet quando a rede estiver online.
# Importante: o bloco precisa ficar DENTRO do while do watchdog, senao roda so
# uma vez no boot e nunca mais reinicia o Skynet se ele cair.
if [ -f /root/eth0_watchdog.sh ]; then
    if ! grep -q "skynet start" /root/eth0_watchdog.sh; then
        echo "Integrando auto-inicio do skynet ao watchdog de rede /root/eth0_watchdog.sh..."
        python3 - << 'PY'
from pathlib import Path

path = Path("/root/eth0_watchdog.sh")
text = path.read_text(encoding="utf-8")
block = '''
    # Auto-inicializacao e auto-restart do Skynet Robot.
    # Fica DENTRO do loop para voltar a subir se o processo morrer.
    if ! pgrep -f "[p]ython3.*main.py" >/dev/null 2>&1; then
        if [ -x /usr/local/bin/skynet ] && ip route show default 2>/dev/null | grep -q "default"; then
            tmux has-session -t skynet 2>/dev/null && tmux kill-session -t skynet 2>/dev/null
            /usr/local/bin/skynet start >/dev/null 2>&1 &
        fi
    fi
'''
index = text.rfind("done")
if index == -1:
    raise SystemExit("watchdog sem 'done'; nao foi possivel inserir o auto-start")
path.write_text(text[:index] + block + text[index:], encoding="utf-8")
print("Auto-start inserido dentro do loop do watchdog.")
PY
    fi
fi

echo "Verificando se o comando esta no PATH..."
if which skynet > /dev/null 2>&1; then
    echo "Sucesso! O comando 'skynet' esta disponivel em qualquer lugar do sistema."
else
    echo "Aviso: /usr/local/bin nao esta no seu PATH atual."
fi

if [ ! -f /usr/local/lib/libskynet_aec.so ]; then
    echo "Aviso: AEC3 nao instalado; o audio funcionara sem cancelamento de eco."
    echo "        Para instalar: bash $DIR/audio/native/build_aec.sh"
fi
