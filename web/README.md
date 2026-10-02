# VFence Central

Aplicação web onde o produtor rural desenha a cerca virtual no mapa, envia e
acompanha a confirmação de cada coleira.

Projeto Integrador V — Engenharia de Computação, UNIPAMPA, 2026/2.

- **O que implementar e como:** [`../docs/VFence_Especificacao.md`](../docs/VFence_Especificacao.md)
- **Contexto e justificativas:** [`../docs/VFence_Planejamento_Web.md`](../docs/VFence_Planejamento_Web.md)

## Onde o Central fica no sistema

```text
Produtor (navegador)
      │  HTTP + WebSocket — rede local
      ▼
Central  ← esta pasta (vfence/web)
      ▲
      │  a Base sempre inicia a conexão (heartbeat)
Base = VFence Monitor (vfence/app) no Raspberry Pi
      │  USB serial
      ▼
Gateway LoRa  ⇄  Coleira (GNSS + geofencing + buzzer)
```

O Central guarda, valida, versiona e entrega a cerca, e mostra o que a Base
relata. Ele **não** fala com o rádio e **não** decide a zona do animal
(SEGURO, ATENCAO, CRITICO, FORA, GNSS_INVALIDO): quem decide zona é a coleira.

A pasta `vfence/app/` (a Base) é de outro integrante do grupo e **não** é
alterada aqui.

## Situação: todas as fases implementadas

A especificação divide o trabalho em fases (seção 12). Esta é a primeira versão
completa; revisões e ajustes vêm depois.

| Fase | Entrega | Situação |
|---|---|---|
| F1 | Esqueleto: servidor, `/api/health`, banco, logs, frontend | pronta |
| F2 | Geometria, validação VAL-01…VAL-12, forma canônica e CRC | pronta |
| F3 | API de cercas: criar, validar, histórico, reativar, log, GeoJSON | pronta |
| F4 | API da Base (heartbeat, download, lote) e simulador | pronta |
| F5 | Editor de cerca, com validação ao vivo igual à do servidor | pronta |
| F6 | WebSocket; telas de Início, Histórico, Rebanho e Eventos | pronta |
| F7 | `docs/CONTRATO_BASE.md` para o responsável pela Base | pronta |
| F8 | Login do produtor com sessão em cookie; rotas protegidas | pronta |

O que **falta** e não depende desta pasta: o teste de integração com o VFence
Monitor de verdade (fase F7, item 3), que só pode ser feito quando o outro
integrante concluir a parte da Base. Até lá, o `tools/simulador_base.py` faz esse
papel por completo.

## Telas

| Endereço | Tela | O que faz |
|---|---|---|
| `/` | Início | cerca valendo, coleiras confirmadas, animais por situação, últimos acontecimentos |
| `/editor` | Editor de cerca | os cinco passos: marcar, conferir, margens, revisar, enviar |
| `/historico` | Histórico | versões anteriores, baixar relatório e mapa, usar um desenho de novo |
| `/rebanho` | Rebanho | mapa com as coleiras coloridas pela situação, e a lista |
| `/eventos` | Acontecimentos | linha do tempo, com filtro por coleira |
| `/login` | Entrar | usuário único configurado no `.env` |
| `/docs` | Documentação da API | gerada pelo FastAPI; permite disparar as rotas pelo navegador |

## Requisitos

- Python 3.11 ou superior (testado em 3.14)
- Navegador atualizado
- Internet **opcional**: só as imagens de satélite e de ruas do mapa dependem
  dela. O Leaflet é servido pelo próprio Central.

## Instalação

### Linux e macOS

```bash
cd vfence/web
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Windows (PowerShell)

```powershell
cd vfence\web
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

### Leaflet

O Leaflet 1.9.4 já está em `frontend/vendor/leaflet/` e versionado no
repositório — **não** depende de CDN, porque o Central roda na rede local da
propriedade. Se algum dia precisar baixar de novo:

```bash
curl -L -o /tmp/leaflet.zip https://github.com/Leaflet/Leaflet/releases/download/v1.9.4/leaflet.zip
unzip -o /tmp/leaflet.zip -d /tmp/leaflet
cp /tmp/leaflet/dist/leaflet.js /tmp/leaflet/dist/leaflet.css frontend/vendor/leaflet/
cp -r /tmp/leaflet/dist/images frontend/vendor/leaflet/
```

## Configuração

Tudo vem do arquivo `.env`, que **nunca** vai para o Git. O modelo
versionado é o `.env.example`. Os nomes das variáveis são os da seção 14 da
especificação, sem alteração.

### Servidor

| Variável | Padrão | Função |
|---|---:|---|
| `APP_HOST` | `0.0.0.0` | Interface de rede. `0.0.0.0` aceita acesso de outros aparelhos da rede |
| `APP_PORT` | `8000` | Porta HTTP |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR` |
| `DATA_DIR` | `./data` | Onde ficam o banco e os logs. Fora do Git |

### Base e coleiras

| Variável | Padrão | Função |
|---|---:|---|
| `BASE_ID` | `BASE01` | Identificador da Base |
| `BASE_TOKEN` | — | Segredo que a Base envia em `Authorization: Bearer`. **Troque antes de usar** |
| `BASE_LAT` | `-31.306200` | Latitude da Base. Centro do mapa e referência da regra VAL-03 |
| `BASE_LON` | `-54.063950` | Longitude da Base |
| `BASE_OFFLINE_APOS_S` | `30` | Segundos sem heartbeat para considerar a Base offline |
| `COLARES_CONHECIDOS` | `COL01,COL02` | Coleiras cadastradas na subida, separadas por vírgula |

O Central guarda apenas o **hash** do `BASE_TOKEN` no banco, nunca o token.

### Regras da cerca

| Variável | Padrão | Função |
|---|---:|---|
| `MARGEM_ATENCAO_PADRAO_M` | `5.0` | Margem de atenção sugerida no editor (dA) |
| `MARGEM_CRITICA_PADRAO_M` | `2.0` | Margem crítica sugerida (dC). Precisa ser `0 < dC < dA` |
| `BASE_RAIO_MAX_KM` | `5` | Distância máxima entre um ponto da cerca e a Base (VAL-03) |
| `LORA_ALCANCE_M` | `200` | Alcance de rádio medido no ensaio da IP1 (VAL-10, aviso) |
| `GNSS_ERRO_ESPERADO_M` | `3.0` | Erro horizontal esperado do GNSS (VAL-07) |
| `GNSS_FATOR_SEGURANCA` | `1.5` | Multiplica o erro para a folga recomendada (VAL-12) |

Com os padrões, a folga recomendada de margem crítica é 3,0 × 1,5 = **4,5 m**;
como o padrão de dC é 2,0 m, o editor vai mostrar o aviso VAL-12 — o que é
esperado e o produtor pode enviar mesmo assim.

### Login (fase F8, ainda não usado)

`ADMIN_USER`, `ADMIN_PASSWORD` e `SESSION_SECRET`.

## Execução

```bash
cd vfence/web
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Durante o desenvolvimento, `--reload` reinicia o servidor a cada alteração:

```bash
uvicorn backend.main:app --reload
```

Endereços:

| Endereço | O que é |
|---|---|
| `http://localhost:8000` | Tela de Início |
| `http://localhost:8000/docs` | Documentação automática da API, com botão de testar |
| `http://localhost:8000/api/health` | Situação do serviço |

Para abrir de outro aparelho da rede (celular, ou o Raspberry chamando o
Central), use o IP da máquina no lugar de `localhost`:

```bash
ipconfig getifaddr en0     # macOS
hostname -I                # Linux
```

## Testes

```bash
cd vfence/web
source .venv/bin/activate
pytest
```

São 352 testes, em cerca de 7 segundos. Eles usam banco em pasta temporária e
**não** tocam em `data/vfence.db` nem no `.env` de quem está rodando.

| Arquivo | O que cobre |
|---|---|
| `test_crc.py` | CRC-32, incluindo o vetor `"123456789"` = `CBF43926` |
| `test_canonical.py` | microgradus, centímetros, bytes canônicos, o vetor `DA29321E` |
| `test_geometry.py` | projeção, área, perímetro, sentido, lados que se cruzam |
| `test_validation.py` | uma função por regra VAL-01…VAL-12, mais os casos compartilhados |
| `test_geometry_js.py` | o espelho em JavaScript concorda com o servidor |
| `test_db.py` | as nove tabelas, cadastro da Base, travas do banco |
| `test_api_health.py` | `/api/health`, páginas, Leaflet local, isolamento |
| `test_api_fences.py` | criar, validar, histórico, reativar, log, GeoJSON |
| `test_api_edge.py` | token, heartbeat, download, lote idempotente, estados de entrega |
| `test_api_monitoring.py` | coleiras, telemetria, eventos, bases, `/api/settings` |
| `test_ws.py` | WebSocket: os quatro tipos de mensagem da seção 6.2 |
| `test_api_auth.py` | login, sessão, o que fica protegido e o que **não** pode ficar |
| `test_fluxo_completo.py` | o caminho inteiro: desenhar → enviar → baixar → confirmar |
| `test_contrato_base.py` | o `CONTRATO_BASE.md` continua verdadeiro |

### Os casos geométricos compartilhados

`tests/casos_geometricos.json` é a referência comum de **três** implementações: a
validação em Python do Central, o espelho em JavaScript do editor
(`frontend/js/geometry.js`) e, futuramente, o geofencing em C++ da coleira. Cada
caso traz os pontos e o resultado esperado de cada regra.

Se as três concordarem nos mesmos casos, a geometria está consistente nas três
pontas. É o arquivo a usar quando o firmware começar a calcular zona.

### Sobre o Node

`test_geometry_js.py` usa o Node para executar o mesmo arquivo `.js` que o
navegador carrega, e comparar o resultado com o do Python. O Node é **apenas
ferramenta de teste**: não está no `requirements.txt`, não participa da execução
e não há etapa de compilação. Sem ele instalado, esses testes são pulados e o
resto da suíte continua valendo.

## Simulador da Base

`tools/simulador_base.py` faz o papel do Raspberry Pi: é com ele que o Central é
construído, testado e demonstrado sem hardware e sem depender do andamento da
Base de verdade.

```bash
# em um terminal
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# em outro
python tools/simulador_base.py --url http://localhost:8000 \
       --intervalo 5 --atraso-confirmacao 3 --falhar COL02
```

Sem `--base-id` e `--token`, ele lê os valores do `.env` do Central.

Cada ciclo: heartbeat → baixa a cerca nova → **recalcula o CRC e confere** →
envia `transmitting` → depois do atraso, `confirmed` (ou `failed`, para as
coleiras de `--falhar`) → gera telemetria de coleiras andando em linha reta e
cruzando a cerca. Ele imprime cada passo:

```text
04:01:10 · heartbeat: tenho a cerca None, o Central quer a 1 (fila: 0)
04:01:10 ✓ cerca 1 baixada: 4 pontos, dA=800cm dC=500cm, CRC 892429BC confere
04:01:10 · COL01: transmitindo a cerca 1
04:01:10 ✓ COL01: CONFIRMOU a cerca 1 (crc 892429BC)
04:01:10 ✗ COL02: FALHOU a cerca 1 (pedido por --falhar)
04:01:10 · lote de 6 item(ns) enviado, confirmado até 10 (6 removido(s) da fila)
```

| Opção | Padrão | O que faz |
|---|---|---|
| `--url` | `http://localhost:8000` | endereço do Central |
| `--intervalo` | `5` | segundos entre ciclos |
| `--coleiras` | do `.env` | identificadores separados por vírgula |
| `--atraso-confirmacao` | `3` | segundos entre o download e a confirmação |
| `--falhar` | — | coleiras que vão FALHAR a entrega |
| `--passo` | `4` | metros que cada coleira anda por ciclo |
| `--silencioso` | — | não imprime cada passo |

Ele **reimplementa** a forma canônica e o CRC de propósito, sem importar nada do
`backend/`: o valor da conferência está em duas implementações independentes
chegarem ao mesmo número — que é a situação do firmware. Há teste conferindo as
duas (`test_fluxo_completo.py`).

## Contrato para a Base

`docs/CONTRATO_BASE.md` tem tudo o que o responsável pela Base precisa: as três
rotas com exemplos reais de requisição e resposta, autenticação, a forma canônica
com os dois vetores de teste do CRC, a semântica de `edge_seq` e `acked_up_to`, os
estados de entrega, exemplos com `curl` e um checklist de integração.

Os valores desse documento são conferidos por `tests/test_contrato_base.py`: se o
código mudar e o documento não, a suíte falha.

## Estrutura da pasta

```text
web/
├── .env.example          modelo de configuração (versionado)
├── requirements.txt      5 dependências, com versão fixada
├── pytest.ini            configuração dos testes
│
├── backend/
│   ├── main.py           monta a aplicação, serve API e frontend, protege as rotas
│   ├── config.py         lê o .env e valida os valores
│   ├── clock.py          hora UTC no formato do projeto
│   ├── logging_setup.py  formato de log da seção 10
│   ├── security.py       hash de token e de senha, cookie de sessão
│   ├── errors.py         corpo de erro no formato da seção 6
│   ├── db.py             SQLite: conexão, tabelas, cadastro inicial
│   ├── schema.sql        as nove tabelas da seção 7
│   ├── routers/
│   │   ├── health.py     GET /api/health
│   │   ├── auth.py       login, logout, sessão
│   │   ├── settings.py   configuração para a tela
│   │   ├── fences.py     /api/fences/* — nove rotas
│   │   ├── edge.py       /api/edge/* — as três rotas da Base
│   │   ├── monitoring.py coleiras, telemetria, eventos, bases
│   │   └── ws.py         WebSocket /ws
│   ├── schemas/          modelos Pydantic de entrada e saída
│   └── services/
│       ├── geometry.py       projeção, área, perímetro, sentido, cruzamentos
│       ├── validation.py     regras VAL-01 a VAL-12
│       ├── canonical.py      microgradus, centímetros, bytes canônicos
│       ├── crc.py            CRC-32
│       ├── fence_service.py  criar, ativar, reativar, histórico
│       ├── delivery.py       estados de entrega por coleira
│       ├── edge_service.py   heartbeat e gravação dos lotes
│       ├── fence_log.py      log por cerca e exportação GeoJSON
│       └── realtime.py       difusão para os navegadores conectados
│
├── frontend/
│   ├── index.html · editor.html · historico.html
│   ├── rebanho.html · eventos.html · login.html
│   ├── css/style.css     estilo único de todas as telas
│   ├── js/
│   │   ├── ui.js         cabeçalho, conexão, utilidades
│   │   ├── api.js        chamadas à API e tratamento de erro
│   │   ├── geometry.js   espelho das regras do servidor
│   │   ├── map.js        mapa Leaflet e camadas
│   │   ├── editor.js     os cinco passos do editor
│   │   ├── realtime.js   WebSocket com reconexão e plano B
│   │   └── paineis.js    Início, Histórico, Rebanho, Eventos
│   └── vendor/leaflet/   Leaflet 1.9.4 local, sem CDN
│
├── static/VFence.png     logotipo e ícone da aba
├── tools/simulador_base.py   a Base simulada
├── docs/CONTRATO_BASE.md     contrato para o responsável pela Base
├── tests/                    352 testes
└── data/                     (fora do Git) banco e logs
```

### Três acréscimos à estrutura da seção 4

A seção 4 da especificação não é exaustiva. Foram acrescentados:

- **`backend/clock.py`** — a hora UTC é usada pelo banco, pelas rotas e pelos
  logs; não caberia dentro de nenhum deles sem criar dependência torta.
- **`backend/security.py`** e **`backend/errors.py`** — pelo mesmo motivo:
  usados por vários routers.
- **`backend/services/edge_service.py`** — o processamento do lote é a lógica
  mais delicada do lado da Base e merece arquivo e testes próprios.

E duas junções, para não haver arquivos de trinta linhas:

- **`routers/monitoring.py`** reúne o que a seção 4 previa em `collars.py`,
  `events.py` e `bases.py`: são todas leituras do mesmo assunto.
- **`frontend/js/paineis.js`** atende as quatro telas de painel, que fazem a
  mesma coisa — buscar dados, desenhar tabela, atualizar ao vivo. Em quatro
  arquivos, os formatadores ficariam duplicados e o orçamento de 100 KB da
  seção 11.1 estouraria.

### Uma rota a mais

**`GET /api/settings`** não está na seção 6. Foi acrescentada porque o editor não
funciona sem conhecer `BASE_LAT`/`BASE_LON` e os limites das regras — e a
especificação não previu nenhuma forma de o navegador descobrir isso. Devolve só o
que a tela usa; nenhum segredo, com teste garantindo.

## Logs

Formato da seção 10 da especificação, em **UTC**, no console e em
`data/logs/central.log`:

```text
2026-10-06 13:02:05 [INFO] db: database ready (/.../data/vfence.db)
2026-10-06 13:02:05 [INFO] db: base=BASE01 registered (lat=-31.306200, lon=-54.063950, collars=COL01,COL02)
2026-10-06 13:02:05 [INFO] central: central started (version=0.1.0, data_dir=/.../data)
```

Da fase F3 em diante, toda linha sobre uma cerca leva `fence=N` e, quando
couber, `collar=COLxx` e `base=BASExx`. Procurar `fence=4` nos logs do Central
e da Base reconstrói o caminho inteiro daquela cerca.

> **Atenção ao comparar com a Base:** o Central grava em UTC, como manda a
> especificação. O VFence Monitor hoje grava em hora local. Até a Base fazer o
> mesmo ajuste, os dois logs podem aparecer com 3 horas de diferença na
> bancada.

## Diagnóstico

| Situação | O que verificar |
|---|---|
| `ModuleNotFoundError: backend` | Rodar de dentro de `vfence/web`, com a `.venv` ativada |
| Servidor recusa subir falando de `BASE_LAT` | Latitude e longitude trocadas, ou sem o sinal negativo |
| Servidor recusa subir falando das margens | `MARGEM_CRITICA_PADRAO_M` precisa ser menor que a de atenção e maior que zero |
| Servidor recusa subir falando de `ADMIN_PASSWORD` | Com `AUTH_ENABLED=true`, a senha não pode ficar vazia |
| Toda tela manda para o login | É o esperado sem sessão. Entre em `/login` |
| A Base leva `401` | `BASE_TOKEN` diferente entre os dois `.env` |
| A Base leva `403` | `BASE_ID` do corpo diferente do dono do token |
| A Base reenvia sempre o mesmo lote | O `edge_seq` dela precisa ser crescente e **persistente** |
| Cerca recusada com `VAL-06` | Os pontos estão anti-horários. Use o botão Inverter ordem |
| Cerca recusada com `VAL-03` | Latitude e longitude trocadas, ou a Base está no `.env` errado |
| "Falhou" com `crc_mismatch` | A coleira não guardou a cerca enviada. Confira a escala E6/E7 no firmware |
| Mapa cinza, sem imagem | As imagens precisam de internet; o resto do sistema segue funcionando |
| "Sem conexão com o servidor" | Conferir se o uvicorn está rodando e se a porta é a mesma |
| Celular não abre o endereço | Subir com `--host 0.0.0.0` e usar o IP da máquina |
| Começar o banco de zero | Parar o servidor e apagar a pasta `data/` |

## Decisões em aberto que afetam este código

Levantadas na fase F0 e registradas aqui para não se perderem. As duas primeiras
precisam de decisão do grupo.

1. **Microgradus (E6) × E7 — a mais importante.** O Central usa grau × 10⁶ na
   forma canônica e no CRC, como manda a especificação. O firmware da coleira
   (`geofence/vfence.ino:136`) e o editor local do Monitor
   (`app/static/js/fence.js:16`) usam grau × 10⁷. **Com escalas diferentes o CRC
   nunca bate e nenhuma cerca sai de "Transmitindo".** Os dois cabem em `int32`,
   então é escolha, não limitação. O E6 dá resolução de ~0,11 m, 18 vezes mais
   fina que a margem crítica padrão de 2 m. Prazo da especificação: 09/10.
2. **Projeção.** O Central usa esfera de raio 6.371.008,8 m (seção 5.3); o
   firmware usa o elipsoide WGS84. A diferença chega a ~1 m em 200 m, então os
   casos geométricos compartilhados comparam com tolerância de 1%.
3. **Zonas e margens no firmware.** O firmware tem `LONGE`/`PERTO`/`MUITO_PERTO`
   com limiares fixos de 10 m e 30 m (`geofence/vfence.ino:27`). O Central envia
   dA e dC e espera de volta as cinco zonas. Não é problema desta pasta, mas é o
   que decide se o marco de 16/10 fecha.
4. **Cores das zonas.** Provisórias, copiadas do Monitor. Ficam em variáveis CSS
   no topo de `frontend/css/style.css`, para a troca ser uma linha por cor.
5. **Critério da regra VAL-09** e os valores de `GNSS_ERRO_ESPERADO_M` e
   `GNSS_FATOR_SEGURANCA`, a calibrar com os dados GNSS da IP1. O critério vive
   isolado em `val09_is_small_for_margin()`, para o ajuste mexer em uma função só.

## Dois pontos de atenção para quem continuar

- **O orçamento de leveza está quase cheio.** A seção 11.1 dá 100 KB para o JS e
  o CSS próprios; hoje estão em 99,8 KB. Acrescentar código ao frontend vai exigir
  enxugar outra parte. O comando para conferir:

  ```bash
  python -c "import pathlib; css=pathlib.Path('frontend/css/style.css').stat().st_size; \
  js=sum(p.stat().st_size for p in pathlib.Path('frontend/js').glob('*.js')); \
  print(f'{css+js} bytes de 100000')"
  ```

- **O logotipo pesa 226 KB para 472×529 px**, codificação ineficiente para esse
  tamanho. Ele carrega em toda página, e o frontend próprio inteiro soma 100 KB.
  Como a seção 11.2 diz "favicon: o mesmo PNG", não foi alterado — mas vale
  reduzir, mantendo o original para o relatório.
