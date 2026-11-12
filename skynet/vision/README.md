# 👁️ SKYNET Vision - captura contínua + Gemini Live

Subsistema de visão do SKYNET para o TV Box Debian. O aplicativo principal é
o **chat de texto** (`chat.py` / `main.py`), com câmera e PTZ expostos como
ferramentas sob demanda. O pipeline contínuo mantém **uma conexão persistente
com a câmera IP**, prepara JPEGs localmente e envia **no máximo 1 frame por
segundo** para a Gemini Live API. Não há IA local, detecção de objetos, áudio,
motores ou navegação neste pacote.

```
câmera RTSP ──FFmpeg rawvideo──> slot único (frame mais recente)
                                      │
                                      ▼
                            Pillow: resize + JPEG
                                      │
                                      ▼
                         heartbeat 1 FPS ──> Gemini Live
                                                  │
                                      transcrição da resposta
```

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `config.py` | lê `.env`, valida presets e monta `Settings` |
| `camera_capture.py` | FFmpeg/RTSP persistente, buffer de 1 frame, reconexão |
| `image_processor.py` | resize sem crop/upscale + JPEG configurável |
| `image_heartbeat.py` | envia o frame mais recente ~1x/s, sem backlog |
| `gemini_live_client.py` | uma conexão WebSocket, tarefas de envio/recepção |
| `session_manager.py` | reconexão, backoff + jitter, Session Resumption, GoAway |
| `diagnostics.py` | métricas, resumo periódico e CSV |
| `chat.py` | chat de texto principal; loop de function calling |
| `tools/camera_tool.py` | ferramenta `capture_image` (foto sob demanda) |
| `tools/ptz_tool.py` | ferramentas `move_camera` e `stop_camera` |
| `main.py` | CLI do pipeline Live e testes obrigatórios 1–8 |
| `tests/` | testes unitários e de integração |

## Instalação no TV Box (Debian)

Requisitos: **Python 3.9+**, FFmpeg/ffprobe e acesso à câmera RTSP.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg \
    python3-dev libjpeg-dev zlib1g-dev

cd /root/skynet
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Se o sistema não permitir `venv`, instale com o Python do sistema:

```bash
pip3 install --break-system-packages -r requirements.txt
```

Se `psutil` ou `Pillow` não tiverem wheel para a arquitetura do TV Box, instale
os pacotes do sistema antes de repetir o `pip`:

```bash
sudo apt-get install -y python3-psutil python3-pil
```

Verifique as dependências:

```bash
python3 -c "import google.genai, PIL, dotenv, psutil; print('ok')"
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
```

## Configuração

Copie o modelo e preencha a chave (nunca versione o `.env`):

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Variáveis principais:

| Variável | Padrão | Descrição |
|---|---|---|
| `GEMINI_API_KEY` | — | chave da API (obrigatória para etapas Gemini) |
| `GEMINI_MODEL` | `gemini-3.8-live` | modelo Live (saída somente AUDIO) |
| `GEMINI_TEXT_MODEL` | `gemini-3.1-flash-lite` | modelo do chat; mais rápido para baixa latência |
| `GEMINI_TEXT_FALLBACK_MODEL` | `gemini-3.6-flash` | usado se o modelo rápido voltar sem texto |
| `CAMERA_URL` | — | RTSP da câmera; o padrão legado é `/onvif2` (640x360) |
| `CAPTURE_FPS` | `10` | taxa de captura local (10–15 recomendado) |
| `CAMERA_SKIP_PROBE` | `false` | `true` pula o ffprobe inicial e usa a resolução do `.env` |
| `IMAGE_HEARTBEAT_ENABLED` | `true` | `false` desativa o envio automático de imagens |
| `IMAGE_SEND_FPS` | `1` | taxa de envio; limitada a 1.0 pela API |
| `PTZ_INTERVAL_SEC` | `0.8` | intervalo entre pulsos PTZ (~1 passo/s nesta câmera) |
| `PERSON_MEMORY_ENABLED` | `true` | ativa o reconhecimento/memória de pessoas |
| `PERSON_DB_PATH` | `data/people.db` | banco SQLite de pessoas |
| `PERSON_PHOTO_DIR` | `data/people` | fotos das pessoas lembradas |
| `FACE_MATCH_THRESHOLD` | `0.6` | distância máxima para considerar a mesma pessoa |
| `IMAGE_PRESET` | `BALANCED` | `LOW`=320, `BALANCED`=640, `DETAIL`=960 |
| `IMAGE_MAX_WIDTH` | `640` | largura máxima local |
| `IMAGE_MAX_HEIGHT` | `480` | altura máxima local |
| `JPEG_QUALITY` | `75` | qualidade JPEG (1–100) |
| `MEDIA_RESOLUTION` | `LOW` | resolução de processamento no Gemini |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Observações:

- `IMAGE_MAX_WIDTH`/`IMAGE_MAX_HEIGHT` explícitos têm prioridade sobre o preset.
  Use `--preset LOW|BALANCED|DETAIL` na CLI para forçar o preset.
- O resize local e `MEDIA_RESOLUTION` são independentes: o primeiro reduz
  CPU/bytes no TV Box; o segundo controla tokens/resolução no Gemini.
- `IMAGE_SEND_FPS` acima de `1` é reduzido para `1.0` com aviso no log.
- Intervalo entre heartbeats: `1 / IMAGE_SEND_FPS` segundos. Com o padrão
  `IMAGE_SEND_FPS=1`, o intervalo é **1,0 s (1000 ms)**. Para enviar a cada
  5 s, use `IMAGE_SEND_FPS=0.2`; a cada 10 s, `0.1` (mínimo aceito).
- Com `IMAGE_HEARTBEAT_ENABLED=false`, nenhuma imagem é enviada
  automaticamente. O teste 4 (`--test 4 --image ...`) continua enviando uma
  única imagem sob demanda.
- O `.env` é procurado em `skynet/.env`, `.env` da raiz do projeto ou no
  diretório atual. `VISION_ENV_FILE=/caminho/.env` força um arquivo específico.

## Execução

Sempre a partir de `/root/skynet`:

```bash
# Pipeline completo (Ctrl+C encerra)
python3 -m vision.main --run

# Com diagnóstico detalhado por frame
python3 -m vision.main --run --verbose
```

Em `--run`, a câmera e o Gemini sobem em segundo plano. O serviço **não
encerra** se um deles estiver momentaneamente indisponível: a captura e a
sessão continuam tentando reconectar, e o heartbeat envia frames assim que
os dois estiverem prontos.

### Chat principal (texto + câmera + PTZ)

O `main.py` da raiz agora abre o chat. Câmera e PTZ são **partes do corpo do
agente**: a IA decide quando olhar, mover e confirmar, com autonomia e propósito.

```bash
cd /root/skynet
python3 main.py
python3 main.py --no-camera
python3 main.py --no-ptz
python3 main.py --source /root/skynet/test_camera_640.mp4
python3 main.py --model gemini-3.5-flash --verbose
```

Também funciona como módulo:

```bash
python3 -m vision.chat
```

Ferramentas expostas ao modelo:

- `capture_image` — tira uma foto atual da câmera;
- `move_camera(pan_degrees, tilt_degrees)` — gira/inclina a cabeça em graus
  (positivo = direita/cima; faixa -180..180 pan e -90..90 tilt);
- `stop_camera` — interrompe o movimento PTZ.

O comportamento/persona do modelo fica no arquivo editável
**`vision/chat_prompt.txt`**. Edite e reinicie o chat; não é necessário mexer
no Python. O prompt dá autonomia para a IA decidir quando olhar e quando
mover, com propósito. O limite de rodadas de ferramentas por pergunta é
`--max-tool-rounds` (padrão **6**), permitindo sequências como
mover → olhar → mover → olhar.

Exemplos de pedidos:

```text
O que você está vendo?
Gire 30 graus para a direita e me diga o que aparece.
Olhe para cima, tire uma foto e descreva.
Pare de girar.
```

Comandos no chat:

- `/foto` — pede ao modelo para tirar uma foto e descrever;
- `/parar` — interrompe o PTZ imediatamente;
- `/limpar` — apaga o histórico da conversa;
- `/sair` — encerra.

Características:

- conversa normal responde apenas texto e **não** gasta imagem;
- a câmera é **preguiçosa**: ela só é aberta na primeira captura, então o chat
  inicia instantaneamente e não fica tentando RTSP enquanto você conversa;
- quando o modelo decide que precisa ver, a câmera entrega o frame mais
  recente e o JPEG é enviado uma única vez;
- o JPEG **não** fica no histórico, portanto não é reenviado a cada pergunta;
- depois de mover o PTZ, o modelo pode chamar `capture_image` para confirmar;
- se a câmera falhar, a primeira captura espera até 30 s (esta câmera às
  vezes precisa de uma segunda conexão RTSP) e devolve erro ao modelo; novas
  tentativas só depois de 10 s. O chat continua funcionando;
- se o PTZ falhar, o modelo recebe o erro e responde o que não funcionou;
- `--save-photo caminho.jpg` salva a última foto capturada para depuração;
- `--no-ptz` desativa o movimento; `--no-camera` deixa o chat somente texto.

O PTZ usa o controlador calibrado em `camera/ptz.py` e o `config.py`
(`DEGREES_PER_STEP_PAN`, `DEGREES_PER_STEP_TILT`). Os pulsos usam WS-Security
(UsernameToken/PasswordDigest) com as credenciais da `CAMERA_URL`; se a senha
ONVIF for diferente, defina `CAMERA_ONVIF_USER` / `CAMERA_ONVIF_PASS`.

Esta câmera aceita aproximadamente **1 passo por segundo**, então o padrão é
`PTZ_INTERVAL_SEC=0.8` (~1,125°/passo). Um movimento de 30° leva ~22 s. Se a
sua câmera aceitar pulsos mais rápidos, reduza `PTZ_INTERVAL_SEC`; se ela
perder passos, aumente. Se a porta ONVIF estiver indisponível, a ferramenta
retorna erro e a IA informa que não conseguiu mover.

O modelo de texto é configurado por `GEMINI_TEXT_MODEL`. O padrão é
`gemini-3.1-flash-lite` (mais rápido). Para respostas mais elaboradas, use
`gemini-3.6-flash`; `gemini-3.8-flash` pode estar com alta demanda.

> **Cota do free tier:** o Gemini gratuito tem limite diário de requisições por
> modelo. Se aparecer `429 RESOURCE_EXHAUSTED`, aguarde o reset da cota, use
> outra chave ou troque `GEMINI_TEXT_MODEL`/`--model`. Cada pergunta visual
> normalmente consome 2 requisições (chamada da ferramenta + resposta final).

### Memória de pessoas (face recognition local)

O reconhecimento é local: **embeddings e fotos de referência não saem do box**.
A foto atual da câmera continua sendo enviada ao modelo (é a percepção visual),
acompanhada do JSON abaixo:

```json
{
  "people": [
    {
      "face_index": 0,
      "known": true,
      "confidence": 0.832,
      "match_confidence": 0.832,
      "distance": 0.41,
      "bbox": [210, 60, 120, 120],
      "person": {
        "person_id": 1,
        "name": "João",
        "nickname": "Jota",
        "notes": "amigo do usuário; gosta de café"
      }
    }
  ],
  "unknown_faces": 0,
  "faces": 1,
  "threshold": 0.6,
  "memory": "ok"
}
```

- `confidence` (0–1) é a confiança na identidade: logística centrada no
  `FACE_MATCH_THRESHOLD` (distância 0 → ~0.99; no limiar → 0.50). Para rosto
  desconhecido vale `0.0`.
- `match_confidence` é a confiança do melhor candidato mesmo abaixo do limiar
  (útil para saber se era "quase" alguém conhecido).
- O prompt orienta: `>=0.75` pode afirmar o nome; `0.60–0.75` “parece ser”;
  `<0.60` tratar como incerto/desconhecido.
- `FACE_PRESENCE_MIN_CONFIDENCE` (padrão `0.6`) define a confiança mínima para
  um rosto entrar como “presente agora” no `CURRENT STATE`.

Ferramentas de memória:

| Ferramenta | Uso |
|---|---|
| `remember_person(name, nickname, notes, face_index)` | salva/atualiza uma pessoa usando o rosto da última foto |
| `recall_person(query)` | busca por nome, apelido ou observação |
| `list_people()` | lista todas as pessoas conhecidas |
| `forget_person(name)` | remove uma pessoa (somente se o usuário pedir) |

Fluxo de apresentação:

```text
Usuário: esse é o João, pode chamar de Jota; ele é meu amigo e gosta de café.
IA: [capture_image] -> JSON com face_index 0 desconhecido
IA: [remember_person(name="João", nickname="Jota", notes="amigo; gosta de café", face_index=0)]
IA: Prazer, Jota! Vou lembrar de você.
```

Nas próximas fotos, o JSON já vem com João reconhecido. A IA pode perguntar
apelido/observações e atualizar a memória. O comando `/pessoas` mostra o banco
sem gastar API.

Dependências de sistema no Debian: `libopenblas0` e `libatlas3-base` (além
dos wheels `dlib`, `face-recognition`, `face-recognition-models` e
`opencv-python-headless`). O reconhecimento acrescenta cerca de **0,5 s** por
foto no TV Box (Haar ~0,09 s + embedding ~0,44 s).

Para apagar toda a memória, pare o chat e remova `data/people.db` e
`data/people/`. Para esquecer uma pessoa específica, peça à IA ou use
`forget_person`.

### Memória de longo prazo (RAG)

Além dos rostos, o chat e o agente de voz compartilham `data/memory.db`, uma
memória de longo prazo com busca híbrida (FTS5 + embeddings) para animais,
acontecimentos, projetos, ideias, preferências, lugares, objetos e tarefas.
A IA decide o que guardar (`remember_memory`), consulta sob demanda
(`recall_memory`) e só recebe algumas memórias relevantes, dentro de um
orçamento de tokens. Detalhes em [`../memory/README.md`](../memory/README.md).

### Testes obrigatórios

| Teste | Comando | O que valida |
|---|---|---|
| 1 | `python3 -m vision.main --test 1 --seconds 15` | captura contínua, FPS, idade do frame |
| 2 | `python3 -m vision.main --test 2 --image /root/skynet/snapshot.jpg` | resize, proporção, JPEG |
| 3 | `python3 -m vision.main --test 3 --seconds 10` | conexão Live e setup |
| 4 | `python3 -m vision.main --test 4 --image /root/skynet/snapshot.jpg` | uma imagem interpretada |
| 5 | `python3 -m vision.main --test 5 --seconds 30` | heartbeat ~1 FPS, sem respostas espontâneas |
| 6 | `python3 -m vision.main --test 6 --seconds 5` | resposta enquanto frames continuam |
| 7 | `python3 -m vision.main --test 7` | reconexão + retomada sem parar a câmera |
| 8 | `python3 -m vision.main --test 8 --stability-minutes 30` | estabilidade, RAM/CPU, CSV |

Todos terminam com uma linha `RESULT PASS` ou `RESULT FAIL` e código de saída
`0`/`1`.

Para testar sem a câmera física, use um vídeo local:

```bash
python3 -m vision.main --test 1 --source /caminho/video.mp4 --seconds 10
python3 -m vision.main --test 5 --source /caminho/video.mp4 --seconds 20
```

### Testes automatizados

```bash
cd /root/skynet
python3 -m unittest discover -s vision/tests -v
```

Os testes de captura geram um vídeo sintético com FFmpeg e são ignorados se
`ffmpeg`/`ffprobe` não estiverem disponíveis.

## Diagnóstico

O resumo periódico (`DIAG ...`) mostra:

- `cap` — FPS de captura; `age` — idade do frame mais recente;
- `orig` / `out` — dimensões originais e após resize;
- `resize` / `encode` — tempo médio de resize e JPEG;
- `jpeg` — tamanho médio do JPEG em KB;
- `sub` / `sent` / `send_rate` / `drops` — frames submetidos, enviados, taxa e descartes;
- `ttf` — tempo até o primeiro token da resposta;
- `answers` / `spont` — respostas a perguntas e respostas espontâneas (deve ser 0);
- `ws` / `conn` / `reconn` / `resump` / `goaway` — estado e eventos da sessão;
- `tokens` — tokens acumulados; `cpu` / `ram` — uso do processo.

O teste 8 grava um CSV por frame com os mesmos campos (mais `frame_age_ms`,
`submitted`, `connected`, `sent_total`, `dropped_total`).

## Serviço systemd

Crie `/etc/systemd/system/skynet-vision.service`:

```ini
[Unit]
Description=SKYNET Vision - camera + Gemini Live
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/skynet
EnvironmentFile=/root/skynet/.env
ExecStart=/root/skynet/.venv/bin/python -m vision.main --run
Restart=always
RestartSec=5
# Limites conservadores para o TV Box
MemoryMax=300M
CPUQuota=60%
# Logs no journal
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Se estiver usando o Python do sistema, troque `ExecStart` por
`/usr/bin/python3 -m vision.main --run`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now skynet-vision
sudo systemctl status skynet-vision
journalctl -u skynet-vision -f
```

## Comportamento de rede e reconexão

- A captura da câmera é independente do Gemini: uma queda da API **não**
  reinicia o decoder nem a conexão RTSP.
- A conexão RTSP é única e persistente; o FFmpeg só é reiniciado se cair.
- O buffer guarda apenas o frame mais recente; não existe fila crescente.
- O envio usa um slot de tamanho 1 com descarte do frame antigo.
- A reconexão do Gemini usa backoff exponencial com jitter (base 1 s, máximo 30 s).
- `SessionResumptionConfig` reutiliza o último handle; após retomar, a instrução
  inicial não é reenviada.
- `GoAway` agenda o fechamento antes do prazo informado, preservando o handle.
- `ContextWindowCompression` com janela deslizante é habilitado por padrão
  (`GEMINI_CONTEXT_COMPRESSION=true`); o gatilho é opcional em
  `CONTEXT_TRIGGER_TOKENS`.

## Rede do TV Box (PROSB-3000 / Android + chroot Debian)

O box usa Android como host e um chroot Debian em `/data/debian`. A rede é
mantida por `/data/adb/service.d/debian-boot.sh`, que chama
`/data/set_static_ip.sh` no boot e a cada 15 s.

Correções aplicadas para a topologia real:

- `wlan0` = LAN/internet (`192.168.1.13/24`, gateway `192.168.1.1`);
- `eth0` = rede isolada da câmera (`192.168.10.1/24`, sem gateway);
- rota padrão adicionada em `table main` e `table 1011` via `wlan0`;
- `/data/static_ip.conf` e `/etc/resolv.conf` mantidos com **LF** (CRLF faz o
  glibc interpretar o `\r` como parte do IP e falhar com
  `Temporary failure in name resolution`);
- o box não tem RTC confiável: `/root/sync_time.py` sincroniza o relógio via
  NTP (UDP 123) ou HTTP `Date` e aplica o fuso `America/Sao_Paulo`. O wrapper
  `/usr/local/bin/skynet` chama esse script **antes** de subir o `main.py`
  (e o `main.py` repete a checagem), evitando
  `CERTIFICATE_VERIFY_FAILED: certificate is not yet valid`;
- o script descobre a interface da rota com `ip route get "$STATIC_GW"` e não
  usa mais o `carrier` do `eth0` para escolher a saída padrão.

Para conferir:

```bash
ip route show table main
ip route show table 1011
cat /etc/resolv.conf
getent hosts generativelanguage.googleapis.com
```

## Segurança

- Nunca escreva a chave no código Python nem no Git.
- Mantenha `.env` com permissão `600`.
- O `.gitignore` da raiz ignora `.env`, `__pycache__`, CSVs e logs.
- Os logs mascaram usuário/senha da URL RTSP (`admin:***@host`).

## Solução de problemas

| Sintoma | Verificação |
|---|---|
| `ffmpeg nao encontrado` | `sudo apt-get install -y ffmpeg` |
| `Camera nao entregou frame` | teste a URL com `ffprobe`; confira IP/porta/credenciais |
| `method OPTIONS failed: 405` | a câmera está recusando RTSP (firmware/modo privacidade); reinicie a câmera e teste `ffprobe` antes de rodar o pipeline |
| Microfone da câmera derruba vídeo/áudio | use `AUDIO_INPUT_BACKEND=local`; se insistir, `CAMERA_AUDIO_PATH=/onvif2` e reinicie a câmera |
| `GEMINI_API_KEY nao definida` | preencha `.env` e rode de `/root/skynet` |
| `Timeout aguardando conexao` | rede/DNS do TV Box; veja `LOG_LEVEL=DEBUG` |
| `Temporary failure in name resolution` | o TV Box não tem rota padrão/DNS para a internet; confira `ip route` (deve existir `default via ...`) |
| Modelo não responde à imagem | confirme `TURN_INCLUDES_ALL_INPUT` (já fixo no cliente) |
| Muitas respostas espontâneas | reforce `GEMINI_SYSTEM_INSTRUCTION`; `spont` deve ficar em 0 |
| RAM crescendo | verifique `drops`, tamanho do JPEG e o CSV do teste 8 |

## Limites deste módulo

Este pacote **não** implementa detecção local, YOLO/MobileNet, reconhecimento
facial, navegação autônoma, motores, processamento de áudio ou conversa completa.
Ele apenas entrega percepção visual para a nuvem e mede o caminho até lá.
