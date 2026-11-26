const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../app/static/js/app.js'), 'utf8');
const settle = () => new Promise(setImmediate);
const iso = seconds => new Date(Date.now() + seconds * 1000).toISOString();
const online = {serial_open: true, last_response_at: iso(0), last_seen: iso(0), freshness_seconds: 180};
function collar(id, seconds, zone='FORA') {
  return {collar_id: id, contact:{state:'online', last_contact_at:iso(seconds)},
    position:{at:iso(seconds), zone, lat:-31, lon:-54, rssi:-93, snr:0, sequence:1}};
}

function dashboard() {
  const requests=[], sockets=[], timers=[], nodes=new Map(), tooltips=[];
  let removed=0;
  function node(id) {
    if(!nodes.has(id)) nodes.set(id,{addEventListener(type, handler){this[type]=handler;}, querySelectorAll(){return [];}});
    return nodes.get(id);
  }
  const map={setView(){return this;}, fitBounds(){}};
  const layer=()=>({addTo(){return this;}, bindTooltip(label){tooltips.push(label);return this;},
    remove(){removed++;}, setLatLng(){}, setStyle(){}});
  const L={map:()=>map,tileLayer:layer,circleMarker:layer};
  const context={L,window:{L},document:{querySelector:node,createElement:()=>({})},
    location:{protocol:'http:',host:'localhost'},AbortSignal,WebSocket:function(){sockets.push(this);},
    fetch(url){return new Promise(resolve=>requests.push({url,resolve}));},
    setTimeout(callback,ms){timers.push({callback,ms});return timers.length;},setInterval(){}};
  vm.runInNewContext(script,context);
  const reply=(url,value)=>requests.find(item=>item.url===url&&!item.done && (item.done=true))
    .resolve({ok:true,json:async()=>value});
  function snapshot(gateway,rows){
    reply('/api/gateway',gateway);reply('/api/central',{enabled:false});
    reply('/api/fence-state',{});reply('/api/collars',rows);reply('/api/events?limit=30',[]);
  }
  return {node,sockets,timers,tooltips,reply,snapshot,requests,removed:()=>removed,
    push(message){sockets.at(-1).onmessage({data:JSON.stringify(message)});}};
}

test('historical alerts do not count and offline or invalid fixes remove markers',async()=>{
  const ui=dashboard();
  const old=collar('COL01',-600), current=collar('COL02',-1,'ATENCAO');
  ui.snapshot(online,[old,current]);
  await settle();
  assert.equal(ui.node('#collar-count').textContent,'01');
  assert.equal(ui.node('#alert-count').textContent,'01');
  assert.equal(ui.node('#outside-count').textContent,'00');
  assert.match(ui.node('#collars').innerHTML,/0\.00 dB/);
  assert.deepEqual(ui.tooltips,[{textContent:'COL02'}]);
  ui.push({type:'telemetry',collar:{...current,position:{...current.position,zone:'GNSS_INVALIDO'}}});
  assert.equal(ui.removed(),1);
  assert.doesNotMatch(ui.node('#collars').innerHTML,/-31\.000000/);
  ui.push({type:'gateway',data:{serial_open:true}});
  assert.equal(ui.node('#gateway-status').textContent,'Sem resposta recente');
  ui.push({type:'gateway',data:{serial_open:false,error:'Port missing'}});
  assert.equal(ui.node('#gateway-status').textContent,'Offline');
  assert.equal(ui.node('#collar-count').textContent,'00');
});

test('REST cannot replace a live update and reconnect reloads every resource',async()=>{
  const ui=dashboard();
  ui.push({type:'telemetry',collar:collar('COL02',0),gateway:online});
  ui.snapshot({serial_open:false},[collar('COL01',-600)]);
  await settle();
  assert.equal(ui.node('#gateway-status').textContent,'Respondendo');
  assert.match(ui.node('#collars').innerHTML,/COL01/);
  assert.match(ui.node('#collars').innerHTML,/COL02/);
  assert.doesNotThrow(()=>ui.sockets[0].onmessage({data:'{'}));
  ui.sockets[0].onclose();
  assert.match(ui.node('#connection').innerHTML,/Reconectando/);
  ui.snapshot(online,[collar('COL01',-600)]);
  await settle();
  ui.timers.find(timer=>timer.ms===2000).callback();
  ui.sockets[1].onopen();
  assert.equal(ui.requests.filter(request=>!request.done).length,5);
  ui.snapshot({serial_open:false},[collar('COL01',-600)]);
  await settle();
  assert.doesNotMatch(ui.node('#collars').innerHTML,/COL02/);
  assert.match(ui.node('#connection').innerHTML,/Tempo real ativo/);
});

test('expanded telemetry shows independent windows, units and missing data',async()=>{
  const ui=dashboard();
  ui.snapshot(online,[collar('COL01',0)]);
  await settle();
  ui.node('#collars').click({target:{closest:()=>({dataset:{unit:'COL01'}})}});
  const window={duration_ms:60000,started_at_ms:200,persisted_at:iso(-600),sample_count:8};
  ui.reply('/api/collars/1/link-metrics?limit=1',{collar_id:1,windows:{
    hdop:[{...window,hdop_mean_x100:142,hdop_mean_valid:true}],
    radio:[{...window,rssi_mean_x100:-9340,snr_mean_x100:0,rssi_mean_valid:true,snr_mean_valid:true}],
    pdr:[{...window,pdr_x100:7500,valid:true,received_count:6,estimated_tx_count:8,estimated_lost_count:2,profile_id:2}],
  }});
  await settle();
  for(const value of ['1.42','-93.40 dBm','0.00 dB','75.00%','P2','6 / 8','60 s','há 10 min']){
    assert.ok(ui.node('#collars').innerHTML.includes(value),value);
  }
  ui.node('#collars').click({target:{closest:()=>({dataset:{unit:'COL01'}})}});
  ui.node('#collars').click({target:{closest:()=>({dataset:{unit:'COL01'}})}});
  ui.reply('/api/collars/1/link-metrics?limit=1',{collar_id:1,windows:{hdop:[],radio:[],pdr:[]}});
  await settle();
  assert.match(ui.node('#collars').innerHTML,/Nenhuma janela registrada/);
  assert.doesNotMatch(ui.node('#collars').innerHTML,/75\.00%/);
});

test('central status reflects observed phases and pending count',async()=>{
  const ui=dashboard();
  ui.snapshot(online,[collar('COL01',0)]);
  await settle();
  for(const [phase,status,label] of [
    ['connecting','offline','Conectando'],
    ['authenticated','offline','Autenticada'],
    ['awaiting_credential','offline','Aguardando credencial válida'],
    ['network_unavailable','offline','Sem conexão com a Central'],
    ['server_unavailable','offline','Temporariamente indisponível'],
    ['reconnected','online','Reconectada'],
    ['synchronized','online','Sincronizada'],
  ]){
    ui.push({type:'central',data:{enabled:true,phase,status,outbox_size:3}});
    assert.equal(ui.node('#central-status').textContent,`${label} · 3 pendentes`);
  }
});
