// Route builder: click to route along paths and roads, drag waypoints to
// reroute, retrace, undo/redo, live distance, and GPX export. Only routed
// legs call the server; retraces copy the route's own geometry, so they
// can't drift or add spurs.
(() => {
  const RETRACE_PICK_PX = 20;   // how close a shift-click must be to the route
  const WAYPOINT_PICK_PX = 8;   // a shift-click this close to a waypoint targets it

  const $ = (id) => document.getElementById(id);
  const toggle = $('route-toggle');   // in the top bar
  const done = $('route-done');       // in the route bar
  const hint = $('route-hint');
  const distanceEl = $('route-distance');
  const messageEl = $('route-message');
  const buttons = {
    undo: $('route-undo'), redo: $('route-redo'), retrace: $('route-retrace'),
    finish: $('route-finish'), build: $('route-build'), clear: $('route-clear'), exportGpx: $('route-export'),
  };
  const newChip = $('route-new');

  // The route is a list of stops. The first is the start; each later stop
  // holds the leg that arrives at it:
  //   { kind: 'start',   latlng }
  //   { kind: 'route',   latlng, latlngs, length_m }   leg from the router
  //   { kind: 'retrace', target, latlng, latlngs, length_m }
  //     back along the route to stop `target`, derived from the geometry
  // Every edit replaces the whole state, so undo and redo swap snapshots.
  const EMPTY = Object.freeze({ stops: [] });
  let state = EMPTY;
  let undoStack = [];
  let redoStack = [];
  let busy = false;
  let picking = false;          // finish mode: clicks on lines pick segments
  const picks = new Map();      // segment_id -> not-run metres on it
  // About a 20 mi loop, built in half a second; 500 picks made 80+ mi and took 5-14 s.
  const MAX_PICKS = 150;

  // The line gets its own pane above the coverage lines and lets clicks
  // through; waypoint markers sit in Leaflet's marker pane above it.
  const pane = map.createPane('route');
  pane.style.zIndex = 450;
  pane.style.pointerEvents = 'none';
  const renderer = L.canvas({ pane: 'route' });
  // Blue is the only cool colour on the map, so the route can't be read as
  // coverage; the white casing sets it apart from the lines beneath. The
  // whole route draws light, and the stretches over ground not yet run
  // (from POST /route/coverage) draw dark on top.
  const weight = parseFloat(token('--map-w-route'));
  const casing = L.polyline([], { renderer, color: token('--map-route-casing'),
    weight: parseFloat(token('--map-w-route-casing')), opacity: 0.95 });
  const known = L.polyline([], { renderer, color: token('--map-route-known'), weight, opacity: 1 });
  const fresh = L.polyline([], { renderer, color: token('--map-route-new'), weight, opacity: 1 });
  const layer = L.layerGroup([casing, known, fresh]).addTo(map);
  const markers = L.layerGroup().addTo(map);

  // Picked segments: a translucent band under the coverage lines.
  map.createPane('picks').style.zIndex = 395;   // above the changes band, below overlayPane (400)
  const pickBand = L.geoJSON(null, {
    pane: 'picks',
    renderer: L.canvas({ pane: 'picks' }),
    style: lineStyle('finish-pick', { opacity: 0.35, interactive: false }),
  }).addTo(map);

  const same = (p, q) => Math.abs(p[0] - q[0]) < 1e-6 && Math.abs(p[1] - q[1]) < 1e-6;
  const pathMeters = (pts) => pts.slice(1).reduce((m, p, i) => m + map.distance(pts[i], p), 0);

  // Points along stops[from..to], with the stop that owns each point.
  function walk(stops, from = 0, to = stops.length - 1) {
    const pts = [];
    const owner = [];
    if (!stops.length) return { pts, owner };
    pts.push(stops[from].latlng);
    owner.push(from);
    for (let i = from + 1; i <= to; i++) {
      for (const p of stops[i].latlngs) {
        if (!same(pts[pts.length - 1], p)) {
          pts.push(p);
          owner.push(i);
        }
      }
    }
    return { pts, owner };
  }

  const points = (s = state) => walk(s.stops).pts;
  const totalMeters = (s = state) => s.stops.reduce((m, st) => m + (st.length_m ?? 0), 0);

  // A retrace stop's leg: the route from its target to the stop before it, reversed.
  function rebuildRetrace(stops, i) {
    const st = stops[i];
    const { pts } = walk(stops, st.target, i - 1);
    let meters = 0;
    for (let j = st.target + 1; j < i; j++) meters += stops[j].length_m;
    stops[i] = { ...st, latlng: stops[st.target].latlng, latlngs: pts.reverse(), length_m: meters };
  }

  // The route bar's message line doubles as help and errors.
  const HELP = 'Drag a waypoint to reroute · shift-click the route to retrace';
  function say(text, isError = false) {
    messageEl.textContent = isError ? `✕ ${text}` : text;
    messageEl.classList.toggle('error', isError);
  }
  function help() {
    if (picking) say(pickMessage());
    else say(state.stops.length ? HELP : 'Click a path or road to start.');
  }

  // ---- drawing ------------------------------------------------------------

  function waypointIcon(cls) {
    return L.divIcon({ className: `waypoint ${cls}`, iconSize: [14, 14], iconAnchor: [7, 7] });
  }

  function render() {
    const pts = points();
    casing.setLatLngs(pts);
    known.setLatLngs(pts);

    markers.clearLayers();
    const lastWaypoint = state.stops.map((s) => s.kind).lastIndexOf('route');
    state.stops.forEach((st, i) => {
      if (st.kind === 'retrace') return;   // sits on its target's waypoint
      const cls = i === 0 ? 'start' : (i === lastWaypoint ? 'end' : '');
      const m = L.marker(st.latlng, {
        icon: waypointIcon(cls), draggable: routeMode, keyboard: false,
        title: routeMode ? 'Drag to reroute' : '',
      });
      m.on('dragend', () => dragStop(i, m.getLatLng()));
      markers.addLayer(m);
    });

    const miles = (totalMeters() / METERS_PER_MILE).toFixed(2);
    distanceEl.textContent = miles;
    buttons.undo.disabled = busy || !undoStack.length;
    buttons.redo.disabled = busy || !redoStack.length;
    buttons.retrace.disabled = busy || pts.length < 2;
    buttons.exportGpx.disabled = busy || pts.length < 2;
    buttons.clear.disabled = busy || !state.stops.length;
    buttons.finish.disabled = busy;
    buttons.finish.setAttribute('aria-pressed', String(picking));
    buttons.build.hidden = !picking;
    buttons.build.disabled = busy || !picks.size || !state.stops.length;
    // Outside planning the route stays drawn; the top bar button says so.
    toggle.textContent = state.stops.length ? `Route · ${miles} mi` : 'Plan a route';
    hint.hidden = !routeMode || state.stops.length > 0;
  }

  // New miles: asked for after every change to the route. A reply to an
  // older route is dropped; the chip hides when the route is empty or the
  // request fails.
  let coverageSeq = 0;
  async function refreshNew() {
    const seq = ++coverageSeq;
    const pts = points();
    fresh.setLatLngs([]);
    if (pts.length < 2) {
      newChip.hidden = true;
      return;
    }
    try {
      const r = await postJson('route/coverage', { latlngs: pts });
      if (seq !== coverageSeq) return;
      fresh.setLatLngs(r.new);
      $('route-new-mi').textContent = `${(r.new_m / METERS_PER_MILE).toFixed(2)} mi new`;
      newChip.hidden = false;
    } catch (err) {
      if (seq === coverageSeq) newChip.hidden = true;
    }
  }

  function changed() {
    render();
    help();
    refreshNew();
  }

  function commit(next) {
    undoStack.push(state);
    redoStack = [];
    state = next;
    changed();
  }

  // Undo moves a snapshot from the undo stack to the redo stack; redo the reverse.
  function step(from, to) {
    if (!from.length || busy) return;
    to.push(state);
    state = from.pop();
    changed();
  }
  const undo = () => step(undoStack, redoStack);
  const redo = () => step(redoStack, undoStack);

  // ---- router calls -------------------------------------------------------

  const postJson = (url, body) => getJson(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const ll = (p) => ({ lat: p[0], lon: p[1] });

  async function snapTo(latlng) {
    const s = await postJson('route/snap', { lat: latlng.lat, lon: latlng.lng });
    return [s.lat, s.lon];
  }

  async function routeLeg(from, to) {
    const r = await postJson('route/leg', { from: ll(from), to: ll(to) });
    return { latlng: [r.to.lat, r.to.lon], latlngs: r.latlngs, length_m: r.length_m };
  }

  async function withBusy(fn) {
    if (busy) return;
    busy = true;
    say('Routing…');
    render();   // disables the tools while the leg is routed
    try {
      await fn();
    } catch (err) {
      busy = false;
      render();   // puts a dragged marker back where it was
      say(err.message, true);
      return;
    }
    busy = false;
    render();
    if (messageEl.textContent === 'Routing…') help();
  }

  // ---- editing --------------------------------------------------------------

  function addStop(latlng) {
    return withBusy(async () => {
      if (!state.stops.length) {
        commit({ stops: [{ kind: 'start', latlng: await snapTo(latlng) }] });
        return;
      }
      const stops = state.stops;
      const end = points()[points().length - 1];
      const leg = await routeLeg(end, [latlng.lat, latlng.lng]);
      if (leg.length_m === 0) return;
      commit({ stops: [...stops, { kind: 'route', ...leg }] });
    });
  }

  function addRetrace(stops, target) {
    const next = [...stops, { kind: 'retrace', target }];
    rebuildRetrace(next, next.length - 1);
    if (next[next.length - 1].length_m < 1) return;
    commit({ stops: next });
  }

  // Shift-click: back along the route to where you clicked. The spot becomes
  // a waypoint (splitting the leg there, no rerouting), so it can be dragged.
  function retraceTo(latlng) {
    const { pts, owner } = walk(state.stops);
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

    // On (or very near) an existing waypoint: retrace to it.
    const near = state.stops.findIndex((st) => st.kind !== 'retrace'
      && map.latLngToLayerPoint(st.latlng).distanceTo(best.p) <= WAYPOINT_PICK_PX);
    if (near >= 0) {
      addRetrace(state.stops, near);
      return;
    }

    const o = owner[best.i + 1];   // the stop whose leg holds that stretch
    if (state.stops[o].kind !== 'route') {
      say('Shift-click on a routed part of the route, not a retrace.', true);
      return;
    }
    const at = map.layerPointToLatLng(best.p);
    const target = [at.lat, at.lng];
    // Split stop o's leg at the clicked point.
    const leg = state.stops[o].latlngs;
    const k = leg.findIndex((p) => same(p, pts[best.i + 1]));
    const first = [...leg.slice(0, k), target];
    const second = [target, ...leg.slice(k)];
    const d1 = pathMeters(first);
    const d2 = pathMeters(second);
    const total = state.stops[o].length_m;
    const stops = state.stops.flatMap((st, i) => {
      const shifted = st.kind === 'retrace' && st.target >= o ? { ...st, target: st.target + 1 } : st;
      if (i !== o) return [shifted];
      return [
        { kind: 'route', latlng: target, latlngs: first, length_m: total * d1 / (d1 + d2) },
        { ...st, latlngs: second, length_m: total * d2 / (d1 + d2) },
      ];
    });
    addRetrace(stops, o);
  }

  // Drag a waypoint: reroute the legs touching it, rebuild every retrace,
  // and reroute the leg after any retrace whose turnaround moved.
  function dragStop(k, latlng) {
    return withBusy(async () => {
      const stops = state.stops.map((st) => ({ ...st }));
      const moved = new Set([k]);
      if (k === 0) stops[0].latlng = await snapTo(latlng);
      else stops[k].latlng = [latlng.lat, latlng.lng];
      for (let i = 1; i < stops.length; i++) {
        const st = stops[i];
        if (st.kind === 'route') {
          if (moved.has(i) || moved.has(i - 1)) {
            stops[i] = { kind: 'route', ...(await routeLeg(stops[i - 1].latlng, st.latlng)) };
          }
        } else {
          if (!same(stops[st.target].latlng, st.latlng)) moved.add(i);
          rebuildRetrace(stops, i);
        }
      }
      commit({ stops });
    });
  }

  map.on('click', (e) => {
    if (!routeMode || busy) return;
    if (picking && state.stops.length) {
      if (!swallowClick) say('Click a red line, or shift-drag a box, to pick segments.');
      return;
    }
    if (e.originalEvent && e.originalEvent.shiftKey) retraceTo(e.latlng);
    else addStop(e.latlng);
  });

  // ---- finish segments ---------------------------------------------------------
  // Pick segments with not-run stretches, then Build: the server returns a
  // loop from the route's start that runs them all, which replaces the route.

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  function pickMessage() {
    if (!state.stops.length) return 'Click your start, then the segments to finish.';
    if (!picks.size) return 'Click red lines, or shift-drag a box, to pick segments to finish.';
    const m = [...picks.values()].reduce((a, b) => a + b, 0);
    return `${plural(picks.size, 'segment')} · ${(m / METERS_PER_MILE).toFixed(2)} mi not run`;
  }

  // Every piece of the picked segments, from the coverage layers (map.js).
  function drawPicks() {
    pickBand.clearLayers();
    Object.values(COVERAGE).forEach((group) => group.eachLayer((l) => {
      if (picks.has(l.feature.properties.segment_id)) pickBand.addData(l.feature);
    }));
  }

  // Node dots sit over the lines and would take the picking clicks, so
  // they're hidden while picking and the ones that were on come back after.
  let hiddenNodes = [];
  function setPicking(on) {
    picking = on;
    if (on) {
      hiddenNodes = Object.values(nodeLayers).map((n) => n.wrapper).filter((w) => map.hasLayer(w));
      hiddenNodes.forEach((w) => map.removeLayer(w));
    } else {
      hiddenNodes.forEach((w) => map.addLayer(w));
      hiddenNodes = [];
      restack();
    }
    picks.clear();
    drawPicks();
    render();
    help();
  }

  // Adds these segments to the picks, with the not-run metres on each.
  function addPicks(ids) {
    const total = new Set([...picks.keys(), ...ids]).size;
    if (total > MAX_PICKS) {
      say(`That makes ${total} segments; pick at most ${MAX_PICKS} (a smaller area).`, true);
      return;
    }
    for (const id of ids) picks.set(id, 0);
    COVERAGE['cartpath-not-run'].eachLayer(addMetres);
    COVERAGE['road-not-run'].eachLayer(addMetres);
    function addMetres(l) {
      const p = l.feature.properties;
      if (ids.has(p.segment_id)) picks.set(p.segment_id, picks.get(p.segment_id) + p.length_m);
    }
    drawPicks();
    render();
    help();
  }

  // A box drag ends in a click where the mouse comes up; that click isn't a pick.
  let swallowClick = false;

  function pick(e) {
    if (!routeMode || !picking || busy || !state.stops.length) return;   // the first click sets the start
    L.DomEvent.stop(e);
    if (swallowClick) return;
    const p = e.propagatedFrom.feature.properties;
    if (!['run', 'not_run'].includes(p.state) || p.nodes_hit >= p.nodes_total) {
      say('That segment has nothing left to run.', true);
      return;
    }
    if (picks.has(p.segment_id)) {
      picks.delete(p.segment_id);
      drawPicks();
      render();
      help();
    } else {
      addPicks(new Set([p.segment_id]));
    }
  }
  Object.values(COVERAGE).forEach((group) => group.on('click', pick));

  // Shift-drag while picking: every segment with not-run ground inside the
  // box is added. The whole segment is finished, even its parts outside.
  let box = null;   // { start, rect }
  map.on('mousedown', (e) => {
    if (!routeMode || !picking || busy || !state.stops.length || !e.originalEvent.shiftKey) return;
    // Leaflet never starts a map drag on a shift-mousedown, so the map stays put.
    box = { start: e.latlng, rect: L.rectangle(L.latLngBounds(e.latlng, e.latlng), {
      renderer, color: token('--map-finish-pick'), weight: 1.5, dashArray: '5 4', fillOpacity: 0.08,
      interactive: false,
    }).addTo(map) };
  });
  map.on('mousemove', (e) => {
    if (box) box.rect.setBounds(L.latLngBounds(box.start, e.latlng));
  });
  document.addEventListener('mouseup', () => {
    if (!box) return;
    const bounds = box.rect.getBounds();
    box.rect.remove();
    box = null;
    const size = map.latLngToContainerPoint(bounds.getNorthEast())
      .distanceTo(map.latLngToContainerPoint(bounds.getSouthWest()));
    if (size < 8) return;   // a shift-click, not a box
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 0);
    const px = L.bounds(map.latLngToLayerPoint(bounds.getNorthWest()), map.latLngToLayerPoint(bounds.getSouthEast()));
    const ids = new Set();
    for (const key of ['cartpath-not-run', 'road-not-run']) {
      COVERAGE[key].eachLayer((l) => {
        const p = l.feature.properties;
        if (ids.has(p.segment_id) || !bounds.intersects(l.getBounds())) return;
        const pts = l.getLatLngs().flat(Infinity).map((ll) => map.latLngToLayerPoint(ll));
        if (pts.some((pt, i) => i && L.LineUtil.clipSegment(pts[i - 1], pt, px, false, true))) ids.add(p.segment_id);
      });
    }
    if (ids.size) addPicks(ids);
    else say('No red (not run) ground in that box.');
  });

  function build() {
    const ids = [...picks.keys()];
    return withBusy(async () => {
      const r = await postJson('route/finish', { start: ll(state.stops[0].latlng), segment_ids: ids });
      commit({
        stops: [{ kind: 'start', latlng: [r.from.lat, r.from.lon] }, ...r.legs.map((lg) => (
          { kind: 'route', latlng: [lg.to.lat, lg.to.lon], latlngs: lg.latlngs, length_m: lg.length_m }))],
      });
      setPicking(false);
      say(`Finishes ${plural(ids.length, 'segment')}`
        + (r.skipped ? ` (${plural(r.skipped, 'stretch')} on islands skipped)` : ''));
    });
  }

  // ---- GPX ---------------------------------------------------------------

  function gpx(name, pts) {
    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="Run PTC" xmlns="http://www.topografix.com/GPX/1/1">',
      `  <metadata><name>${escapeHtml(name)}</name><time>${new Date().toISOString()}</time></metadata>`,
      '  <trk>',
      `    <name>${escapeHtml(name)}</name>`,
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

  // Planning is a mode: the dark route bar replaces the top bar (same
  // height, so the map doesn't move), drawers close, and the map gets an edge.
  function setMode(on) {
    routeMode = on;
    $('topbar').hidden = on;
    $('route').hidden = !on;
    document.body.classList.toggle('planning', on);
    map.getContainer().classList.toggle('routing', on);
    // Shift-drag box zoom and double-click zoom would fight route clicks.
    if (on) {
      openDrawer(null);
      map.closePopup();
      map.boxZoom.disable();
      map.doubleClickZoom.disable();
      help();
    } else {
      map.boxZoom.enable();
      map.doubleClickZoom.enable();
      if (picking) setPicking(false);
    }
    render();   // waypoints are draggable only while planning
    map.invalidateSize();   // the phone layout docks the route bar at the bottom
  }

  toggle.addEventListener('click', () => setMode(true));
  done.addEventListener('click', () => setMode(false));
  buttons.undo.addEventListener('click', undo);
  buttons.redo.addEventListener('click', redo);
  buttons.retrace.addEventListener('click', () => { if (!busy) addRetrace(state.stops, 0); });
  buttons.clear.addEventListener('click', () => { if (state.stops.length && !busy) commit(EMPTY); });
  buttons.finish.addEventListener('click', () => { if (!busy) setPicking(!picking); });
  buttons.build.addEventListener('click', () => { if (picks.size && !busy) build(); });
  buttons.exportGpx.addEventListener('click', exportGpx);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && routeMode && !e.target.closest('input, textarea')) {
      if (picking) setPicking(false);   // Esc leaves finish mode first
      else setMode(false);
      return;
    }
    if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return;
    if (e.target.closest('input, textarea')) return;
    if (!undoStack.length && !redoStack.length) return;
    e.preventDefault();
    if (e.shiftKey) redo();
    else undo();
  });

  render();
})();
