import { lerHistorico, lerRascunhos, reativarCerca, explicarErro, produtor, enderecoDoLog, enderecoDoGeojson } from './api.js';
import { formatClock } from './ui.js';

const editor = produtor ? '/produtor/cercas' : '/gestao/cercas';
const feedback = document.getElementById('fences-feedback');
const draftsList = document.getElementById('draft-list');
const editorTools = Boolean(document.getElementById('producer-edit-active'));
const item = (list, text) => {
  const row = document.createElement('li');
  const label = document.createElement('strong');
  label.textContent = text;
  row.append(label);
  list.append(row);
  return row;
};

function link(row, text, url) {
  const a = document.createElement('a');
  a.className = 'button';
  a.textContent = text;
  a.href = url;
  row.append(a);
}

function action(row, text, fn) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'button';
  button.textContent = text;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await fn(button); }
    catch (error) { feedback.textContent = explicarErro(error); feedback.hidden = false; }
    finally { button.disabled = false; }
  });
  row.append(button);
}

async function refreshLists() {
  try {
  const [drafts, published] = await Promise.all([draftsList ? lerRascunhos() : [], lerHistorico()]);
  const publishedList = document.getElementById('published-list');
  draftsList?.replaceChildren();
  publishedList.replaceChildren();
  if (editorTools) {
    const active = published.find(fence => fence.status === 'active');
    for (const [id, mode] of [['producer-edit-active', 'edit'], ['producer-track-active', 'version']]) {
      const link = document.getElementById(id);
      link.hidden = !active;
      if (active) link.href = editor + '?' + mode + '=' + active.version;
    }
  }
  if (draftsList && !drafts.length) item(draftsList, 'Nenhum rascunho salvo.');
  if (!published.length) item(publishedList, 'Nenhuma cerca publicada.');
  for (const draft of drafts) {
    const row = item(draftsList, (draft.name || 'Rascunho sem nome') + ' · ' +
      formatClock(draft.updated_at, { withDate: true }));
    link(row, 'Continuar edição', editor + '?draft=' + draft.id);
  }
  for (const fence of published) {
    const row = item(publishedList, fence.name + ' · Versão ' + fence.version +
      (fence.status === 'active' ? ' · Publicada' : ' · Anterior'));
    link(row, produtor ? 'Ver versão e entregas' : 'Acompanhar entrega', editor + '?version=' + fence.version);
    link(row, 'Editar como nova versão', editor + '?edit=' + fence.version);
    if (!produtor) {
      row.firstElementChild.append(' · ' + formatClock(fence.created_at, { withDate: true }));
      link(row, 'Relatório', enderecoDoLog(fence.version));
      link(row, 'GeoJSON', enderecoDoGeojson(fence.version));
    }
    let retryKey;
    if (fence.status === 'active') continue;
    action(row, produtor ? 'Reativar versão' : 'Usar de novo', async () => {
      if (editorTools && !window.dispatchEvent(new Event('producer:leave-editor', { cancelable: true }))) return;
      if (!window.confirm(produtor ? 'Publicar este desenho como uma nova versão e iniciar a distribuição?'
        : 'Publicar este desenho como uma nova versão?')) return;
      retryKey ||= crypto.randomUUID();
      const next = await reativarCerca(fence.version, retryKey);
      if (editorTools) window.dispatchEvent(new Event('producer:published'));
      location.assign(editor + '?version=' + next.version);
    });
  }
  feedback.hidden = true;
  } catch (error) {
    feedback.textContent = explicarErro(error);
    feedback.hidden = false;
  }
}

if (editorTools) window.addEventListener('producer:fences-changed', refreshLists);
refreshLists();
