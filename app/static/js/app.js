const collarsNode=document.querySelector('#collars'),eventsNode=document.querySelector('#events');
let collars=[],events=[],map=null;
const zoneColors={SEGURO:'#35745c',ATENCAO:'#946e22',CRITICO:'#a0643f',FORA:'#a34f4f',GNSS_INVALIDO:'#69736f'};
const zoneLabels={SEGURO:'NORMAL',ATENCAO:'ATENÇÃO',CRITICO:'CRÍTICO',FORA:'FORA DA ÁREA',GNSS_INVALIDO:'GNSS INVÁLIDO'};
const markers=new Map();
const esc=value=>String(value??'—').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const number=(value,digits=2)=>value==null?'—':Number(value).toFixed(digits);
const timestamp=value=>value?new Date(value).toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';

function renderGateway(gateway){
  const online=gateway.status==='online';
  const status=document.querySelector('#gateway-status');
  status.textContent=online?'Online':'Offline';
  status.className=`status-pill ${online?'online':'offline'}`;
  document.querySelector('#serial-port').textContent=gateway.serial_port||'—';
  document.querySelector('#last-message').textContent=gateway.last_message||'Sem dados';
}

function renderCollars(){
  document.querySelector('#collar-count').textContent=String(collars.length).padStart(2,'0');
  const alerts=collars.filter(collar=>collar.zone==='ATENCAO'||collar.zone==='CRITICO').length;
  const outside=collars.filter(collar=>collar.zone==='FORA').length;
  document.querySelector('#alert-count').textContent=String(alerts).padStart(2,'0');
  document.querySelector('#outside-count').textContent=String(outside).padStart(2,'0');
  if(!collars.length){
    collarsNode.innerHTML='<tr class="empty-row"><td colspan="10">Sem telemetria disponível</td></tr>';
    return;
  }
  collarsNode.innerHTML=collars.map(collar=>{
    const zone=collar.zone||'GNSS_INVALIDO';
    return `<tr>
      <td class="unit-id">${esc(collar.collar_id)}</td>
      <td><span class="zone-badge" style="--zone:${zoneColors[zone]||zoneColors.GNSS_INVALIDO}">${esc(zoneLabels[zone]||zone)}</span></td>
      <td>${number(collar.latitude,6)}</td><td>${number(collar.longitude,6)}</td>
      <td>${esc(collar.satellites)}</td><td>${number(collar.hdop)}</td>
      <td>${collar.rssi==null?'—':`${number(collar.rssi,0)} dBm`}</td>
      <td>${collar.snr==null?'—':number(collar.snr)}</td>
      <td>${esc(collar.last_sequence)}</td><td class="cell-muted">${timestamp(collar.last_seen)}</td>
    </tr>`;
  }).join('');
  document.querySelector('#updated-at').textContent=`Atualizado ${new Date().toLocaleTimeString('pt-BR')}`;
  renderMap();
}

function eventText(event){
  const next=zoneLabels[event.new_value]||event.new_value;
  const previous=zoneLabels[event.old_value]||event.old_value;
  return event.old_value==null?`ZONA INICIAL: ${next}`:`${previous} → ${next}`;
}

function renderEvents(){
  eventsNode.innerHTML=events.length?events.map(event=>`<tr><td>${timestamp(event.timestamp)}</td><td>${esc(event.collar_id)}</td><td>${esc(eventText(event))}</td></tr>`).join(''):'<tr class="empty-row"><td colspan="3">Sem eventos registrados</td></tr>';
}

function initMap(){
  if(!window.L){document.querySelector('#map-state').textContent='Base indisponível';return;}
  map=L.map('map',{zoomControl:true}).setView([-31.3061,-54.0639],15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
}

function renderMap(){
  if(!map)return;
  const located=collars.filter(collar=>collar.latitude!=null&&collar.longitude!=null);
  located.forEach(collar=>{
    const position=[collar.latitude,collar.longitude];
    let marker=markers.get(collar.collar_id);
    const color=zoneColors[collar.zone]||zoneColors.GNSS_INVALIDO;
    if(!marker){
      marker=L.circleMarker(position,{radius:7,color,weight:2,fillOpacity:.85}).addTo(map).bindTooltip(collar.collar_id);
      markers.set(collar.collar_id,marker);
    }else{marker.setLatLng(position);marker.setStyle({color});}
  });
  if(located.length)map.fitBounds(located.map(collar=>[collar.latitude,collar.longitude]),{padding:[34,34],maxZoom:16});
}

async function load(){
  try{
    const[gateway,collarList,eventList]=await Promise.all([
      fetch('/api/gateway').then(response=>response.json()),
      fetch('/api/collars').then(response=>response.json()),
      fetch('/api/events?limit=30').then(response=>response.json()),
    ]);
    renderGateway(gateway);collars=collarList;events=eventList;renderCollars();renderEvents();
  }catch{document.querySelector('#connection').textContent='LINK API INDISPONÍVEL';}
}

function connect(){
  const protocol=location.protocol==='https:'?'wss':'ws';
  const socket=new WebSocket(`${protocol}://${location.host}/ws`);
  const status=document.querySelector('#connection');
  socket.onopen=()=>{status.className='connection live';status.innerHTML='<i></i> Tempo real ativo';};
  socket.onmessage=({data})=>{
    const message=JSON.parse(data);
    if(message.type==='gateway')renderGateway(message.data);
    if(message.type==='telemetry'){
      const index=collars.findIndex(collar=>collar.collar_id===message.collar.collar_id);
      if(index<0)collars.push(message.collar);else collars[index]=message.collar;
      if(message.event)events.unshift(message.event);
      renderCollars();renderEvents();
    }
  };
  socket.onclose=()=>{status.className='connection';status.innerHTML='<i></i> Reconectando';setTimeout(connect,2000);};
}

initMap();load();connect();
