// Route builder: click to route along paths and roads, drag waypoints to
// reroute, retrace, undo/redo, live distance, and GPX export. Only routed
// legs call the server; retraces copy the route's own geometry, so they
// can't drift or add spurs.
(() => {
  // Magenta passes the dataviz colorblind check against every coverage
  // color; the white casing and width set it apart as well.
  const ROUTE_COLOR = '#e87ba4';
  const RETRACE_PICK_PX = 20;   // how close a shift-click must be to the route
  const WAYPOINT_PICK_PX = 8;   // a shift-click this close to a waypoint targets it

  const $ = (id) => document.getElementById(id);
  const toggle = $('route-toggle');
  const tools = $('route-tools');
  const distanceEl = $('route-distance');
  const messageEl = $('route-message');
  const buttons = {
    undo: $('route-undo'), redo: $('route-redo'), retrace: $('route-retrace'),
    clear: $('route-clear'), exportGpx: $('route-export'),
  };

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

  // The line gets its own pane above the coverage lines and lets clicks
  // through; waypoint markers sit in Leaflet's marker pane above it.
  const pane = map.createPane('route');
  pane.style.zIndex = 450;
  pane.style.pointerEvents = 'none';
  const renderer = L.canvas({ pane: 'route' });
  const casing = L.polyline([], { renderer, color: '#fff', weight: 9, opacity: 0.95 });
  const line = L.polyline([], { renderer, color: ROUTE_COLOR, weight: 5, opacity: 1 });
  const layer = L.layerGroup([casing, line]).addTo(map);
  const markers = L.layerGroup().addTo(map);

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

  function say(text, isError = false) {
    messageEl.textContent = text;
    messageEl.classList.toggle('error', isError);
  }

  // ---- drawing ------------------------------------------------------------

  function waypointIcon(cls) {
    return L.divIcon({ className: `waypoint ${cls}`, iconSize: [14, 14], iconAnchor: [7, 7] });
  }

  function render() {
    const pts = points();
    casing.setLatLngs(pts);
    line.setLatLngs(pts);

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

    distanceEl.textContent = state.stops.length ? `${(totalMeters() / METERS_PER_MILE).toFixed(2)} mi` : '';
    buttons.undo.disabled = !undoStack.length;
    buttons.redo.disabled = !redoStack.length;
    buttons.retrace.disabled = pts.length < 2;
    buttons.exportGpx.disabled = pts.length < 2;
    buttons.clear.disabled = !state.stops.length;
    tools.hidden = !(routeMode || state.stops.length);
  }

  function commit(next) {
    undoStack.push(state);
    redoStack = [];
    state = next;
    say('');
    render();
  }

  function undo() {
    if (!undoStack.length || busy) return;
    redoStack.push(state);
    state = undoStack.pop();
    say('');
    render();
  }

  function redo() {
    if (!redoStack.length || busy) return;
    undoStack.push(state);
    state = redoStack.pop();
    say('');
    render();
  }

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
    try {
      await fn();
    } catch (err) {
      say(err.message, true);
      render();   // puts a dragged marker back where it was
    } finally {
      busy = false;
      if (messageEl.textContent === 'Routing…') say('');
    }
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
    if (e.originalEvent && e.originalEvent.shiftKey) retraceTo(e.latlng);
    else addStop(e.latlng);
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
      say(state.stops.length ? '' : 'Click a path or road to start.');
    } else {
      map.boxZoom.enable();
      map.doubleClickZoom.enable();
      say('');
    }
    render();   // waypoints are draggable only while planning
  }

  toggle.addEventListener('click', () => setMode(!routeMode));
  buttons.undo.addEventListener('click', undo);
  buttons.redo.addEventListener('click', redo);
  buttons.retrace.addEventListener('click', () => { if (!busy) addRetrace(state.stops, 0); });
  buttons.clear.addEventListener('click', () => { if (state.stops.length && !busy) commit(EMPTY); });
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
