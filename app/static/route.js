// Route builder: click to route along paths and roads, retrace, undo/redo,
// live distance, and GPX export. Only new legs call the server; retraces
// copy the route's own geometry, so they can't drift or add spurs.
(() => {
  // Magenta passes the dataviz colorblind check against every coverage
  // color; the white casing and width set it apart as well.
  const ROUTE_COLOR = '#e87ba4';
  const RETRACE_PICK_PX = 20;   // how close a shift-click must be to the route

  const $ = (id) => document.getElementById(id);
  const toggle = $('route-toggle');
  const tools = $('route-tools');
  const distanceEl = $('route-distance');
  const messageEl = $('route-message');
  const buttons = {
    undo: $('route-undo'), redo: $('route-redo'), retrace: $('route-retrace'),
    clear: $('route-clear'), exportGpx: $('route-export'),
  };

  // Route state: a snapped start and ordered legs. Every edit replaces the
  // state object, so undo and redo just swap snapshots.
  const EMPTY = Object.freeze({ start: null, legs: [] });
  let state = EMPTY;
  let undoStack = [];
  let redoStack = [];
  let busy = false;

  // Its own pane above the coverage lines. The route isn't clickable, so the
  // pane lets clicks through to the map.
  const pane = map.createPane('route');
  pane.style.zIndex = 450;
  pane.style.pointerEvents = 'none';
  const renderer = L.canvas({ pane: 'route' });
  const casing = L.polyline([], { renderer, color: '#fff', weight: 9, opacity: 0.95 });
  const line = L.polyline([], { renderer, color: ROUTE_COLOR, weight: 5, opacity: 1 });
  const marker = (fill) => L.circleMarker([0, 0], {
    renderer, radius: 6, color: '#fff', weight: 2, fillColor: fill, fillOpacity: 1,
  });
  const startMarker = marker('#1a1a19');
  const endMarker = marker(ROUTE_COLOR);
  const layer = L.layerGroup([casing, line]).addTo(map);

  // Within ~10 cm counts as the same point: leg ends come back rounded to 7
  // decimals, so a leg's first point can differ slightly from the last one.
  const same = (p, q) => Math.abs(p[0] - q[0]) < 1e-6 && Math.abs(p[1] - q[1]) < 1e-6;

  function points(s = state) {
    const pts = s.start ? [s.start] : [];
    for (const leg of s.legs) {
      for (const p of leg.latlngs) {
        if (!pts.length || !same(pts[pts.length - 1], p)) pts.push(p);
      }
    }
    return pts;
  }

  const totalMeters = (s = state) => s.legs.reduce((m, leg) => m + leg.length_m, 0);

  function say(text, isError = false) {
    messageEl.textContent = text;
    messageEl.classList.toggle('error', isError);
  }

  function showMarker(m, latlng) {
    if (latlng) {
      m.setLatLng(latlng);
      if (!layer.hasLayer(m)) layer.addLayer(m);
    } else if (layer.hasLayer(m)) {
      layer.removeLayer(m);
    }
  }

  function render() {
    const pts = points();
    casing.setLatLngs(pts);
    line.setLatLngs(pts);
    showMarker(startMarker, state.start);
    showMarker(endMarker, pts.length > 1 ? pts[pts.length - 1] : null);
    distanceEl.textContent = state.start ? `${(totalMeters() / METERS_PER_MILE).toFixed(2)} mi` : '';
    buttons.undo.disabled = !undoStack.length;
    buttons.redo.disabled = !redoStack.length;
    buttons.retrace.disabled = pts.length < 2;
    buttons.exportGpx.disabled = pts.length < 2;
    buttons.clear.disabled = !state.start;
    tools.hidden = !(routeMode || state.start);
  }

  function commit(next) {
    undoStack.push(state);
    redoStack = [];
    state = next;
    say('');
    render();
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push(state);
    state = undoStack.pop();
    say('');
    render();
  }

  function redo() {
    if (!redoStack.length) return;
    undoStack.push(state);
    state = redoStack.pop();
    say('');
    render();
  }

  const postJson = (url, body) => getJson(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  // ---- adding legs -------------------------------------------------------

  async function routeTo(latlng) {
    const to = { lat: latlng.lat, lon: latlng.lng };
    busy = true;
    say('Routing…');
    try {
      if (!state.start) {
        const s = await postJson('route/snap', to);
        commit({ start: [s.lat, s.lon], legs: [] });
        return;
      }
      const pts = points();
      const end = pts[pts.length - 1];
      const r = await postJson('route/leg', { from: { lat: end[0], lon: end[1] }, to });
      if (r.length_m === 0) {
        say('');
        return;
      }
      commit({ ...state, legs: [...state.legs, { latlngs: r.latlngs, length_m: r.length_m, kind: 'routed' }] });
    } catch (err) {
      say(err.message, true);
    } finally {
      busy = false;
    }
  }

  function addRetrace(latlngs) {
    let meters = 0;
    for (let k = 1; k < latlngs.length; k++) meters += map.distance(latlngs[k - 1], latlngs[k]);
    if (meters < 1) return;
    commit({ ...state, legs: [...state.legs, { latlngs, length_m: meters, kind: 'retrace' }] });
  }

  // Shift-click: back along the route itself to the nearest point on it.
  function retraceTo(latlng) {
    const pts = points();
    if (pts.length < 2) {
      say('Shift-click an earlier point on the route to retrace to it.', true);
      return;
    }
    const click = map.latLngToLayerPoint(latlng);
    let best = null;
    for (let i = 0; i < pts.length - 1; i++) {
      const p = L.LineUtil.closestPointOnSegment(
        click, map.latLngToLayerPoint(pts[i]), map.latLngToLayerPoint(pts[i + 1]));
      const d = click.distanceTo(p);
      if (!best || d < best.d) best = { i, p, d };
    }
    if (best.d > RETRACE_PICK_PX) {
      say('Shift-click on the route itself to retrace to that point.', true);
      return;
    }
    const target = map.layerPointToLatLng(best.p);
    const back = pts.slice(best.i + 1).reverse();
    back.push([target.lat, target.lng]);
    addRetrace(back);
  }

  map.on('click', (e) => {
    if (!routeMode || busy) return;
    if (e.originalEvent && e.originalEvent.shiftKey) retraceTo(e.latlng);
    else routeTo(e.latlng);
  });

  // ---- GPX ---------------------------------------------------------------

  function gpx(name, pts) {
    const esc = (s) => s.replace(/[<>&"']/g, (c) => (
      { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[c]));
    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="Run PTC" xmlns="http://www.topografix.com/GPX/1/1">',
      `  <metadata><name>${esc(name)}</name><time>${new Date().toISOString()}</time></metadata>`,
      '  <trk>',
      `    <name>${esc(name)}</name>`,
      '    <trkseg>',
      ...pts.map(([lat, lon]) => `      <trkpt lat="${lat.toFixed(7)}" lon="${lon.toFixed(7)}"></trkpt>`),
      '    </trkseg>',
      '  </trk>',
      '</gpx>',
      '',
    ].join('\n');
  }

  function exportGpx() {
    const pts = points();
    if (pts.length < 2) return;
    const miles = (totalMeters() / METERS_PER_MILE).toFixed(2);
    // en-CA formats as YYYY-MM-DD.
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
    const url = URL.createObjectURL(
      new Blob([gpx(`Run PTC ${day} ${miles} mi`, pts)], { type: 'application/gpx+xml' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `run-ptc-${day}-${miles}mi.gpx`;
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // ---- controls ------------------------------------------------------------

  function setMode(on) {
    routeMode = on;
    toggle.textContent = on ? 'Done planning' : 'Plan a route';
    toggle.classList.toggle('active', on);
    map.getContainer().classList.toggle('routing', on);
    // Shift-drag box zoom and double-click zoom would fight route clicks.
    if (on) {
      map.closePopup();
      map.boxZoom.disable();
      map.doubleClickZoom.disable();
      say(state.start ? '' : 'Click a path or road to start.');
    } else {
      map.boxZoom.enable();
      map.doubleClickZoom.enable();
      say('');
    }
    render();
  }

  toggle.addEventListener('click', () => setMode(!routeMode));
  buttons.undo.addEventListener('click', undo);
  buttons.redo.addEventListener('click', redo);
  buttons.retrace.addEventListener('click', () => addRetrace(points().reverse()));
  buttons.clear.addEventListener('click', () => { if (state.start) commit(EMPTY); });
  buttons.exportGpx.addEventListener('click', exportGpx);

  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return;
    if (e.target.closest('input, textarea')) return;
    if (!undoStack.length && !redoStack.length) return;
    e.preventDefault();
    if (e.shiftKey) redo();
    else undo();
  });

  render();
})();
