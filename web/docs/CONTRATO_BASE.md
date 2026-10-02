# Contrato Central ↔ Base (interface I2)

> **Para quem:** o integrante responsável pela Base (VFence Monitor, em `vfence/app/`).
> **O que é:** tudo o que o Monitor precisa para conversar com o Central, com exemplos
> tirados do código que já está rodando — não de rascunho.
> **Versão:** contrato v0, correspondente ao Central 0.1.0.
> **Fonte:** gerado na fase F7 a partir da implementação em `vfence/web/`. Onde este
> documento e `docs/VFence_Especificacao.md` discordarem, vale este, e a divergência
> precisa ser corrigida no outro.

---

## 1. Em uma frase

A Base chama o Central a cada poucos segundos, descobre se há cerca nova, baixa,
confere o CRC, repassa ao rádio, e entrega de volta tudo o que acumulou.

**O Central nunca chama a Base.** No campo o Raspberry fica atrás de um roteador ou
modem 4G, quase sempre com CGNAT: ele acessa a internet mas não pode ser acessado de
fora. Consequência boa: levar o Central da rede local para a nuvem exige trocar uma
linha (`CENTRAL_URL`) na configuração da Base.

---

## 2. As três rotas

Endereço base: `http://IP_DO_CENTRAL:8000`

| Método | Rota | Para que serve |
|---|---|---|
| `POST` | `/api/edge/heartbeat` | "estou vivo, tenho a cerca N" → recebe qual deveria ter |
| `GET` | `/api/edge/fences/{version}` | baixar a cerca na forma canônica |
| `POST` | `/api/edge/batch` | entregar telemetria, eventos e status de entrega |

Além dessas, `GET /api/health` responde sem autenticação e serve para a Base conferir
se achou o Central na rede:

```bash
curl http://192.168.0.10:8000/api/health
# {"status":"ok","version":"0.1.0","time":"2026-10-06T13:02:05Z"}
```

---

## 3. Autenticação

Todas as rotas de `/api/edge/` exigem o cabeçalho:

```
Authorization: Bearer <BASE_TOKEN>
```

O `BASE_TOKEN` é o mesmo valor configurado no `.env` do Central. O Central guarda
apenas o **hash SHA-256** dele no banco, nunca o token.

| Situação | Resposta |
|---|---|
| Cabeçalho ausente | `401` · `{"detail": "missing_token"}` |
| Formato inesperado ou token desconhecido | `401` · `{"detail": "invalid_token"}` |
| Token válido, mas `base_id` do corpo diferente | `403` · `{"detail": "base_id_mismatch"}` |

O login do produtor **não** afeta estas rotas: a Base não tem navegador e não faz
login. As rotas de `/api/edge/` ficam fora do controle de sessão de propósito, e
existe teste garantindo isso (`tests/test_api_auth.py`).

---

## 4. `POST /api/edge/heartbeat`

A chamada mais importante. É por ela que a cerca chega ao campo.

**Requisição**

```json
{
  "base_id": "BASE01",
  "fence_version": 0,
  "serial": "online",
  "queue_size": 12
}
```

| Campo | Tipo | Obrigatório | O que é |
|---|---|---|---|
| `base_id` | texto | sim | tem de bater com o dono do token |
| `fence_version` | inteiro ou `null` | não | versão que a Base tem AGORA. `null` se nenhuma |
| `serial` | texto | não | situação da porta serial, texto livre |
| `queue_size` | inteiro | não | quantos itens há na fila local |

**Resposta `200`**

```json
{
  "desired_fence_version": 1,
  "server_time": "2026-10-02T08:56:42Z"
}
```

- `desired_fence_version`: a versão da cerca ativa, ou `null` se nenhuma foi criada.
  **Diferente de `fence_version`? Baixe a nova.** É só isso que dispara a atualização.
- `server_time`: a hora do Central, em UTC. O Raspberry Pi 3 não tem relógio de
  bateria; sem internet para o NTP, esta é a melhor referência que a Base tem.

A Base é considerada **offline** depois de `BASE_OFFLINE_APOS_S` segundos sem
heartbeat (padrão 30). Esse valor só afeta o que aparece na tela do produtor.

```bash
curl -X POST http://192.168.0.10:8000/api/edge/heartbeat \
  -H "Authorization: Bearer troque-este-token" \
  -H "Content-Type: application/json" \
  -d '{"base_id":"BASE01","fence_version":0,"serial":"online","queue_size":0}'
```

---

## 5. `GET /api/edge/fences/{version}`

**Resposta `200`**

```json
{
  "version": 1,
  "margin_attention_cm": 500,
  "margin_critical_cm": 200,
  "points_e6": [
    [-31306000, -54064200],
    [-31306000, -54063700],
    [-31306400, -54063700],
    [-31306400, -54064200]
  ],
  "crc32": "DA29321E"
}
```

Note que **não há grau com casa decimal aqui**: só inteiros. Margens em centímetros,
coordenadas em microgradus (grau × 10⁶). É isso que faz o CRC bater em todas as
camadas: a conversão de `float` para inteiro acontece uma vez só, no Central.

Os pontos vêm **em ordem**, sempre no **sentido horário**, e `p1` **não** é repetido
no fim.

**Erro:** versão inexistente → `404` · `{"detail": "fence_not_found"}`

**Efeito colateral:** as entregas em `pending` daquela versão passam a `at_base`. O
download é a prova de que a cerca chegou à propriedade — sem ele, o produtor não
distinguiria "a Base nem sabe da cerca" de "a Base sabe mas o rádio não entregou".

```bash
curl http://192.168.0.10:8000/api/edge/fences/1 \
  -H "Authorization: Bearer troque-este-token"
```

---

## 6. A forma canônica e o CRC

Esta é a parte que a Base **e o firmware** precisam reproduzir exatamente.

### 6.1 Os bytes

Little-endian (ordem nativa do ESP32 e do x86), nesta ordem:

| Campo | Tipo | Bytes |
|---|---|---|
| `version` | `uint32` | 4 |
| `margin_attention_cm` | `uint16` | 2 |
| `margin_critical_cm` | `uint16` | 2 |
| `n` (número de pontos) | `uint8` | 1 |
| por ponto, em ordem: `lat_e6`, `lon_e6` | `int32`, `int32` | 8 × n |

Uma cerca de 4 pontos dá 41 bytes. Com 32 pontos dá 265 — acima do limite de 255
bytes por pacote do SX1276, e é daí que vem a necessidade de fragmentar.

Em Python:

```python
blob = struct.pack("<IHHB", version, dA_cm, dC_cm, n)
for lat_e6, lon_e6 in pontos:
    blob += struct.pack("<ii", lat_e6, lon_e6)
```

### 6.2 O CRC

CRC-32 padrão IEEE 802.3 (o mesmo do `zlib.crc32`, do ZIP e do Ethernet), sobre
esses bytes. Guardado e transmitido como **8 dígitos hexadecimais MAIÚSCULOS**.

A comparação é de texto, então o formato importa: `da29321e` e `DA29321E` são o mesmo
número, mas uma comparação de texto diria que não. O Central aceita minúsculas no que
RECEBE, e sempre guarda em maiúsculas.

### 6.3 Os dois vetores de teste obrigatórios

**Primeiro — a implementação do CRC:**

```
CRC-32("123456789") = CBF43926
```

**Segundo — a cerca completa.** Versão 1, dA = 5 m, dC = 2 m, quatro pontos em
sentido horário:

```
(-31.306000, -54.064200)
(-31.306000, -54.063700)
(-31.306400, -54.063700)
(-31.306400, -54.064200)
```

Resultado esperado — **41 bytes**:

```
01000000f401c80004f04e22feb80bc7fcf04e22feac0dc7fc604d22feac0dc7fc604d22feb80bc7fc
```

```
CRC-32 = DA29321E
área   ≈ 0,2113 ha
perímetro ≈ 183,96 m
```

Se o seu CRC sair diferente, compare primeiro os BYTES. O erro quase sempre está em
um destes três lugares: ordem dos bytes (big-endian em vez de little), tipo de algum
campo, ou arredondamento.

### 6.4 O arredondamento

`arredonda(x × 10⁶)`, com empate indo **para longe do zero**:

```
0,0000005  →  1
-0,0000005 → -1
```

Atenção às duas armadilhas de linguagem:

- **Python:** `round()` arredonda meio para o PAR (`round(0.5)` dá 0). Use
  `Decimal(str(x)).scaleb(6).quantize(Decimal(1), rounding=ROUND_HALF_UP)`.
- **JavaScript e C:** `Math.round()`/`round()` arredondam meio para CIMA, o que
  diverge em números NEGATIVOS — e todas as coordenadas do projeto são negativas.
  Use `sinal(x) * round(abs(x) × 1e6)`.

O `tools/simulador_base.py` tem uma implementação independente (em
`graus_para_e6()` e `bytes_canonicos()`) que chega ao mesmo resultado, e há teste
conferindo as duas (`tests/test_fluxo_completo.py`).

---

## 7. `POST /api/edge/batch`

**Requisição**

```json
{
  "base_id": "BASE01",
  "items": [
    {
      "edge_seq": 1531,
      "type": "telemetry",
      "data": {
        "collar_id": "COL01", "ts": "2026-10-06T13:01:58Z",
        "lat": -31.306119, "lon": -54.063935, "zone": "SEGURO",
        "satellites": 12, "hdop": 0.9, "battery_pct": 87,
        "fence_version": 1, "rssi": -92, "snr": 7.5
      }
    },
    {
      "edge_seq": 1532,
      "type": "delivery",
      "data": {
        "collar_id": "COL01", "fence_version": 1,
        "status": "confirmed", "crc32": "DA29321E"
      }
    },
    {
      "edge_seq": 1533,
      "type": "event",
      "data": {
        "collar_id": "COL01", "kind": "zone_change",
        "zone": "ATENCAO", "ts": "2026-10-06T13:02:00Z"
      }
    }
  ]
}
```

**Resposta `200`**

```json
{"acked_up_to": 1533}
```

Lote vazio é aceito e responde `{"acked_up_to": 0}` quando nada foi recebido ainda.

### 7.1 `edge_seq` e `acked_up_to` — leia com atenção

`edge_seq` é um contador **por Base**, **crescente** e **persistente** (sobrevive a
reinício do Raspberry). Ele é a chave da entrega confiável.

- O Central guarda `(base_id, edge_seq)`. Item já visto é **ignorado sem erro**:
  a Base pode reenviar o mesmo lote à vontade.
- `acked_up_to` é o maior `edge_seq` **contínuo** já gravado, contado a partir do
  menor item que o Central tem.
- A Base pode apagar da fila local tudo até esse número, e **só até ele**.

A palavra "contínuo" é o ponto. Se chegaram 1531, 1532, 1533 e 1540, a resposta é
**1533**, não 1540. O item 1540 está gravado e não será gravado de novo, mas o 1534
ao 1539 ainda não chegaram e a Base precisa continuar guardando.

```
enviados: 1531 1532 1533 ____ ____ ____ ____ ____ ____ 1540
                         ↑ primeiro buraco
resposta: acked_up_to = 1533
```

**Não reinicie o contador.** Se a Base voltar a numerar do 1 depois de já ter enviado
1531, o Central passa a responder um `acked_up_to` baixo e a fila nunca mais é
limpa. O contador precisa estar gravado em disco, não em memória.

### 7.2 Ordem de processamento

O Central ordena os itens por `edge_seq` antes de gravar, não pela ordem do JSON. O
estado de uma entrega depende da sequência, e a ordem do arquivo não pode decidir o
resultado. Ainda assim, mandar em ordem é mais fácil de depurar.

### 7.3 Os três tipos de item

**`telemetry`** — uma posição lida da serial.

| Campo | Tipo | Observação |
|---|---|---|
| `collar_id` | texto | obrigatório; item sem ele é descartado com aviso no log |
| `ts` | texto | ISO 8601 UTC terminando em `Z`. Ausente: o Central usa a hora dele |
| `lat`, `lon` | número | graus |
| `zone` | texto | `SEGURO`, `ATENCAO`, `CRITICO`, `FORA` ou `GNSS_INVALIDO` |
| `satellites`, `hdop` | número | qualidade do GNSS |
| `battery_pct` | inteiro | 0 a 100 |
| `fence_version` | inteiro | **a versão que a COLEIRA está usando** |
| `rssi`, `snr` | número | qualidade do rádio |

Campos ausentes **não apagam** o último valor conhecido da coleira: um pacote sem
bateria não zera a bateria na tela.

`fence_version` é o que fecha o ciclo do "estado reportado": se for diferente da
cerca ativa, aquela coleira aparece como **desatualizada** no painel.

**Zona desconhecida** não derruba o lote: a telemetria é gravada com zona vazia e um
evento `invalid_zone` é registrado. Recusar o lote criaria um item envenenado — a
Base reenviaria o mesmo lote para sempre e pararia de entregar todo o resto.

**O Central nunca recalcula zona.** Quem decide é a coleira.

**Coleira desconhecida** é cadastrada automaticamente ao aparecer na telemetria.

**`delivery`** — situação da entrega de uma cerca em uma coleira.

| Campo | Tipo | Observação |
|---|---|---|
| `collar_id` | texto | obrigatório |
| `fence_version` | inteiro | obrigatório |
| `status` | texto | só `transmitting`, `confirmed` ou `failed` (ver seção 8) |
| `crc32` | texto | **obrigatório quando `status` é `confirmed`** |
| `detail` | texto | motivo, quando `failed`. Texto livre, aparece no log da cerca |

**`event`** — algo que a coleira relatou.

| Campo | Tipo | Observação |
|---|---|---|
| `kind` | texto | obrigatório. Use `zone_change` para mudança de zona |
| `collar_id` | texto | quem relatou |
| `zone` | texto | a zona nova, quando for mudança de zona |
| `detail` | texto | livre |
| `ts` | texto | ISO 8601 UTC com `Z` |

```bash
curl -X POST http://192.168.0.10:8000/api/edge/batch \
  -H "Authorization: Bearer troque-este-token" \
  -H "Content-Type: application/json" \
  -d '{"base_id":"BASE01","items":[{"edge_seq":1,"type":"delivery",
       "data":{"collar_id":"COL01","fence_version":1,"status":"confirmed","crc32":"DA29321E"}}]}'
```

---

## 8. Estados da entrega

```
pending ──(a Base baixa)──▶ at_base ──(a Base relata)──▶ transmitting ──▶ confirmed
                                                                 └──────▶ failed
```

| Estado | Quem muda | Quando | Na tela |
|---|---|---|---|
| `pending` | Central | cerca criada, uma linha por coleira conhecida | Salva |
| `at_base` | Central | a Base chamou `GET /api/edge/fences/{v}` | Na Base |
| `transmitting` | **Base** | começou a enviar pelo rádio | Transmitindo |
| `confirmed` | **Base** | a coleira confirmou **e** o CRC bate | Confirmada |
| `failed` | **Base** | desistiu, ou o CRC veio diferente | Falhou |

A Base relata **apenas** `transmitting`, `confirmed` e `failed`. `pending` e
`at_base` são decisão do Central; se a Base os relatar, o item é aceito (não quebra o
lote) mas ignorado, com aviso no log.

### 8.1 A regra do CRC

`confirmed` só vale com o CRC igual ao da cerca. É isso que transforma "a coleira
respondeu" em "a coleira tem exatamente a cerca que o produtor desenhou".

| O que a Base manda | O que o Central grava |
|---|---|
| `confirmed` + CRC igual | `confirmed` |
| `confirmed` + CRC diferente | `failed`, `detail = "crc_mismatch"` |
| `confirmed` **sem** `crc32` | `failed`, `detail = "crc_missing"` |

Exemplo do CRC errado, como aparece em `GET /api/fences/2/deliveries`:

```json
{
  "fence_version": 2, "collar_id": "COL02",
  "status": "failed", "status_label": "Falhou",
  "attempts": 0, "crc32_reported": "0BADC0DE",
  "detail": "crc_mismatch", "updated_at": "2026-10-02T08:56:42Z"
}
```

### 8.2 Transições recusadas

- **Retrocesso é ignorado.** Um pacote atrasado não faz a tela piscar de
  "Confirmada" para "Transmitindo". `confirmed` é ponto final.
- **Exceção:** `failed → transmitting` é aceito. É o reenvio legítimo de quando a
  coleira reaparece com versão antiga.
- **Versão antiga** (diferente da cerca ativa): o relato vai para o log da cerca mas
  **não** altera a entrega. Isso evita que um pacote atrasado reabra uma entrega
  encerrada.

### 8.3 Contagem de tentativas

`attempts` é incrementado a cada `transmitting` recebido. A Base não precisa mandar o
número: o Central conta.

---

## 9. O que a Base precisa implementar

Para o outro integrante. Esta lista não é implementada pelo lado do Central.

### 9.1 Ciclo de sincronização

```
a cada SYNC_INTERVAL_SECONDS:
    POST /api/edge/heartbeat  (versão local, estado da serial, tamanho da fila)
    se desired_fence_version ≠ versão local:
        GET /api/edge/fences/{versão}
        recalcular o CRC pela forma canônica (seção 6) e CONFERIR
        CRC errado  → DESCARTAR; a cerca anterior continua valendo
        CRC certo   → gravar localmente e agendar a entrega
    POST /api/edge/batch com a fila → remover o que vier em acked_up_to

continuamente, lendo a serial:
    telemetria   → gravar e colocar na fila
    ACK de cerca → atualizar a entrega e colocar na fila
```

Intervalo sugerido: **5 s em bancada**, maior em campo com 4G — a cada 5 s são 17.280
requisições por dia.

### 9.2 Fila local (*outbox*)

- `edge_seq` crescente e **persistente** (em SQLite, não em memória).
- Enviada em lote; remover só o que vier em `acked_up_to`.
- Com limite de tamanho, para uma queda longa de internet não lotar o cartão SD.

### 9.3 Sem internet

Continuar registrando e entregando a última cerca que tem. A fila sobe quando a
conexão voltar. Isso é situação **normal**, não erro.

### 9.4 Enquanto o firmware não tiver os comandos de cerca

Um modo de entrega simulada (configurável) que marca a cerca como confirmada depois
de um atraso, com o CRC esperado. **Precisa aparecer no log como simulado**, senão o
relatório da IP2 registraria uma entrega que não aconteceu.

O `tools/simulador_base.py` do Central já faz exatamente isso e serve de referência
executável: ele é uma Base completa em um arquivo.

### 9.5 Configuração sugerida no `.env` do Monitor

```bash
CENTRAL_SYNC_ENABLED=false     # entra desligado, para não quebrar o que já funciona
CENTRAL_URL=http://192.168.0.10:8000
BASE_ID=BASE01
BASE_TOKEN=<o mesmo do .env do Central>
SYNC_INTERVAL_SECONDS=5
```

---

## 10. Como testar sem o Central pronto

O Central tem uma Base simulada que faz o ciclo completo. Rodando os dois lado a
lado, dá para comparar o comportamento:

```bash
cd vfence/web
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000     # em um terminal

python tools/simulador_base.py --url http://localhost:8000 \
       --intervalo 5 --atraso-confirmacao 3 --falhar COL02   # em outro
```

Ele imprime cada passo:

```
04:01:10 · heartbeat: tenho a cerca None, o Central quer a 1 (fila: 0)
04:01:10 ✓ cerca 1 baixada: 4 pontos, dA=800cm dC=500cm, CRC 892429BC confere
04:01:10 · COL01: transmitindo a cerca 1
04:01:10 ✓ COL01: CONFIRMOU a cerca 1 (crc 892429BC)
04:01:10 ✗ COL02: FALHOU a cerca 1 (pedido por --falhar)
04:01:10 · lote de 6 item(ns) enviado, confirmado até 10 (6 removido(s) da fila)
```

A documentação interativa do Central, em `http://localhost:8000/docs`, permite
disparar qualquer uma das rotas pelo navegador e ver a resposta — útil para conferir
um corpo de requisição antes de escrevê-lo em código.

---

## 11. Checklist de integração (marco de 16/10)

Para o teste de bancada, na ordem:

- [ ] Central e Raspberry na mesma rede; `CENTRAL_URL` aponta para o IP do notebook
- [ ] `BASE_ID` e `BASE_TOKEN` iguais nos dois `.env`
- [ ] `GET /api/health` responde do Raspberry (`curl` de dentro dele)
- [ ] Heartbeat aparece no painel do Central: a Base fica "ligada"
- [ ] `CRC-32("123456789")` = `CBF43926` na implementação da Base
- [ ] A cerca de teste da seção 6.3 produz `DA29321E` na implementação da Base
- [ ] Produtor cria uma cerca no editor; a Base detecta em até um ciclo
- [ ] A Base baixa, confere o CRC e grava localmente
- [ ] Status chega ao Central: a tela mostra Transmitindo e depois Confirmada
- [ ] `GET /api/fences/{v}/log` baixa e mostra a linha do tempo completa
- [ ] Queda de rede de alguns minutos: a fila sobe depois, sem duplicar nada

Se algo falhar do lado do Monitor, registre a requisição, a resposta e o trecho do
log do Central, e avise o responsável pela Base. **O Central não corrige nada em
`vfence/app/`.**

---

## 12. Decisões ainda em aberto que afetam este contrato

| Item | Situação |
|---|---|
| **Microgradus (E6) × E7** | Este contrato usa **E6** (grau × 10⁶). O firmware da coleira (`geofence/vfence.ino`) usa E7. **Precisa ser decidido pelo grupo:** com escalas diferentes o CRC nunca bate e nenhuma cerca sai de "Transmitindo" |
| **Projeção** | O Central usa esfera de raio 6.371.008,8 m; o firmware usa o elipsoide WGS84. A diferença chega a ~1 m em 200 m, então os casos geométricos compartilhados precisam de tolerância de 1% |
| **Zonas no firmware** | O firmware tem `LONGE`/`PERTO`/`MUITO_PERTO` com limiares fixos (10 m e 30 m). Este contrato espera as cinco zonas e as margens VINDAS da cerca |
| **CRC no firmware** | Ainda não implementado |
