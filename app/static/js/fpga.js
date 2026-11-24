const byId = id => document.getElementById(id);
const selector = byId('fpga-collar');
const EMPTY = '—';
const REFRESH_MS = 15000;
const MAX_AGE_MS = 180000;
const labels = {
  valid: 'Válido', missing: 'Ausente', invalid: 'Inválido', uncorrelated: 'Sem correlação',
};
const pdrReasons = {
  no_samples: 'Sem amostras', one_sample: 'Apenas uma amostra', session_changed: 'Sessão mudou',
  profile_changed: 'Perfil mudou', discontinuity: 'Descontinuidade no intervalo',
};
const calendar = new Intl.DateTimeFormat('pt-BR', {
  dateStyle: 'short', timeStyle: 'medium', timeZone: 'America/Sao_Paulo',
});
const number = new Intl.NumberFormat('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const between = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;
const hexId = value => typeof value === 'string' && /^[0-9a-f]{16}$/.test(value);
const date = value => {
  const time = Date.parse(value);
  return Number.isFinite(time) ? calendar.format(time) : EMPTY;
};
const isRecent = (stamp, maxAge = MAX_AGE_MS) => {
  const age = Date.now() - Date.parse(stamp);
  return age >= -5000 && age <= maxAge;
};
const profileName = value => between(value, 0, 3) ? `P${value}` : EMPTY;
const display = (value, unit = '') => Number.isFinite(value) ? `${number.format(value / 100)}${unit}` : EMPTY;

let collars = [];
let gateway = null;
let linkMetrics = null;
let pdr = null;
let overviewPending = false;
let metricsRequest = null;
let overviewFailed = false;
let metricsState = 'idle';
let lastUpdate = null;

function element(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value ?? EMPTY;
  if (className) node.className = className;
  return node;
}

function showDetails(id, values) {
  byId(id).replaceChildren(...values.map(([label, value]) => {
    const row = element('div', '');
    row.append(element('dt', label), element('dd', value));
    return row;
  }));
}

function collarNumber(id) {
  const match = /^COL(\d{2,3})$/.exec(id);
  const value = match ? Number(match[1]) : null;
  return between(value, 1, 254) && id === `COL${String(value).padStart(2, '0')}` ? value : null;
}

function selectedUnit() {
  return collars.find(collar => collar.collar_id === selector.value);
}

function renderUnit() {
  const unit = selectedUnit();
  const position = unit?.position;
  const contact = unit?.contact;
  const contactAge = (gateway?.freshness_seconds ?? 180) * 1000;
  const state = !unit ? EMPTY : gateway?.mock ? 'Simulado' : overviewFailed ? 'Estado indisponível' :
    contact?.state === 'offline' ? 'OFFLINE reportado' :
    contact?.state === 'online' && gateway?.serial_open && isRecent(contact.last_contact_at, contactAge)
      ? 'ONLINE' : 'Sem contato recente confirmado';
  const gpsLost = unit?.gnss_event?.type === 'gps_lost' &&
    (!position || Date.parse(unit.gnss_event.at) >= Date.parse(position.at));
  const gnss = gpsLost || position?.zone === 'GNSS_INVALIDO' ? 'Inválido no último relato' :
    Number.isFinite(position?.lat) && Number.isFinite(position?.lon) ? 'Válido na última posição' : 'Sem informação';
  const observed = linkMetrics?.gateway?.effective_profile;
  const profile = profileName(observed?.profile_id);
  showDetails('fpga-unit', [
    ['Identificador', unit?.collar_id], ['Estado da coleira', state],
    ['Última comunicação', date(contact?.last_contact_at)], ['GNSS', unit ? gnss : EMPTY],
    ['Perfil observado no Gateway', profile === EMPTY ? profile :
      `${profile} · ${date(observed.observed_at)}${isRecent(observed.observed_at) ? '' : ' · Antigo'}`],
    ['Última posição recebida', date(position?.at)], ['Última atualização da consulta', date(lastUpdate)],
  ]);
}

function windowDetails(row, clock, sessionId = row?.session_id) {
  return [
    ['Sessão', sessionId], ['Janela', row?.window_id],
    ['Duração', Number.isFinite(row?.duration_ms) ? `${row.duration_ms / 1000} s` : EMPTY],
    [`Início · relógio ${clock}`, between(row?.started_at_ms, 0, 0xffffffff) ? `${row.started_at_ms} ms` : EMPTY],
    ['Gravação no Raspberry', date(row?.persisted_at)],
    ['Idade do registro', !row?.persisted_at ? 'Indisponível' : isRecent(row.persisted_at) ? 'Recente' : 'Antigo'],
  ];
}

function flagLabel(flag) {
  if (flag === true) return 'Válido';
  if (flag === false) return 'Inválido';
  return 'Ausente';
}

function metricRows() {
  return {hdop: linkMetrics?.windows?.hdop?.[0], radio: linkMetrics?.windows?.radio?.[0], pdr};
}

function renderMetrics() {
  const {hdop, radio, pdr: delivery} = metricRows();
  const empty = metricsState === 'loading' ? 'Consultando…' :
    metricsState === 'error' ? 'Consulta indisponível' : 'Sem registro';
  byId('fpga-hdop-value').textContent = hdop
    ? (hdop.hdop_mean_valid === true ? display(hdop.hdop_mean_x100) : 'N/A') : empty;
  showDetails('fpga-hdop', hdop ? [
    ['Amostras', hdop.sample_count], ['Validade', flagLabel(hdop.hdop_mean_valid)],
    ['Produtor', hdop.producer_id], ...windowDetails(hdop, 'da coleira'),
    ['Recepção · relógio do Gateway', between(hdop.gateway_received_at_ms, 0, 0xffffffff)
      ? `${hdop.gateway_received_at_ms} ms` : EMPTY],
    ['Sessão do Gateway na recepção', hdop.gateway_session_id],
  ] : []);
  byId('fpga-radio-value').textContent = radio
    ? `${radio.rssi_mean_valid === true ? display(radio.rssi_mean_x100, ' dBm') : 'N/A'} · ${
      radio.snr_mean_valid === true ? display(radio.snr_mean_x100, ' dB') : 'N/A'}` : empty;
  showDetails('fpga-radio', radio ? [
    ['Validade RSSI', flagLabel(radio.rssi_mean_valid)], ['Validade SNR', flagLabel(radio.snr_mean_valid)],
    ['Amostras', radio.sample_count], ['Produtor', radio.producer_id],
    ...windowDetails(radio, 'do Gateway'),
  ] : []);
  byId('fpga-pdr-value').textContent = delivery
    ? (delivery.valid === true ? display(delivery.pdr_x100, '%') : 'N/A') : empty;
  showDetails('fpga-pdr', delivery ? [
    ['Recebidos', delivery.received_count], ['Enviados estimados', delivery.estimated_tx_count],
    ['Perfil', profileName(delivery.profile_id)], ['Validade', flagLabel(delivery.valid)],
    ['Motivo', delivery.valid === true ? EMPTY : pdrReasons[delivery.reason] ?? 'Motivo indisponível'],
    ['Gateway', delivery.gateway_id], ['Sessão da coleira', delivery.collar_session_id],
    ...windowDetails(delivery, 'do Gateway', delivery.gateway_session_id),
  ] : []);
}

function metricField(row, key, validKey, min, max, count, minCount = 1, maxCount = 65535) {
  if (!row) return [null, 'missing'];
  const value = row[key];
  if (row[validKey] === false || !between(count, minCount, maxCount) ||
      (value != null && !between(value, min, max))) return [value, 'invalid'];
  if (value == null || row[validKey] !== true) return [value, 'missing'];
  if (!isRecent(row.persisted_at) || row.duration_ms !== 60000 ||
      !hexId(row.window_id) || !between(row.started_at_ms, 0, 0xffffffff)) return [value, 'invalid'];
  return [value, 'valid'];
}

function previewFields() {
  const {hdop, radio, pdr: delivery} = metricRows();
  const id = collarNumber(selector.value);
  const observed = linkMetrics?.gateway?.effective_profile;
  const profile = [observed?.profile_id, !observed ? 'missing' :
    between(observed.profile_id, 0, 3) && isRecent(observed.observed_at) ? 'valid' : 'invalid'];
  const hdopField = metricField(hdop, 'hdop_mean_x100', 'hdop_mean_valid', 0, 65534, hdop?.sample_count, 1, 60);
  const rssiField = metricField(radio, 'rssi_mean_x100', 'rssi_mean_valid', -25500, 0, radio?.sample_count, 1, 255);
  const snrField = metricField(radio, 'snr_mean_x100', 'snr_mean_valid', -32768, 32767, radio?.sample_count, 1, 255);
  const pdrField = metricField(delivery, 'pdr_x100', 'valid', 1, 10000, delivery?.received_count, 2, 64);
  const radioMatchesPdr = radio && delivery && radio.producer_id === delivery.gateway_id &&
    hexId(radio.session_id) && radio.session_id === delivery.gateway_session_id &&
    radio.window_id === delivery.window_id && radio.started_at_ms === delivery.started_at_ms &&
    radio.duration_ms === delivery.duration_ms;
  const correlate = (field, matches) => field[1] === 'valid' && !matches ? [field[0], 'uncorrelated'] : field;
  const alignedHdop = correlate(hdopField, false);
  const alignedRssi = correlate(rssiField, radioMatchesPdr);
  const alignedSnr = correlate(snrField, radioMatchesPdr);
  const alignedPdr = correlate(pdrField, radioMatchesPdr && hexId(delivery?.collar_session_id) &&
    profile[1] === 'valid' && delivery.profile_id === profile[0]);
  const samples = [radio?.sample_count, !radio ? 'missing' :
    !between(radio.sample_count, 1, 255) || !isRecent(radio.persisted_at) || radio.duration_ms !== 60000 ? 'invalid' :
    radioMatchesPdr ? 'valid' : 'uncorrelated'];
  const flag = field => [field[1] === 'valid' ? '1' : field[1] === 'invalid' ? '0' : null, field[1]];
  return [
    ['current_profile', 'Perfil observado no Gateway', profile],
    ['collar_id', 'Identificador da Base', [id, id == null ? 'missing' : 'valid']],
    ['minute_index', 'Janela de votação do Nios II', [null, 'uncorrelated']],
    ['hdop_x100', 'HDOP da coleira', alignedHdop],
    ['rssi_mag', 'RSSI médio · magnitude em dBm', [
      between(alignedRssi[0], -25500, 0) ? Math.round(Math.abs(alignedRssi[0]) / 100) : null, alignedRssi[1],
    ]],
    ['snr_x100', 'SNR médio do Gateway', alignedSnr],
    ['pdr_x100', 'PDR estimado pelo Gateway', alignedPdr],
    ['sample_count', 'Amostras da janela RADIO', samples],
    ['metrics_valid[3]', 'HDOP', flag(alignedHdop)],
    ['metrics_valid[2]', 'PDR', flag(alignedPdr)],
    ['metrics_valid[1]', 'SNR', flag(alignedSnr)],
    ['metrics_valid[0]', 'RSSI', flag(alignedRssi)],
  ];
}

function renderPreview() {
  const contextAvailable = !overviewFailed && metricsState === 'ready' && !gateway?.mock;
  const fields = previewFields();
  const status = byId('fpga-sufficiency');
  status.textContent = 'Dados insuficientes';
  status.dataset.state = 'Sem correlação';
  byId('fpga-input-detail').textContent = overviewFailed || metricsState === 'error'
    ? 'Consulta incompleta. Os valores anteriores, quando disponíveis, são apenas históricos.'
    : gateway?.mock ? 'A Base está em modo simulado. A prévia não está habilitada para votação.'
    : !selector.value ? 'Selecione uma coleira com telemetria na Base.'
    : 'A Base ainda não identifica o minuto de votação nem correlaciona o relógio do HDOP com a janela do Gateway.';
  byId('fpga-input').replaceChildren(...fields.map(([name, source, [value, state]]) => {
    if (!contextAvailable && state === 'valid' && name !== 'collar_id') {
      state = 'uncorrelated';
      if (name.startsWith('metrics_valid[')) value = null;
    }
    const tr = element('tr', '');
    const badge = element('span', labels[state], 'fpga-badge');
    badge.dataset.state = labels[state];
    const cell = element('td', '');
    cell.append(badge);
    tr.append(element('td', name, 'mono'), element('td', value, 'mono'), element('td', source), cell);
    return tr;
  }));
}

function render() {
  const status = byId('fpga-api-status');
  const failed = overviewFailed || metricsState === 'error';
  status.dataset.warning = String(failed || Boolean(gateway?.mock));
  status.textContent = failed ? 'API indisponível ou incompleta. Últimos dados mantidos para a coleira selecionada.'
    : gateway?.mock ? 'Serial do Gateway em modo simulado. Os cartões consultam somente registros persistidos.'
    : metricsState === 'loading' ? 'Consultando métricas da Base…'
    : lastUpdate ? 'API da Base disponível · horários em Brasília · registro antigo após 3 min da gravação'
    : collars.length ? 'Consultando a Base…' : 'Nenhuma coleira encontrada na Base.';
  renderUnit();
  renderMetrics();
  renderPreview();
}

async function getJson(path) {
  const reply = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(6000)});
  if (!reply.ok) throw new Error(`HTTP ${reply.status}`);
  return reply.json();
}

async function refreshMetrics() {
  const selected = selector.value;
  const id = collarNumber(selected);
  const request = Symbol();
  metricsRequest = request;
  if (id == null) {
    metricsState = 'idle';
    render();
    return;
  }
  const [link, delivery] = await Promise.allSettled([
    getJson(`/api/collars/${id}/link-metrics?limit=1`),
    getJson(`/api/collars/${id}/pdr?limit=1`),
  ]);
  if (metricsRequest !== request || selector.value !== selected) return;
  const linkOk = link.status === 'fulfilled' && link.value?.collar_id === id && link.value.windows;
  const pdrOk = delivery.status === 'fulfilled' && Array.isArray(delivery.value) &&
    delivery.value.every(row => row?.collar_id === id);
  if (linkOk) linkMetrics = link.value;
  if (pdrOk) pdr = delivery.value[0] ?? null;
  metricsState = linkOk && pdrOk ? 'ready' : 'error';
  if (linkOk && pdrOk) lastUpdate = new Date().toISOString();
  render();
}

function resetMetrics() {
  linkMetrics = null;
  pdr = null;
  lastUpdate = null;
  metricsState = selector.value ? 'loading' : 'idle';
  render();
}

async function refreshOverview() {
  if (overviewPending || document.hidden) return;
  overviewPending = true;
  try {
    const [units, gatewayResponse] = await Promise.allSettled([
      getJson('/api/collars'), getJson('/api/gateway'),
    ]);
    const unitsOk = units.status === 'fulfilled' && Array.isArray(units.value);
    const gatewayOk = gatewayResponse.status === 'fulfilled' && gatewayResponse.value &&
      typeof gatewayResponse.value.mock === 'boolean';
    overviewFailed = !unitsOk || !gatewayOk;
    if (unitsOk) {
      const previous = selector.value;
      collars = units.value.filter(unit => collarNumber(unit?.collar_id) != null);
      if (collars.map(unit => unit.collar_id).join() !== Array.from(selector.options, option => option.value).join()) {
        const options = collars.map(unit => new Option(unit.collar_id, unit.collar_id));
        selector.replaceChildren(...(options.length ? options : [new Option('Sem coleiras', '')]));
        if (collars.some(unit => unit.collar_id === previous)) selector.value = previous;
      }
      selector.disabled = !collars.length;
      if (selector.value !== previous) resetMetrics();
    } else if (!collars.length) {
      selector.replaceChildren(new Option('Consulta indisponível', ''));
    }
    if (gatewayOk) gateway = gatewayResponse.value;
    render();
    await refreshMetrics();
  } finally {
    overviewPending = false;
  }
}

selector.addEventListener('change', () => {
  resetMetrics();
  refreshMetrics();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshOverview();
});
refreshOverview();
setInterval(refreshOverview, REFRESH_MS);
