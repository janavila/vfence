# VFence Monitor

Servidor local de telemetria para o sistema VFence. Recebe pacotes do gateway LoRa por USB/Serial, valida o protocolo, persiste dados em SQLite e disponibiliza API HTTP, WebSocket e painel operacional na rede local.

## Arquitetura

```text
Coleira / GPS / Geofencing
          │ LoRa 915 MHz
          ▼
Gateway ESP32 + SX1276
          │ USB / Serial
          ▼
Raspberry Pi
├── Serial reader
├── Protocol parser
├── SQLite
├── FastAPI / WebSocket
└── VFence Monitor
          │ HTTP — rede local
          ▼
Navegador
```

O geofencing é executado na coleira. O servidor recebe a posição e a zona já calculada; nenhuma decisão de cerca virtual é realizada no Raspberry Pi.

## Requisitos

- Python 3.10 ou superior
- Porta USB/Serial disponível para operação com hardware
- Navegador moderno
- Acesso à internet opcional, utilizado somente pelos tiles do OpenStreetMap

## Instalação

### Raspberry Pi OS / Linux

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Configuração

Variáveis disponíveis em `.env`:

| Variável | Padrão | Função |
|---|---:|---|
| `VFENCE_HOST` | `0.0.0.0` | Interface HTTP |
| `VFENCE_PORT` | `8000` | Porta HTTP |
| `SERIAL_PORT` | `/dev/ttyUSB0` | Dispositivo Serial |
| `SERIAL_BAUD` | `115200` | Baud rate |
| `DATABASE_URL` | `sqlite:///./vfence.db` | Banco SQLite |
| `VFENCE_MOCK_SERIAL` | `false` | Gerador interno de telemetria |
| `SERIAL_RECONNECT_SECONDS` | `3` | Intervalo de reconexão |
| `COLLAR_OFFLINE_SECONDS` | `30` | Limite reservado para estado offline |
| `MAX_LINE_LENGTH` | `512` | Comprimento máximo de pacote |
| `MOCK_INTERVAL_SECONDS` | `3` | Intervalo do gerador mock |

## Execução

```bash
uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Endereços:

- Painel: `http://IP_DO_RASPBERRY:8000`
- Painel via mDNS: `http://raspberrypi.local:8000`
- OpenAPI: `http://IP_DO_RASPBERRY:8000/docs`

## Operação Serial

Dispositivos esperados no Linux:

```bash
ls /dev/ttyUSB*
ls /dev/ttyACM*
```

Permissão de acesso:

```bash
sudo usermod -a -G dialout $USER
```

A alteração do grupo requer nova sessão ou reinicialização do sistema.

## Protocolo

### Legado

```text
Recebido: VFENCE_TESTE_630 | RSSI: -103 dBm | SNR: 6.50
```

### VFence V1 / POS

```text
V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|630
```

| Posição | Campo | Exemplo |
|---:|---|---|
| 1 | Versão | `V1` |
| 2 | Identificador | `COL01` |
| 3 | Tipo | `POS` |
| 4 | Latitude | `-31.306119` |
| 5 | Longitude | `-54.063935` |
| 6 | Zona | `SEGURO` |
| 7 | Satélites | `12` |
| 8 | HDOP | `0.90` |
| 9 | Sequência | `630` |

Zonas válidas: `SEGURO`, `ATENCAO`, `CRITICO`, `FORA`, `GNSS_INVALIDO`.

O parser também aceita V1 encapsulado pela saída do receptor:

```text
Recebido: V1|COL01|POS|...|630 | RSSI: -103 dBm | SNR: 6.50
```

## Persistência

Banco padrão: `vfence.db`.

| Tabela | Conteúdo |
|---|---|
| `collars` | Último estado conhecido por coleira |
| `telemetry` | Histórico de pacotes válidos |
| `events` | Transições de zona |

O schema é inicializado automaticamente. Eventos de zona são registrados somente quando o valor muda.

## API

| Método | Rota | Resultado |
|---|---|---|
| `GET` | `/api/health` | Estado do serviço e gateway |
| `GET` | `/api/gateway` | Estado detalhado da interface Serial |
| `GET` | `/api/collars` | Estado atual das coleiras |
| `GET` | `/api/collars/{collar_id}` | Estado de uma coleira |
| `GET` | `/api/collars/{collar_id}/telemetry` | Histórico por coleira |
| `GET` | `/api/events` | Histórico de eventos |
| `WS` | `/ws` | Atualizações em tempo real |

Parâmetro `limit`: mínimo `1`, máximo `1000`, padrão `100`.

Coleiras inexistentes retornam HTTP 404:

```json
{"detail":"collar_not_found"}
```

## Modo mock

Configuração:

```env
VFENCE_MOCK_SERIAL=true
```

O gerador interno publica uma sequência cíclica de estados `SEGURO`, `ATENCAO`, `CRITICO` e `FORA`. O script independente está disponível em `scripts/mock_gateway.py`.

## Testes

```bash
pytest tests/test_protocol.py tests/test_api.py -q
```

Escopo atual: protocolo legado, protocolo V1, validação numérica e geográfica, zonas, sequências e endpoints principais.

## Serviço systemd

O arquivo `systemd/vfence.service` assume instalação em `/home/pi/vfence` e usuário `pi`. Ajustar antes da instalação quando necessário.

```bash
sudo cp systemd/vfence.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vfence
sudo systemctl start vfence
sudo systemctl status vfence
```

## Diagnóstico

| Condição | Verificação |
|---|---|
| Gateway offline | Cabo USB, `SERIAL_PORT`, baud rate e logs do serviço |
| Acesso Serial negado | Grupo `dialout` e nova sessão do usuário |
| Porta inexistente | `/dev/ttyUSB*` e `/dev/ttyACM*` |
| Mapa indisponível | Conectividade com OpenStreetMap; telemetria local permanece ativa |
| Ausência de pacotes | Ativar `VFENCE_MOCK_SERIAL=true` para isolar hardware e aplicação |
| Serviço systemd | `journalctl -u vfence -f` |

## Limites da versão 1.0

- Sem transmissão de comandos para coleiras
- Sem autenticação
- Sem mapa offline
- Um gateway por instância
- Estado offline de coleiras ainda não aplicado automaticamente
