import { consultar, lerColeiras, lerConfiguracao, explicarErro } from './api.js';
import { conectarTempoReal } from './realtime.js';
import { collarName, eventLabels, node, propertyClock, zoneLabels } from './management-ui.js';
import { localDay, nextDay, zonedTime } from './movement.js';

const el = id => document.getElementById('management-events-' + id);
let clock;
let zone;
let collars;
let active;
let configuration;
let pages = [];
let page = 0;
const limit = 50;
const previous = node('button', 'Voltar aos eventos mais recentes', 'button visitor-secondary');
previous.type = 'button';
previous.hidden = true;
el('more').before(previous);

function eventCard(event) {
  const entry = node('li', null, 'management-event');
  const details = node('details');
  const who = collars.get(event.collar_id);
  details.append(node('summary', [clock(event.ts), eventLabels[event.kind] || event.kind,
    who ? collarName(who) : event.collar_id].filter(Boolean).join(' · ')));
  details.append(node('p', 'Situação registrada: ' + (zoneLabels[event.zone] || 'Indisponível')));
  if (event.detail) details.append(node('p', event.detail));
  if (event.collar_id && Number.isFinite(Date.parse(event.ts))) {
    const link = node('a', 'Ver deslocamento', 'button visitor-secondary');
    link.href = '/gestao/historico?' + new URLSearchParams({ coleira: event.collar_id, evento: event.ts });
    details.append(link);
  }
  entry.append(details);
  return entry;
}

async function search(advance = false) {
  active?.abort();
  active = new AbortController();
  const signal = active.signal;
  const start = el('start').value;
  const end = el('end').value;
  if (!advance) {
    page = 0;
    pages = [];
    configuration = null;
    el('list').replaceChildren();
    el('more').hidden = previous.hidden = true;
  }
  try {
    const today = localDay(Date.now(), zone);
    if ((start && end && start > end) || start > today || end > today) throw new Error('Escolha um intervalo válido até hoje.');
    const params = new URLSearchParams({ limit: String(limit + 1) });
    if (el('collar').value) params.set('collar_id', el('collar').value);
    if (el('kind').value) params.set('kind', el('kind').value);
    if (start) params.set('start', zonedTime(start, '00:00', zone));
    if (end) params.set('end', zonedTime(nextDay(end), '00:00', zone));
    configuration ??= params.toString();
    const query = new URLSearchParams(configuration);
    if (advance) query.set('before_id', pages[page].rows.at(-1).id);
    el('status').textContent = 'Consultando eventos…';
    const response = await consultar('/api/events?' + query, { signal });
    if (signal.aborted) return;
    if (advance) page++;
    pages[page] = { rows: response.slice(0, limit), more: response.length > limit };
    renderPage();
  } catch (error) {
    if (!signal.aborted) el('status').textContent = error.message || explicarErro(error);
  }
}

function renderPage() {
  const { rows, more } = pages[page];
  el('list').replaceChildren(...(rows.length ? rows.map(eventCard) : [node('li', 'Nenhum evento para esses filtros.', 'visitor-empty')]));
  el('status').textContent = rows.length ? `Página ${page + 1} · ${rows.length} eventos exibidos` : '';
  el('more').hidden = !more;
  previous.hidden = page === 0;
}

async function refreshAlerts() {
  try {
    const data = await consultar('/api/gestao/rebanho');
    const alerts = data.animals.filter(animal => !el('collar').value || animal.collar_id === el('collar').value)
      .flatMap(animal => animal.alerts.map(alert => node('li', animal.name + ' · ' + alert, 'visitor-alert')));
    el('active').replaceChildren(...(alerts.length ? alerts : [node('li', 'Nenhum aviso ativo.', 'visitor-empty')]));
  } catch {
    el('active').replaceChildren(node('li', 'Avisos temporariamente indisponíveis.', 'visitor-empty'));
  }
}

try {
  const [config, rows, kinds] = await Promise.all([lerConfiguracao(), lerColeiras(), consultar('/api/events/kinds')]);
  zone = config.property_timezone;
  clock = propertyClock(zone, true);
  collars = new Map(rows.map(collar => [collar.collar_id, collar]));
  el('timezone').textContent = 'Horários da propriedade · ' + zone;
  el('collar').append(...rows.map(row => new Option(collarName(row), row.collar_id)));
  el('kind').append(...kinds.map(kind => new Option(eventLabels[kind] || kind, kind)));
  for (const field of ['start', 'end']) el(field).max = localDay(Date.now(), zone);
  el('form').querySelector('button').disabled = false;
  el('form').addEventListener('submit', event => { event.preventDefault(); search(); refreshAlerts(); });
  el('more').addEventListener('click', () => {
    if (pages[page + 1]) { page++; renderPage(); }
    else search(true);
  });
  previous.addEventListener('click', () => { page--; renderPage(); });
  search();
  conectarTempoReal({ path: '/ws/producer', onPollingTick: refreshAlerts });
} catch (error) {
  el('status').textContent = explicarErro(error);
}
