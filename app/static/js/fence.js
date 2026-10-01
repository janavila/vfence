const MAX_VERTICES = 32;
const vertices = [];
const markerLayers = [];
let map = null;
let polygon = null;
let guideLine = null;

const verticesNode = document.querySelector('#vertices');
const countNode = document.querySelector('#vertex-count');
const stateNode = document.querySelector('#fence-state');
const vectorNode = document.querySelector('#vector-output');
const undoButton = document.querySelector('#undo-point');
const clearButton = document.querySelector('#clear-fence');
const copyButton = document.querySelector('#copy-vector');

function encodeE7(value) {
  return Math.sign(value) * Math.round(Math.abs(value) * 10000000);
}

function vectorData() {
  return vertices.map((point, index) => ({
    index,
    latitude: Number(point.lat.toFixed(7)),
    longitude: Number(point.lng.toFixed(7)),
    latitude_e7: encodeE7(point.lat),
    longitude_e7: encodeE7(point.lng),
  }));
}

function markerIcon(index) {
  return L.divIcon({
    className: 'fence-marker',
    html: String(index + 1),
    iconSize: [24, 24],
  });
}

function rebuildGeometry() {
  markerLayers.forEach(marker => marker.remove());
  markerLayers.length = 0;
  if (polygon) { polygon.remove(); polygon = null; }
  if (guideLine) { guideLine.remove(); guideLine = null; }

  vertices.forEach((point, index) => {
    markerLayers.push(L.marker(point, {icon: markerIcon(index), interactive: false}).addTo(map));
  });

  if (vertices.length >= 3) {
    polygon = L.polygon(vertices, {
      color: '#245c4f', weight: 2, opacity: .9,
      fillColor: '#245c4f', fillOpacity: .12,
    }).addTo(map);
  } else if (vertices.length === 2) {
    guideLine = L.polyline(vertices, {color: '#245c4f', weight: 2, dashArray: '5 5'}).addTo(map);
  }
}

function render() {
  const data = vectorData();
  countNode.textContent = `${vertices.length} / ${MAX_VERTICES}`;
  undoButton.disabled = vertices.length === 0;
  clearButton.disabled = vertices.length === 0;
  copyButton.disabled = vertices.length === 0;

  if (vertices.length >= 3) {
    stateNode.className = 'fence-state valid';
    stateNode.innerHTML = '<i></i><div><strong>Perímetro definido</strong><span>Vetor pronto para validação futura.</span></div>';
  } else {
    const remaining = 3 - vertices.length;
    stateNode.className = 'fence-state pending';
    stateNode.innerHTML = `<i></i><div><strong>Aguardando pontos</strong><span>Adicione mais ${remaining} ${remaining === 1 ? 'vértice' : 'vértices'}.</span></div>`;
  }

  verticesNode.innerHTML = data.length ? data.map(point => `
    <tr>
      <td>${point.index + 1}</td>
      <td>${point.latitude.toFixed(7)}</td>
      <td>${point.longitude.toFixed(7)}</td>
      <td><button class="remove-point" type="button" data-index="${point.index}" aria-label="Remover ponto ${point.index + 1}">×</button></td>
    </tr>`).join('') : '<tr class="empty-row"><td colspan="4">Nenhum vértice definido</td></tr>';

  vectorNode.textContent = JSON.stringify(data.map(point => [point.latitude_e7, point.longitude_e7]), null, 2);
  if (map) rebuildGeometry();
}

function addPoint(latlng) {
  if (vertices.length >= MAX_VERTICES) return;
  vertices.push({lat: latlng.lat, lng: latlng.lng});
  render();
}

function initMap() {
  if (!window.L) {
    document.querySelector('#map-coordinate').textContent = 'Base indisponível';
    return;
  }
  map = L.map('fence-map', {zoomControl: true}).setView([-31.31328, -54.08682], 17);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 20,
    attribution: '© OpenStreetMap',
  }).addTo(map);
  map.on('mousemove', event => {
    document.querySelector('#map-coordinate').textContent = `${event.latlng.lat.toFixed(6)}, ${event.latlng.lng.toFixed(6)}`;
  });
  map.on('click', event => addPoint(event.latlng));
}

undoButton.addEventListener('click', () => {
  vertices.pop();
  render();
});

clearButton.addEventListener('click', () => {
  vertices.length = 0;
  render();
});

verticesNode.addEventListener('click', event => {
  const button = event.target.closest('[data-index]');
  if (!button) return;
  vertices.splice(Number(button.dataset.index), 1);
  render();
});

copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(vectorNode.textContent);
    copyButton.textContent = 'Copiado';
    setTimeout(() => { copyButton.textContent = 'Copiar'; }, 1200);
  } catch {
    copyButton.textContent = 'Indisponível';
  }
});

initMap();
render();
