import { consultar, lerColeiras, lerConfiguracao, explicarErro } from './api.js';
import { criarMapa } from './map.js';
import { collarName, fillCards, node, propertyClock, zoneLabels } from './management-ui.js';
import { localDay, periodBounds, registeredMovement } from './movement.js';

const el = id => document.getElementById('movement-' + id);
const args = new URLSearchParams(location.search);
const MAX_RECORDS = 50000;
const tablePageSize = 100;
const interruptionKinds = ['gps_lost', 'collar_offline', 'offline'];
const geometryCache = new Map();
let activeQuery;
let result;
let collars;
let zone;
let clock;
let map;
let paths;
let overlays;
let pointLayer;
let renderer;
let visibleBounds;
let pageStart = 0;
let geometry = new Map();
let providerWarning = false;
let chosenName;
let eventTime;
let highlightedId;

async function allPages(path, bounds, signal, extra = []) {
  const query = new URLSearchParams({ ...bounds, limit: '1000' });
  for (const [key, value] of extra) query.append(key, value);
  const rows = [];
  while (true) {
    const page = await consultar(path + '?' + query, { signal });
    rows.push(...page);
    if (rows.length > MAX_RECORDS) throw new Error('Há mais de 50 mil registros neste período. Reduza o intervalo para consultar todos os dados.');
    if (page.length < 1000) return rows;
    query.set('before_id', page.at(-1).id);
  }
}

async function loadGeometry(versions, signal) {
  if (!versions.length) return new Map();
  const available = new Set((await consultar('/api/fences', { signal })).map(fence => fence.version));
  const values = new Map();
  const candidates = versions.filter(version => available.has(version));
  for (let i = 0; i < candidates.length; i += 4) {
    await Promise.all(candidates.slice(i, i + 4).map(async version => {
      const fence = geometryCache.get(version) ?? await consultar('/api/fences/' + version, { signal });
      geometryCache.set(version, fence);
      values.set(version, fence);
    }));
  }
  return values;
}

const point = row => [row.lat, row.lon];
const versionsOf = track => [...new Set(track.valid.map(row => row.fence_version).filter(version => version > 0))].sort((a, b) => a - b);
const hasZone = row => row.valid && geometry.has(row.fence_version) && Object.hasOwn(zoneLabels, row.zone);
const zoneOf = row => hasZone(row) ? zoneLabels[row.zone] : 'Indisponível';
const eventMatches = row => eventTime != null && row.valid && row.time === eventTime;

function popup(row, title) {
  const entry = node('div');
  entry.append(node('strong', title), node('p', chosenName + ' · ' + clock(row.ts)),
    node('p', zoneOf(row)), node('p', row.fence_version > 0 ? 'Versão reportada ' + row.fence_version : 'Versão não informada'));
  return entry;
}

function marker(row, label, type) {
  return L.marker(point(row), { title: label, icon: L.divIcon({
    className: 'movement-pin ' + type, iconSize: [30, 30], iconAnchor: [15, 15],
    html: type === 'end' ? 'F' : type === 'event' ? '•' : 'I',
  }) }).bindPopup(popup(row, label));
}

function mapStatus() {
  if (!map) return;
  const issues = [];
  if (providerWarning) issues.push('A camada cartográfica está temporariamente indisponível.');
  if (result) {
    if (!result.valid.length) issues.push('Nenhuma posição GPS válida neste período.');
    else {
      if (result.valid.length === 1) issues.push('Uma posição válida. Não há registros suficientes para traçar um deslocamento.');
      else if (result.distance === null) issues.push('As posições estão isoladas por lacunas ou saltos. Não há trechos contínuos.');
      if (result.valid.some(row => !geometry.has(row.fence_version))) issues.push('Parte das posições não tem uma cerca histórica identificada ou disponível.');
      if (result.gaps) issues.push(result.gaps + (result.gaps === 1 ? ' interrupção identificada.' : ' interrupções identificadas.'));
      if (el('points').checked && result.valid.length > 2500) issues.push('O mapa mostra uma amostra de até 2.500 pontos intermediários. A tabela mantém todos os registros.');
      if (eventTime != null && highlightedId == null) issues.push('Nenhuma posição válida pôde ser associada de forma inequívoca ao horário do evento.');
    }
  }
  el('map-status').textContent = issues.join(' ');
}

function drawFences() {
  if (!overlays) return;
  overlays.clearLayers();
  visibleBounds = result?.valid.map(point) ?? [];
  if (!result || !el('fences').checked) return;
  const selected = el('version').value;
  for (const [version, fence] of geometry) {
    if (selected && Number(selected) !== version) continue;
    const points = fence.points.map(point);
    const label = node('span', 'Cerca histórica · versão ' + version + ' · ' + fence.name);
    L.polygon(points, { color: '#245c4f', weight: 2, fillColor: '#35745c', fillOpacity: .12,
      dashArray: version % 2 === 0 ? '7 5' : null }).bindTooltip(label, { sticky: true }).addTo(overlays);
    visibleBounds.push(...points);
  }
}

function drawPoints() {
  if (!pointLayer) return;
  pointLayer.clearLayers();
  if (!result?.valid.length || !el('points').checked) return;
  const step = Math.max(1, Math.ceil(result.valid.length / 2500));
  for (let i = step; i < result.valid.length - 1; i += step) {
    const row = result.valid[i];
    L.circleMarker(point(row), { renderer, radius: 4, color: '#fff', weight: 1,
      fillColor: hasZone(row) && row.zone === 'FORA' ? '#a34f4f' : '#245fc3', fillOpacity: 1 })
      .addTo(pointLayer).bindPopup(popup(row, 'Posição registrada'));
  }
}

function frame() {
  if (map && visibleBounds?.length) map.fitBounds(visibleBounds, { maxZoom: 17, padding: [28, 28], animate: false });
}

function drawTrack() {
  if (!map) return;
  paths.clearLayers();
  for (const segment of result?.segments ?? []) {
    if (segment.length > 1) L.polyline(segment.map(point), { color: '#245fc3', weight: 4, renderer }).addTo(paths);
  }
  const first = result?.valid[0];
  const last = result?.valid.at(-1);
  if (first) {
    marker(first, first === last ? 'Única posição válida' : 'Início do percurso registrado', 'start').addTo(paths);
    if (first !== last) marker(last, 'Fim do percurso registrado', 'end').addTo(paths);
    const highlighted = result.valid.find(row => row.id === highlightedId);
    if (highlighted) marker(highlighted, 'Posição registrada no horário do evento', 'event').addTo(paths);
  }
  drawFences();
  drawPoints();
  el('fit').disabled = !visibleBounds.length;
  frame();
  mapStatus();
}

function distanceLabel(distance) {
  if (distance == null) return '—';
  if (distance === 0) return '0 m';
  if (distance < 10) return '< 10 m';
  if (distance < 1000) return Math.round(distance / 10) * 10 + ' m';
  return (distance / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' km';
}

function showCounts() {
  const classified = result?.valid.filter(hasZone);
  fillCards(el('counts'), [
    ['Distância estimada', distanceLabel(result?.distance)], ['Posições válidas', result?.valid.length],
    ['Posições fora da cerca', classified?.length ? classified.filter(row => row.zone === 'FORA').length : null],
    ['Primeiro registro GPS', clock?.(result?.valid[0]?.ts)], ['Último registro GPS', clock?.(result?.valid.at(-1)?.ts)],
  ]);
}

function showRecords() {
  const records = result?.records ?? [];
  const rows = records.slice(pageStart, pageStart + tablePageSize).map(row => {
    const tr = node('tr');
    if (row.id === highlightedId) tr.className = 'movement-register-highlight';
    const last = node('td', '—');
    if (map && row.valid) {
      const link = node('button', 'Localizar', 'button visitor-secondary');
      link.type = 'button';
      link.addEventListener('click', () => {
        el('map').scrollIntoView({ block: 'center' });
        map.panTo(point(row), { animate: false });
        L.popup().setLatLng(point(row)).setContent(popup(row, 'Posição registrada')).openOn(map);
      });
      last.replaceChildren(link);
    }
    tr.append(node('td', clock(row.ts)), node('td', zoneOf(row)), node('td', row.valid ? 'Válida' : 'GPS inválido ou posição ausente'),
      node('td', row.fence_version > 0 ? (geometry.has(row.fence_version) ? String(row.fence_version) : row.fence_version + ' · geometria indisponível') : '—'), last);
    return tr;
  });
  if (!rows.length) {
    const empty = node('tr');
    const cell = node('td', 'Nenhum registro para exibir.', 'visitor-empty');
    cell.colSpan = 5;
    empty.append(cell);
    rows.push(empty);
  }
  el('records').replaceChildren(...rows);
  el('previous').hidden = pageStart === 0 || !records.length;
  el('more').hidden = records.length <= pageStart + tablePageSize;
  el('page').textContent = records.length ? `${pageStart + 1}–${Math.min(records.length, pageStart + tablePageSize)} de ${records.length}` : '';
}

async function search() {
  activeQuery?.abort();
  const controller = new AbortController();
  activeQuery = controller;
  const signal = controller.signal;
  const id = el('collar').value;
  const day = el('date').value;
  const initialEvent = Date.parse(args.get('evento'));
  eventTime = args.get('coleira') === id && Number.isFinite(initialEvent) && localDay(initialEvent, zone) === day ? initialEvent : null;
  map?.closePopup();
  result = null;
  pageStart = 0;
  highlightedId = null;
  geometry = new Map();
  el('version').replaceChildren(new Option('Todas as reportadas', ''));
  drawTrack();
  showCounts();
  showRecords();
  if (!id) {
    el('status').textContent = 'Selecione uma coleira e uma data.';
    return;
  }
  try {
    const bounds = periodBounds(day, el('period').value, el('start').value, el('end').value, zone);
    el('status').textContent = 'Consultando os registros do período…';
    const [samples, events] = await Promise.all([
      allPages('/api/collars/' + encodeURIComponent(id) + '/telemetry', bounds, signal),
      allPages('/api/events', bounds, signal, [['collar_id', id], ...interruptionKinds.map(kind => ['kind', kind])]),
    ]);
    const track = registeredMovement(samples, events);
    const versions = versionsOf(track);
    let geometryFailed = false;
    const fences = await loadGeometry(versions, signal).catch(() => {
      geometryFailed = true;
      return new Map();
    });
    if (signal.aborted) return;
    geometry = fences;
    result = track;
    const highlighted = track.valid.filter(eventMatches);
    if (highlighted.length === 1) highlightedId = highlighted[0].id;
    chosenName = collarName(collars.find(collar => collar.collar_id === id));
    el('version').append(...versions.map(version => new Option(
      'Versão ' + version + (geometry.has(version) ? '' : ' · geometria indisponível'), version)));
    el('status').textContent = !track.records.length ? 'Nenhum registro para esta coleira no período.'
      : track.records.length + ' registros no período.' + (geometryFailed ? ' A consulta das cercas históricas está indisponível.' : '');
    drawTrack();
    showCounts();
    showRecords();
  } catch (error) {
    if (!signal.aborted) el('status').textContent = error.message || explicarErro(error);
  }
}

el('fit').addEventListener('click', frame);
el('more').addEventListener('click', () => { pageStart += tablePageSize; showRecords(); });
el('previous').addEventListener('click', () => { pageStart -= tablePageSize; showRecords(); });
el('points').addEventListener('change', () => { drawPoints(); mapStatus(); });
for (const id of ['fences', 'version']) el(id).addEventListener('change', () => {
  drawFences();
  el('fit').disabled = !visibleBounds?.length;
});
function toggleTimes() {
  for (const label of el('form').querySelectorAll('[data-custom-time]')) label.hidden = el('period').value !== 'custom';
}
showCounts();
showRecords();
try {
  const [config, rows] = await Promise.all([lerConfiguracao(), lerColeiras()]);
  zone = config.property_timezone;
  clock = propertyClock(zone);
  collars = rows;
  const today = localDay(Date.now(), zone);
  el('timezone').textContent = 'Horários da propriedade · ' + zone;
  el('date').max = today;
  const eventDate = Date.parse(args.get('evento'));
  el('date').value = args.get('data') || (Number.isFinite(eventDate) ? localDay(eventDate, zone) : today);
  map = criarMapa('movement-map', config, 14, available => { providerWarning = !available; mapStatus(); });
  if (map) {
    paths = L.layerGroup().addTo(map);
    overlays = L.layerGroup().addTo(map);
    pointLayer = L.layerGroup().addTo(map);
    renderer = L.canvas({ padding: .4 });
  }
  el('collar').append(...rows.map(collar => new Option(collarName(collar), collar.collar_id)));
  el('collar').value = rows.some(row => row.collar_id === args.get('coleira')) ? args.get('coleira') : '';
  el('form').querySelector('[type=submit]').disabled = !rows.length;
  el('form').addEventListener('submit', event => { event.preventDefault(); search(); });
  for (const id of ['collar', 'date', 'period', 'start', 'end']) el(id).addEventListener('change', () => { toggleTimes(); search(); });
  if (el('collar').value) search();
  else if (!rows.length) el('status').textContent = 'Nenhuma coleira cadastrada.';
} catch (error) {
  el('status').textContent = 'Não foi possível carregar as opções do histórico. ' + explicarErro(error);
}
