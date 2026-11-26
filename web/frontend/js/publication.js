import { consultar, explicarErro } from './api.js';
import './ui.js';

const endpoint = '/api/gestao/publicacao';
const form = document.getElementById('public-policy');
const feedback = document.getElementById('publication-feedback');
const submit = form.querySelector('[type=submit]');
const element = (tag, text) => {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
};

function field(parent, text, type, value, checked) {
  const label = element('label', text);
  const input = document.createElement('input');
  input.type = type;
  input.value = value ?? '';
  input.checked = Boolean(checked);
  label.prepend(input);
  if (type === 'checkbox') label.className = 'checkbox-row';
  parent.append(label);
  return input;
}

try {
  const [policy, rows, fences] = await Promise.all([
    consultar(endpoint), consultar('/api/collars'), consultar('/api/fences'),
  ]);
  const animals = document.getElementById('publication-animals');
  const polygons = document.getElementById('publication-fences');
  const names = new Map(policy.animals.map(entry => [entry.collar_id, entry]));
  const choices = rows.map(row => {
    const saved = names.get(row.collar_id);
    const container = element('fieldset');
    container.className = 'stack-sm';
    container.append(element('legend', row.animal_label || row.collar_id));
    animals.append(container);
    const access = field(container, 'Publicar animal', 'checkbox', '', saved);
    const name = field(container, 'Nome público', 'text', saved?.name, false);
    name.maxLength = 80;
    const decimals = field(container, 'Casas decimais da posição (em branco para ocultar)', 'number', saved?.decimals, false);
    decimals.min = 0;
    decimals.max = 7;
    const status = field(container, 'Mostrar situação', 'checkbox', '', saved?.status);
    const alerts = field(container, 'Mostrar avisos', 'checkbox', '', saved?.alerts);
    const timestamps = field(container, 'Mostrar horário da posição', 'checkbox', '', saved?.timestamps);
    return { collar_id: row.collar_id, access, name, decimals, status, alerts, timestamps };
  });
  if (!rows.length) animals.textContent = 'Nenhuma coleira cadastrada.';
  const selectedFences = fences.map(fence => field(polygons, 'Publicar polígono de ' + fence.name +
    ' · versão ' + fence.version, 'checkbox', fence.version, policy.fence_versions.includes(fence.version)));
  if (!fences.length) polygons.textContent = 'Nenhuma cerca publicada.';
  submit.disabled = false;
  feedback.textContent = '';
  form.addEventListener('submit', async event => {
    event.preventDefault();
    submit.disabled = true;
    try {
      await consultar(endpoint, { metodo: 'PUT', corpo: {
        animals: choices.filter(row => row.access.checked).map(row => ({
          collar_id: row.collar_id, name: row.name.value.trim(),
          decimals: row.decimals.value === '' ? null : Number(row.decimals.value),
          status: row.status.checked, alerts: row.alerts.checked, timestamps: row.timestamps.checked,
        })),
        fence_versions: selectedFences.filter(input => input.checked).map(input => Number(input.value)),
      } });
      feedback.textContent = 'Autorização pública atualizada.';
    } catch (error) {
      feedback.textContent = explicarErro(error);
    } finally {
      submit.disabled = false;
    }
  });
} catch (error) {
  feedback.textContent = explicarErro(error);
}
