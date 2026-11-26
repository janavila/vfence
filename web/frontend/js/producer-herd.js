import { consultar } from './api.js';
import { formatClock } from './ui.js';
import { conectarTempoReal } from './realtime.js';

const management = document.body.classList.contains('management-area');

export function renderDeliverySummary(container, deliveries) {
  const summary = document.createElement('div');
  summary.className = 'producer-fence-summary';
  const description = document.createElement('p');
  if (!deliveries.length) {
    description.textContent = 'Nenhuma coleira destinatária nesta versão.';
    summary.append(description);
  } else {
    const confirmed = deliveries.filter(row => row.status === 'confirmed').length;
    const failed = deliveries.filter(row => row.status === 'failed').length;
    const pending = deliveries.length - confirmed - failed;
    description.textContent = [
      deliveries.length + (deliveries.length === 1 ? ' destinatário' : ' destinatários'),
      confirmed + (confirmed === 1 ? ' confirmação informada pela Base' : ' confirmações informadas pela Base'),
      pending + (pending === 1 ? ' pendente' : ' pendentes'),
      failed + (failed === 1 ? ' falha' : ' falhas'),
    ].join(' · ');
    const progress = document.createElement('progress');
    progress.max = deliveries.length;
    progress.value = confirmed;
    progress.setAttribute('aria-label', 'Confirmações informadas pela Base');
    const evidence = document.createElement('p');
    evidence.textContent = confirmed ? 'Origem física das confirmações ainda não verificada.'
      : pending ? 'Aguardando confirmação das coleiras pela Base.' : 'Nenhuma entrega confirmada.';
    summary.append(description, progress, evidence);
  }
  container.replaceChildren(summary);
  return summary;
}

function renderFence(data) {
  const view = document.getElementById('producer-current-fence');
  const fence = data?.fences[0];
  view.classList.toggle('visitor-empty', !fence);
  if (!data) {
    view.textContent = 'Informações sobre a cerca temporariamente indisponíveis.';
    return;
  }
  const action = document.createElement('a');
  action.className = 'button visitor-secondary';
  action.href = (management ? '/gestao/cercas' : '/produtor/cercas') + (fence ? '?version=' + fence.version : '');
  action.textContent = fence ? 'Gerenciar cerca' : 'Criar primeira cerca';
  if (!fence) {
    const description = document.createElement('p');
    description.textContent = 'Nenhuma cerca publicada.';
    view.replaceChildren(description, action);
    return;
  }
  const summary = renderDeliverySummary(view, data.deliveries);
  const heading = document.createElement('strong');
  heading.textContent = fence.name + ' · Versão ' + fence.version;
  const date = document.createElement('p');
  date.textContent = 'Publicada na Central em ' + formatClock(fence.created_at, { withDate: true });
  summary.prepend(heading, date);
  summary.append(action);
}

function renderHistory(events) {
  const container = document.getElementById('producer-history');
  container.replaceChildren(...events.map(event => {
    const row = document.createElement('li');
    row.className = 'visitor-alert';
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = event.name;
    const detail = document.createElement('p');
    detail.textContent = event.message;
    const at = document.createElement('time');
    at.dateTime = event.at;
    at.textContent = formatClock(event.at, { withDate: true });
    text.append(name, detail, at);
    row.append(text);
    return row;
  }));
  if (events.length) return;
  const empty = document.createElement('li');
  empty.className = 'visitor-empty';
  empty.textContent = 'Nenhum acontecimento registrado.';
  container.append(empty);
}

export function startProducerHerd(render, centerOnBase) {
  let pending = false;
  let queued = false;
  let lastFenceSignature;
  let lastHistorySignature;
  async function refresh() {
    if (pending) {
      queued = true;
      return;
    }
    pending = true;
    try {
      const data = await consultar(management ? '/api/gestao/rebanho' : '/api/producer/rebanho');
      if (!data.fences.length) {
        for (const animal of data.animals) {
          if (!animal.fence_used && ['inside', 'near', 'outside'].includes(animal.status)) {
            animal.status = 'unknown';
            animal.alerts = animal.alerts.filter(alert => !['Saiu da cerca', 'Perto da cerca', 'Muito perto da cerca'].includes(alert));
          }
        }
      }
      render(data);
      const fenceSignature = JSON.stringify([data.fences, data.deliveries]);
      if (fenceSignature !== lastFenceSignature) {
        renderFence(data);
        lastFenceSignature = fenceSignature;
      }
      const historySignature = JSON.stringify(data.history);
      if (historySignature !== lastHistorySignature) {
        renderHistory(data.history);
        lastHistorySignature = historySignature;
      }
    } catch {
      render(null);
      renderFence(null);
      lastFenceSignature = undefined;
    } finally {
      pending = false;
      if (queued) {
        queued = false;
        refresh();
      }
    }
  }
  if (centerOnBase) {
    consultar('/api/producer/settings')
      .then(config => centerOnBase(config.base_lat, config.base_lon)).catch(() => {});
  }
  conectarTempoReal({ path: '/ws/producer', onPollingTick: refresh, onInvalidate: refresh });
}
