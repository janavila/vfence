/* ==========================================================================
   map.js — o mapa Leaflet e suas camadas. O desenho da cerca fica no
   `editor.js`; aqui é só o mapa, para o Rebanho também reusar.

   Duas camadas: OpenStreetMap (estradas, açudes, construções) e Esri
   World Imagery (o terreno de verdade, que o produtor reconhece ao
   marcar divisa). Nenhuma exige chave de API (DEC-05) — chave significa
   cadastro, cota e conta para alguém administrar.

   As IMAGENS do mapa precisam de internet; o Leaflet não, porque vem do
   próprio Central. Sem internet o mapa fica cinza, mas marcar pontos,
   validar e enviar continua funcionando (mapa offline é COULD).

   ATENÇÃO à ordem das coordenadas: Leaflet usa `[lat, lng]`, GeoJSON usa
   `[lon, lat]`, e trocar os dois é um dos riscos do planejamento. Aqui
   só existe a ordem do Leaflet; a conversão para GeoJSON acontece em um
   lugar só do servidor (`services/fence_log.py`), com teste.
   ========================================================================== */

export const CAMADAS = {
  ruas: {
    nome: 'Mapa de ruas',
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    atribuicao: '© OpenStreetMap contributors',
    zoomMaximo: 19,
  },
  satelite: {
    nome: 'Satélite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    atribuicao: 'Esri',
    zoomMaximo: 19,
  },
};

/**
 * Cria o mapa centralizado na Base. Zoom 17 é o padrão da seção 11.3:
 * dá para ver um piquete inteiro. Devolve null se o Leaflet não carregou.
 * A Base é só o ponto de partida: `mostrarLocalizacao` leva o mapa até
 * onde o produtor está.
 */
export function criarMapa(elementoId, config, zoom = 17) {
  const elemento = document.getElementById(elementoId);
  if (!elemento) return null;

  if (!window.L) {
    // O Leaflet é servido localmente, então isto quase nunca acontece.
    // Mas se acontecer, a tela precisa dizer o que é — e não ficar
    // mostrando um retângulo cinza sem explicação.
    elemento.innerHTML =
      '<div class="map-fallback"><strong>Mapa indisponível</strong>' +
      '<span>O arquivo do mapa não carregou. Recarregue a página.</span></div>';
    return null;
  }

  const mapa = L.map(elementoId, { zoomControl: true, attributionControl: true })
    .setView([config.base_lat, config.base_lon], zoom);

  const ruas = L.tileLayer(CAMADAS.ruas.url, {
    maxZoom: CAMADAS.ruas.zoomMaximo,
    attribution: CAMADAS.ruas.atribuicao,
  });
  const satelite = L.tileLayer(CAMADAS.satelite.url, {
    maxZoom: CAMADAS.satelite.zoomMaximo,
    attribution: CAMADAS.satelite.atribuicao,
  });

  // Satélite primeiro: é o que o produtor reconhece ao marcar divisa.
  satelite.addTo(mapa);
  L.control.layers(
    { [CAMADAS.satelite.nome]: satelite, [CAMADAS.ruas.nome]: ruas },
    {},
    { position: 'topright' },
  ).addTo(mapa);

  return mapa;
}

/**
 * Marca a Base: o produtor precisa saber de onde parte o rádio para
 * entender os avisos das regras VAL-03 e VAL-10.
 */
export function marcarBase(mapa, config) {
  if (!mapa) return null;
  const marcador = L.circleMarker([config.base_lat, config.base_lon], {
    radius: 7,
    color: '#245c4f',
    weight: 3,
    fillColor: '#ffffff',
    fillOpacity: 1,
    interactive: true,
  }).addTo(mapa);
  marcador.bindTooltip(`Base ${config.base_id}`, { direction: 'top' });
  return marcador;
}

/**
 * Círculo do alcance de rádio medido na IP1 — a regra VAL-10 em forma de
 * desenho, para o produtor ver antes de marcar.
 */
export function desenharAlcanceDoRadio(mapa, config) {
  if (!mapa) return null;
  return L.circle([config.base_lat, config.base_lon], {
    radius: config.lora_range_m,
    color: '#8a6410',
    weight: 1,
    dashArray: '6 6',
    fill: false,
    interactive: false,
  }).addTo(mapa);
}

/* Os movimentos automáticos (enquadrar, ir até o produtor) não animam:
   durante uma animação de zoom o Leaflet ignora o próximo `setView`, e
   aí a localização e o enquadramento disputariam quem chega primeiro. */
const SEM_ANIMACAO = { animate: false };

/**
 * Mostra onde o produtor está (ponto azul e a precisão do GPS em volta)
 * e centraliza o mapa ali na primeira posição — em vez de deixá-lo
 * sempre no ponto fixo da Base.
 *
 * `watch` segue a pessoa: quem caminha pela divisa com o celular se vê
 * andar no mapa. Só centraliza se `podeCentralizar()` deixar: mover o
 * mapa no meio do desenho faria o produtor errar o clique. Os círculos
 * não recebem clique, para marcar um ponto bem onde se está.
 *
 * O navegador só dá a posição com permissão e em HTTPS ou `localhost`;
 * pelo IP da rede local, sem HTTPS, ela é recusada e o mapa fica na Base.
 */
export function mostrarLocalizacao(mapa, podeCentralizar) {
  if (!mapa) return;
  const nota = (texto) => {
    const elemento = document.getElementById('location-note');
    if (elemento) elemento.textContent = texto;
  };
  let ponto = null;
  let precisao = null;

  mapa.on('locationfound', ({ latlng, accuracy }) => {
    nota('');
    if (!ponto) {
      precisao = L.circle(latlng, {
        color: '#2f6db5', weight: 1, fillOpacity: 0.1, interactive: false,
      }).addTo(mapa);
      ponto = L.circleMarker(latlng, {
        radius: 8, color: '#fff', weight: 3, fillColor: '#2f6db5', fillOpacity: 1, interactive: false,
      }).addTo(mapa);
      if (podeCentralizar()) mapa.setView(latlng, 17, SEM_ANIMACAO);
    }
    ponto.setLatLng(latlng);
    precisao.setLatLng(latlng).setRadius(accuracy);
  });
  mapa.on('locationerror', () => nota('Sem acesso à sua localização: o mapa mostra a Base.'));
  mapa.locate({ watch: true, enableHighAccuracy: true });
}

/** Converte os pontos do nosso formato `{lat, lon}` para o do Leaflet. */
export function paraLeaflet(pontos) {
  return pontos.map((p) => [p.lat, p.lon]);
}

/** Enquadra o mapa em um conjunto de pontos, com folga nas bordas. */
export function enquadrar(mapa, pontos, zoomMaximo = 18) {
  if (!mapa || pontos.length === 0) return;
  if (pontos.length === 1) {
    mapa.setView([pontos[0].lat, pontos[0].lon], zoomMaximo, SEM_ANIMACAO);
    return;
  }
  mapa.fitBounds(paraLeaflet(pontos), { ...SEM_ANIMACAO, padding: [40, 40], maxZoom: zoomMaximo });
}
