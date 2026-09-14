// The share pages: left.html (what's still unrun) and progress.html (what's
// done). Each is a binary map: one state draws, in one color for cart path
// and road alike; the other state isn't drawn at all, so the basemap shows
// through it. No controls, nodes, popups, or routing
// (docs/design/map-styles.md, SHARE_STYLE).

const view = document.body.dataset.view;   // 'left' or 'done'
const $ = (id) => document.getElementById(id);

// Per layer, the one state that draws: [colour token, weight]. Cart path and
// road share a color; every other state (including excluded and uncounted)
// isn't drawn.
const SHARE_STYLE = {
  left: {
    cartpath: { not_run: ['--map-cartpath-not-run', 2] },
    road: { not_run: ['--map-cartpath-not-run', 2] },
  },
  done: {
    cartpath: { complete: ['--map-share-done', 2], run: ['--map-share-done', 2] },
    road: { run: ['--map-share-done', 2] },
  },
}[view];

const LEGEND = {
  left: [['--map-cartpath-not-run', 2, 'Not yet run']],
  done: [['--map-share-done', 2, 'Run']],
}[view];

// Styled Leaflet layers for one GeoJSON layer's drawn state, built without
// a map so their bounds can be known before the map (and its first paint)
// exists at all.
function styledLayers(layer, fc) {
  const out = [];
  for (const f of fc.features) {
    const s = SHARE_STYLE[layer][f.properties.state];
    if (!s) continue;
    const [colour, weight] = s;
    const l = L.GeoJSON.geometryToLayer(f);
    l.setStyle({ color: token(colour), weight, opacity: 1, interactive: false });
    out.push(l);
  }
  return out;
}

const one = (n) => n.toFixed(1);

(async () => {
  const [stats, cartpaths, roads] = await Promise.all([
    getJson('stats'), getJson('network?layer=cartpath'), getJson('network?layer=road')]);
  const roadLayers = styledLayers('road', roads);
  const cartpathLayers = styledLayers('cartpath', cartpaths);

  // The map isn't created until here, already fitted to what it's about to
  // show, so there's only one paint and nothing to jump from -- a fixed
  // placeholder view can't work because the right zoom depends on this
  // device's viewport.
  const map = L.map('map', { preferCanvas: true, zoomControl: false, zoomSnap: 0.25 });
  const bounds = L.featureGroup([...roadLayers, ...cartpathLayers]).getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20], animate: false });
  else map.setView([33.39, -84.57], 13);   // nothing drawn on this layer at all
  basemap(map);
  // From here on, zoom by whole levels like the main map: at quarter levels
  // a scroll took four zoom animations and canvas redraws to go one level.
  map.options.zoomSnap = 1;

  // Cart paths draw above roads.
  const loud = { road: L.geoJSON(null).addTo(map), cartpath: L.geoJSON(null).addTo(map) };
  roadLayers.forEach((l) => loud.road.addLayer(l));
  cartpathLayers.forEach((l) => loud.cartpath.addLayer(l));

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
