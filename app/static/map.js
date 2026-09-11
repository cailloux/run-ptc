const map = L.map('map', { preferCanvas: true }).setView([33.39, -84.57], 13);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
  maxNativeZoom: 16,
  maxZoom: 20,
}).addTo(map);

const METERS_PER_MILE = 1609.344;
const NODE_MIN_ZOOM = 15;
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

function formatEastern(iso, opts = { dateStyle: 'medium', timeStyle: 'short' }) {
  return new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', ...opts });
}

function intervalsLink(intervalsId) {
  const url = `https://intervals.icu/activities/${encodeURIComponent(intervalsId)}`;
  return `<a href="${url}" target="_blank" rel="noopener">Open in Intervals</a>`;
}

async function getJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: ${resp.status}`);
  return resp.json();
}

const overlays = {};
const cartpathById = new Map();
const nodeLayers = [];

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

// ---- nodes --------------------------------------------------------------

const NODE_STYLE = {
  hit: { radius: 2, weight: 0, fillColor: '#333', fillOpacity: 0.75 },
  missed: { radius: 3.5, weight: 1, color: '#fff', fillColor: '#d7301f', fillOpacity: 1 },
};

async function nodePopup(id) {
  const d = await getJson(`nodes/${id}`);
  if (!d.hit) return `Missed · radius ${d.radius_m} m`;
  const date = formatEastern(d.hit.start_at, { dateStyle: 'medium' });
  return `${escapeHtml(date)} · ${escapeHtml(d.hit.name ?? '')}<br>${intervalsLink(d.hit.intervals_id)}`;
}

// Nodes share the segments' canvas so they can be clicked. The layer-control
// entry toggles a wrapper group; the nodes inside it appear only when zoomed
// in far enough, so the checkbox always reflects what you asked for.
function zoomGated(inner) {
  const wrapper = L.layerGroup();
  const update = () => {
    const show = map.getZoom() >= NODE_MIN_ZOOM;
    if (show && !wrapper.hasLayer(inner)) {
      wrapper.addLayer(inner);
      inner.bringToFront();
    } else if (!show && wrapper.hasLayer(inner)) {
      wrapper.removeLayer(inner);
    }
  };
  wrapper.on('add', update);
  map.on('zoomend', update);
  return wrapper;
}

async function loadNodes(layer, label, show) {
  const inner = L.geoJSON(await getJson(`nodes?layer=${layer}`), {
    pointToLayer: (f, latlng) => L.circleMarker(latlng, NODE_STYLE[f.properties.hit ? 'hit' : 'missed']),
    onEachFeature: (f, l) => {
      l.bindPopup('Loading…');
      l.on('popupopen', (e) => nodePopup(f.properties.id)
        .then((html) => e.popup.setContent(html))
        .catch((err) => e.popup.setContent(escapeHtml(err.message))));
    },
  });
  nodeLayers.push(inner);
  const gated = zoomGated(inner);
  overlays[label] = gated;
  if (show) gated.addTo(map);
}

// Canvas draws in the order layers were added, so keep nodes on top of
// anything toggled on later.
function raiseNodes() {
  nodeLayers.forEach((l) => { if (map.hasLayer(l)) l.bringToFront(); });
}
map.on('overlayadd', raiseNodes);

// ---- run tracks ---------------------------------------------------------

function trackPopup(f) {
  const p = f.properties;
  return `<b>${escapeHtml(formatEastern(p.start_at))}</b><br>`
    + `${escapeHtml(p.name ?? '')}<br>`
    + `${(p.distance_m / METERS_PER_MILE).toFixed(2)} mi · ${p.parts} part${p.parts === 1 ? '' : 's'}<br>`
    + intervalsLink(p.intervals_id);
}

// Run tracks are off by default and fetched the first time they're shown.
const tracks = L.geoJSON(null, {
  style: { color: '#e6550d', weight: 2, opacity: 0.5 },
  onEachFeature: (f, l) => l.bindPopup(() => trackPopup(f)),
});
let tracksRequested = false;
tracks.on('add', () => {
  if (tracksRequested) return;
  tracksRequested = true;
  getJson('activities')
    .then((fc) => { tracks.addData(fc); raiseNodes(); })
    .catch((err) => alert(err.message));
});

// ---- panel --------------------------------------------------------------

async function loadStats() {
  const s = await getJson('stats');
  const c = s.completion;
  const row = (label, v) => v
    ? `<tr><td>${label}</td><td>${v.counted}</td><td>${v.counted_mi} mi</td><td>${v.nodes}</td></tr>`
    : '';
  const latest = s.runs.latest_start_at
    ? ` (latest ${escapeHtml(formatEastern(s.runs.latest_start_at, { dateStyle: 'medium' }))})`
    : '';
  document.getElementById('completion').innerHTML =
    `<div class="headline">${c.cartpath_complete_mi} of ${c.cartpath_total_mi} cart path mi `
    + `<span class="pct">${c.cartpath_pct}%</span></div>`
    + `<div>${c.segments_complete} of ${c.segments_total} segments complete</div>`
    + `<div>${c.road_covered_mi} of ${c.road_total_mi} road mi covered</div>`;
  document.getElementById('stats').innerHTML =
    '<tr><th></th><th>Counted</th><th>Miles</th><th>Nodes</th></tr>'
    + row('Cart paths', s.cartpath) + row('Roads', s.road)
    + `<tr><td colspan="4">${s.runs.city} runs in the city${latest}</td></tr>`;
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
  overlays['Run tracks'] = tracks;
  L.control.layers(null, overlays, { collapsed: false, position: 'bottomright' }).addTo(map);
  // Link to a feature with #oid=12062; add &tracks to show run tracks.
  const linked = location.hash.match(/oid=(\d+)/);
  if (linked) findCartpath(Number(linked[1]));
  if (/\btracks\b/.test(location.hash)) tracks.addTo(map);
})().catch((err) => alert(err.message));
