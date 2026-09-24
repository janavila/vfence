# VFence — servidor local IP2

Servidor leve para receber telemetria do gateway LoRa por USB/Serial, persistir o histórico em SQLite e exibir as coleiras em tempo real na rede local. O geofencing permanece na coleira; o Raspberry Pi monitora, registra e apresenta os dados.

## Arquitetura

```text
Coleira (GPS + geofencing) → LoRa 915 MHz → Gateway ESP32/SX1276
                                             ↓ USB/Serial
Navegador local ← HTTP/WebSocket ← Raspberry Pi (FastAPI + SQLite)
```

O leitor serial roda em uma tarefa assíncrona independente da API. Pacotes válidos passam pelo parser, são gravados em transação e publicados por WebSocket. Se o gateway desconectar, API e dashboard continuam disponíveis e o leitor tenta reconectar.

## Instalação e execução

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000 --reload
```

Acesse `http://IP_DO_RASPBERRY:8000` ou, com mDNS, `http://raspberrypi.local:8000`.

## Serial

Defina `SERIAL_PORT` como `/dev/ttyUSB0` ou `/dev/ttyACM0` e confira:

```bash
ls /dev/ttyUSB*
ls /dev/ttyACM*
sudo usermod -a -G dialout $USER
```

Pode ser necessário sair da sessão e entrar novamente, ou reiniciar, após adicionar o usuário ao grupo `dialout`. Ajuste `SERIAL_BAUD` se o firmware não estiver em 115200 baud.

## Teste sem hardware

Altere `VFENCE_MOCK_SERIAL=true` no `.env` e execute o servidor. Ele gera continuamente `SEGURO → ATENCAO → CRITICO → FORA`.

```bash
uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

O comando `python scripts/mock_gateway.py --interval 2` também imprime os pacotes, útil para terminal ou porta serial virtual.

## Protocolo

Legado:

```text
Recebido: VFENCE_TESTE_630 | RSSI: -103 dBm | SNR: 6.50
```

VFence v1 POS:

```text
V1|COL01|POS|-31.306119|-54.063935|SEGURO|12|0.90|630
```

O parser também aceita esse V1 envolvido pela saída do Receiver atual, preservando RSSI e SNR: `Recebido: V1|... | RSSI: -103 dBm | SNR: 6.50`.

Campos: versão, ID, tipo, latitude, longitude, zona, satélites, HDOP e sequência. Zonas: `SEGURO`, `ATENCAO`, `CRITICO`, `FORA` e `GNSS_INVALIDO`. Linhas vazias são ignoradas; mensagens longas, campos inválidos e coordenadas fora do intervalo são rejeitados sem interromper o serviço. A separação entre parser e transporte deixa espaço para tipos futuros como `FENCE`.

## API

- `GET /api/health` — saúde e estado resumido do gateway
- `GET /api/gateway` — porta, última mensagem e erro
- `GET /api/collars` — todas as coleiras
- `GET /api/collars/{collar_id}` — uma coleira
- `GET /api/collars/{collar_id}/telemetry?limit=100` — histórico
- `GET /api/events?limit=100` — mudanças de zona
- `WS /ws` — atualizações em tempo real
- `GET /docs` — OpenAPI interativa

O mapa usa Leaflet/OpenStreetMap quando há internet. Sem internet, coordenadas, cartões, histórico e tempo real continuam funcionando.

## Testes

```bash
pytest -q
```

Cobertura: parser legado e V1, mensagens/campos/coordenadas inválidos, zona, sequência, saúde da API e coleiras.

## systemd

Edite usuário e caminhos em `systemd/vfence.service` se o projeto não estiver em `/home/pi/vfence`, depois:

```bash
sudo cp systemd/vfence.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vfence
sudo systemctl start vfence
sudo systemctl status vfence
```

Nenhuma unidade é ativada automaticamente pelo projeto.

## Solução de problemas

- **Gateway OFFLINE:** confira cabo, porta, baud rate, grupo `dialout` e `journalctl -u vfence -f`.
- **Permissão negada:** confirme `groups` e reinicie a sessão após `usermod`.
- **Porta mudou:** liste `/dev/ttyUSB*` e `/dev/ttyACM*` e atualize `.env`.
- **Dashboard sem mapa:** o Raspberry está sem internet; a telemetria local segue funcional.
- **Nenhum dado:** ative o modo mock para separar servidor e hardware.

## Limitações atuais

Não há envio de comandos para coleiras, autenticação, mapa offline ou múltiplos gateways. O estado salvo reflete o último pacote; expiração visual automática de coleiras pode ser adicionada depois.
