export const GAP_SECONDS = 120;
const SPEED_LIMIT_M_S = 5;
const GPS_TOLERANCE_M = 15;
const breaksOn = new Set(['gps_lost', 'collar_offline', 'offline']);
const dateFormatters = new Map();

function localParts(instant, zone) {
  if (!dateFormatters.has(zone)) {
    dateFormatters.set(zone, new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
      minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }));
  }
  return Object.fromEntries(dateFormatters.get(zone).formatToParts(instant).map(part => [part.type, part.value]));
}

export function localDay(instant, zone) {
  const { year, month, day } = localParts(instant, zone);
  return `${year}-${month}-${day}`;
}

export function nextDay(day) {
  const midnight = new Date(day + 'T00:00:00Z');
  midnight.setUTCDate(midnight.getUTCDate() + 1);
  return midnight.toISOString().slice(0, 10);
}

export function zonedTime(day, time, zone) {
  // Procura os offsets dos dois lados de uma troca de hora local.
  const nominal = Date.parse(`${day}T${time}:00Z`);
  if (!Number.isFinite(nominal)) throw new Error('Informe uma data e um horário válidos.');
  const offsets = new Set([-36, 0, 36].map(hours => {
    const reference = nominal + hours * 3600000;
    const local = localParts(reference, zone);
    return Date.parse(`${local.year}-${local.month}-${local.day}T${local.hour}:${local.minute}:${local.second}Z`) - reference;
  }));
  const candidates = [...offsets].map(offset => nominal - offset).filter(candidate => {
    const local = localParts(candidate, zone);
    return `${local.year}-${local.month}-${local.day}` === day && `${local.hour}:${local.minute}` === time;
  });
  if (!candidates.length) {
    // Alguns fusos pulam meia-noite. A data ainda começa no primeiro horário existente.
    if (time === '00:00') {
      for (let minute = 1; minute <= 180; minute++) {
        const candidate = `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
        try { return zonedTime(day, candidate, zone); } catch { /* horário inexistente */ }
      }
    }
    throw new Error('O horário não existe nessa data no fuso da propriedade.');
  }
  return new Date(Math.min(...candidates)).toISOString();
}

export function periodBounds(day, period, start, end, zone, now = Date.now()) {
  if (!day || day > localDay(now, zone)) throw new Error('Escolha uma data válida até hoje.');
  if (period === 'custom' && (!start || !end || start >= end)) {
    throw new Error('O horário final deve ser posterior ao inicial.');
  }
  const first = period === 'custom' ? start : period === 'pm' ? '12:00' : '00:00';
  const fixedEnd = period === 'custom' ? end : period === 'am' ? '12:00' : null;
  return { start: zonedTime(day, first, zone), end: zonedTime(fixedEnd ? day : nextDay(day), fixedEnd || '00:00', zone) };
}

export function haversineMeters(a, b) {
  const radians = (degrees) => degrees * Math.PI / 180;
  const lat = radians(b.lat - a.lat);
  const lon = radians(b.lon - a.lon);
  const component = Math.sin(lat / 2) ** 2 + Math.cos(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.sin(lon / 2) ** 2;
  return 6371008.8 * 2 * Math.asin(Math.sqrt(Math.max(0, Math.min(1, component))));
}

export function registeredMovement(samples, events = []) {
  const invalidations = events.filter(row => breaksOn.has(row.kind)).map(row => Date.parse(row.ts))
    .filter(Number.isFinite).sort((a, b) => a - b);
  const ordered = samples.map(row => ({ ...row, time: Date.parse(row.ts) })).sort((a, b) => a.time - b.time || a.id - b.id);
  const packets = new Set();
  const points = new Set();
  const records = [];
  const valid = [];
  const segments = [];
  let segment = [];
  let distance = 0;
  let links = 0;
  let gaps = 0;
  let invalidation = 0;

  function closeSegment() {
    if (segment.length) segments.push(segment);
    segment = [];
  }

  for (const row of ordered) {
    const packet = JSON.stringify([row.base_id, row.edge_seq, row.ts]);
    const point = JSON.stringify([row.ts, row.lat, row.lon, row.zone, row.fence_version]);
    if (packets.has(packet) || points.has(point)) continue;
    packets.add(packet);
    points.add(point);
    row.valid = Number.isFinite(row.time) && Number.isFinite(row.lat) && Math.abs(row.lat) <= 90
      && Number.isFinite(row.lon) && Math.abs(row.lon) <= 180 && row.zone !== 'GNSS_INVALIDO';
    records.push(row);
    if (!row.valid) {
      if (segment.length) gaps++;
      closeSegment();
      continue;
    }
    const previous = segment.at(-1);
    if (previous) {
      const seconds = (row.time - previous.time) / 1000;
      const separation = haversineMeters(previous, row);
      while (invalidations[invalidation] <= previous.time) invalidation++;
      if (invalidations[invalidation] <= row.time || seconds > GAP_SECONDS
          || separation > SPEED_LIMIT_M_S * seconds + GPS_TOLERANCE_M) {
        gaps++;
        closeSegment();
      } else {
        links++;
        distance += separation;
      }
    }
    segment.push(row);
    valid.push(row);
  }
  closeSegment();
  return { records, valid, segments, gaps, distance: links ? distance : null };
}
