const map = L.map('map', { preferCanvas: true }).setView([33.39, -84.57], 13);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
  maxNativeZoom: 16,
  maxZoom: 20,
}).addTo(map);

// Nodes get their own pane, hidden when zoomed out so they don't bury the lines.
// The pane ignores the mouse so clicks reach the segment canvas underneath.
const NODE_MIN_ZOOM = 15;
map.createPane('nodes').style.pointerEvents = 'none';
const nodeRenderer = L.canvas({ pane: 'nodes' });
function updateNodePane() {
  map.getPane('nodes').style.display = map.getZoom() >= NODE_MIN_ZOOM ? '' : 'none';
}
map.on('zoomend', updateNodePane);
updateNodePane();

const COLORS = { cartpath: '#1a9850', road: '#3b6fb6' };

function segmentStyle(layer) {
  return (f) => {
    const p = f.properties;
    if (p.excluded) return { color: '#777', weight: 3, dashArray: '4 6' };
    if (!p.counted) return { color: '#aaa', weight: 1.5 };
    return { color: COLORS[layer], weight: layer === 'cartpath' ? 3 : 2.5 };
  };
}

function segmentPopup(f) {
  const p = f.properties;
  const rows = [
    ['OID', p.source_oid],
    ['Key', p.source_key],
    ['Name', p.name ?? ''],
    ['Type', p.seg_type ?? ''],
    ['Length', `${p.length_m} m`],
    ['Parts', p.parts],
    ['Counted', p.counted ? 'yes' : 'no'],
  ];
  if (p.excluded) rows.push(['Excluded', p.exclusion_reason]);
  return rows.map(([k, v]) => `<b>${k}</b> ${escapeHtml(String(v))}`).join('<br>');
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

async function getJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: ${resp.status}`);
  return resp.json();
}

const overlays = {};
const cartpathById = new Map();

async function loadNetwork(layer, label, show) {
  const group = L.geoJSON(await getJson(`network?layer=${layer}`), {
    style: segmentStyle(layer),
    onEachFeature: (f, l) => {
      l.bindPopup(() => segmentPopup(f));
      if (layer === 'cartpath') cartpathById.set(f.properties.source_oid, l);
    },
  });
  overlays[label] = group;
  if (show) group.addTo(map);
  return group;
}

async function loadNodes(layer, label, show) {
  const group = L.geoJSON(await getJson(`nodes?layer=${layer}`), {
    pointToLayer: (f, latlng) => L.circleMarker(latlng, {
      renderer: nodeRenderer, interactive: false,
      radius: 2.5, weight: 1, color: '#333', fillColor: '#fff', fillOpacity: 1,
    }),
  });
  overlays[label] = group;
  if (show) group.addTo(map);
}

async function loadStats() {
  const s = await getJson('stats');
  const row = (label, v) => v
    ? `<tr><td>${label}</td><td>${v.counted}</td><td>${v.counted_mi} mi</td><td>${v.nodes}</td></tr>`
    : '';
  document.getElementById('stats').innerHTML =
    '<tr><th></th><th>Counted</th><th>Miles</th><th>Nodes</th></tr>'
    + row('Cart paths', s.cartpath) + row('Roads', s.road);
}

function findCartpath(oid) {
  const l = cartpathById.get(oid);
  if (!l) return alert('No cart path with that OBJECTID_1');
  map.fitBounds(l.getBounds(), { maxZoom: 19, padding: [40, 40] });
  l.openPopup(l.getBounds().getCenter());
}

document.getElementById('find').addEventListener('submit', (e) => {
  e.preventDefault();
  findCartpath(Number(document.getElementById('find-oid').value));
});

(async () => {
  const [, cartpaths] = await Promise.all([
    loadNetwork('road', 'Roads', true),
    loadNetwork('cartpath', 'Cart paths', true),
    loadStats(),
  ]);
  cartpaths.bringToFront();
  if (cartpaths.getLayers().length) map.fitBounds(cartpaths.getBounds());
  await Promise.all([
    loadNodes('cartpath', 'Cart path nodes', true),
    loadNodes('road', 'Road nodes', false),
  ]);
  L.control.layers(null, overlays, { collapsed: false, position: 'bottomright' }).addTo(map);
  // Link to a feature with #oid=12062.
  const linked = location.hash.match(/oid=(\d+)/);
  if (linked) findCartpath(Number(linked[1]));
})().catch((err) => alert(err.message));
