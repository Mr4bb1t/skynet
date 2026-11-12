# <p align="center">🤖 SKYNET ROBOT</p>

<p align="center">
  <strong>Agente Físico Conversacional Multimodal com Gemini Live, Visão Computacional, PTZ Motorizado e Memória Temporal RAG</strong>
</p>

<p align="center">
  <img src="docs/images/banner.png" alt="Skynet Robot Hero Banner" width="100%">
</p>

<p align="center">
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.9%20%7C%203.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://ai.google.dev"><img src="https://img.shields.io/badge/Google_Gemini-Live_3.8-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Gemini Live"></a>
  <a href="https://webrtc.org"><img src="https://img.shields.io/badge/Audio-WebRTC_AEC3_C++-333333?style=for-the-badge&logo=cplusplus&logoColor=white" alt="WebRTC AEC3"></a>
  <a href="https://onvif.org"><img src="https://img.shields.io/badge/Hardware-ONVIF_PTZ-orange?style=for-the-badge" alt="ONVIF PTZ"></a>
  <a href="https://www.sqlite.org"><img src="https://img.shields.io/badge/Memory-RAG_FTS5_%2B_Temporal_v2-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="Temporal RAG"></a>
  <a href="https://debian.org"><img src="https://img.shields.io/badge/OS-Debian_ARM_TV_Box-A81D33?style=for-the-badge&logo=debian&logoColor=white" alt="Debian"></a>
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

---

## ⚡ Visão Geral

O **SKYNET Robot** transforma um TV Box barato (Debian armhf/arm64) e uma câmera IP PTZ comum (Yoosee / Gwelltimes) em um **robô físico inteligente e consciente do ambiente**.

Alimentado pelo **Google Gemini Live (3.8)**, o Skynet processa áudio bidirecional em tempo real, enxerga através da câmera via RTSP sob demanda ou continuamente, controla motores de Pan e Tilt (PTZ) via ONVIF, reconhece moradores por biometria facial local e preserva uma **memória temporal de longo prazo** estruturada com RAG híbrido.

### 🌟 Destaques Principais

- 🎙️ **Conversação por Voz Real-Time:** Conexão WebSocket persistente com o Gemini Live (áudio in/out ultrarrápido).
- 🎧 **Cancelamento de Eco Acústico WebRTC AEC3 (C++ nativo):** Suporte nativo a *barge-in* (interromper o robô enquanto ele fala) com cancelamento da própria voz em tempo real no TV Box.
- 👁️ **Visão Computacional & Biometria Facial:** Detecção e reconhecimento facial local com persistência SQLite (`data/people.db`), scores de confiança e injeção contextual em fotos para a IA.
- 🦾 **Controle Motorizado PTZ Adaptativo:** Engenharia reversa ONVIF SOAP contornando as limitações do firmware Yoosee através de micro-passos calibrados (~8°/passo) e cadência de pulso.
- 🧠 **Memória Temporal RAG v2 (4 Camadas):** Separação cognitiva estrita entre *Estado Atual (RAM)*, *Memórias Episódicas (passado datado)*, *Conhecimento Semântico (fatos vigentes com supersessão)* e *Planos Futuros (tarefas pendentes)*.
- 🕒 **Sincronizador Inteligente de Relógio:** Auto-sincronização de data/hora (NTP com fallback HTTP Date) e fuso (`America/Sao_Paulo`) no boot, eliminando erros de certificado TLS em TV Boxes sem bateria de RTC.
- 🖥️ **CLI Global & Gerenciamento de Serviço:** Comando global `skynet`, sessões desacopladas em `tmux`, logs em tempo real, TUI de setup e watchdog de auto-reinicialização.

---

## 🏗️ Arquitetura do Sistema

```mermaid
flowchart TD
    subgraph Hardware ["Ambiente Físico & Hardware"]
        CAM["📷 Câmera IP Yoosee / ONVIF"]
        BOX["📺 TV Box (Debian ARM)"]
        MIC["🎤 Microfone (USB ou RTSP)"]
        SPK["🔊 Caixa de Som (P2 / AV / USB)"]
    end

    subgraph AudioEngine ["Engenharia de Áudio"]
        AEC["⚙️ WebRTC AEC3 (libskynet_aec.so)"]
        REF["Far-End Speaker Ref"]
        GAIN["Ganho Digital (mic_gain)"]
    end

    subgraph CoreLoop ["Skynet Core Loop (main.py)"]
        LIVE["🌐 Gemini Live Client (WS)"]
        VOICE["🎙️ Voice Agent"]
        FC["⚡ Function Calling Dispatcher"]
    end

    subgraph PerceptionVision ["Percepção & Visão"]
        RTSP["FFmpeg RTSP Stream Buffer"]
        FACE["👤 Biometria Facial Local (people.db)"]
        JPG["Image Processor (Resize / JPEG)"]
        PTZ_CTRL["🦾 ONVIF PTZ Controller"]
    end

    subgraph MemoryEngine ["Memória Temporal RAG v2"]
        WM["Working Memory (RAM - TTL 30m)"]
        EP["Episodic (Eventos Passados Datados)"]
        SEM["Semantic (Fatos Vigentes & Superseded)"]
        PL["Plans (Intenções & Tarefas Futuras)"]
        RAG["Recuperador Híbrido: Vetores + SQLite FTS5"]
    end

    CAM -- "RTSP /onvif2" --> RTSP
    MIC --> GAIN --> AEC
    SPK <-- "Áudio PCM 24kHz" -- LIVE
    SPK -. "Loopback Ref" .-> REF --> AEC
    AEC --> VOICE --> LIVE
    LIVE <== "WebSocket Seguro" ==> CoreGemini["☁️ Google Gemini Live API"]
    
    LIVE --> FC
    FC -->|"capture_image"| RTSP
    RTSP --> JPG --> FACE --> FC
    FC -->|"move_camera"| PTZ_CTRL --> CAM
    FC <-->|"remember / recall"| MemoryEngine
    MemoryEngine -->|"Contexto em 4 Blocos"| LIVE
```

---

## 🧠 Arquitetura de Memória Temporal v2

> *"Memória não é percepção. Percepção não é conhecimento. Conhecimento não é estado atual."*

Para impedir que a IA alucine eventos do passado como se estivessem acontecendo agora, o contexto é entregue em **4 seções temporais explícitas**:

| Camada | Escopo | Armazenamento | Injeção no Prompt |
|---|---|---|---|
| **Working Memory** | O que está acontecendo agora (últimas falas, tarefa ativa, pessoas vistas no frame) | RAM (`memory/working.py`), TTL 30 min | `=== CURRENT STATE ===` |
| **Episodic** | Acontecimentos passados com data exata (`event_time`) | SQLite (`memory_type='event'`) | `=== EPISODIC MEMORIES ===` |
| **Semantic** | Fatos, preferências, relacionamentos e rotinas com validade temporal | SQLite (`fact`, `preference`, `routine`) | `=== SEMANTIC KNOWLEDGE ===` |
| **Future Plans** | Compromissos e tarefas futuras com status (`active`, `done`, `cancelled`) | SQLite (`plan`, `task`) | `=== FUTURE PLANS ===` |

Quando um fato se atualiza (ex: mudança de emprego ou cidade), o registro anterior não é apagado: torna-se `superseded` com data de expiração, e um novo registro é ativado com `valid_from`.

---

## 📂 Estrutura do Repositório

```
skynet-box/
├── docs/
│   └── images/
│       └── banner.png            # Banner e artes visuais do projeto
├── references/                   # Documentações e engenharia reversa de câmeras
├── skynet/                       # Núcleo da aplicação
│   ├── main.py                   # ENTRADA PRINCIPAL: Agente de Voz Gemini Live
│   ├── voice_chat.py             # Launcher especializado para modo de voz
│   ├── ptz_console.py            # Console de calibração e controle manual de PTZ
│   ├── calibrate.py              # Calibrador interativo de graus por pulso do motor
│   ├── config.py                 # Configurações globais, calibração e portas
│   ├── .env.example              # Modelo de variáveis de ambiente
│   ├── requirements.txt          # Dependências Python
│   ├── audio/                    # Áudio em tempo real e cancelamento de eco
│   │   ├── live_agent.py         # VoiceAgent bidirecional (Gemini Live)
│   │   ├── aec.py                # Pipeline AEC (near-end + far-end)
│   │   ├── player.py             # Reprodução ALSA com thread segura
│   │   ├── sources.py            # Captura de microfone (Local ALSA / RTSP)
│   │   └── native/
│   │       ├── skynet_aec.cpp    # Motor WebRTC AEC3 em C++
│   │       └── build_aec.sh      # Script de compilação da lib nativa
│   ├── camera/                   # Módulo de baixo nível da câmera
│   │   ├── ptz.py                # Controlador PTZ via SOAP ONVIF
│   │   ├── tracker.py            # Rastreamento de alvos
│   │   └── vision.py             # Utilitários de frames legados
│   ├── core/                     # Utilitários de sistema e configuração
│   │   ├── alsa_detector.py      # Autodetecção de placas e rotas de áudio ALSA
│   │   ├── config_tui.py         # Interface visual no terminal (TUI)
│   │   ├── env_manager.py        # Gerenciamento dinâmico de .env
│   │   └── time_sync.py          # Sincronização NTP/HTTP de alta precisão
│   ├── memory/                   # Memória de longo prazo com RAG Temporal
│   │   ├── store.py              # Schema SQLite v2, FTS5 e consultas temporais
│   │   ├── working.py            # Working Memory em memória volátil
│   │   ├── rag.py                # Recuperação híbrida (semântica + temporal)
│   │   ├── consolidate.py        # Consolidação periódica da conversa em memórias
│   │   ├── tools.py              # Ferramentas da IA (remember, recall, forget)
│   │   └── README.md             # Documentação detalhada da memória
│   ├── scripts/                  # Scripts de instalação e daemon do sistema
│   │   ├── skynet                # Wrapper CLI global com suporte a TMUX
│   │   ├── install_service.sh    # Instalador automático para SysVinit / Systemd
│   │   ├── skynet.service        # Unidade Systemd
│   │   ├── skynet.init           # Script de serviço SysVinit
│   │   └── sync_time.py          # Utilitário de sincronização de relógio no boot
│   ├── tests/                    # Bateria de testes de hardware, PTZ e IA
│   └── vision/                   # Pipeline visual contínuo e reconhecimento
│       ├── chat.py               # Modo texto legado com function calling
│       ├── camera_capture.py     # Captura FFmpeg RTSP persistente (slot único)
│       ├── person_memory.py      # Reconhecimento facial local e banco SQLite
│       ├── image_processor.py    # Otimização e resize dinâmico de frames
│       ├── image_heartbeat.py    # Heartbeat visual 1 FPS para Gemini Live
│       └── tools/                # Ferramentas de câmera, ptz e biometria
└── sync_to_server.ps1            # Script de deploy rápido para o TV Box via SCP
```

---

## 🚀 Instalação Rápida (TV Box Debian / Armbian)

### 1. Pré-requisitos do Sistema

Instale as dependências essenciais de áudio (ALSA), multimídia (FFmpeg) e compilação:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg \
    python3-dev libjpeg-dev zlib1g-dev libasound2-dev tmux curl
```

### 2. Clonar e Instalar Dependências

```bash
git clone https://github.com/Mr4bb1t/skynet.git /root/skynet
cd /root/skynet/skynet

# Instalar dependências Python
pip3 install --break-system-packages -r requirements.txt
```

### 3. Compilar o Cancelador de Eco Nativo (WebRTC AEC3)

Para usufruir de **barge-in completo** (falar por cima do robô enquanto ele fala):

```bash
sudo bash audio/native/build_aec.sh
python3 tests/test_aec_offline.py
```
*(Se não compilado, o Skynet opera automaticamente em half-duplex seguro para não se autointerromper).*

### 4. Configurar Variáveis de Ambiente

Copie o modelo de ambiente e insira a sua API Key do Google Gemini:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

### 5. Instalar o Comando Global e Serviço Automático

```bash
cd /root/skynet/skynet/scripts
sudo bash install_service.sh
```

Pronto! Agora o comando `skynet` está disponível globalmente em todo o sistema operacional.

---

## 🎮 Como Usar

### Menu Interativo de Configuração (TUI)

Configure facilmente dispositivos de áudio, RTSP e credenciais direto pelo terminal:

```bash
skynet tui
# ou
python3 main.py --config
```

### 1. Iniciar Conversação por Voz (Modo Principal)

```bash
# Inicia usando o microfone embutido da câmera IP (padrão)
skynet

# Inicia usando um microfone USB conectado diretamente ao TV Box
python3 main.py --backend local --local-device default

# Ativa streaming contínuo de vídeo a 1 FPS (além de fotos sob demanda)
python3 main.py --continuous-vision
```

### 2. Controle do Serviço em Segundo Plano (Background / TMUX)

O comando `skynet` cria uma sessão desacoplada em `tmux`, mantendo o robô ativo mesmo após desconectar do SSH:

```bash
skynet start      # Inicia o robô em background
skynet status     # Verifica se o processo e o tmux estão ativos
skynet attach     # Conecta ao console interativo em tempo real
skynet log        # Acompanha os logs ao vivo (/var/log/skynet.log)
skynet stop       # Para a execução do robô
skynet restart    # Reinicia o serviço
```

> 💡 **Dica TMUX:** Quando estiver conectado via `skynet attach`, pressione `Ctrl+B` seguido da tecla `D` para sair do terminal **sem interromper** o robô.

### 3. Modo Chat de Texto (Legado / Debug)

```bash
skynet --chat
# ou
python3 main.py --chat
```
Neste modo, digite mensagens normalmente. A IA chamará `capture_image` e `move_camera` de forma autônoma conforme a necessidade da conversa. Comandos rápidos: `/foto`, `/pessoas`, `/memorias`, `/parar`, `/sair`.

### 4. Calibração e Controle Manual dos Motores PTZ

```bash
python3 ptz_console.py
```
- `180` / `-90`: Gira horizontalmente nos ângulos desejados.
- `t30` / `t-20`: Inclina a câmera verticalmente (Tilt).
- `W`, `A`, `S`, `D`: Micro-passos manuais.
- `F`: Captura snapshot em tempo real.
- `SCAN`: Executa varredura panorâmica contínua tirando fotos.

Para rodar o assistente de medição de passos:
```bash
python3 calibrate.py
```

---

## ⚙️ Variáveis de Configuração (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | *(obrigatório)* | Chave da API do Google AI Studio |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Modelo para a sessão de voz real-time |
| `GEMINI_TEXT_MODEL` | `gemini-3.1-flash-lite` | Modelo ultrarrápido para chat de texto |
| `CAMERA_URL` | `rtsp://admin:senha@IP:554/onvif2` | URL RTSP da câmera (substream recomendada) |
| `ONVIF_IP` | `192.168.10.14` | Endereço IP da câmera para comandos SOAP PTZ |
| `ONVIF_PORT` | `5000` | Porta ONVIF (geralmente `5000` ou `80` em câmeras Yoosee) |
| `AUDIO_INPUT_BACKEND` | `camera` | Fonte do microfone: `camera` (RTSP) ou `local` (USB TV Box) |
| `AUDIO_LOCAL_DEVICE` | `default` | Dispositivo de captura ALSA quando backend for `local` |
| `AUDIO_OUTPUT_DEVICE` | `default` | Dispositivo ALSA para alto-falante (`default`, `hw:0,0`, etc.) |
| `AUDIO_MIC_GAIN` | `5.0` | Multiplicador de ganho digital do microfone |
| `AEC_ENABLED` | `true` | Ativa cancelamento de eco WebRTC AEC3 (se compilado) |
| `AUDIO_HALF_DUPLEX` | `auto` | `auto`: silencia mic durante fala da IA caso AEC3 esteja ausente |
| `PAN_DEGREES_PER_STEP` | `8.0` | Calibração de rotação horizontal por pulso |
| `TILT_DEGREES_PER_STEP` | `8.0` | Calibração de inclinação vertical por pulso |
| `PERSON_MEMORY_ENABLED`| `true` | Habilita identificação e cadastro biométrico facial |
| `MEMORY_DB_PATH` | `data/memory.db` | Caminho do banco SQLite da memória temporal RAG |

---

## ⚠️ Peculiaridades de Hardware & Soluções de Engenharia

Durante o desenvolvimento do Skynet em câmeras Yoosee / Gwelltimes (firmware 32.00.47) e TV Boxes ARM, diversas limitações foram contornadas:

1. **Movimento ONVIF Pulsado:**
   - O firmware não suporta `ContinuousMove` real nem altera velocidade via ONVIF: cada requisição aciona um pulso fixo (~8°).
   - O Skynet converte requisições de graus da IA em trens de pulsos espaçados em ~1.05s, prevenindo buffer overflow e descarte de comandos pela câmera.
2. **Relógio do TV Box (TLS & Certificados SSL):**
   - TV Boxes baratos não possuem bateria de RTC e acordam no ano padrão da compilação do Kernel.
   - O Skynet executa `sync_time.py` antes de qualquer inicialização: ajusta via NTP (ou cabeçalho HTTP Date) com timezone `America/Sao_Paulo`. Sem isso, o handshake TLS da API Gemini falha com `certificate is not yet valid`.
3. **Áudio Duplex e Microfone RTSP:**
   - Abrir canais RTSP simultâneos para vídeo e áudio pode congelar o firmware da câmera (gerando erro `405`).
   - Para máxima estabilidade e cancelamento de eco, o uso de microfone USB local no TV Box (`AUDIO_INPUT_BACKEND=local`) é altamente recomendado.

---

## 🤝 Contribuindo

Contribuições, correções de bugs e novas ideias são muito bem-vindas!
1. Faça um Fork do projeto
2. Crie uma branch para a sua feature (`git checkout -b feature/nova-funcionalidade`)
3. Faça commit das mudanças (`git commit -m 'feat: adiciona nova funcionalidade'`)
4. Faça push para a branch (`git push origin feature/nova-funcionalidade`)
5. Abra um Pull Request

---

## 📄 Licença

Distribuído sob a licença **MIT**. Consulte `LICENSE` para mais informações.

<p align="center">
  Desenvolvido com ☕, Python e IA por <strong><a href="https://github.com/Mr4bb1t">Mr4bb1t</a></strong>
</p>

