const collarsNode=document.querySelector('#collars'),eventsNode=document.querySelector('#events');
let collars=[],events=[],map=null,fence=null,fenceLayer=null,fenceVertices=null;
let gateway=null, central=null, collarsLoaded=false, eventsLoaded=false, fitted=false;
let selectedCollar=null, metrics=null, metricsState='idle', metricsRequest=0, metricsTimer=null;
let live=false, apiState='loading', loadRequest=0, collarRevision=0, collarRequest=0;
let collarTimer=null;
const collarChanges=new Map(), resourceRequests=new Map(), resourceRevisions=new Map();
const zoneColors={SEGURO:'#35745c',ATENCAO:'#946e22',CRITICO:'#a0643f',FORA:'#a34f4f',GNSS_INVALIDO:'#69736f'};
const zoneLabels={SEGURO:'NORMAL',ATENCAO:'ATENÇÃO',CRITICO:'CRÍTICO',FORA:'FORA DA ÁREA',GNSS_INVALIDO:'GNSS INVÁLIDO'};
const gatewayEventLabels={
  outside:'SAÍDA DA CERCA',back_inside:'RETORNO À CERCA',gps_lost:'GNSS PERDIDO',gps_recovered:'GNSS RESTABELECIDO',
  low_battery:'BATERIA BAIXA',critical_battery:'BATERIA CRÍTICA',device_started:'COLEIRA INICIADA',
  collar_offline:'COLEIRA OFFLINE',collar_online:'COLEIRA ONLINE',offline:'COLEIRA OFFLINE',
};
let eventHistoryTimer=null;
const markers=new Map();
const esc=value=>String(value??'—').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const number=(value,digits=2)=>Number.isFinite(value)?value.toFixed(digits):'—';
const timeValue=value=>typeof value==='string'?Date.parse(value):NaN;
const dateFormat=new Intl.DateTimeFormat('pt-BR',{
  dateStyle:'short',timeStyle:'medium',timeZone:'America/Sao_Paulo',
});
const timestamp=value=>Number.isFinite(timeValue(value))?dateFormat.format(timeValue(value)):'—';

function age(value){
  const elapsed=Date.now()-timeValue(value);
  if(!Number.isFinite(elapsed))return '—';
  const seconds=Math.max(0,Math.floor(elapsed/1000));
  if(seconds<60)return `${seconds} s`;
  if(seconds<3600)return `${Math.floor(seconds/60)} min`;
  const hours=Math.floor(seconds/3600);
  return hours<24?`${hours} h ${Math.floor(seconds%3600/60)} min`:`${Math.floor(hours/24)} d ${hours%24} h`;
}

function recent(value){
  const elapsed=Date.now()-timeValue(value);
  return elapsed>=-5000&&elapsed<=1000*(gateway?.freshness_seconds??180);
}

function unitState(collar){
  const sample=collar.position;
  const online=Boolean((gateway?.serial_open||gateway?.mock)&&collar.contact?.state==='online'&&
    recent(collar.contact.last_contact_at));
  const lost=collar.gnss_event?.type==='gps_lost'&&
    (!sample||timeValue(collar.gnss_event.at)>=timeValue(sample.at));
  const hasFix=Boolean(!lost&&sample&&sample.zone!=='GNSS_INVALIDO'&&
    Number.isFinite(sample.lat)&&Number.isFinite(sample.lon));
  const current=online&&recent(sample?.at);
  return {sample,online,lost,hasFix,current,zone:current&&hasFix?sample.zone:null};
}

function renderGateway(gateway){
  const status=document.querySelector('#gateway-status');
  const responding=gateway?.serial_open&&recent(gateway.last_response_at);
  status.textContent=!gateway?'—':gateway.mock?'Simulado':responding?'Respondendo':
    gateway.serial_open?'Sem resposta recente':'Offline';
  status.className=`status-pill ${responding?'online':gateway&&!gateway.serial_open&&!gateway.mock?'offline':''}`;
  document.querySelector('#gateway-detail').textContent=!gateway?'Aguardando consulta':gateway.mock?'Fonte simulada':
    gateway.serial_open?'Porta Serial aberta':gateway.error_kind==='storage'?'Falha no SQLite':
      gateway.error?'Falha na porta Serial':'Porta Serial fechada';
  document.querySelector('#serial-port').textContent=gateway?.serial_port||'—';
  document.querySelector('#last-message').textContent=gateway?.last_seen?
    `${timestamp(gateway.last_seen)} · há ${age(gateway.last_seen)}`:'Sem pacote neste processo';
  document.querySelector('#last-message').title=gateway?.last_message||'';
  document.querySelector('#gateway-detail').title=gateway?.error||'';
}

function renderCentral(central){
  const status=document.querySelector('#central-status');
  const online=central?.enabled&&central.status==='online';
  const labels={online:'Online',offline:'Offline',error:'Erro de configuração',
    storage_full:'Armazenamento cheio',storage_unavailable:'Armazenamento indisponível'};
  const phases={connecting:'Conectando',authenticated:'Autenticada',synchronized:'Sincronizada',
    reconnected:'Reconectada',awaiting_credential:'Aguardando credencial válida',
    network_unavailable:'Sem conexão com a Central',server_unavailable:'Temporariamente indisponível',
    protocol_error:'Resposta incompatível'};
  const state=phases[central?.phase]||labels[central?.status]||'Desconhecido';
  const pending=Number.isInteger(central?.outbox_size)?`${central.outbox_size} pendentes`:'';
  status.textContent=!central?'—':!central.enabled?'Desativada':pending?`${state} · ${pending}`:state;
  status.className=online?'online':central?.enabled?'offline':'';
  status.title=[central?.last_sync?`Última sincronização: ${timestamp(central.last_sync)}`:'',
    central?.next_retry_at?`Próxima tentativa: ${timestamp(central.next_retry_at)}`:'',
    central?.last_error||''].filter(Boolean).join(' · ');
}

function renderFence(data){
  fence=data||null;
  const desired=fence?.desired_version;
  const local=fence?.downloaded_version;
  const labels={not_available:'Sem cópia local',downloaded:'Baixada',pending:'Pendente',synchronized:'Confirmada'};
  document.querySelector('#fence-desired').textContent=desired==null?'—':`v${desired}`;
  document.querySelector('#fence-local').textContent=local==null?'—':`v${local}`;
  const status=document.querySelector('#fence-status');
  status.textContent=fence?.last_error?'Falha na entrega':labels[fence?.status]||'—';
  status.title=fence?.last_error||'';
  document.querySelector('#fence-confirmed').textContent=fence?.confirmed_version==null?'—':`v${fence.confirmed_version}`;
  document.querySelector('#fence-points').textContent=String(fence?.point_count||0);
  document.querySelector('#map-state').textContent=local==null?'Sem cerca local':`Cerca v${local}`;
  drawFence();
}

function renderCollars(){
  const units=collars.map(collar=>({collar,...unitState(collar)}));
  const active=units.filter(unit=>unit.online).length;
  const alerts=units.filter(unit=>unit.zone==='ATENCAO'||unit.zone==='CRITICO').length;
  const outside=units.filter(unit=>unit.zone==='FORA'&&unit.sample.fence_version!==0).length;
  const count=value=>collarsLoaded||units.length?String(value).padStart(2,'0'):'—';
  document.querySelector('#collar-count').textContent=count(active);
  document.querySelector('#collar-detail').textContent=`Contato recente · ${units.length} com histórico${gateway?.mock?' · simulado':''}`;
  document.querySelector('#alert-count').textContent=count(alerts);
  document.querySelector('#outside-count').textContent=count(outside);
  const time=document.querySelector('#updated-at');
  const newest=units.reduce((latest,unit)=>timeValue(unit.sample?.at)>timeValue(latest)||!latest?unit.sample?.at||latest:latest,null);
  time.textContent=newest?`Última amostra recebida ${timestamp(newest)}`:'Sem amostras recebidas';
  time.dateTime=newest||'';
  if(!collars.length){
    const empty=collarsLoaded?'Sem telemetria disponível':apiState==='loading'?'Carregando telemetria…':'Telemetria indisponível';
    collarsNode.innerHTML=`<tr class="empty-row"><td colspan="10">${empty}</td></tr>`;
    renderMap();
    return;
  }
  const focused=document.activeElement?.dataset?.unit;
  collarsNode.innerHTML=units.map(({collar,sample,online,current,hasFix,lost,zone})=>{
    const id=collar.collar_id;
    const open=selectedCollar===id;
    const label=!current?'Histórico':lost||sample?.zone==='GNSS_INVALIDO'?'GNSS INVÁLIDO':
      !hasFix?'GNSS desconhecido':sample.fence_version===0?'Sem cerca reportada':zoneLabels[zone]||'Zona desconhecida';
    const fix=collar.last_fix;
    const currentPosition=current&&hasFix;
    const lastPosition=!currentPosition&&Number.isFinite(fix?.lat)&&Number.isFinite(fix?.lon);
    const coordinates=currentPosition?sample:lastPosition?fix:null;
    return `<tr>
      <td class="unit-id"><button type="button" class="unit-toggle" data-unit="${esc(id)}" aria-expanded="${open}">${esc(id)} <span aria-hidden="true">${open?'−':'+'}</span></button></td>
      <td><span class="zone-badge" style="--zone:${zoneColors[current&&label===zoneLabels[zone]&&Object.hasOwn(zoneColors,zone)?zone:'GNSS_INVALIDO']}">${esc(label)}</span>
        <small class="unit-subtitle">${online?'Contato recente':collar.contact?.state==='offline'?'Offline reportado':'Sem contato recente'}</small></td>
      <td>${number(coordinates?.lat,6)}${lastPosition?'<small class="unit-subtitle">Última válida</small>':''}</td>
      <td>${number(coordinates?.lon,6)}${lastPosition?'<small class="unit-subtitle">Última válida</small>':''}</td>
      <td>${number(current&&hasFix?sample.satellites:null,0)}</td><td>${number(current&&hasFix?sample.hdop:null)}</td>
      <td>${measurement(sample?.rssi,0,'dBm')}</td><td>${measurement(sample?.snr,2,'dB')}</td>
      <td>${number(sample?.sequence,0)}</td><td class="cell-muted">${timestamp(sample?.at)}<small class="unit-subtitle">Idade: ${age(sample?.at)}</small></td>
    </tr>${open?renderUnitDetails(collar):''}`;
  }).join('');
  if(focused)Array.from(collarsNode.querySelectorAll('[data-unit]')).find(button=>button.dataset.unit===focused)?.focus({preventScroll:true});
  renderMap();
}

function measurement(value,digits,unit){
  return Number.isFinite(value)?`${number(value,digits)}${unit==='%'?'':' '}${unit}`:'—';
}

function metricValue(value,valid,unit){
  return valid===true&&Number.isFinite(value)?measurement(value/100,2,unit).trim():'—';
}

function detailGroup(title,rows){
  return `<section><h4>${title}</h4><dl>${rows.map(([label,value])=>
    `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl></section>`;
}

function renderUnitDetails(collar){
  const {sample,hasFix,current,lost}=unitState(collar);
  const fix=collar.last_fix;
  const window=kind=>metrics?.windows?.[kind]?.[0];
  const hdop=window('hdop'), radio=window('radio'), pdr=window('pdr');
  const windowRows=(data,clock)=>[
    ['Janela',`${data.duration_ms/1000} s · início ${data.started_at_ms} ms (${clock})`],
    ['Gravada na Base',`${timestamp(data.persisted_at)} · há ${age(data.persisted_at)}`],
    ...(data.source==='proven'?[['Atualidade',data.freshness==='recent'?'Recente':
      data.freshness==='historical'?'Histórica':'Sem referência temporal atual']]:[]),
  ];
  const validity=value=>value===true?'Válida':value===false?'Inválida':'—';
  const pdrReasons={no_samples:'Sem amostras',one_sample:'Apenas uma amostra',session_changed:'Sessão mudou',
    profile_changed:'Perfil mudou',discontinuity:'Descontinuidade no intervalo'};
  const metricStatus={loading:'Consultando resumos…',error:'Falha ao consultar resumos',unsupported:'Identificador sem suporte a resumos',
    ready:'Nenhuma janela registrada'}[metricsState]||'Nenhuma janela registrada';
  const config=gateway?.commit_config;
  const ackModes=config?Object.entries({Médias:config.metrics,Eventos:config.events,PDR:config.pdr})
    .filter(([,enabled])=>enabled).map(([name])=>name).join(', ')||'Desativado':null;
  return `<tr class="unit-detail-row"><td colspan="10"><div class="unit-details">
    ${detailGroup('Posição e GNSS',[
      ['GNSS',lost||sample?.zone==='GNSS_INVALIDO'?'Inválido no último relato':hasFix?
        (current?'Válido':'Válido na última posição'):'Sem informação'],
      ['Último contato',timestamp(collar.contact?.last_contact_at)],
      ['Última posição válida',fix?`${number(fix.lat,6)}, ${number(fix.lon,6)} · ${timestamp(fix.at)}`:'—'],
      ['Última zona reportada',zoneLabels[sample?.zone]||'—'],
      ['Cerca reportada',sample?.fence_version===0?'Nenhuma':sample?.fence_version!=null?`v${sample.fence_version}`:'—'],
      ['Distância à borda',measurement(sample?.distance_border_m,0,'m')],
      ['Bateria',measurement(sample?.battery_pct,0,'%')],
    ])}
    ${detailGroup('HDOP médio',hdop?[
      ['HDOP',metricValue(hdop.hdop_mean_x100,hdop.hdop_mean_valid,'')],
      ['Validade',validity(hdop.hdop_mean_valid)],['Amostras',hdop.sample_count],
      ...windowRows(hdop,hdop.timing_proven?'Gateway':'coleira'),
    ]:[['Estado',metricStatus]])}
    ${detailGroup('Rádio · médias',radio?[
      ['RSSI',`${metricValue(radio.rssi_mean_x100,radio.rssi_mean_valid,'dBm')} · ${validity(radio.rssi_mean_valid)}`],
      ['SNR',`${metricValue(radio.snr_mean_x100,radio.snr_mean_valid,'dB')} · ${validity(radio.snr_mean_valid)}`],
      ['Amostras',radio.sample_count],...windowRows(radio,'Gateway'),
    ]:[['Estado',metricStatus]])}
    ${detailGroup(pdr?.tx_proven?'PDR comprovado':'PDR estimado',pdr?[
      ['Entrega',metricValue(pdr.pdr_x100,pdr.valid,'%')],
      ['Validade',pdr.valid?'Válido no intervalo observado':pdrReasons[pdr.reason]||'Inválido'],
      [pdr.tx_proven?'Recebidos / transmissões comprovadas':'Recebidos / esperados',
        `${number(pdr.received_count,0)} / ${number(pdr.tx_proven?pdr.transmitted_count:pdr.estimated_tx_count,0)}`],
      [pdr.tx_proven?'Perdas':'Perdas estimadas',pdr.tx_proven?pdr.lost_count:pdr.estimated_lost_count],
      ['Perfil',pdr.profile_id==null?'—':`P${pdr.profile_id}`],
      ...windowRows(pdr,'Gateway'),
    ]:[['Estado',metricStatus]])}
    <small class="unit-subtitle unit-footnote">Os resumos mostram o horário de gravação na Base.
      ${metricsState==='error'&&metrics?'Falha ao atualizar os resumos.':''}
      ${ackModes?esc(`ACK Serial configurado: ${ackModes}.`):''}</small>
  </div></td></tr>`;
}

function eventText(event){
  if(event.event_type!=='zone_change'){
    const label=gatewayEventLabels[event.event_type]||event.event_type;
    try{
      const data=JSON.parse(event.new_value);
      const details=[];
      if(Number.isInteger(data?.fenceVersion))details.push(`Cerca v${data.fenceVersion}`);
      if(Number.isInteger(data?.sequence))details.push(`Seq. ${data.sequence}`);
      return details.length?`${label} · ${details.join(' · ')}`:label;
    }catch{return label;}
  }
  const next=zoneLabels[event.new_value]||event.new_value;
  const previous=zoneLabels[event.old_value]||event.old_value;
  return event.old_value==null?`ZONA INICIAL: ${next}`:`${previous} → ${next}`;
}

function updateEvents(incoming){
  const byId=new Map([...events,...incoming].map(event=>[event.id,event]));
  events=[...byId.values()].sort((a,b)=>(timeValue(b.timestamp)-timeValue(a.timestamp))||b.id-a.id).slice(0,30);
  renderEvents();
}

function scheduleEventHistory(){
  if(eventHistoryTimer!==null)return;
  eventHistoryTimer=setTimeout(async()=>{
    eventHistoryTimer=null;
    try{
      await loadEvents();
    }catch{/* The independent refresh will retry. */}
  },150);
}

function renderEvents(){
  const empty=eventsLoaded?'Sem eventos registrados':apiState==='loading'?'Carregando eventos…':'Histórico indisponível';
  eventsNode.innerHTML=events.length?events.map(event=>`<tr><td>${timestamp(event.timestamp)}</td><td>${esc(event.collar_id)}</td><td>${esc(eventText(event))}</td></tr>`).join(''):
    `<tr class="empty-row"><td colspan="3">${empty}</td></tr>`;
}

function initMap(){
  if(!window.L){document.querySelector('#map-state').textContent='Base indisponível';return;}
  map=L.map('map',{zoomControl:true}).setView([-31.3061,-54.0639],15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
}

function operationalBounds(){
  const positions=collars.map(unitState).filter(unit=>unit.current&&unit.hasFix)
    .map(({sample})=>[sample.lat,sample.lon]);
  (fence?.points||[]).forEach(point=>positions.push([point.latitude,point.longitude]));
  return positions;
}

function fitOperationalBounds(){
  if(!map)return;
  const positions=operationalBounds();
  if(positions.length){
    map.fitBounds(positions,{padding:[34,34],maxZoom:17});
    fitted=true;
  }
}

function drawFence(){
  if(!map)return;
  if(fenceLayer){fenceLayer.remove();fenceLayer=null;}
  if(fenceVertices){fenceVertices.remove();fenceVertices=null;}
  const points=(fence?.points||[]).map(point=>[point.latitude,point.longitude]);
  if(points.length<3){fitOperationalBounds();return;}
  fenceLayer=L.polygon(points,{color:'#245c4f',weight:2,opacity:.9,fillColor:'#35745c',fillOpacity:.12})
    .addTo(map).bindTooltip(`Cerca v${fence.downloaded_version}`,{sticky:true});
  fenceVertices=L.layerGroup(points.map((position,index)=>L.circleMarker(position,{
    radius:3,weight:1.5,color:'#245c4f',fillColor:'#fff',fillOpacity:1,
  }).bindTooltip(`V${index+1}`,{direction:'top'}))).addTo(map);
  fitOperationalBounds();
}

function renderMap(){
  if(!map)return;
  const visible=new Set();
  collars.forEach(collar=>{
    const {sample,current,hasFix,zone}=unitState(collar);
    if(!current||!hasFix)return;
    visible.add(collar.collar_id);
    const position=[sample.lat,sample.lon];
    let marker=markers.get(collar.collar_id);
    const color=sample.fence_version===0?zoneColors.GNSS_INVALIDO:zoneColors[zone]||zoneColors.GNSS_INVALIDO;
    if(!marker){
      const label=document.createElement('span');
      label.textContent=collar.collar_id;
      marker=L.circleMarker(position,{radius:7,color,weight:2,fillOpacity:.85}).addTo(map).bindTooltip(label);
      markers.set(collar.collar_id,marker);
    }else{marker.setLatLng(position);marker.setStyle({color});}
  });
  for(const [id,marker] of markers){
    if(!visible.has(id)){marker.remove();markers.delete(id);}
  }
  if(!fitted)fitOperationalBounds();
}

async function fetchJSON(path){
  const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(8000)});
  if(!response.ok)throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function applyResource(name,data){
  if(!data||typeof data!=='object'||Array.isArray(data))return;
  if(name==='gateway'){
    if(timeValue(data.as_of)<timeValue(gateway?.as_of))return;
    gateway=data;
    renderGateway(gateway);
  }else if(name==='central'){
    central=data;
    renderCentral(central);
  }else if(name==='fence'){
    if(timeValue(data.updated_at)<timeValue(fence?.updated_at))return;
    if(JSON.stringify(data)===JSON.stringify(fence))return;
    renderFence(data);
  }
}

async function loadResource(name,path){
  const request=(resourceRequests.get(name)||0)+1;
  const revision=resourceRevisions.get(name);
  resourceRequests.set(name,request);
  const data=await fetchJSON(path);
  if(resourceRequests.get(name)===request&&resourceRevisions.get(name)===revision)applyResource(name,data);
}

async function loadCollars(){
  const request=++collarRequest, revision=collarRevision;
  const rows=await fetchJSON('/api/collars');
  if(!Array.isArray(rows))throw new Error('Resposta de unidades inválida');
  if(request!==collarRequest)return;
  const byId=new Map(rows.map(collar=>[collar.collar_id,collar]));
  for(const current of collars){
    const row=byId.get(current.collar_id);
    if((collarChanges.get(current.collar_id)||0)>revision||
        timeValue(current.position?.at)>timeValue(row?.position?.at))byId.set(current.collar_id,current);
  }
  collars=[...byId.values()].sort((a,b)=>a.collar_id.localeCompare(b.collar_id));
  collarsLoaded=true;
  if(selectedCollar&&!byId.has(selectedCollar)){
    selectedCollar=null;
    metricsRequest++;
  }
  renderCollars();
}

async function loadEvents(){
  const rows=await fetchJSON('/api/events?limit=30');
  if(!Array.isArray(rows))throw new Error('Resposta de eventos inválida');
  eventsLoaded=true;
  updateEvents(rows);
}

function scheduleCollars(){
  if(collarTimer!==null)return;
  collarTimer=setTimeout(()=>{
    collarTimer=null;
    loadCollars().catch(()=>{});
  },150);
}

async function loadMetrics(){
  const id=selectedCollar, request=++metricsRequest;
  if(!id)return;
  const numeric=Number(id.slice(3));
  if(!/^COL\d+$/.test(id)||numeric<1||numeric>254||id!==`COL${String(numeric).padStart(2,'0')}`){
    metricsState='unsupported';
    renderCollars();
    return;
  }
  try{
    const snapshot=await fetchJSON(`/api/collars/${numeric}/link-metrics?limit=1`);
    if(request!==metricsRequest||selectedCollar!==id)return;
    if(snapshot?.collar_id!==numeric||!snapshot.windows)throw new Error('Resposta de métricas inválida');
    metrics=snapshot;
    metricsState='ready';
  }catch{
    if(request!==metricsRequest||selectedCollar!==id)return;
    metricsState='error';
  }
  renderCollars();
}

function scheduleMetrics(){
  if(!selectedCollar||metricsTimer!==null)return;
  metricsTimer=setTimeout(()=>{
    metricsTimer=null;
    loadMetrics();
  },150);
}

function renderConnection(){
  const status=document.querySelector('#connection');
  status.className=`connection${live&&apiState==='ready'?' live':''}`;
  const label=!live?'Reconectando':apiState==='loading'?'Atualizando':apiState==='error'?
    'Atualização parcial':'Tempo real ativo';
  status.innerHTML=`<i></i> ${label}`;
}

async function load(){
  const request=++loadRequest;
  const results=await Promise.allSettled([
    loadResource('gateway','/api/gateway'),
    loadResource('central','/api/central'),
    loadResource('fence','/api/fence-state'),
    loadCollars(),loadEvents(),
  ]);
  if(request!==loadRequest)return;
  apiState=results.every(result=>result.status==='fulfilled')?'ready':'error';
  renderConnection();
  renderCollars();
  renderEvents();
}

function acceptUpdate(name,data){
  resourceRevisions.set(name,(resourceRevisions.get(name)||0)+1);
  applyResource(name,data);
}

function connect(){
  const protocol=location.protocol==='https:'?'wss':'ws';
  const socket=new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onopen=()=>{
    live=true;
    apiState='loading';
    renderConnection();
    load();
    scheduleMetrics();
  };
  socket.onmessage=({data})=>{
    let message;
    try{message=JSON.parse(data);}catch{return;}
    if(['gateway','central','fence'].includes(message?.type)){
      acceptUpdate(message.type,message.data);
      if(message.type==='gateway')renderCollars();
    }
    if(message?.type==='gateway_event'){
      const kind=message.data?.kind, subtype=String(message.data?.subtype||message.data?.event||'').toUpperCase();
      if(['event','collar_state','offline'].includes(kind))scheduleEventHistory();
      if(['collar_state','collar_status','offline'].includes(kind)||['GPS_LOST','GPS_RECOVERED'].includes(subtype))scheduleCollars();
    }
    if(message?.type==='metric_window'&&message.data?.collar_id===Number(selectedCollar?.replace(/^COL\s*/i,'')))scheduleMetrics();
    if(message?.type==='telemetry'&&typeof message.collar?.collar_id==='string'){
      if(message.gateway)acceptUpdate('gateway',message.gateway);
      const index=collars.findIndex(collar=>collar.collar_id===message.collar.collar_id);
      if(index<0||!(timeValue(message.collar.position?.at)<timeValue(collars[index].position?.at))){
        collarChanges.set(message.collar.collar_id,++collarRevision);
        if(index<0)collars.push(message.collar);else collars[index]=message.collar;
      }
      if(message.event)updateEvents([message.event]);
      renderCollars();
    }
  };
  socket.onclose=()=>{live=false;renderConnection();load();setTimeout(connect,2000);};
}

collarsNode.addEventListener('click',event=>{
  const id=event.target.closest('[data-unit]')?.dataset.unit;
  if(!id)return;
  selectedCollar=selectedCollar===id?null:id;
  metrics=null;
  metricsState='loading';
  renderCollars();
  loadMetrics();
});

initMap();load();connect();
setInterval(()=>{
  renderGateway(gateway);
  renderCollars();
},5000);
setInterval(()=>{
  load();
  scheduleMetrics();
},15000);
