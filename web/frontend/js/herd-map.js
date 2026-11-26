import { criarMapa } from './map.js';

const statuses = new Set(['inside', 'near', 'outside', 'unknown']);

export function herdMap(id, onSelect, config = null) {
  let map;
  let fenceLayer;
  let signature;
  let framed = false;
  const markers = new Map();

  function update(animals, fences) {
    const current = animals.filter(animal => animal.position?.current);
    const bounds = current.map(a => [a.position.lat, a.position.lon]);
    for (const fence of fences) for (const p of fence.points) bounds.push([p.lat, p.lon]);
    if (!map && (bounds.length || config)) {
      const center = config || { base_lat: bounds[0][0], base_lon: bounds[0][1] };
      map = criarMapa(id, center, 16);
      if (!map) return;
      fenceLayer = L.layerGroup().addTo(map);
    }
    if (!map) return;
    const nextFence = JSON.stringify(fences.map(fence => fence.points));
    if (signature !== nextFence) {
      signature = nextFence;
      fenceLayer.clearLayers();
      for (const fence of fences) {
        L.polygon(fence.points.map(p => [p.lat, p.lon]), {
          color: '#245c4f', weight: 2, fillColor: '#35745c', fillOpacity: .12, interactive: false,
        }).addTo(fenceLayer);
      }
    }
    const live = new Set();
    for (const animal of current) {
      live.add(animal.key);
      const point = [animal.position.lat, animal.position.lon];
      const status = statuses.has(animal.status) ? animal.status : 'unknown';
      const previous = markers.get(animal.key);
      let marker = previous?.marker;
      const icon = () => L.divIcon({ className: 'herd-pin pin-' + status, iconSize: [16, 16], iconAnchor: [8, 8] });
      if (!marker) {
        marker = L.marker(point, { icon: icon(), title: animal.name, alt: animal.name }).addTo(map);
        marker.on('click', () => onSelect(animal.key));
      } else {
        marker.setLatLng(point);
        if (previous.status !== status) marker.setIcon(icon());
      }
      if (!previous || previous.name !== animal.name) {
        const label = document.createElement('span');
        label.textContent = animal.name;
        marker.unbindTooltip().bindTooltip(label, { direction: 'top' });
      }
      marker.getElement()?.setAttribute('aria-label', animal.name);
      markers.set(animal.key, { marker, status, name: animal.name });
    }
    for (const [key, entry] of markers) {
      if (!live.has(key)) {
        entry.marker.remove();
        markers.delete(key);
      }
    }
    if (!framed && bounds.length) {
      map.fitBounds(bounds, { padding: [32, 32], maxZoom: 17, animate: false });
      framed = true;
    }
  }
  return {
    update,
    focus(key) {
      const marker = markers.get(key)?.marker;
      if (marker) {
        map.panTo(marker.getLatLng(), { animate: false });
        marker.openTooltip();
      }
    },
    instance: () => map,
  };
}
