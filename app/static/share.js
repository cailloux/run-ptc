// The share pages: left.html (what's still unrun) and progress.html (what's
// done). One loud layer carries the page; everything else is a ghost. No
// controls, nodes, popups, or routing (docs/design/map-styles.md, SHARE_STYLE).

const view = document.body.dataset.view;   // 'left' or 'done'
const $ = (id) => document.getElementById(id);

// A fractional zoom lets the first view fit the city to the page; see the
// fitBounds below for why it's only the first view.
const map = L.map('map', { preferCanvas: true, zoomControl: false, zoomSnap: 0.25 }).setView([33.39, -84.57], 13);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  className: 'basemap',
  maxNativeZoom: 19,
  maxZoom: 20,
}).addTo(map);

// Per layer and state: [colour token, weight, loud]. Excluded and uncounted
// segments aren't drawn.
const SHARE_STYLE = {
  left: {
    cartpath: { not_run: ['--map-cartpath-not-run', 4.5, true],
      run: ['--map-share-ghost', 1.5], complete: ['--map-share-ghost', 1.5] },
    road: { not_run: ['--map-share-road', 2.5, true], run: ['--map-share-ghost', 1.5] },
  },
  done: {
    cartpath: { complete: ['--map-share-done', 3.5, true], run: ['--map-share-done', 3.5, true],
      not_run: ['--map-share-ghost-left', 2] },
    road: { run: ['--map-share-road', 2, true], not_run: ['--map-share-ghost-left', 2] },
  },
}[view];

const LEGEND = {
  left: [['--map-cartpath-not-run', 4, 'Cart path left'], ['--map-share-road', 2.5, 'Road left']],
  done: [['--map-share-done', 3.5, 'Run'], ['--map-share-ghost-left', 2, 'Not yet']],
}[view];

// Ghosts first so the loud lines draw on top; cart paths above roads.
const quiet = L.geoJSON(null).addTo(map);
const loud = { road: L.geoJSON(null).addTo(map), cartpath: L.geoJSON(null).addTo(map) };

function draw(layer, fc) {
  for (const f of fc.features) {
    const s = SHARE_STYLE[layer][f.properties.state];
    if (!s) continue;
    const [colour, weight, isLoud] = s;
    const l = L.GeoJSON.geometryToLayer(f);
    l.setStyle({ color: token(colour), weight, opacity: 1, interactive: false });
    (isLoud ? loud[layer] : quiet).addLayer(l);
  }
}

const one = (n) => n.toFixed(1);

(async () => {
  const [stats, cartpaths, roads] = await Promise.all([
    getJson('stats'), getJson('network?layer=cartpath'), getJson('network?layer=road')]);
  draw('road', roads);
  draw('cartpath', cartpaths);
  const all = L.featureGroup([quiet, loud.cartpath, loud.road]);
  if (all.getBounds().isValid()) map.fitBounds(all.getBounds(), { padding: [20, 20] });
  // After that, zoom by whole levels like the main map: at quarter levels a
  // scroll took four zoom animations and canvas redraws to go one level.
  map.options.zoomSnap = 1;

  const c = stats.completion;
  if (view === 'left') {
    $('fig-cartpath').textContent = one(c.cartpath_total_mi - c.cartpath_complete_mi);
    $('fig-cartpath-label').textContent = `cart path mi left of ${one(c.cartpath_total_mi)}`;
    $('fig-road').textContent = one(c.road_total_mi - c.road_covered_mi);
    $('fig-road-label').textContent = `road mi left of ${one(c.road_total_mi)}`;
  } else {
    const roadPct = c.road_total_mi ? (100 * c.road_covered_mi) / c.road_total_mi : 0;
    $('fig-cartpath').textContent = `${one(c.cartpath_pct)}%`;
    $('fig-cartpath-label').textContent = `${one(c.cartpath_complete_mi)} of ${one(c.cartpath_total_mi)} cart path mi`;
    $('fig-road').textContent = `${one(roadPct)}%`;
    $('fig-road-label').textContent = `${one(c.road_covered_mi)} of ${one(c.road_total_mi)} road mi`;
  }
  const latest = stats.runs.latest_start_at
    ? `<span class="when">Latest run ${escapeHtml(formatEastern(stats.runs.latest_start_at, { dateStyle: 'medium' }))}</span>`
    : '';
  $('mini-legend').innerHTML = LEGEND.map(([colour, weight, label]) => `<span>`
    + `<span class="swatch line" style="--c:var(${colour});--w:${weight}"></span>${label}</span>`).join('') + latest;
})().catch((err) => {
  $('mini-legend').innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
});
