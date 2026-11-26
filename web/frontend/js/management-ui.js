export function node(tag, text, css) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (css) element.className = css;
  return element;
}

export function facts(container, entries, css = '') {
  const cards = entries.map(([label, value]) => {
    const card = node('div', null, css);
    card.append(node('dt', label), node('dd', value ?? '—'));
    return card;
  });
  container.replaceChildren(...cards);
}

export function fillCards(container, entries) {
  facts(container, entries, 'visitor-counter');
}

export const statusLabels = { online: 'Online', offline: 'Offline', unknown: 'Sem informação' };
export const zoneLabels = { SEGURO: 'Dentro da cerca', ATENCAO: 'Próximo da cerca', CRITICO: 'Muito próximo da cerca', FORA: 'Fora da cerca', GNSS_INVALIDO: 'GPS inválido' };
export const eventLabels = {
  outside: 'Saída da cerca', back_inside: 'Retorno à cerca', gps_lost: 'Perda de GPS', gps_recovered: 'GPS recuperado',
  collar_offline: 'Coleira sem comunicação', offline: 'Coleira sem comunicação', collar_online: 'Coleira voltou a comunicar',
  zone_change: 'Mudança de zona', low_battery: 'Bateria baixa', critical_battery: 'Bateria crítica',
  device_started: 'Coleira iniciada', invalid_zone: 'Zona recebida inválida',
};
export function collarName(collar) {
  return [collar.animal_label, collar.collar_id].filter(Boolean).join(' · ');
}

export function propertyClock(zone, withDate = false) {
  const formatter = new Intl.DateTimeFormat('pt-BR', {
    timeZone: zone, ...(withDate ? { dateStyle: 'short' } : {}), timeStyle: 'medium',
  });
  return value => value && Number.isFinite(Date.parse(value)) ? formatter.format(new Date(value)) : '—';
}
