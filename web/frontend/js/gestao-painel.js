import { consultar } from './api.js';
import { conectarTempoReal } from './realtime.js';
import { formatClock } from './ui.js';
import { facts, fillCards, node, eventLabels, statusLabels } from './management-ui.js';
import { renderDeliverySummary } from './producer-herd.js';

const el = id => document.getElementById('technical-' + id);
const clock = value => formatClock(value, { withDate: true });
let pending = false;
function list(id, rows, empty) {
  el(id).replaceChildren(...(rows.length ? rows : [node('li', empty, 'visitor-empty')]));
}

async function refresh() {
  if (pending) return;
  pending = true;
  try {
    const [health, bases, herd, events] = (await Promise.allSettled([
      consultar('/api/health'), consultar('/api/bases'), consultar('/api/gestao/rebanho'), consultar('/api/events?limit=12'),
    ])).map(reply => reply.status === 'fulfilled' ? reply.value : null);
    const base = bases?.[0];
    const fence = herd?.fences[0];
    const link = status => herd?.animals.filter(row => row.link === status).length;
    fillCards(el('counts'), [
      ['Central Web', health ? 'Acessível' : 'Indisponível'], ['Base Windows', base ? (base.online ? 'Online' : 'Offline') : 'Sem informação'],
      ['Coleiras cadastradas', herd?.animals.length], ['Coleiras online', link('online')], ['Coleiras offline', link('offline')],
      ['Cerca publicada', fence ? 'v' + fence.version : herd ? 'Nenhuma' : null],
      ['Entregas pendentes', herd?.deliveries.filter(row => !['confirmed', 'failed'].includes(row.status)).length],
      ['Eventos recentes exibidos', events?.length],
    ]);
    facts(el('comms'), [
      ['Versão da Central', health?.version], ['Hora da Central', clock(health?.time)], ['Último heartbeat da Base', clock(base?.last_heartbeat)],
      ['Comunicação Base–Central', base ? (base.online ? 'Heartbeat recente' : 'Sem heartbeat recente') : null],
      ['Serial informada pela Base', base?.serial_status], ['Itens na fila informada', base?.queue_size],
      ['Versão de cerca reportada pela Base', base?.fence_version_reported || null],
      ['Sincronização da cerca', base?.online && fence && base.fence_version_reported
        ? (base.fence_version_reported === fence.version ? 'Base reporta a versão publicada' : 'Base reporta outra versão') : null],
    ]);
    if (fence) {
      const summary = renderDeliverySummary(el('fence'), herd.deliveries);
      summary.prepend(node('strong', fence.name + ' · Versão ' + fence.version), node('p', 'Publicada em ' + clock(fence.created_at)));
    } else el('fence').replaceChildren(node('p', herd ? 'Nenhuma cerca publicada.' : 'Situação da cerca indisponível.', 'visitor-empty'));
    list('collars', herd?.animals.map(animal => node('li',
      [animal.name, statusLabels[animal.link], clock(animal.last_contact_at)].join(' · '), 'visitor-alert')) ?? [],
    herd ? 'Nenhuma coleira cadastrada.' : 'Coleiras indisponíveis.');
    const names = new Map(herd?.animals.map(animal => [animal.collar_id, animal.name]));
    list('events', events?.map(event => node('li',
      [names.get(event.collar_id) || event.collar_id, eventLabels[event.kind] || event.kind, clock(event.ts)].filter(Boolean).join(' · '),
      'visitor-alert')) ?? [], events ? 'Nenhum acontecimento recente.' : 'Eventos indisponíveis.');
    list('alerts', herd?.animals.flatMap(animal => animal.alerts.map(alert => node('li',
      'Aviso ativo · ' + animal.name + ' · ' + alert, 'visitor-alert'))) ?? [],
    herd ? 'Nenhum aviso ativo.' : 'Avisos indisponíveis.');
  } finally {
    pending = false;
  }
}
conectarTempoReal({ path: '/ws/producer', onPollingTick: refresh, onInvalidate: refresh });
