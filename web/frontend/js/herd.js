import { consultar, explicarErro } from './api.js';
import { formatClock } from './ui.js';
import { conectarTempoReal } from './realtime.js';
import { herdMap } from './herd-map.js';

const el = id => document.getElementById(id);
const area = document.querySelector('[data-area]').dataset.area;
const visitor = area === 'visitor';
const notices = location.pathname === '/produtor/avisos';
const endpoint = visitor ? '/api/public/rebanho' : '/api/producer/rebanho';
const labels = {
  inside: 'Dentro da cerca', near: 'Perto da cerca', outside: 'Fora da cerca',
  offline: 'Sem comunicação', no_position: 'Sem localização atual', unknown: 'Sem informação suficiente',
};
const clock = value => value ? formatClock(value, { withDate: true }) : 'Indisponível';
const age = seconds => Number.isFinite(seconds)
  ? ' · há ' + (seconds < 60 ? seconds + ' s' : Math.floor(seconds / 60) + ' min')
  : '';
let snapshot = null;
let selected = null;
let pending = false;
let stale = false;
const maps = herdMap('shared-herd-map', key => select(key));

function element(tag, text, css) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (css) node.className = css;
  return node;
}

function select(key) {
  selected = key;
  detail();
  maps.focus(key);
}

function detail() {
  const animal = snapshot?.animals.find(row => row.key === selected);
  const view = el('herd-animal-detail');
  if (!animal) {
    view.replaceChildren();
    return;
  }
  const rows = [element('h3', animal.name), element('p', stale ? 'Dados sem atualização' : labels[animal.status])];
  if (!visitor) {
    rows.push(element('p', 'Último contato: ' + clock(animal.last_contact_at) + age(animal.contact_age_s)));
    rows.push(element('p', (!stale && animal.position?.current ? 'Posição atual: ' : 'Última posição conhecida: ') +
      clock(animal.last_position_at) + age(animal.position_age_s)));
    rows.push(element('p', animal.fence_used ? 'Cerca informada pela coleira: versão ' + animal.fence_used
      : 'Cerca usada pela coleira ainda não informada.'));
  } else if (animal.last_position_at) rows.push(element('p', 'Última posição: ' + clock(animal.last_position_at)));
  for (const note of animal.alerts) rows.push(element('p', note));
  view.replaceChildren(...rows);
}

function updateList(id, rows, empty) {
  const list = el(id);
  const retained = new Map(Array.from(list.children, row => [row.dataset.key, row]));
  if (!rows.length) rows = [{ id: 'empty', text: empty }];
  rows.forEach((row, index) => {
    let item = retained.get(row.id);
    if (!item) {
      item = element('li');
      item.dataset.key = row.id;
      if (row.animal) {
        const button = element('button', row.text, 'button');
        button.type = 'button';
        button.addEventListener('click', () => select(row.animal));
        item.append(button);
      } else item.textContent = row.text;
    }
    const target = item.firstChild?.nodeType === Node.ELEMENT_NODE ? item.firstChild : item;
    if (target.textContent !== row.text) target.textContent = row.text;
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
    retained.delete(row.id);
  });
  for (const item of retained.values()) item.remove();
}

function render(data) {
  stale = false;
  snapshot = data;
  const counts = [['Animais', data.animals.length]];
  if (!visitor || data.animals.some(animal => animal.status !== null)) {
    for (const [id, label] of Object.entries(labels)) counts.push([label, data.animals.filter(a => a.status === id).length]);
  }
  const cards = el('herd-counts');
  if (cards.children.length !== counts.length) cards.replaceChildren(...counts.map(([label]) => {
    const card = element('article', null, 'card');
    card.append(element('h2', label), element('strong', '', 'herd-number'));
    return card;
  }));
  counts.forEach(([, count], index) => { cards.children[index].lastChild.textContent = count; });
  const enabled = data.animals.length || data.fences.length;
  el('herd-map-panel').hidden = visitor && !data.fences.length && !data.animals.some(a => a.position);
  el('herd-status').textContent = visitor && !enabled ? 'Ainda não há informações autorizadas para divulgação.'
    : data.updated_at ? 'Última atualização: ' + clock(data.updated_at)
      : visitor ? 'Horário de atualização indisponível.' : 'Aguardando dados do rebanho.';
  const fence = data.fences[0];
  if (!visitor) {
    const deliveries = data.deliveries;
    const confirmed = deliveries.filter(row => row.status === 'confirmed').length;
    el('herd-fence-state').textContent = fence ? fence.name + ' · Publicada · ' + confirmed + '/' + deliveries.length +
      ' confirmações informadas pela Base' : 'Nenhuma cerca publicada.';
    const edit = el('edit-current-fence');
    edit.hidden = !fence;
    if (fence) edit.href = (area === 'producer' ? '/produtor/editor' : '/gestao/editor') + '?edit=' + fence.version;
  }
  maps.update(data.animals, data.fences);
  updateList('herd-animals', data.animals.map(animal => ({
    id: animal.key, animal: animal.key, text: animal.name + (animal.status ? ' · ' + labels[animal.status] : ''),
  })), 'Nenhum animal disponível.');
  const alerts = data.animals.flatMap(animal => animal.alerts.map(note => ({
    id: animal.key + ':' + note, animal: animal.key, text: animal.name + ' · ' + note,
  })));
  updateList('herd-alerts', alerts, 'Nenhum aviso ativo.');
  if (!visitor) updateList('herd-history', data.history.map(event => ({
    id: event.animal + ':' + event.message + ':' + event.at, animal: event.animal,
    text: [event.name, event.resolved ? 'Resolvido: ' + event.message : event.message, clock(event.at)].join(' · '),
  })), 'Nenhum acontecimento registrado.');
  detail();
}

async function refresh() {
  if (pending) return;
  pending = true;
  try {
    render(await consultar(endpoint));
  } catch (error) {
    stale = true;
    maps.update([], snapshot?.fences || []);
    el('herd-status').textContent = explicarErro(error) + (snapshot ? ' · Resumo sem atualização.' : '');
    detail();
  } finally {
    pending = false;
  }
}

if (visitor) {
  document.title = 'Visitante — VFence';
  el('herd-title').textContent = 'Rebanho público';
  el('herd-history-panel').hidden = true;
  window.setInterval(refresh, 15000);
} else {
  const title = notices ? 'Avisos' : area === 'producer' ? 'Meu Rebanho' : 'Rebanho';
  document.title = title + ' — VFence';
  el('herd-title').textContent = title;
  if (notices) {
    el('herd-title').textContent = 'Avisos';
    el('herd-notices').before(el('herd-history-panel'));
  }
  conectarTempoReal({ path: '/ws/producer', onPollingTick: refresh, onInvalidate: refresh });
}
refresh();
