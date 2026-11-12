# 🤖 Skynet Robot - Chat com Visão & PTZ

O aplicativo principal agora é um **chat de texto**. Câmera e PTZ são
**ferramentas que a IA usa sob demanda**: ela só tira foto ou move a cabeça
quando a conversa exigir. O controle manual de PTZ continua disponível em
`ptz_console.py` para calibração e operação direta.

> ⚠️ **Importante:** esta câmera (Gwelltimes, fw 32.00.47) **não implementa movimento
> contínuo** por ONVIF. Veja a seção [Limitação do firmware](#-limitação-do-firmware)
> para as medições e o que foi descartado.

## 📂 Arquitetura do Projeto

```
/root/skynet/
├── main.py                   # ENTRADA PRINCIPAL: chat de texto com ferramentas
├── ptz_console.py            # Console manual de PTZ (legado/calibração)
├── config.py                 # IPs, credenciais e calibração do motor
├── calibrate.py              # Assistente de calibração graus/passo
├── camera/
│   ├── ptz.py                # Controlador PTZ por pulsos ONVIF
│   ├── vision.py             # Visão legada (snapshots/buffer)
│   └── tracker.py            # Tracker legado (não usado pelo chat)
├── core/
│   └── commander.py          # API de alto nível legada (PTZ + visão)
├── audio/
│   └── live_agent.py         # Agente de voz Gemini Live (áudio + visão + PTZ)
├── memory/                   # Memória de longo prazo com RAG
│   ├── store.py              # SQLite + FTS5 + vetores
│   ├── rag.py                # Recuperação híbrida e orçamento de tokens
│   ├── tools.py              # remember/recall/forget para a IA
│   └── README.md             # Documentação da memória
├── vision/
│   ├── chat.py               # Chat principal e loop de ferramentas
│   ├── tools/
│   │   ├── camera_tool.py    # Ferramenta capture_image
│   │   └── ptz_tool.py       # Ferramentas move_camera / stop_camera
│   ├── main.py               # Pipeline Live contínuo e testes 1..8
│   ├── camera_capture.py     # FFmpeg/RTSP persistente, buffer de 1 frame
│   ├── image_processor.py    # Resize + JPEG (LOW/BALANCED/DETAIL)
│   ├── image_heartbeat.py    # Envio visual ~1 FPS, sem backlog
│   ├── gemini_live_client.py # Uma conexão Live (envio/recepção)
│   ├── session_manager.py    # Reconexão, backoff, Session Resumption, GoAway
│   └── diagnostics.py        # Métricas, resumo periódico e CSV
├── tests/
├── .env.example              # Modelo de configuração (sem segredos)
└── requirements.txt
```

---

## 🚀 Como Executar

### 1. Chat principal (texto + câmera + PTZ)
Acesse o TV Box via SSH e execute:
```bash
cd /root/skynet
python3 main.py
```

- A conversa normal é **somente texto**.
- Se a resposta precisar de visão, a IA chama `capture_image` e recebe uma foto.
- Se você pedir para girar/olhar/inclinar, ela chama `move_camera`.
- Depois de mover, ela pode chamar `capture_image` para confirmar o que vê.
- Comandos: `/foto`, `/parar`, `/pessoas`, `/memorias [termo]`, `/limpar`, `/sair`.

> 🕒 **Relógio e fuso:** o box não tem RTC confiável. Ao iniciar, o wrapper
> `skynet` roda `/root/sync_time.py` **antes** do `main.py`: busca a hora na
> internet (NTP, com fallback HTTP `Date`), ajusta o relógio e aplica o fuso
> `America/Sao_Paulo` (Londrina/PR). Sem isso o TLS do Gemini falha com
> `certificate is not yet valid`. Para pular: `SKYNET_SKIP_TIME_SYNC=1`.

Opções úteis:
```bash
python3 main.py --no-camera          # chat somente texto
python3 main.py --no-ptz             # desativa movimento PTZ
python3 main.py --source video.mp4   # usa vídeo local em vez da câmera
python3 main.py --model gemini-3.5-flash
python3 main.py --save-photo /tmp/ultima.jpg
```

### 2. Console manual de PTZ (legado/calibração)
O antigo `main.py` virou `ptz_console.py`:
```bash
cd /root/skynet
python3 ptz_console.py
```
- Digite `180` : gira ~180° à direita em passos.
- Digite `-90` : gira ~90° à esquerda.
- Digite `t30` : inclina ~30° para cima.
- Digite `t-20` : inclina ~20° para baixo.
- Teclas `W`, `A`, `S`, `D` : micro-passos (1 passo do motor).
- `F` : captura foto instantânea.
- `SCAN` : varredura de 180° com capturas.
- `Q` : sai.

### 3. Comandos diretos de PTZ (para automações)
```bash
python3 ptz_console.py --degrees 180
python3 ptz_console.py --degrees -90
python3 ptz_console.py --tilt 25
python3 ptz_console.py --snapshot
```

### 4. Usando a API legada nos seus scripts (Python)
```python
from core.commander import RobotCommander

robot = RobotCommander()
foto = robot.turn_and_see(degrees=180)
robot.nudge("left", steps=1)
frame_bytes = robot.get_vision_frame()
print(robot.ptz.last_steps, robot.ptz.last_degrees)
```

### 5. Pipeline de visão contínua (Gemini Live)
O pacote `vision` também mantém o pipeline contínuo de captura + envio de
imagens (máximo 1 FPS) para a Gemini Live API. Não há IA local.

```bash
cd /root/skynet
pip3 install --break-system-packages -r requirements.txt   # ou use um venv
cp .env.example .env && chmod 600 .env                     # preencha GEMINI_API_KEY
python3 -m vision.main --test 3                            # conexão
python3 -m vision.main --test 5 --seconds 30               # heartbeat ~1 FPS
python3 -m vision.main --run                               # pipeline completo
```

O chat de texto e o **agente de voz** compartilham a **memória de pessoas**:
reconhecimento facial local, banco SQLite em `data/people.db` e um JSON com as
pessoas reconhecidas junto de cada foto enviada ao Gemini. No agente de voz o
JSON acompanha a foto sob demanda (`capture_image`), a foto automática após
movimentos do PTZ e os frames da visão contínua (a cada ~5 s). Ferramentas do
chat: `remember_person`, `recall_person`, `list_people` e `forget_person`.

Além das pessoas, existe uma **memória de longo prazo com RAG** em
`data/memory.db`: animais, acontecimentos, projetos, ideias, preferências,
lugares e tarefas. A IA decide o que guardar (`remember_memory`), consulta sob
demanda (`recall_memory`) e só recebe um bloco compacto de memórias relevantes
para a conversa + rostos reconhecidos, dentro de um orçamento de tokens. Veja
[`memory/README.md`](memory/README.md).

Documentação completa (configuração, testes 1–8, systemd, memória e
diagnóstico): [`vision/README.md`](vision/README.md).

---

## 🎧 Áudio e cancelamento de eco (AEC3)

O agente de voz usa o áudio que está indo para o alto-falante como **referência**
e aplica o **WebRTC AEC3** na captura do microfone antes de enviar ao Gemini.
Assim a voz do próprio Skynet é removida do que o microfone ouve, o que permite
**barge-in** (falar por cima enquanto ele responde).

- Camada independente: `audio/aec.py` (referência far-end + captura near-end).
- Backend nativo: `audio/native/skynet_aec.cpp` → `libskynet_aec.so` sobre
  `webrtc-audio-processing` 1.3 (AEC3, sem GPU e 100% local).
- Se a biblioteca nativa não estiver instalada, o áudio funciona normalmente
  (passthrough) e o log avisa.
- Instalação no box (compila uma vez, ~20-40 min em armhf):

```bash
sudo bash /root/skynet/audio/native/build_aec.sh
cd /root/skynet && python3 tests/test_aec_offline.py
```

Configuração no `.env`: `AEC_ENABLED`, `AEC_DELAY_MS` (dica de atraso
alto-falante→microfone) e `AEC_FAR_MAX_MS` (buffer de referência).

Enquanto o AEC3 não está ativo, o código usa **half-duplex automático**
(`AUDIO_HALF_DUPLEX=auto`): o microfone é silenciado durante a fala do robô
para ele não se autointerromper. Quando o AEC3 entra, o modo volta a
full-duplex e o barge-in funciona. Para forçar full-duplex: `AUDIO_HALF_DUPLEX=0`.

> ⚠️ **Microfone da câmera:** ele abre uma segunda sessão RTSP no mesmo
> firmware do vídeo e, nesta câmera, isso pode travar o RTSP inteiro (vídeo e
> áudio param e ela passa a responder `405`). Para AEC e estabilidade, use
> `AUDIO_INPUT_BACKEND=local` (microfone USB). Se a câmera travar, desligue e
> ligue a alimentação dela. Se realmente for usar o mic da câmera, tente
> `CAMERA_AUDIO_PATH=/onvif2` para separar da stream de vídeo.

---

## 📐 Calibração

Cada comando ONVIF gira **um passo fixo do motor**. O padrão é `8.0°` por passo,
estimado por análise de vídeo (≈14 px de deslocamento em quadro de 160 px).
Para medir o valor real:

```bash
python3 calibrate.py
```

O script dá 10 passos, pergunta quantos graus a cena andou e grava
`PAN_DEGREES_PER_STEP` / `TILT_DEGREES_PER_STEP` no `config.py`.

---

## ⚠️ Limitação do firmware

Medições feitas ao vivo contra `192.168.10.14` (substream RTSP + diff de frames):

| Teste | Resultado |
|---|---|
| `ContinuousMove` único | HTTP 200, mas gira **um passo fixo** (~8°) e para sozinho |
| `Timeout` / duração | Ignorados |
| Velocidade `0.03` a `1.0` | Passo constante (~13–15 px) — não altera o ângulo |
| `Stop` real (SOAP) | Não trunca o passo; não devolve resposta (normal) |
| `RelativeMove` | Conexão fechada (não suportado) |
| `AbsoluteMove` / presets | Conexão fechada (não suportado) |
| `GetStatus` / `GetConfigurationOptions` | Não implementados |
| Taxa máxima | ~1 passo/segundo; comandos mais rápidos são descartados |
| RTSP `SET_PARAMETER ptzCmd:*` | HTTP 200, sem movimento |
| `USER_CMD_SET` | 405 Method Not Allowed |
| Sessão RTSP (`SETUP`) | 400 Bad Request (câmera é broadcast) |
| Portas TCP abertas | 21 (FTP quebrado), 554, 5000, 50000 (silenciosa) |

O app da Yoosee parece fluido porque usa o protocolo P2P proprietário
(IoTVideo/Gwell), cujo handshake exige um **access token da nuvem** — não é
acessível só com a senha do dispositivo. Implementar isso localmente exigiria
reproduzir a sessão P2P inteira, não apenas o ONVIF.

**Consequência prática:** o código não finge mais movimento contínuo. Ele converte
graus em passos e os espaça em ~1,05 s para a câmera não descartar comandos.
