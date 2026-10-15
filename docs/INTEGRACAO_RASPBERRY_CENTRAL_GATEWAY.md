# Integração Raspberry ↔ Central ↔ Gateway

## Demonstração atual sem hardware

O fluxo validado nesta etapa é:

```text
VFence Web → Central API → CentralClient → CentralSyncService
           → SQLite local → FastAPI local → Dashboard/Leaflet
```

O produtor cria a cerca no editor do Central. `POST /api/fences` valida o
polígono, cria uma versão crescente, torna essa versão ativa, converte os
pontos para E6 e calcula o CRC-32 canônico. O Monitor envia heartbeat para
`POST /api/edge/heartbeat`; ao receber uma versão desejada diferente da versão
local, baixa `GET /api/edge/fences/{version}`, valida o CRC e persiste a cerca
nas tabelas `fence_state` e `fence_points`.

Em `VFENCE_MOCK_SERIAL=true`, a entrega ao hardware é confirmada localmente.
O endpoint `GET /api/fence-state` lê somente o SQLite local e devolve os pontos
E6 junto com latitude/longitude em graus. O dashboard desenha esses pontos com
Leaflet e também recebe atualizações `type: fence` pelo WebSocket `/ws`.

Se o Central ficar indisponível, a última cerca baixada continua no SQLite e
permanece visível no mapa depois de reiniciar o Monitor.

### Executar no Windows

Use o mesmo `BASE_ID` e `BASE_TOKEN` nos dois processos. O Central usa a porta
8001 e o Monitor usa a porta 8000.

Terminal 1, na raiz do repositório:

```powershell
cd web
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m uvicorn backend.main:app --env-file .env --host 127.0.0.1 --port 8001
```

Terminal 2, na raiz do repositório:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:VFENCE_MOCK_SERIAL='true'
$env:CENTRAL_SYNC_ENABLED='true'
$env:CENTRAL_BASE_URL='http://127.0.0.1:8001'
$env:BASE_ID='BASE01'
$env:BASE_TOKEN='troque-este-token'
python -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Abra:

- Central/editor: `http://127.0.0.1:8001/editor`
- Monitor local: `http://127.0.0.1:8000/`
- Estado do Central no Monitor: `http://127.0.0.1:8000/api/central`
- Cerca persistida no Monitor: `http://127.0.0.1:8000/api/fence-state`

Depois de salvar a cerca no editor, aguarde o intervalo definido em
`CENTRAL_SYNC_INTERVAL_SECONDS`. O Monitor mostra a versão, a quantidade de
vértices e o mesmo perímetro no mapa sem reiniciar.

## Fluxo futuro com hardware

```text
VFence Web → Central API → Raspberry → Serial → Gateway → LoRa → Coleira
```

O código de Serial e entrega ao Gateway foi preservado. Ele fica fora do
caminho da demonstração quando `VFENCE_MOCK_SERIAL=true`.

## Fluxo de telemetria

```text
Coleira → LoRa VFENCE Protocol v1 → Gateway → linha Serial [APP] JSON
→ SerialReader → SQLite → WebSocket/dashboard → central_outbox
→ POST /api/edge/batch
```

O gateway converte a telemetria binária para uma linha iniciada por `[APP]`.
`SerialReader._parse_gateway_app()` transforma essa linha em `ParsedPacket`.

## Fluxo de cerca

```text
Produtor → Central publica versão → heartbeat do Raspberry
→ download e validação → persistência pendente
→ PARAM → FENCE_NEW → FENCE_POINT → FENCE_SEND
→ Gateway: FENCE_BEGIN/FENCE_CHUNK/FENCE_COMMIT por LoRa
→ Coleira: ACK/NACK → Gateway: resultado Serial
→ Raspberry: delivery confirmed/failed → Central: batch
```

Uma escrita na Serial não é confirmação. A Base somente marca `confirmed` depois
da linha `[FENCE TX] SUCESSO: nova cerca confirmada pela coleira.`.

## Representação e CRC

Existem dois contratos reais:

| Trecho | Representação | CRC |
|---|---|---|
| Central → Raspberry | E6 | little-endian: versão uint32, margens uint16, quantidade uint8 e pontos int32 |
| Raspberry → Gateway | graus decimais textuais | gateway converte |
| Gateway → coleira | E7 | ISO-HDLC sobre versão uint16, quantidade uint8 e pontos, big-endian |
| Coleira | E7 | confere o CRC LoRa |

Fronteira adotada:

1. `CentralClient` valida o CRC E6.
2. `SerialReader.send_fence()` divide E6 por 1.000.000 e escreve graus.
3. O gateway usa `degreesToE7()` e calcula o CRC E7 do protocolo LoRa.
4. Após o ACK real, a Base relata ao Central o CRC E6 da versão baixada.

Isso preserva os dois protocolos. Os E6 viram E7 múltiplos de 10.

## Persistência e operação offline

Tabelas acrescentadas:

- `central_outbox`: itens com `edge_seq` crescente;
- `fence_state`: versões desejada, baixada, pendente, enviada e confirmada;
- `fence_points`: pontos E6;
- `fence_deliveries`: estado por versão e coleira.

Sem Central, Serial, SQLite e dashboard continuam ativos. A outbox é reenviada
quando a rede volta, e só apaga itens até o `acked_up_to`.

## Configuração

```env
CENTRAL_SYNC_ENABLED=true
CENTRAL_BASE_URL=http://IP_DO_CENTRAL:8000
BASE_ID=BASE01
BASE_TOKEN=token-configurado-no-central
CENTRAL_TIMEOUT_SECONDS=10
CENTRAL_SYNC_INTERVAL_SECONDS=5
CENTRAL_RETRY_SECONDS=5
CENTRAL_BATCH_SIZE=100
COLLAR_IDS=COL01,COL02
```

Não publique o `.env`.

## Execução no Raspberry

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Use o firmware de `gateway/VFenceGateway/` e configure `SERIAL_PORT`.

## Teste sem hardware

Inicie o Central, ative `VFENCE_MOCK_SERIAL=true` e `CENTRAL_SYNC_ENABLED=true`,
publique uma cerca e consulte `/api/central` e `/api/fence-state`. O modo mock
marca a confirmação como simulada; ele não prova rádio ou ACK físico.

Testes automatizados:

```bash
python -m pytest tests -q -p no:cacheprovider
```

Usam Central HTTP falso, Serial falsa e SQLite em memória.

## Limitações

- IDs Serial aceitos: `COL01` a `COL254`, convertidos para uint8.
- O gateway transfere uma cerca por vez; a Base serializa as coleiras.
- Offline automático de coleiras no banco local continua não implementado.
- O dashboard ainda não mostra os novos campos, embora as APIs existam.
- Hardware, queda de energia durante transferência e alcance LoRa exigem bancada.
