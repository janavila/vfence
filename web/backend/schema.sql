-- Banco do Central (VFence Web) — seção 7 da especificação.
--
-- As nove tabelas abaixo são as da especificação, copiadas sem alteração.
-- O que foi ACRESCENTADO vem no fim do arquivo, em um bloco próprio e
-- comentado: são índices e uma garantia de unicidade. Nada disso muda a
-- API nem o formato dos dados, então não altera contrato.
--
-- Convenções que valem para todo o arquivo:
--   * datas são texto ISO 8601 em UTC terminando em Z (ver clock.py);
--   * pontos da cerca são inteiros em microgradus (grau x 10^6), nunca float;
--   * margens são inteiros em centímetros, nunca float;
--   * o campo de longitude se chama lon, nunca long.
--
-- Todo CREATE usa IF NOT EXISTS: este arquivo é executado em cada subida
-- do servidor e precisa ser inofensivo quando o banco já existe.

-- ===========================================================================
-- Cercas
-- ===========================================================================

-- Uma linha por VERSÃO de cerca. Versões nunca são reaproveitadas nem
-- editadas: mudar a cerca cria uma versão nova e maior (seção 3.3 do
-- planejamento). Por isso não existe UPDATE nos pontos de uma cerca.
CREATE TABLE IF NOT EXISTS fences (
  id INTEGER PRIMARY KEY,
  version INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','inactive')),
  margin_attention_cm INTEGER NOT NULL,
  margin_critical_cm INTEGER NOT NULL,
  area_m2 REAL NOT NULL,
  perimeter_m REAL NOT NULL,
  crc32 TEXT NOT NULL,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  reactivated_from INTEGER,
  created_at TEXT NOT NULL
);

-- Os pontos, em microgradus e na ordem de desenho (sempre horário).
-- A chave primária composta (fence_id, seq) já garante que não existem
-- dois pontos com o mesmo número na mesma cerca.
CREATE TABLE IF NOT EXISTS fence_points (
  fence_id INTEGER NOT NULL REFERENCES fences(id),
  seq INTEGER NOT NULL,
  lat_e6 INTEGER NOT NULL,
  lon_e6 INTEGER NOT NULL,
  PRIMARY KEY (fence_id, seq)
);

-- ===========================================================================
-- Base e coleiras
-- ===========================================================================

-- A Base (VFence Monitor no Raspberry). token_hash guarda o hash do
-- BASE_TOKEN, nunca o token em si. Os campos *_reported são o "estado
-- reportado" da seção 3.2 do planejamento: o que a Base diz que tem.
CREATE TABLE IF NOT EXISTS bases (
  base_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL,
  lat REAL, lon REAL,
  fence_version_reported INTEGER,
  serial_status TEXT,
  queue_size INTEGER,
  last_heartbeat TEXT
);

-- Último estado conhecido de cada coleira. last_zone é informação que a
-- COLEIRA calculou e mandou; o Central só repassa e exibe.
CREATE TABLE IF NOT EXISTS collars (
  collar_id TEXT PRIMARY KEY,
  base_id TEXT REFERENCES bases(base_id),
  animal_label TEXT,
  fence_version_reported INTEGER,
  last_zone TEXT,
  last_lat REAL, last_lon REAL,
  battery_pct INTEGER,
  rssi INTEGER, snr REAL,
  last_seen TEXT
);

-- ===========================================================================
-- Entrega da cerca
-- ===========================================================================

-- Estado da entrega de UMA versão de cerca em UMA coleira (seção 8).
-- pending -> at_base -> transmitting -> confirmed | failed
CREATE TABLE IF NOT EXISTS deliveries (
  fence_version INTEGER NOT NULL,
  collar_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','at_base','transmitting','confirmed','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  crc32_reported TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (fence_version, collar_id)
);

-- ===========================================================================
-- Dados que sobem da Base
-- ===========================================================================

-- Controle de duplicidade do lote (seção 9.3): a Base pode reenviar o
-- mesmo item à vontade. A chave (base_id, edge_seq) faz a segunda vez
-- ser ignorada sem erro.
CREATE TABLE IF NOT EXISTS edge_items (
  base_id TEXT NOT NULL,
  edge_seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (base_id, edge_seq)
);

-- Histórico de posições. zone vem calculada pela coleira.
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY,
  base_id TEXT NOT NULL, edge_seq INTEGER NOT NULL,
  collar_id TEXT NOT NULL, ts TEXT NOT NULL,
  lat REAL, lon REAL, zone TEXT,
  satellites INTEGER, hdop REAL, battery_pct INTEGER,
  fence_version INTEGER, rssi INTEGER, snr REAL
);

-- Eventos relatados pela Base (ex.: kind = 'zone_change').
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  base_id TEXT, edge_seq INTEGER,
  collar_id TEXT, kind TEXT NOT NULL, zone TEXT,
  detail TEXT, ts TEXT NOT NULL
);

-- ===========================================================================
-- Log por cerca
-- ===========================================================================

-- Linhas do arquivo de log de cada cerca. Guardar em tabela, e não em
-- arquivo solto, é o que faz o download de GET /api/fences/{version}/log
-- continuar funcionando depois de reiniciar o servidor (decisão C8 da
-- fase F0). O .txt é montado a partir destas linhas.
CREATE TABLE IF NOT EXISTS fence_log_lines (
  id INTEGER PRIMARY KEY,
  fence_version INTEGER NOT NULL,
  ts TEXT NOT NULL,
  line TEXT NOT NULL
);

-- ===========================================================================
-- ACRÉSCIMOS (não constam da seção 7; não mudam a API nem os dados)
-- ===========================================================================

-- Premissa P3 do planejamento: "uma propriedade, uma Base e UMA CERCA
-- ATIVA". Este índice único parcial transforma essa frase do documento
-- em garantia do banco: só pode existir uma linha com status 'active'.
-- Se um erro de programação tentar ativar duas cercas, o SQLite recusa
-- na hora, em vez de o produtor descobrir depois que metade do rebanho
-- está com a cerca errada.
-- Consequência para a fase F3: ao criar uma cerca nova é obrigatório
-- desativar a anterior ANTES de inserir a nova, dentro da mesma transação.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fences_single_active
  ON fences(status) WHERE status = 'active';

-- Histórico de cercas: a tela mostra "mais recente primeiro".
CREATE INDEX IF NOT EXISTS idx_fences_version_desc
  ON fences(version DESC);

-- Telemetria recente de uma coleira (GET /api/collars/{id}/telemetry).
-- Ordenamos por id, não por ts: id é a ordem real de chegada e não
-- empata quando dois pacotes têm o mesmo segundo.
CREATE INDEX IF NOT EXISTS idx_telemetry_collar
  ON telemetry(collar_id, id DESC);

-- Montagem do arquivo de log de uma cerca, na ordem em que aconteceu.
CREATE INDEX IF NOT EXISTS idx_fence_log_lines_version
  ON fence_log_lines(fence_version, id);
