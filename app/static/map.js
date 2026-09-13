const map = L.map('map', { preferCanvas: true }).setView([33.39, -84.57], 13);

// OpenStreetMap's standard tiles, used within the OSMF tile policy
// (https://operations.osmfoundation.org/policies/tiles/): visible
// attribution, the browser's normal Referer, no bulk or prefetching. They're
// shown in grayscale (style.css) so OSM's own colours don't compete with
// the coverage lines, which are judged against this gray.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  className: 'basemap',
  maxNativeZoom: 19,
  maxZoom: 20,
}).addTo(map);

const NODE_MIN_ZOOM = 15;
const SYNC_POLL_MS = 3000;
const $ = (id) => document.getElementById(id);

// Set by route.js while the route builder is using map clicks.
let routeMode = false;

// ---- popups -------------------------------------------------------------------

// One shape for every popup: a swatch naming the line you clicked, a
// headline, then label/value rows. `swatch` is a CSS class plus inline style.
function popupHtml({ swatch, title, note = '', rows = [], body = '' }) {
  const head = `<div class="popup-head"><span class="swatch ${swatch.cls}" style="${swatch.style ?? ''}"></span>`
    + `<b>${escapeHtml(title)}</b>${note ? `<span class="note">${escapeHtml(note)}</span>` : ''}</div>`;
  const dl = rows.length
    ? `<dl class="popup-rows">${rows.map(([k, v, cls]) => `<dt>${escapeHtml(k)}</dt>`
      + `<dd${cls ? ` class="${cls}"` : ''}>${v}</dd>`).join('')}</dl>`
    : '';
  return `<div class="popup">${head}${dl}${body}</div>`;
}

const SKELETON = '<div class="popup-skeleton"><i></i><i></i><i></i></div>';
const POPUP_OPTIONS = { minWidth: 250, maxWidth: 300 };

// Popups open on click, except in route mode: Leaflet's own popups stop the
// click from reaching the map, which would swallow route clicks on any line.
// `loading` is shown at once (the swatch and headline are known before any
// fetch); `content` may return a promise.
function clickPopup(layer, content, loading = null) {
  layer.on('click', (e) => {
    if (routeMode) return;
    L.DomEvent.stop(e);
    const popup = L.popup(POPUP_OPTIONS).setLatLng(e.latlng)
      .setContent(loading ? loading() : SKELETON).openOn(map);
    Promise.resolve(content())
      .then((html) => popup.setContent(html))
      .catch((err) => popup.setContent(
        `<div class="popup"><div class="popup-error">✕ ${escapeHtml(err.message)}</div></div>`));
  });
}

function lineSwatch(style) {
  return { cls: `line${style.dashArray ? ' dashed' : ''}`, style: `--c:${style.color};--w:${style.weight}` };
}

function intervalsLink(intervalsId) {
  const url = `https://intervals.icu/activities/${encodeURIComponent(intervalsId)}`;
  return `<a class="popup-link" href="${url}" target="_blank" rel="noopener">Open in Intervals ↗</a>`;
}

// ---- coverage -------------------------------------------------------------------

const STATE_STYLE = coverageStyles();
const STATE_LABEL = {
  complete: 'Complete', run: 'Run', not_run: 'Not run', excluded: 'Excluded', uncounted: 'Not counted',
};

function stateStyle(layer, state) {
  return STATE_STYLE[layer][state] ?? STATE_STYLE[state];
}

function segmentPopup(f, layer) {
  const p = f.properties;
  const rows = [
    ['OID', escapeHtml(p.source_oid), 'mono'],
    ['Key', escapeHtml(p.source_key), 'mono'],
    ['Name', escapeHtml(p.name ?? '—')],
    ['Type', escapeHtml(p.seg_type ?? '—')],
    ['Length', `${p.segment_length_m} m · ${p.parts} part${p.parts === 1 ? '' : 's'}`],
  ];
  if (p.nodes_total) rows.push(['Nodes hit', `${p.nodes_hit} of ${p.nodes_total}`]);
  if (p.state === 'excluded') rows.push(['Excluded', escapeHtml(p.exclusion_reason ?? '')]);
  else if (p.uncounted_reason && p.uncounted_reason !== 'second carriageway') {
    rows.push(['Not counted', escapeHtml(p.uncounted_reason)]);
  }
  if (p.changed_at) {
    rows.push([`City ${p.city_change === 'added' ? 'added' : 'changed'}`,
      escapeHtml(formatEastern(p.changed_at, { dateStyle: 'medium' }))]);
  }
  let title = STATE_LABEL[p.state];
  let note = ['run', 'not_run'].includes(p.state) ? `this piece, ${p.length_m} m` : '';
  if (p.uncounted_reason === 'second carriageway') {
    title = 'Second carriageway';
    note = 'shows the other side\'s coverage';
  }
  return popupHtml({ swatch: lineSwatch(stateStyle(layer, p.state)), title, note, rows });
}

// One Leaflet layer per coverage state, so each Layers row is its own toggle
// (docs/design/component-spec.md, "Layer split"). Excluded and uncounted
// share a layer. Listed bottom to top: heavier, more important lines on top.
const COVERAGE_ORDER = ['excluded', 'road-run', 'road-not-run',
  'cartpath-complete', 'cartpath-run', 'cartpath-not-run'];
const COVERAGE = Object.fromEntries(COVERAGE_ORDER.map((key) => [key, L.geoJSON(null)]));

function coverageKey(layer, state) {
  return state === 'excluded' || state === 'uncounted' ? 'excluded' : `${layer}-${state.replace('_', '-')}`;
}

// A segment can be several features (one per run of intervals, across state
// layers), so find-by-OID keeps every piece.
const cartpathPieces = new Map();

async function loadNetwork(layer) {
  const data = await getJson(`network?layer=${layer}`);
  if (layer === 'cartpath') cartpathPieces.clear();
  COVERAGE_ORDER.filter((k) => k === 'excluded' || k.startsWith(layer)).forEach((k) => {
    // The excluded layer holds both networks; clear only this one's pieces.
    if (k === 'excluded') {
      COVERAGE.excluded.eachLayer((l) => { if (l.feature.properties.layer === layer) COVERAGE.excluded.removeLayer(l); });
    } else {
      COVERAGE[k].clearLayers();
    }
  });
  for (const f of data.features) {
    f.properties.layer = layer;
    const l = L.GeoJSON.geometryToLayer(f);
    l.setStyle(stateStyle(layer, f.properties.state));
    l.feature = f;
    clickPopup(l, () => segmentPopup(f, layer));
    COVERAGE[coverageKey(layer, f.properties.state)].addLayer(l);
    if (layer === 'cartpath') {
      const oid = f.properties.source_oid;
      if (!cartpathPieces.has(oid)) cartpathPieces.set(oid, []);
      cartpathPieces.get(oid).push(l);
    }
  }
}

// Canvas draws in the order layers were added, so re-stack after any toggle.
function restack() {
  COVERAGE_ORDER.forEach((k) => { if (map.hasLayer(COVERAGE[k])) COVERAGE[k].bringToFront(); });
  raiseNodes();
}

// ---- nodes --------------------------------------------------------------------

const NODE_STYLE = {
  hit: { radius: 2, weight: 0, fillColor: token('--map-node-hit'), fillOpacity: 0.75 },
  missed: { radius: 3.5, weight: 1.5, color: token('--map-node-ring'),
    fillColor: token('--map-node-missed-fill'), fillOpacity: 1 },
  missedRoad: { radius: 3, weight: 1.5, color: token('--map-node-ring'),
    fillColor: token('--map-node-missed-road-fill'), fillOpacity: 1 },
};
const NODE_SWATCH = { hit: 'dot hit', missed: 'dot missed', missedRoad: 'dot missed-road' };

function nodeHead(kind) {
  return { hit: 'Hit node', missed: 'Missed node', missedRoad: 'Missed road node' }[kind];
}

async function nodePopup(id, kind) {
  const d = await getJson(`nodes/${id}`);
  const swatch = { cls: NODE_SWATCH[kind] };
  if (d.hit) {
    const date = formatEastern(d.hit.start_at, { dateStyle: 'medium' });
    return popupHtml({ swatch, title: 'Hit node', body: `<div class="popup-body"><b>${escapeHtml(date)}</b><br>`
      + `${escapeHtml(d.hit.name ?? '')}<br>${intervalsLink(d.hit.intervals_id)}</div>` });
  }
  if (d.segment.layer === 'road') {
    return popupHtml({ swatch, title: 'Missed road node', note: `radius ${d.radius_m} m`,
      body: `<div class="popup-body">${escapeHtml(d.segment.name ?? 'Unnamed road')}</div>` });
  }
  return popupHtml({ swatch, title: 'Missed node', note: `radius ${d.radius_m} m`,
    body: `<div class="popup-body">No run has passed within ${d.radius_m} m of this node.</div>` });
}

// Node layers share the segments' canvas so they're clickable. Each Layers
// row toggles a wrapper; the nodes inside appear only when zoomed in, and
// their data loads the first time the layer is turned on.
function nodeLayer(urls, missedKind = 'missed') {
  const inner = L.geoJSON(null, {
    pointToLayer: (f, latlng) => L.circleMarker(latlng, NODE_STYLE[f.properties.hit ? 'hit' : missedKind]),
    onEachFeature: (f, l) => {
      const kind = f.properties.hit ? 'hit' : missedKind;
      clickPopup(l, () => nodePopup(f.properties.id, kind),
        () => popupHtml({ swatch: { cls: NODE_SWATCH[kind] }, title: nodeHead(kind), body: SKELETON }));
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
    if (!loaded) load().catch(showLoadError);
  });
  map.on('zoomend', update);
  return { wrapper, inner, reload: () => (loaded ? load() : Promise.resolve()) };
}

const nodeLayers = {
  'nodes-missed-cartpath': nodeLayer(['nodes?layer=cartpath&status=missed']),
  'nodes-missed-road': nodeLayer(['nodes?layer=road&status=missed'], 'missedRoad'),
  'nodes-hit': nodeLayer(['nodes?layer=cartpath&status=hit', 'nodes?layer=road&status=hit']),
};

function raiseNodes() {
  Object.values(nodeLayers).forEach(({ inner }) => { if (map.hasLayer(inner)) inner.bringToFront(); });
}

// ---- run tracks, graph islands, city changes ----------------------------------

const TRACK_STYLE = { color: token('--map-track'), weight: 2, opacity: 0.5 };

function trackPopup(f) {
  const p = f.properties;
  return popupHtml({
    swatch: lineSwatch(TRACK_STYLE), title: 'Run track',
    body: `<div class="popup-body"><b>${escapeHtml(formatEastern(p.start_at))}</b><br>`
      + `${escapeHtml(p.name ?? '')} · ${(p.distance_m / METERS_PER_MILE).toFixed(2)} mi · `
      + `${p.parts} part${p.parts === 1 ? '' : 's'}<br>${intervalsLink(p.intervals_id)}</div>`,
  });
}

// Off by default and fetched the first time they're shown.
const tracks = L.geoJSON(null, {
  style: TRACK_STYLE,
  onEachFeature: (f, l) => clickPopup(l, () => trackPopup(f)),
});
let tracksLoaded = false;

async function loadTracks() {
  const fc = await getJson('activities');
  tracks.clearLayers();
  tracks.addData(fc);
  tracksLoaded = true;
  raiseNodes();
}
tracks.on('add', () => { if (!tracksLoaded) loadTracks().catch(showLoadError); });

// Parts of the routing graph not connected to the main network: routes
// can't reach them. Off by default, for review.
const ISLAND_STYLE = { color: token('--map-island'), weight: 5, dashArray: '2 6', opacity: 0.9 };
const islands = L.geoJSON(null, {
  style: ISLAND_STYLE,
  onEachFeature: (f, l) => clickPopup(l, () => {
    const p = f.properties;
    return popupHtml({
      swatch: lineSwatch(ISLAND_STYLE), title: 'Graph island', note: `${p.island_length_m} m in all`,
      rows: [['Layer', escapeHtml(p.layer)], ['OID', escapeHtml(p.source_oid), 'mono'],
        ['Type', escapeHtml(p.seg_type ?? '—')], ['Name', escapeHtml(p.name ?? '—')]],
    });
  }),
});
let islandsLoaded = false;
islands.on('add', () => {
  if (islandsLoaded) return;
  islandsLoaded = true;
  getJson('graph/islands').then((fc) => islands.addData(fc)).catch(showLoadError);
});

// Segments from each layer's latest city change, drawn as a wide band in a
// pane beneath the coverage lines so their colours stay true. Not clickable
// (the lines above take the clicks, and their popups say when the city
// changed them). Loaded at startup so the Layers row can say when there's
// nothing to show.
map.createPane('changes').style.zIndex = 390;   // just below overlayPane (400)
const changes = L.geoJSON(null, {
  pane: 'changes',
  renderer: L.canvas({ pane: 'changes' }),
  style: lineStyle('change-band', { interactive: false }),
});

// ---- Layers drawer: legend rows are the toggles ------------------------------------

const LAYERS = {
  ...COVERAGE,
  ...Object.fromEntries(Object.entries(nodeLayers).map(([k, n]) => [k, n.wrapper])),
  tracks,
  islands,
  changes,
};

function layerInput(key) {
  return document.querySelector(`#legend input[data-layer="${key}"]`);
}

function setLayer(key, on) {
  const input = layerInput(key);
  if (input) input.checked = on;
  if (on) map.addLayer(LAYERS[key]);
  else map.removeLayer(LAYERS[key]);
  restack();
}

document.querySelectorAll('#legend input[data-layer]').forEach((input) => {
  input.addEventListener('change', () => setLayer(input.dataset.layer, input.checked));
});

function disableLayerRow(key, why) {
  const input = layerInput(key);
  input.checked = false;
  input.disabled = true;
  input.closest('.layer-row').classList.add('disabled');
  input.closest('.layer-row').title = why;
}

// ---- drawers: one open at a time ----------------------------------------------------

const DRAWERS = { 'layers-toggle': 'legend', 'data-toggle': 'data-drawer' };

function openDrawer(id) {
  for (const [button, drawer] of Object.entries(DRAWERS)) {
    const open = drawer === id;
    $(drawer).hidden = !open;
    $(button).setAttribute('aria-pressed', String(open));
  }
}

for (const [button, drawer] of Object.entries(DRAWERS)) {
  $(button).addEventListener('click', () => openDrawer($(drawer).hidden ? drawer : null));
  $(drawer).querySelector('.btn-close').addEventListener('click', () => openDrawer(null));
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && Object.values(DRAWERS).some((d) => !$(d).hidden)) openDrawer(null);
});

// ---- completion and stats -------------------------------------------------------------

function meter(id, pct) {
  $(id).style.width = `${Math.min(100, pct)}%`;
}

async function loadStats() {
  const s = await getJson('stats');
  const c = s.completion;
  const roadPct = c.road_total_mi ? (100 * c.road_covered_mi) / c.road_total_mi : 0;
  // One decimal in the bar; the Data drawer keeps the full figures.
  $('cartpath-pct').textContent = `${c.cartpath_pct.toFixed(1)}%`;
  $('cartpath-detail').textContent = `${c.cartpath_complete_mi.toFixed(1)} / ${c.cartpath_total_mi.toFixed(1)} `
    + `cart path mi · ${c.segments_complete} / ${c.segments_total} segments`;
  meter('cartpath-meter', c.cartpath_pct);
  $('road-pct').textContent = `${roadPct.toFixed(1)}%`;
  $('road-detail').textContent = `${c.road_covered_mi.toFixed(1)} / ${c.road_total_mi.toFixed(1)} road mi`;
  meter('road-meter', roadPct);

  const num = (n) => n.toLocaleString('en-US');
  const row = (label, v) => (v
    ? `<tr><td>${label}</td><td>${num(v.counted)}</td><td>${v.counted_mi.toFixed(2)}</td><td>${num(v.nodes)}</td></tr>`
    : '');
  $('stats').innerHTML = '<tr><th></th><th>Counted</th><th>Miles</th><th>Nodes</th></tr>'
    + row('Cart paths', s.cartpath) + row('Roads', s.road);
  $('runs-note').textContent = `${num(s.runs.city)} runs in the city`
    + (s.runs.latest_start_at ? ` · latest ${formatEastern(s.runs.latest_start_at, { dateStyle: 'medium' })}` : '');
}

// ---- sync -------------------------------------------------------------------------

const syncButtons = [$('sync-now'), $('sync-now-full')];
let syncPoll = null;

function sameEasternDay(a, b) {
  const day = (d) => formatEastern(d, { dateStyle: 'short' });
  return day(a) === day(b);
}

function renderSync(s) {
  const status = $('sync-status');
  const failed = !s.running && s.latest?.status === 'failed';
  status.classList.toggle('error', failed);
  status.title = failed ? briefError(s.latest.error) : '';
  if (s.running) {
    status.textContent = 'Syncing…';
  } else if (failed) {
    status.textContent = `Sync failed: ${briefError(s.latest.error)}`;
  } else if (s.last_ok) {
    const at = s.last_ok.finished_at;
    status.textContent = `Synced ${sameEasternDay(at, new Date())
      ? formatEastern(at, { timeStyle: 'short' }) : formatEastern(at, { month: 'short', day: 'numeric' })}`;
  } else {
    status.textContent = 'Never synced';
  }
  const detail = [];
  if (s.last_ok) {
    detail.push(`Last sync ${escapeHtml(formatEastern(s.last_ok.finished_at))}`);
    if (s.last_ok.headline) detail.push(escapeHtml(s.last_ok.headline));
  } else {
    detail.push('Never synced');
  }
  if (failed) detail.push(`<span class="error">Last attempt failed: ${escapeHtml(briefError(s.latest.error))}</span>`);
  $('sync-detail').innerHTML = detail.join('<br>');
  syncButtons.forEach((b) => { b.disabled = s.running; });
  $('sync-now').classList.toggle('spinning', !!s.running);
}

async function refreshAll() {
  await Promise.all([
    loadBanner(),
    loadStats(),
    loadNetwork('cartpath'),
    loadNetwork('road'),
    ...Object.values(nodeLayers).map((n) => n.reload()),
    tracksLoaded ? loadTracks() : Promise.resolve(),
  ]);
  restack();
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
      $('sync-status').textContent = err.message;
    }
  }, SYNC_POLL_MS);
}

async function startSync() {
  syncButtons.forEach((b) => { b.disabled = true; });
  try {
    await getJson('sync', { method: 'POST' });
    renderSync({ running: true });
    pollSync();
  } catch (err) {   // 409 (another job running) or 503 (no credentials)
    const status = $('sync-status');
    status.textContent = err.message;
    status.classList.add('error');
    status.title = err.message;
    syncButtons.forEach((b) => { b.disabled = false; });
  }
}
syncButtons.forEach((b) => b.addEventListener('click', startSync));

// ---- freshness banner ---------------------------------------------------------------

// Shown in the page flow (never over the map) when a data source is failing
// or stale, or the nightly script is overdue; details are on the status page.
async function loadBanner() {
  const s = await getJson('status');
  const rows = Object.values(s.sources).filter((src) => src.state !== 'ok').map((src) => {
    if (src.state === 'failing') {
      return { level: 'failing', title: `${src.label} failing`, detail: briefError(src.latest?.error),
        when: src.latest?.finished_at };
    }
    return { level: 'stale', title: `${src.label} stale`, detail: src.last_ok
      ? `No success since ${formatEastern(src.last_ok.finished_at)}.` : 'Has never succeeded.' };
  });
  if (s.nightly.overdue) {
    rows.push({ level: 'stale', title: 'Nightly script overdue',
      detail: `It hasn't run since ${formatEastern(s.nightly.last_run)}.` });
  }
  const banner = $('banner');
  const wasHidden = banner.hidden;
  banner.hidden = !rows.length;
  banner.className = rows.some((r) => r.level === 'failing') ? 'failing' : 'stale';
  banner.innerHTML = rows.map((r) => `<div class="banner-row ${r.level}">`
    + `<span class="icon" aria-hidden="true">${r.level === 'failing' ? '✕' : '!'}</span>`
    + `<div class="banner-body"><b>${escapeHtml(r.title)}</b><div class="banner-detail">${escapeHtml(r.detail)}`
    + `${r.when ? ` <span class="when">· ${escapeHtml(formatEastern(r.when))}</span>` : ''}</div></div>`
    + '<a href="status.html">Status page →</a></div>').join('');
  if (wasHidden !== banner.hidden) map.invalidateSize();
}

// ---- find ---------------------------------------------------------------------------

function findCartpath(oid) {
  const error = $('find-error');
  const pieces = cartpathPieces.get(oid);
  error.hidden = !!pieces;
  if (!pieces) {
    error.textContent = `No cart path with OBJECTID_1 ${oid}.`;
    return false;
  }
  const bounds = L.featureGroup(pieces).getBounds();
  map.fitBounds(bounds, { maxZoom: 19, padding: [40, 40] });
  L.popup(POPUP_OPTIONS).setLatLng(bounds.getCenter())
    .setContent(segmentPopup(pieces[0].feature, 'cartpath')).openOn(map);
  return true;
}

$('find').addEventListener('submit', (e) => {
  e.preventDefault();
  const oid = Number($('find-oid').value);
  if (oid && findCartpath(oid) && window.matchMedia('(max-width: 700px)').matches) openDrawer(null);
});

function showLoadError(err) {
  $('sync-status').textContent = err.message;
  $('sync-status').classList.add('error');
}

// ---- startup ------------------------------------------------------------------------

(async () => {
  // The small requests go first so the bar fills in while the networks load.
  const changesLoaded = getJson('changes');
  getJson('sync').then((s) => {
    renderSync(s);
    if (s.running) pollSync();   // e.g. a CLI sync already in progress
  }).catch(showLoadError);
  loadBanner().catch((err) => console.error(err));

  await Promise.all([loadNetwork('road'), loadNetwork('cartpath'), loadStats()]);
  document.querySelectorAll('#legend input[data-layer]').forEach((input) => {
    if (input.checked) map.addLayer(LAYERS[input.dataset.layer]);
  });
  restack();
  const bounds = L.featureGroup(COVERAGE_ORDER.filter((k) => k.startsWith('cartpath'))
    .map((k) => COVERAGE[k])).getBounds();
  if (bounds.isValid()) map.fitBounds(bounds);

  const changeData = await changesLoaded;
  changes.addData(changeData);
  if (!changeData.features.length) {
    disableLayerRow('changes', 'The city hasn\'t changed anything since the first import.');
  }

  // Link to a feature with #oid=12062; add &tracks or &changes to show those.
  const linked = location.hash.match(/oid=(\d+)/);
  if (linked) findCartpath(Number(linked[1]));
  if (/\btracks\b/.test(location.hash)) setLayer('tracks', true);
  if (/\bchanges\b/.test(location.hash) && changeData.features.length) setLayer('changes', true);
})().catch(showLoadError);
