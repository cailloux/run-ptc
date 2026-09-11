const map = L.map('map', { preferCanvas: true }).setView([33.39, -84.57], 13);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
  maxNativeZoom: 16,
  maxZoom: 20,
}).addTo(map);

const METERS_PER_MILE = 1609.344;
const NODE_MIN_ZOOM = 15;
const SYNC_POLL_MS = 3000;

// Coverage colors, validated with the dataviz palette checker against the
// gray basemap: the three cart path hues are colorblind-safe as a set, and
// line weight separates complete from partial as a second cue.
const STATE_STYLE = {
  cartpath: {
    complete: { color: '#199e70', weight: 4 },
    run: { color: '#1c5cab', weight: 3 },
    not_run: { color: '#d95926', weight: 3 },
  },
  road: {
    run: { color: '#5598e7', weight: 2 },
    not_run: { color: '#8f8e89', weight: 1.5 },
  },
  excluded: { color: '#5f5e5a', weight: 2.5, dashArray: '4 6' },
  uncounted: { color: '#cac9c4', weight: 1.5 },
};
const STATE_LABEL = {
  complete: 'Complete', run: 'Run', not_run: 'Not run', excluded: 'Excluded', uncounted: 'Not counted',
};

function coverageStyle(layer) {
  return (f) => {
    const s = f.properties.state;
    return { opacity: 1, ...(STATE_STYLE[layer][s] ?? STATE_STYLE[s]) };
  };
}

function segmentPopup(f) {
  const p = f.properties;
  const rows = [
    ['OID', p.source_oid],
    ['Key', p.source_key],
    ['Name', p.name ?? ''],
    ['Type', p.seg_type ?? ''],
    ['Length', `${p.segment_length_m} m`],
    ['Parts', p.parts],
  ];
  if (p.nodes_total) rows.push(['Nodes hit', `${p.nodes_hit} of ${p.nodes_total}`]);
  if (p.state === 'excluded') rows.push(['Excluded', p.exclusion_reason]);
  const head = p.state === 'uncounted' || p.state === 'excluded' || p.state === 'complete'
    ? STATE_LABEL[p.state]
    : `${STATE_LABEL[p.state]} (this piece, ${p.length_m} m)`;
  return `<b>${escapeHtml(head)}</b><br>`
    + rows.map(([k, v]) => `<b>${k}</b> ${escapeHtml(String(v))}`).join('<br>');
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

async function getJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ?? `${url}: ${resp.status}`);
  }
  return resp.json();
}

const overlays = {};

// ---- coverage ---------------------------------------------------------------

// A segment can be several features (one per run of intervals), so find-by-OID
// keeps every piece.
const cartpathPieces = new Map();
const networks = {};

function makeNetwork(layer) {
  return L.geoJSON(null, {
    style: coverageStyle(layer),
    onEachFeature: (f, l) => {
      l.bindPopup(() => segmentPopup(f));
      if (layer !== 'cartpath') return;
      const oid = f.properties.source_oid;
      if (!cartpathPieces.has(oid)) cartpathPieces.set(oid, []);
      cartpathPieces.get(oid).push(l);
    },
  });
}

async function loadNetwork(layer) {
  const data = await getJson(`network?layer=${layer}`);
  if (layer === 'cartpath') cartpathPieces.clear();
  networks[layer].clearLayers();
  networks[layer].addData(data);
}

// ---- nodes --------------------------------------------------------------

const NODE_STYLE = {
  hit: { radius: 2, weight: 0, fillColor: '#333', fillOpacity: 0.75 },
  missed: { radius: 3.5, weight: 1.5, color: '#3a3936', fillColor: '#fff', fillOpacity: 1 },
};

async function nodePopup(id) {
  const d = await getJson(`nodes/${id}`);
  if (!d.hit) return `Missed · radius ${d.radius_m} m`;
  const date = formatEastern(d.hit.start_at, { dateStyle: 'medium' });
  return `${escapeHtml(date)} · ${escapeHtml(d.hit.name ?? '')}<br>${intervalsLink(d.hit.intervals_id)}`;
}

// Node layers share the segments' canvas so they're clickable. Each layer
// control entry toggles a wrapper; the nodes inside appear only when zoomed
// in, and their data loads the first time the layer is turned on.
function nodeLayer(urls) {
  const inner = L.geoJSON(null, {
    pointToLayer: (f, latlng) => L.circleMarker(latlng, NODE_STYLE[f.properties.hit ? 'hit' : 'missed']),
    onEachFeature: (f, l) => {
      l.bindPopup('Loading…');
      l.on('popupopen', (e) => nodePopup(f.properties.id)
        .then((html) => e.popup.setContent(html))
        .catch((err) => e.popup.setContent(escapeHtml(err.message))));
    },
  });
  const wrapper = L.layerGroup();
  let loaded = false;
  const load = async () => {
    const collections = await Promise.all(urls.map((u) => getJson(u)));
    inner.clearLayers();
    collections.forEach((fc) => inner.addData(fc));
    loaded = true;
    raiseNodes();
  };
  const update = () => {
    const show = map.getZoom() >= NODE_MIN_ZOOM;
    if (show && !wrapper.hasLayer(inner)) {
      wrapper.addLayer(inner);
      inner.bringToFront();
    } else if (!show && wrapper.hasLayer(inner)) {
      wrapper.removeLayer(inner);
    }
  };
  wrapper.on('add', () => {
    update();
    if (!loaded) load().catch((err) => alert(err.message));
  });
  map.on('zoomend', update);
  return { wrapper, inner, reload: () => (loaded ? load() : Promise.resolve()) };
}

const nodeLayers = {
  'Missed cart path nodes': nodeLayer(['nodes?layer=cartpath&status=missed']),
  'Missed road nodes': nodeLayer(['nodes?layer=road&status=missed']),
  'Hit nodes': nodeLayer(['nodes?layer=cartpath&status=hit', 'nodes?layer=road&status=hit']),
};

// Canvas draws in the order layers were added, so keep nodes on top of
// anything toggled on later.
function raiseNodes() {
  Object.values(nodeLayers).forEach(({ inner }) => { if (map.hasLayer(inner)) inner.bringToFront(); });
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

async function loadTracks() {
  const fc = await getJson('activities');
  tracks.clearLayers();
  tracks.addData(fc);
  raiseNodes();
}

tracks.on('add', () => {
  if (tracksRequested) return;
  tracksRequested = true;
  loadTracks().catch((err) => alert(err.message));
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

// ---- sync ---------------------------------------------------------------

const syncButton = document.getElementById('sync-now');
const syncStatus = document.getElementById('sync-status');
let syncPoll = null;

function renderSync(s) {
  const lines = [];
  if (s.running) {
    lines.push('Syncing…');
  } else if (s.last_ok) {
    lines.push(`Last sync: ${escapeHtml(formatEastern(s.last_ok.finished_at))}`
      + (s.last_ok.headline ? ` · ${escapeHtml(s.last_ok.headline)}` : ''));
  } else {
    lines.push('Never synced');
  }
  if (!s.running && s.latest?.status === 'failed') {
    lines.push(`<span class="error">Last attempt failed: ${escapeHtml(s.latest.error ?? '')}</span>`);
  }
  syncStatus.innerHTML = lines.join('<br>');
  syncButton.disabled = s.running;
}

async function refreshAll() {
  await Promise.all([
    loadStats(),
    loadNetwork('cartpath'),
    loadNetwork('road'),
    ...Object.values(nodeLayers).map((n) => n.reload()),
    tracksRequested ? loadTracks() : Promise.resolve(),
  ]);
  raiseNodes();
}

function pollSync() {
  if (syncPoll) return;
  syncPoll = setInterval(async () => {
    try {
      const s = await getJson('sync');
      renderSync(s);
      if (!s.running) {
        clearInterval(syncPoll);
        syncPoll = null;
        await refreshAll();
      }
    } catch (err) {
      syncStatus.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    }
  }, SYNC_POLL_MS);
}

syncButton.addEventListener('click', async () => {
  syncButton.disabled = true;
  try {
    await getJson('sync', { method: 'POST' });
    renderSync({ running: true });
    pollSync();
  } catch (err) {   // 409 (another job running) or 503 (no credentials)
    syncStatus.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    syncButton.disabled = false;
  }
});

// ---- find ---------------------------------------------------------------

function findCartpath(oid) {
  const pieces = cartpathPieces.get(oid);
  if (!pieces) return alert('No cart path with that OBJECTID_1');
  const bounds = L.featureGroup(pieces).getBounds();
  map.fitBounds(bounds, { maxZoom: 19, padding: [40, 40] });
  pieces[0].openPopup(bounds.getCenter());
}

document.getElementById('find').addEventListener('submit', (e) => {
  e.preventDefault();
  findCartpath(Number(document.getElementById('find-oid').value));
});

// ---- startup ------------------------------------------------------------

(async () => {
  networks.road = makeNetwork('road');
  networks.cartpath = makeNetwork('cartpath');
  overlays['Roads'] = networks.road;
  overlays['Cart paths'] = networks.cartpath;
  networks.road.addTo(map);
  networks.cartpath.addTo(map);
  await Promise.all([loadNetwork('road'), loadNetwork('cartpath'), loadStats()]);
  networks.cartpath.bringToFront();
  if (networks.cartpath.getLayers().length) map.fitBounds(networks.cartpath.getBounds());

  for (const [label, n] of Object.entries(nodeLayers)) overlays[label] = n.wrapper;
  nodeLayers['Missed cart path nodes'].wrapper.addTo(map);
  overlays['Run tracks'] = tracks;
  L.control.layers(null, overlays, { collapsed: false, position: 'bottomright' }).addTo(map);

  const s = await getJson('sync');
  renderSync(s);
  if (s.running) pollSync();   // e.g. a CLI sync already in progress

  // Link to a feature with #oid=12062; add &tracks to show run tracks.
  const linked = location.hash.match(/oid=(\d+)/);
  if (linked) findCartpath(Number(linked[1]));
  if (/\btracks\b/.test(location.hash)) tracks.addTo(map);
})().catch((err) => alert(err.message));
