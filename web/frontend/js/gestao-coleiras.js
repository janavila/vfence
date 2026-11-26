import { consultar, lerColeiras, explicarErro } from './api.js';
import { formatClock } from './ui.js';
import { collarName, facts, statusLabels, zoneLabels } from './management-ui.js';

const el = id => document.getElementById('management-collar' + id);
const select = el('');
let collars = [];
let individual;
let snapshot;
let selected;
let pending = false;
let detailRequest;
const clock = value => formatClock(value, { withDate: true });

function show() {
  const row = collars.find(collar => collar.collar_id === selected);
  const live = snapshot?.animals.find(animal => animal.collar_id === selected);
  el('-name').hidden = el('-history').hidden = !row;
  if (!row) {
    el('-details').replaceChildren();
    return;
  }
  el('-history').href = '/gestao/historico?' + new URLSearchParams({ coleira: selected });
  const details = individual?.collar_id === selected ? individual : null;
  const current = live?.position?.current;
  const position = row.last_lat != null && row.last_lon != null ? `${row.last_lat}, ${row.last_lon}` : null;
  facts(el('-details'), [
    ['Identificador', row.collar_id], ['Nome amigável', row.animal_label], ['Comunicação', statusLabels[live?.link ?? row.link_status]],
    ['Último contato', clock(live?.last_contact_at ?? row.last_contact_at)], ['Última posição válida', clock(row.last_position_at)],
    ['Coordenadas atuais', current ? `${live.position.lat}, ${live.position.lon}` : null],
    ['Últimas coordenadas conhecidas', position], ['GPS', ({ valid: 'Válido', invalid: 'Inválido' })[row.gnss_status]],
    ['Zona da última leitura', zoneLabels[row.last_zone]], ['Bateria', row.battery_pct == null ? null : row.battery_pct + '%'],
    ['RSSI da última leitura', row.rssi == null ? null : row.rssi + ' dBm'],
    ['SNR da última leitura', row.snr == null ? null : row.snr + ' dB'],
    ['HDOP da última amostra recebida', details?.hdop], ['Horário dessa amostra', clock(details?.ts)],
    ['Versão reportada', row.fence_version_reported || null],
    ['Confirmação informada pela Base', row.confirmed_fence_version ? 'Versão ' + row.confirmed_fence_version + ' · origem física não verificada' : null],
    ['Avisos ativos', live?.alerts.length ? live.alerts.join(' · ') : live ? 'Nenhum' : null],
  ]);
}

async function readSample() {
  detailRequest?.abort();
  if (!selected) return;
  const request = new AbortController();
  detailRequest = request;
  try {
    const [row] = await consultar('/api/collars/' + encodeURIComponent(selected) + '/telemetry?limit=1', { signal: request.signal });
    if (!request.signal.aborted) {
      individual = row;
      show();
    }
  } catch {
    if (!request.signal.aborted) el('-status').textContent = 'Última amostra indisponível.';
  }
}

function choose(id) {
  selected = id;
  select.value = id;
  individual = null;
  el('-status').textContent = '';
  el('-label').value = collars.find(row => row.collar_id === id)?.animal_label || '';
  show();
  readSample();
}

async function refresh() {
  if (pending) return;
  pending = true;
  try {
    collars = await lerColeiras();
    select.replaceChildren(new Option(collars.length ? 'Selecione' : 'Nenhuma coleira cadastrada', ''),
      ...collars.map(collar => new Option(collarName(collar), collar.collar_id)));
    select.value = selected ?? '';
    show();
  } catch (error) {
    el('-status').textContent = explicarErro(error);
  } finally {
    pending = false;
  }
}

select.addEventListener('change', () => choose(select.value));
window.addEventListener('gestao:coleira', event => choose(event.detail));
window.addEventListener('gestao:rebanho', event => {
  const updated = event.detail?.animals.find(animal => animal.collar_id === selected);
  const previous = snapshot?.animals.find(animal => animal.collar_id === selected);
  snapshot = event.detail;
  show();
  if (updated?.last_contact_at !== previous?.last_contact_at) {
    refresh();
    readSample();
  }
});
el('-name').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    await consultar('/api/collars/' + encodeURIComponent(selected), {
      metodo: 'PATCH', corpo: { animal_label: el('-label').value.trim() || null },
    });
    el('-status').textContent = 'Nome atualizado.';
    await refresh();
  } catch (error) {
    el('-status').textContent = explicarErro(error);
  } finally {
    button.disabled = false;
  }
});
refresh();
