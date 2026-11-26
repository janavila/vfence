import assert from 'node:assert/strict';
import { test } from 'node:test';
import { haversineMeters, registeredMovement, periodBounds } from '../frontend/js/movement.js';

const stamp = seconds => new Date(Date.UTC(2026, 5, 10, 12, 0, seconds)).toISOString();
const sample = (id, seconds, lon, version = 1, zone = 'SEGURO') =>
  ({ id, edge_seq: id, base_id: 'BASE', ts: stamp(seconds), lat: 0, lon, fence_version: version, zone });

test('ordena por geração e exclui duplicatas, falhas e saltos da distância', () => {
  const a = sample(1, 0, 0);
  const b = sample(2, 10, .0001);
  const c = sample(3, 20, .0002, 2);
  const d = sample(4, 30, .0003, 2, 'GNSS_INVALIDO');
  const e = sample(5, 40, .0004, 2);
  const f = sample(6, 50, .1, 991);
  const g = sample(7, 60, .1001, 991);
  const h = sample(8, 3600, .1002);
  const i = sample(9, 3610, .1003);
  const j = sample(10, 3620, 181);
  const track = registeredMovement([e, a, b, c, d, f, { ...b, id: 11 }, g, h, i, j], [
    { ts: stamp(3605), kind: 'collar_offline' },
  ]);
  assert.deepEqual(track.valid.map(row => row.id), [1, 2, 3, 5, 6, 7, 8, 9]);
  assert.deepEqual(track.segments.map(segment => segment.map(row => row.id)), [[1, 2, 3], [5], [6, 7], [8], [9]]);
  assert.equal(track.gaps, 5);
  assert(Math.abs(track.distance - 33.3585) < .01);
  assert(Math.abs(haversineMeters({ lat: 0, lon: 0 }, { lat: 0, lon: 1 }) - 111195) < 1);
});

test('não calcula deslocamento com uma posição, sem registros ou com trechos isolados', () => {
  assert.equal(registeredMovement([]).distance, null);
  assert.equal(registeredMovement([sample(1, 0, 0)]).distance, null);
  assert.equal(registeredMovement([sample(1, 0, 0), sample(2, 500, .0001)]).distance, null);
});

test('converte dia, manhã e intervalo personalizado no fuso da propriedade', () => {
  const now = Date.parse('2026-06-12T00:00:00Z');
  const zone = 'America/Sao_Paulo';
  assert.deepEqual(periodBounds('2026-06-10', 'day', '', '', zone, now),
    { start: '2026-06-10T03:00:00.000Z', end: '2026-06-11T03:00:00.000Z' });
  assert.equal(periodBounds('2026-06-10', 'am', '', '', zone, now).end, '2026-06-10T15:00:00.000Z');
  assert.equal(periodBounds('2026-06-10', 'custom', '07:00', '08:30', zone, now).end, '2026-06-10T11:30:00.000Z');
  assert.throws(() => periodBounds('2026-06-12', 'day', '', '', zone, now), /até hoje/);
  assert.throws(() => periodBounds('2026-06-10', 'custom', '08:00', '06:00', zone, now), /posterior/);
  const ny = periodBounds('2026-03-08', 'day', '', '', 'America/New_York', now);
  assert.equal((Date.parse(ny.end) - Date.parse(ny.start)) / 3600000, 23);
  assert.throws(() => periodBounds('2026-03-08', 'custom', '02:10', '04:00', 'America/New_York', now), /não existe/);
});
