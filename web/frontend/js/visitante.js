import { formatClock } from './ui.js';

const el = id => document.getElementById(id);
const producer = document.body.classList.contains('producer-area');
const management = document.body.classList.contains('management-area');
const fenceLabels = { inside: 'Dentro da cerca', near: 'Próximo da cerca', outside: 'Fora da cerca' };
const linkLabels = { online: 'Online', offline: 'Offline', unknown: 'Sem informação' };
const locationLabels = {
  current: 'Localização não divulgada', no_fix: 'Sem sinal de GPS',
  stale: 'Sem posição recente', unavailable: 'Posição indisponível',
};
const icons = {
  animal: 'M7 9 3 5v6l4 2v3a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3v-3l4-2V5l-4 4H7Zm3 6h4',
  collar: 'M3 7h18v10H3z M8 7v10 M15 12h2',
  online: 'M3 9a14 14 0 0 1 18 0M6 12a9 9 0 0 1 12 0m-9 3a5 5 0 0 1 6 0m-3 3h.01',
  offline: 'm3 3 18 18M9 6a14 14 0 0 1 12 3M3 9l2-1m1 4 2-1m5-1 5 2m-9 3a5 5 0 0 1 6 0m-3 3h.01',
  inside: 'M12 22s7-5 7-12a7 7 0 1 0-14 0c0 7 7 12 7 12Zm-3-12 2 2 4-4',
  near: 'M12 3 2 20h20L12 3Zm0 6v5m0 3h.01',
  outside: 'M12 22s7-5 7-12a7 7 0 1 0-14 0c0 7 7 12 7 12ZM9 7l6 6m0-6-6 6',
};
const counters = [
  ['animal', 'Animais monitorados', ''], ['collar', 'Coleiras cadastradas', ''],
  ['online', 'Coleiras online', 'inside'], ['offline', 'Coleiras offline', 'unknown'],
  ...Object.entries(fenceLabels).map(([key, label]) => [key, label, key]),
];
if (producer) counters.push(['unknown', 'Situação indisponível', 'unknown']);

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function icon(key, tone) {
  const result = node('span', '', 'visitor-icon ' + tone);
  result.setAttribute('aria-hidden', 'true');
  // O SVG usa exclusivamente os caminhos definidos neste módulo.
  result.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ' +
    'stroke-linejoin="round" stroke-linecap="round"><path d="' + icons[key] + '"/></svg>';
  return result;
}

const counterValues = new Map(counters.map(([key, label, tone]) => {
  const group = node('div', '', 'visitor-counter ' + tone);
  const title = node('dt', label);
  const value = node('dd', '—');
  group.append(icon(key === 'unknown' ? 'near' : key, tone), title, value);
  el('visitor-counts').append(group);
  return [key, value];
}));

function renderCounts(animals) {
  const disclosed = animals?.filter(animal => animal.status !== null) ?? [];
  const count = (field, value) => disclosed.length ? disclosed.filter(animal => animal[field] === value).length : '—';
  for (const [key, target] of counterValues) {
    if (key === 'animal' || key === 'collar') target.textContent = producer && animals ? animals.length : animals?.length || '—';
    else if (key === 'unknown') target.textContent = animals ? animals.filter(animal => zoneOf(animal) === 'unknown').length : '—';
    else if (producer && animals && !animals.length) target.textContent = 0;
    else target.textContent = key in fenceLabels ? count('status', key) : count('link', key);
  }
  const note = el('visitor-count-note');
  note.hidden = !animals?.length || disclosed.length === animals.length;
  note.textContent = disclosed.length ? 'Indicadores de situação e comunicação consideram apenas divulgações autorizadas.'
    : 'Os indicadores de situação e comunicação estão indisponíveis para divulgação.';
}

const positionOf = animal => animal.position?.current ? [animal.position.lat, animal.position.lon] : null;
const zoneOf = animal => Object.hasOwn(fenceLabels, animal.status) ? animal.status : 'unknown';
const zoneLabel = animal => fenceLabels[zoneOf(animal)] ?? 'Situação indisponível';
const linkLabel = animal => linkLabels[animal.link] ?? 'Não divulgado';
const clock = value => value ? formatClock(value, { withDate: true }) : 'Indisponível';
const fit = el('visitor-fit');
const map = window.L ? L.map('shared-herd-map').setView([0, 0], 2) : null;
const fenceLayer = map ? L.layerGroup().addTo(map) : null;
const markers = new Map();
let bounds = [];
let fenceSignature;
let framed = false;
let selected;
let tileError = false;

if (map) {
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  if (producer) {
    const setTileError = unavailable => {
      tileError = unavailable;
      el('visitor-map-status').classList.toggle('producer-map-error', unavailable);
      if (mapState) showMapStatus();
    };
    tiles.on('tileerror', () => setTileError(true));
    tiles.on('tileload', () => setTileError(false));
  }
  map.on('movestart zoomstart', () => { framed = true; });
}

function frameMap() {
  if (map && bounds.length) map.fitBounds(bounds, { padding: [34, 34], maxZoom: 17, animate: false });
}
fit.addEventListener('click', frameMap);

function popupContent(animal) {
  const content = node('div', '', 'visitor-popup');
  content.append(node('strong', animal.name), node('p', zoneLabel(animal), 'visitor-tag ' + zoneOf(animal)),
    node('p', 'Comunicação: ' + linkLabel(animal)), node('p', 'Última posição: ' + clock(animal.last_position_at)));
  if (producer) {
    content.append(node('p', 'Último contato: ' + clock(animal.last_contact_at)),
      node('p', animal.fence_used ? 'Versão em uso informada: ' + animal.fence_used : 'Versão em uso indisponível.'));
    content.append(node('p', applicationLabel(animal)));
    for (const alert of animal.alerts) content.append(node('p', alert));
  }
  return content;
}

function applicationLabel(animal) {
  const labels = { pending: 'Aguardando a Base', at_base: 'Download solicitado pela Base',
    transmitting: 'Distribuição em andamento', confirmed: 'Confirmação informada pela Base',
    failed: 'Falha de distribuição' };
  return labels[animal.delivery_status] ?? 'Sem registro de entrega da cerca atual.';
}

function selectAnimal(key) {
  selected = key;
  for (const row of el('visitor-animals').children) {
    row.classList.toggle('producer-selected', row.dataset.animal === selected);
  }
}

function focusAnimal(animal) {
  selectAnimal(animal.key);
  const marker = markers.get(animal.key)?.marker;
  if (marker) {
    el('visitor-map-panel').scrollIntoView({ block: 'start' });
    map.panTo(marker.getLatLng(), { animate: false });
    marker.getElement()?.focus({ preventScroll: true });
    marker.openPopup();
  }
}

function updateMap(animals, fences) {
  if (!map) return;
  const polygons = fences.map(fence => fence.points.map(point => [point.lat, point.lon]));
  const signature = JSON.stringify(polygons);
  if (signature !== fenceSignature) {
    fenceSignature = signature;
    fenceLayer.clearLayers();
    for (const points of polygons) {
      L.polygon(points, { color: '#245c4f', weight: 2, opacity: .9, fillColor: '#35745c', fillOpacity: .12 })
        .bindTooltip(producer ? 'Cerca publicada na Central' : 'Cerca virtual autorizada', { sticky: true }).addTo(fenceLayer);
    }
  }
  bounds = polygons.flat();
  const current = new Set();
  for (const animal of animals) {
    const point = positionOf(animal);
    if (!point) continue;
    bounds.push(point);
    current.add(animal.key);
    const tone = zoneOf(animal);
    const markerIcon = () => L.divIcon({ className: 'visitor-pin ' + tone, iconSize: [14, 14], iconAnchor: [7, 7] });
    let entry = markers.get(animal.key);
    if (!entry) {
      const marker = L.marker(point, { icon: markerIcon(), title: animal.name }).addTo(map);
      if (producer) marker.on('click', () => {
        selectAnimal(animal.key);
        if (management) window.dispatchEvent(new CustomEvent('gestao:coleira', { detail: animal.collar_id }));
      });
      entry = { marker, tone };
      markers.set(animal.key, entry);
    } else {
      if (!entry.marker.getLatLng().equals(point, 0)) entry.marker.setLatLng(point);
      if (entry.tone !== tone) entry.marker.setIcon(markerIcon());
    }
    entry.marker.getElement()?.setAttribute('aria-label', animal.name);
    entry.tone = tone;
    const popup = JSON.stringify([animal.name, tone, animal.link, animal.last_position_at,
      animal.last_contact_at, animal.fence_used, animal.delivery_status, producer ? animal.alerts : null]);
    if (entry.popup !== popup) {
      const content = popupContent(animal);
      const existing = entry.marker.getPopup();
      if (existing) {
        // Atualizar um popup aberto não deve deslocar a câmera.
        existing.options.autoPan = false;
        existing.setContent(content);
        existing.options.autoPan = true;
      } else entry.marker.bindPopup(content);
      entry.popup = popup;
    }
  }
  for (const [key, entry] of markers) {
    if (!current.has(key)) {
      entry.marker.remove();
      markers.delete(key);
    }
  }
  fit.disabled = !bounds.length;
  if (!framed && bounds.length) {
    framed = true;
    frameMap();
  }
}

function field(label, value, className) {
  const group = node('div');
  group.append(node('span', label, 'visitor-field'), node('span', value, className));
  return group;
}

function animalRow(animal) {
  const row = node('li', '', 'visitor-animal');
  if (producer) {
    row.dataset.animal = animal.key;
    if (management) row.dataset.collar = animal.collar_id;
    row.classList.toggle('producer-selected', animal.key === selected);
  }
  const identity = node('div');
  identity.append(node('strong', animal.name));
  const point = positionOf(animal);
  if (map && point) {
    const button = node('button', 'Ver no mapa', 'button visitor-secondary visitor-locate');
    button.type = 'button';
    button.setAttribute('aria-label', 'Ver ' + animal.name + ' no mapa');
    button.addEventListener('click', () => {
      const marker = markers.get(animal.key)?.marker;
      if (!marker) return;
      if (management) window.dispatchEvent(new CustomEvent('gestao:coleira', { detail: animal.collar_id }));
      if (producer) focusAnimal(animal);
      else {
        el('visitor-map-panel').scrollIntoView({ block: 'start' });
        map.panTo(marker.getLatLng(), { animate: false });
        marker.getElement()?.focus({ preventScroll: true });
        marker.openPopup();
      }
    });
    identity.append(button);
  }
  row.append(identity, field('Situação', zoneLabel(animal), 'visitor-tag ' + zoneOf(animal)),
    field('Comunicação', linkLabel(animal), 'visitor-tag ' + (animal.link === 'online' ? 'inside' : 'unknown')),
    field('Localização', point ? 'Posição atual' : locationLabels[animal.location_state] ?? 'Indisponível'),
    field('Última posição', clock(animal.last_position_at)));
  if (producer) row.lastElementChild.append(field('Último contato', clock(animal.last_contact_at)));
  if (management) {
    const link = node('a', 'Ver histórico de deslocamento', 'button visitor-secondary visitor-locate');
    link.href = '/gestao/historico?' + new URLSearchParams({ coleira: animal.collar_id });
    identity.append(link);
  }
  return row;
}

function alertRows(animals) {
  return animals.flatMap(animal => animal.alerts.map(message => {
    const row = node('li', '', 'visitor-alert');
    const tone = message === 'Saiu da cerca' ? 'outside' : 'near';
    const text = node('div');
    text.append(node('strong', animal.name), node('p', message));
    row.append(icon(tone, tone), text);
    return row;
  }));
}

function fillList(id, rows, empty) {
  const list = el(id);
  const focused = list.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
  list.replaceChildren(...(rows.length ? rows : [node('li', empty, 'visitor-empty')]));
  if (focused) {
    [...list.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === focused)
      ?.focus({ preventScroll: true });
  }
}

let listSignature;
let mapState;
function showMapStatus() {
  const { data, fences } = mapState;
  if (producer) {
    el('visitor-map-status').textContent = !map ? 'Leaflet indisponível.'
      : tileError ? 'Camada cartográfica temporariamente indisponível.'
      : !data ? 'Aguardando conexão com a Central.'
      : [fences.length ? 'Cerca publicada na Central.' : 'Nenhuma cerca publicada.',
        markers.size ? 'Posições atuais.' : 'Sem posições atuais para exibir.'].join(' ');
    return;
  }
  el('visitor-map-status').textContent = !map ? 'Mapa indisponível.' : !data ? 'Aguardando conexão com a Central.'
    : fences.length ? (markers.size ? 'Cerca virtual autorizada e posições atuais.' : 'Cerca virtual autorizada. Sem posições atuais para exibir.')
      : markers.size ? 'Posições atuais. Nenhuma cerca autorizada para exibição.' : 'Sem cerca ou posições públicas disponíveis.';
}

function render(data) {
  const animals = data?.animals ?? [];
  const fences = data?.fences ?? [];
  if (management) window.dispatchEvent(new CustomEvent('gestao:rebanho', { detail: data }));
  renderCounts(data?.animals);
  updateMap(animals, fences);
  el('visitor-update').textContent = !data ? 'Dados temporariamente indisponíveis.'
    : data.updated_at ? 'Última atualização: ' + clock(data.updated_at)
      : 'Horário da última atualização indisponível.';
  if (producer && data && !data.base_online) el('visitor-update').append(' · Base sem comunicação');
  mapState = { data, fences };
  showMapStatus();
  const signature = JSON.stringify(data?.animals);
  if (signature !== listSignature || !data) {
    listSignature = signature;
    fillList('visitor-animals', animals.map(animalRow),
      data ? (producer ? 'Nenhuma coleira cadastrada.' : 'Nenhum animal autorizado para exibição.') : 'Lista de animais indisponível.');
    fillList('visitor-alerts', alertRows(animals),
      data ? (producer ? 'Nenhum aviso ativo.' : 'Nenhum aviso ativo autorizado para exibição.') : 'Avisos temporariamente indisponíveis.');
  }
}

async function refresh() {
  try {
    const response = await fetch('/api/public/rebanho', {
      cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(6000),
    });
    if (!response.ok) throw new Error('A Central não respondeu.');
    render(await response.json());
  } catch {
    render(null);
  } finally {
    window.setTimeout(refresh, 15000);
  }
}

if (producer) import('./producer-herd.js').then(({ startProducerHerd }) => startProducerHerd(render, map && ((lat, lon) => {
  if (framed) return;
  map.setView([lat, lon], 14, { animate: false });
  framed = false;
})));
else refresh();
