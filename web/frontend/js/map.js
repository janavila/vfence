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

/** Converte os pontos do nosso formato `{lat, lon}` para o do Leaflet. */
export function paraLeaflet(pontos) {
  return pontos.map((p) => [p.lat, p.lon]);
}

/** Enquadra o mapa em um conjunto de pontos, com folga nas bordas. */
export function enquadrar(mapa, pontos, zoomMaximo = 18) {
  if (!mapa || pontos.length === 0) return;
  if (pontos.length === 1) {
    mapa.setView([pontos[0].lat, pontos[0].lon], zoomMaximo);
    return;
  }
  mapa.fitBounds(paraLeaflet(pontos), { padding: [40, 40], maxZoom: zoomMaximo });
}
