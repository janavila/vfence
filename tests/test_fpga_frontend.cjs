const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/static/js/fpga.js'), 'utf8');
const now = () => new Date().toISOString();
const stale = () => new Date(Date.now() - 240000).toISOString();

function page() {
  const nodes = new Map(), requests = [];
  const element = () => ({
    textContent: '', dataset: {}, options: [], value: '', children: [],
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) {
      this.children = children;
      if (children[0]?.isOption) {
        this.options = children;
        this.value = children[0].value;
      }
    },
    addEventListener(type, handler) { this[type] = handler; },
  });
  function getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  }
  const context = vm.createContext({
    document: {hidden: true, addEventListener() {}, getElementById, createElement: element},
    Option: function (text, value) { return {text, value, isOption: true}; },
    fetch: async (url, options) => {
      requests.push({url, options});
      const response = context.fixtures[url];
      return {ok: response !== undefined, status: 503, json: async () => response};
    },
    AbortSignal, setInterval() {},
  });
  vm.runInContext(source, context);
  context.document.hidden = false;
  return {
    getElementById, requests,
    async refresh(fixtures) {
      context.fixtures = fixtures;
      await vm.runInContext('refreshOverview()', context);
    },
    field(name) {
      const row = getElementById('fpga-input').children.find(tr => tr.children[0].textContent === name);
      return {value: row.children[1].textContent, state: row.children[3].children[0].textContent};
    },
  };
}

function fixtures() {
  const session = 'a'.repeat(16), windowId = '0000000000000010';
  const common = {session_id: session, window_id: windowId, started_at_ms: 60000, duration_ms: 60000, persisted_at: now()};
  return {
    '/api/collars': [{
      collar_id: 'COL01', contact: {state: 'online', last_contact_at: now()},
      position: {zone: 'GNSS_INVALIDO', at: now()},
    }],
    '/api/gateway': {mock: false, serial_open: true, freshness_seconds: 180},
    '/api/collars/1/link-metrics?limit=1': {
      collar_id: 1, gateway: {effective_profile: {profile_id: 0, observed_at: now()}},
      windows: {
        hdop: [{...common, producer_id: 'INST', sample_count: 0, hdop_mean_x100: null, hdop_mean_valid: false}],
        radio: [{
          ...common, producer_id: 'GW', sample_count: 8,
          rssi_mean_x100: -9530, snr_mean_x100: 0, rssi_mean_valid: true, snr_mean_valid: true,
        }],
      },
    },
    '/api/collars/1/pdr?limit=1': [],
  };
}

test('reads only real data endpoints, preserves a valid zero and shows missing PDR', async () => {
  const ui = page();
  const data = fixtures();
  await ui.refresh(data);
  assert.deepEqual(ui.requests.map(request => request.url), Object.keys(data));
  assert.ok(ui.requests.every(request => request.options.method === undefined));
  assert.equal(ui.getElementById('fpga-hdop-value').textContent, 'N/A');
  assert.equal(ui.getElementById('fpga-radio-value').textContent, '-95,30 dBm · 0,00 dB');
  assert.equal(ui.getElementById('fpga-pdr-value').textContent, 'Sem registro');
  assert.deepEqual(ui.field('hdop_x100'), {value: '—', state: 'Inválido'});
  assert.deepEqual(ui.field('metrics_valid[3]'), {value: '0', state: 'Inválido'});
  assert.deepEqual(ui.field('pdr_x100'), {value: '—', state: 'Ausente'});
  assert.equal(ui.getElementById('fpga-sufficiency').textContent, 'Dados insuficientes');
});

test('correlates RADIO and PDR by producer and window and keeps HDOP on its own clock', async () => {
  const ui = page();
  const data = fixtures();
  const {hdop, radio} = data['/api/collars/1/link-metrics?limit=1'].windows;
  Object.assign(hdop[0], {sample_count: 35, hdop_mean_x100: 140, hdop_mean_valid: true});
  data['/api/collars/1/pdr?limit=1'] = [{
    ...radio[0], collar_id: 1, gateway_id: 'GW', gateway_session_id: radio[0].session_id,
    collar_session_id: hdop[0].session_id, received_count: 8, estimated_tx_count: 10,
    pdr_x100: 8000, profile_id: 0, valid: true,
  }];
  await ui.refresh(data);
  assert.deepEqual(ui.field('rssi_mag'), {value: 95, state: 'Válido'});
  assert.deepEqual(ui.field('pdr_x100'), {value: 8000, state: 'Válido'});
  assert.equal(ui.field('hdop_x100').state, 'Sem correlação');
  assert.equal(ui.field('minute_index').state, 'Sem correlação');
  assert.equal(ui.getElementById('fpga-sufficiency').textContent, 'Dados insuficientes');
  data['/api/collars/1/pdr?limit=1'][0].gateway_session_id = 'b'.repeat(16);
  await ui.refresh(data);
  assert.equal(ui.field('pdr_x100').state, 'Sem correlação');
});

test('marks old or out-of-range input and reports a failed refresh', async () => {
  const ui = page();
  const data = fixtures();
  const radio = data['/api/collars/1/link-metrics?limit=1'].windows.radio[0];
  Object.assign(radio, {persisted_at: stale(), sample_count: 256});
  await ui.refresh(data);
  assert.equal(ui.field('sample_count').state, 'Inválido');
  assert.equal(ui.field('snr_x100').state, 'Inválido');
  assert.match(JSON.stringify(ui.getElementById('fpga-radio').children), /Antigo/);
  await ui.refresh({});
  assert.match(ui.getElementById('fpga-api-status').textContent, /API indisponível/);
  assert.equal(ui.getElementById('fpga-radio-value').textContent, '-95,30 dBm · 0,00 dB');
});
