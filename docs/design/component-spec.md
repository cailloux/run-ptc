# Component spec

Everything below is plain HTML + CSS against `tokens.css`. IDs the JS depends
on are kept; new IDs are listed in **ID map** at the end.

Base: `font-family: var(--font-ui)`, `font-size: var(--text-body)` (14px),
`line-height: var(--leading-body)`, colour `var(--color-text)`.

---

## 1. App bar (`#topbar`) — map page, view mode

```html
<header id="topbar">
  <a class="wordmark" href="./">Run PTC</a>
  <div id="completion">
    <div class="completion-primary">
      <b class="pct">50.9%</b>
      <span class="detail">52.68 / 103.56 cart path mi · 327 / 780 segments</span>
    </div>
    <div class="meter"><i style="width:50.9%"></i></div>
  </div>
  <div class="rule"></div>
  <div id="completion-road">
    <div class="completion-secondary"><b>33.8%</b><span class="detail">83.95 / 248.47 road mi</span></div>
    <div class="meter meter-sm"><i style="width:33.8%"></i></div>
  </div>
  <div class="spacer"></div>
  <div id="sync">
    <span id="sync-status">Synced 1:51 PM</span>
    <button id="sync-now" class="btn btn-icon" title="Sync now" aria-label="Sync now">↻</button>
  </div>
  <button id="route-toggle" class="btn btn-primary">Plan a route</button>
  <button id="layers-toggle" class="btn" aria-expanded="false" aria-controls="legend">Layers</button>
  <button id="data-toggle" class="btn" aria-expanded="false" aria-controls="data-drawer">Data</button>
  <a id="status-link" href="status.html">Status</a>
</header>
```

- Height `var(--bar-height)` (56px), `padding: 0 var(--space-7)`, `display:flex;
  align-items:center; gap:var(--space-8)`, `background:var(--color-surface)`,
  `border-bottom:1px solid var(--color-border)`.
- `.pct` = `var(--text-number)`/700/`--tracking-tight`; `.detail` =
  `var(--text-small)`/`--color-text-muted`. **One decimal everywhere in the bar**
  — 50.9%, 52.7 / 103.6 cart path mi, 84.0 / 248.5 road mi. The `#stats` table
  in the Data drawer keeps the full two-decimal figures.
- `.meter` 290x6px, `--color-border-subtle` track, radius 3px, fill
  `--map-meter-cartpath`. `.meter-sm` 150x4px, fill `--map-meter-road`. The
  meters use ink tokens, not map colours — in palette B the map's "done" gray is
  too weak to read as a progress fill.
- **Loading**: `.pct` renders `—`, `.detail` is empty, the meter fill is 0
  width. No spinner; the numbers arrive in one paint.
- **Sync running**: `#sync-status` reads `Syncing…`, `#sync-now` disabled with
  a 0.9s `rotate` animation on the glyph.
- **Sync error**: `#sync-status` gets `.error` (`color:var(--color-danger)`)
  and clamps to one line (`text-overflow:ellipsis`); full text on the status page.

### Buttons

`.btn` — `padding:9px 13px`, `border:1px solid var(--color-border)`,
`background:var(--color-surface)`, `radius:var(--radius-md)`, weight 500.
`.btn-primary` — `background/border: var(--color-text)`, text white, weight 600.
`.btn-icon` — 30x30, centred glyph.

| State | Treatment |
| --- | --- |
| hover | `background:var(--color-surface-hover)`; primary lightens to #302e2a |
| active | `transform:translateY(1px)` |
| focus-visible | `box-shadow:var(--focus-ring)`, no default outline |
| disabled | `background:var(--color-surface-sunken)`, `color:#a5a29b`, `border-color:var(--color-border)`, `cursor:default` |
| pressed/on | `aria-pressed="true"` → primary fill (used by `#layers-toggle` when its drawer is open) |

---

## 2. Freshness banner (`#banner`)

```html
<div id="banner" class="failing" hidden>
  <span class="icon" aria-hidden="true">✕</span>
  <div class="banner-body">
    <b>Intervals sync failing</b>
    <div class="banner-detail">HTTPStatusError: Client error '401 Unauthorized' <span class="when">· Sep 12, 2026, 5:36 PM</span></div>
  </div>
  <a href="status.html">Status page →</a>
</div>
```

- Sits **between** `#topbar` and `#map` in normal flow — it no longer floats,
  so it can never cover the map or the layer control.
- `display:flex; gap:var(--space-5); padding:11px 13px; border-radius:var(--radius-md)`.
- `.stale` → warn tokens, icon `!`; `.failing` → danger tokens, icon `✕`;
  both add `border-left:4px solid` in the accent colour.
- `.banner-detail` clamps to 2 lines (`-webkit-line-clamp:2`), `overflow-wrap:anywhere`.
- Several problems: repeat the flex row inside `#banner`, one per problem,
  separated by `border-top:1px solid` in the accent at 25% alpha.
- Empty state = `hidden` (unchanged).

---

## 3. Route bar (`#route`) — planning mode

Replaces `#topbar` in place (same height, same grid position) so the map does
not resize when the mode changes.

```html
<header id="route" class="planning">
  <span class="mode-flag"><i></i>Planning</span>
  <div class="route-figures">
    <b id="route-distance">4.82</b><span class="unit">mi</span>
    <span id="route-new" class="chip"><span class="swatch"></span>3.10 mi new</span>
  </div>
  <div class="rule"></div>
  <div id="route-tools">
    <button id="route-undo" class="btn btn-dark" title="Undo (Ctrl/Cmd+Z)">Undo</button>
    <button id="route-redo" class="btn btn-dark" title="Redo (Ctrl/Cmd+Shift+Z)" disabled>Redo</button>
    <button id="route-retrace" class="btn btn-dark">Retrace to start</button>
    <button id="route-clear" class="btn btn-dark">Clear</button>
    <button id="route-export" class="btn btn-light">Export GPX</button>
    <button id="route-export-fit" class="btn btn-outline-light">Export FIT</button>
  </div>
  <div class="spacer"></div>
  <div id="route-message" class="route-help">Drag a waypoint to reroute · shift-click to retrace</div>
  <button id="route-toggle" class="btn btn-outline-light">Done planning</button>
</header>
```

- Background `var(--color-surface-dark)`, text white; buttons `.btn-dark`
  (`background:var(--color-surface-dark-raised)`, `border:1px solid var(--color-border-dark)`).
- `#route-distance` = `var(--text-number-lg)`/700; it is the largest thing on
  screen while planning. Planning figures keep **two** decimals (4.82 mi,
  3.10 mi new); every headline number elsewhere is one decimal (50.9%, 52.7 of
  103.6 mi).
- All bar controls are 34px tall with `display:inline-flex; align-items:center`
  so labels sit on one optical line; the figures group is its own 34px box.
- Two exports sit side by side: `#route-export` (solid, GPX) and
  `#route-export-fit` (outline, FIT). Both disabled until the route has two
  points. **FIT is a placeholder** — the button exists so the bar is laid out
  for it; until it works, render it \`disabled\` with
  \`title="FIT export coming soon"\`.
- `#route-new` chip: pill, `background:#14315e`, text `#cfe2ff`, with a 14px
  swatch in `--map-route-known`. Hidden when the backend can't split the route.
- `#route-message` doubles as help and error. Default = the help sentence in
  `#b3afa6`; `.error` → `#ffb4ab` with a leading `✕`, e.g. *"That click is 80 m
  from any path or road."* Routing in progress → *"Routing…"* plus the bar's
  buttons disabled.
- **Empty route**: distance reads `0.00`, Undo/Redo/Retrace/Clear/Export
  disabled, message = *"Click a path or road to start."*
- Map while planning: `inset 0 0 0 2px var(--map-route-new)`, crosshair cursor,
  and a pill hint centred at the top of the map ("Click a path or road to add a
  waypoint") that fades out after the first waypoint.
- Exit: `Done planning`, or `Esc`.

---

## 4. Elevation strip (`#route-elevation`) — planning mode

```html
<section id="route-elevation" data-open="true">
  <div class="elev-head">
    <button id="route-elevation-toggle" aria-expanded="true">⌄ Elevation</button>
    <span><b>+312 ft</b> gain · −298 ft loss</span>
    <span class="note">Max grade 6.2% at 2.4 mi</span>
    <span class="spacer"></span>
    <span class="note">Hover the profile to follow it on the map</span>
  </div>
  <svg class="elev-chart" viewBox="0 0 1008 78" preserveAspectRatio="none">…</svg>
</section>
```

- Docked below the map, full width, `var(--elevation-height)` + 30px head.
  Collapsed = head only; state persists in `localStorage['runptc.elev']`.
- Area fill `#f3effe → #ded3fb`, stroke `var(--map-route-new)` 2px, x-axis mile
  ticks in `--color-border-subtle`.
- Hover: a 1px vertical rule plus a dot on the profile, and a matching marker on
  the map at the same distance along the route.
- **Empty** (no route): strip hidden entirely. **Loading**: head shows
  *"Reading elevation…"*, chart area is a 78px `--color-surface-sunken` block.
- **Not available** (no backend yet): head shows *"Elevation data not available
  yet"* in `--color-text-faint`, chart hidden, toggle disabled. Ship it this way
  and the layout does not move when the data lands.

---

## 5. Layers drawer (`#legend`) — legend and toggles merged

```html
<aside id="legend" class="drawer" hidden>
  <div class="drawer-head"><b>Layers</b><button class="btn-close" aria-label="Close">✕</button></div>
  <div class="drawer-body">
    <h3>Cart paths</h3>
    <label class="layer-row" for="layer-cartpath-not-run">
      <input type="checkbox" id="layer-cartpath-not-run" data-layer="cartpath-not-run" checked>
      <span class="swatch line" style="--w:5px;--c:var(--map-cartpath-not-run)"></span>
      <span class="layer-label">Not run</span>
    </label>
    …
  </div>
</aside>
```

- Absolutely positioned under the bar, right edge, `var(--drawer-width)` wide,
  `max-height: calc(100vh - var(--bar-height) - 32px)`, own scroll,
  `box-shadow:var(--shadow-md)`. Opening Data closes Layers and vice versa —
  only one drawer at a time, which is what stops the old overflow problem.
- Row: `display:flex; align-items:center; gap:var(--space-4); padding:6px 8px;
  border-radius:var(--radius-sm)`. Hover `--color-surface-hover`. The native
  checkbox may stay native or be styled as a 15px rounded box.
- **Unchecked row**: label and value drop to `--color-text-faint`; the swatch
  stays at full colour (it is the key).
- **Disabled row**: layers with no data (e.g. no city changes) get
  `opacity:.45`, `cursor:default`, and a `title` explaining why.
- Groups: Cart paths · Roads · Nodes (*from zoom 15*) · Extra.
- Keyboard: rows are `<label>`s wrapping real checkboxes, so tab + space work.

---

## 6. Data drawer (`#data-drawer`)

Holds the find form, the stats table, the run count and the sync block — all
things you consult, not things you watch.

```html
<aside id="data-drawer" class="drawer" hidden>
  <div class="drawer-head"><b>Data</b><button class="btn-close" aria-label="Close">✕</button></div>
  <div class="drawer-body">
    <form id="find">
      <input id="find-oid" type="number" placeholder="Cart path OBJECTID_1">
      <button class="btn">Find</button>
    </form>
    <table id="stats">…</table>
    <p class="note">329 runs in the city · latest Sep 12, 2026</p>
    <div id="sync-detail">Last sync Sep 12, 1:51 PM<br>1 new city run · 375 nodes newly hit</div>
    <button id="sync-now-full" class="btn btn-block">Sync now</button>
    <a href="status.html">Data status page →</a>
  </div>
</aside>
```

- `#find input`: `padding:8px 10px`, `border:1px solid var(--color-border-strong)`,
  radius `--radius-md`. Focus = `--focus-ring`, border `--color-text`.
- **Error** (no such OID): a `.field-error` line under the form in
  `--color-danger`, *"No cart path with OBJECTID_1 12062."* — replaces today's
  `alert()`.
- `#stats` numbers are right-aligned, `font-variant-numeric:tabular-nums`,
  rules `1px solid var(--color-border-subtle)`, header row micro/uppercase/faint.

---

## 7. Popups (Leaflet `.leaflet-popup-content`)

One structure for all three kinds: swatch + state headline + `<dl>`.

```html
<div class="popup">
  <div class="popup-head"><span class="swatch line"></span><b>Not run</b><span class="note">this piece, 214 m</span></div>
  <dl class="popup-rows"><dt>OID</dt><dd class="mono">12062</dd>…</dl>
</div>
```

- Popup shell: radius `--radius-lg`, `box-shadow:var(--shadow-lg)`, no border,
  width 290px, head separated by `1px solid var(--color-border-subtle)`.
- `dt` `--color-text-faint`, `dd` normal; identifiers use `--font-mono` at 12px.
- **Loading** (node and track popups fetch): head keeps the swatch, body shows
  three 10px skeleton bars in `--color-surface-sunken`; no "Loading…" text jump.
- **Error**: body shows the message in `--color-danger` with an `✕`.
- Links ("Open in Intervals ↗") are weight 600, `--color-text`, underlined on hover.

---

## 8. Status page

- Header: wordmark · "Data status" · `#updated` (faint, right) · back link.
  `background:var(--color-surface)`, bottom border, `padding:14px var(--space-9)`.
- Main: `max-width:900px`, `padding:var(--space-8) var(--space-9)`, ground
  `--color-surface-sunken`, `gap:var(--space-7)`.
- The two source cards sit in a `grid-template-columns:1fr 1fr` row (stacking
  below 760px); city layers, alerts and jobs are full width.
- `.card`: white, `1px solid var(--color-border)`, radius `--radius-lg`,
  `padding:var(--space-7)`. State edge: `border-left:4px solid` in
  `--color-danger-accent` (failing), `--color-warn-accent` (stale),
  `--color-running-accent` (running); none when current.
- `.badge`: pill, `var(--text-small)`/600, `padding:3px 10px`, 1px border,
  icon + word — `.ok ✓ Current`, `.stale ! Stale`, `.failing ✕ Failing`,
  `.running ⟳ Running` (glyph spins). Colour tokens per state; **never colour alone**.
- `.error` blocks: monospace 12px on `--color-danger-bg`, radius `--radius-md`,
  `white-space:pre-wrap; overflow-wrap:anywhere` — this is the overflow fix.
- Running: indeterminate 4px bar under the card head, button disabled, the live
  row in `#jobs` tinted `#f7faff`; polling continues unchanged (3s).
- Button errors (409/503) render as `.message.error` **below** the field list,
  not in place of it.
- `#jobs` / `#episodes`: `<details>` stays; the summary row gets a `▸` marker
  that rotates when open, and expanded output sits in the same monospace block.
- **Empty**: "No problems on record." / "No city changes since the first import."
  in a `--color-surface-sunken` block rather than loose text.

---

## 9. Share pages — `left.html` and `progress.html`

```html
<body class="share">
  <header>
    <div><div class="kicker">Peachtree City</div><h1>What's still unrun</h1></div>
    <div class="spacer"></div>
    <div class="figure"><b class="big">50.88</b><span>cart path mi left of 103.56</span></div>
    <div class="figure"><b class="mid">164.52</b><span>road mi left of 248.47</span></div>
  </header>
  <div id="map"></div>
  <footer class="map-footer">…mini legend… · Latest run Sep 12, 2026</footer>
</body>
```

- `.big` = `var(--text-display)`/700/`--tracking-tight`; colour
  `--map-cartpath-not-run` (left.html) or `#34332f` (progress.html).
- Two pages off one template and one styles object: `left.html` shows what is
  still unrun (the chosen framing), `progress.html` the inverse. No controls, no
  drawers, no nodes, no popups, no route code — each loads `/network` twice and
  draws.
- Header figures are one decimal: **50.9** cart path mi left of 103.6.
- Two-line mini legend bottom-left over the map; attribution bottom-right.
- Desktop-sized, but the header wraps to two rows under 700px.

---

## ID map (old → new)

| Old | New | Note |
| --- | --- | --- |
| `#panel` | `#topbar` + `#legend` + `#data-drawer` | the floating panel is gone; its contents split across the bar and two drawers |
| `#completion` | `#completion` (+ new `#completion-road`) | kept; road figures move to their own element |
| `#sync`, `#sync-status`, `#sync-now` | same IDs, now in the bar | `#sync-status` text shortens to "Synced 1:51 PM"; the long line moves to `#sync-detail` in the Data drawer |
| — | `#sync-detail`, `#sync-now-full` | new: the full sync line and a second Sync button in the Data drawer (wire both buttons to the same handler) |
| `#route` | `#route` | now the whole route bar, not a block in the panel |
| `#route-toggle` | `#route-toggle` | lives in `#topbar` when idle and in `#route` when planning — one element, moved by JS, or two elements sharing a handler (your call) |
| `#route-distance`, `#route-tools`, `#route-undo/redo/retrace/clear/export`, `#route-message` | unchanged | `#route-tools` is no longer `hidden`-toggled; the whole bar swaps instead |
| — | `#route-new` | new: the "3.10 mi new" chip |
| — | `#route-export-fit` | new: FIT export next to `#route-export` |
| — | `#route-elevation`, `#route-elevation-toggle` | new: elevation strip |
| `#stats`, `#find`, `#find-oid` | unchanged | moved into `#data-drawer` |
| `#legend` | `#legend` | now the merged legend + layer toggles drawer |
| Leaflet `L.control.layers` | removed | replaced by `#legend` rows; each row is `input[data-layer]` → `map.addLayer/removeLayer`. Keys: `cartpath-not-run`, `cartpath-run`, `cartpath-complete`, `road-not-run`, `road-run`, `nodes-missed-cartpath`, `nodes-missed-road`, `nodes-hit`, `tracks`, `islands`, `changes`, `excluded` |
| — | `#layers-toggle`, `#data-toggle`, `#data-drawer` | new drawer buttons and the Data drawer |
| `#banner`, `#status-link`, `#map` | unchanged | `#banner` is now in flow, not absolute |
| `.waypoint`, `.basemap`, `.badge`, `.error` | unchanged | `.waypoint` colours follow `--map-route-new` |
| Status page `#updated`, `#sync-card`, `#city-card`, `#alerts-card`, `#jobs`, `#episodes` | unchanged | the two source cards are wrapped in a `.source-grid` div |

## Layer split (decided: split, not filter)

Per-state toggles need each network drawn as one Leaflet layer per state rather
than one layer per network:

```js
const COVERAGE = {
  'cartpath-not-run':  L.geoJSON(null, { style: STATE_STYLE.cartpath.not_run }),
  'cartpath-run':      L.geoJSON(null, { style: STATE_STYLE.cartpath.run }),
  'cartpath-complete': L.geoJSON(null, { style: STATE_STYLE.cartpath.complete }),
  'road-not-run':      L.geoJSON(null, { style: STATE_STYLE.road.not_run }),
  'road-run':          L.geoJSON(null, { style: STATE_STYLE.road.run }),
  'excluded':          L.geoJSON(null, { style: STATE_STYLE.excluded }),   // also holds uncounted
};
```

- `loadNetwork(layer)` fetches `/network?layer=…` exactly as it does now, then
  routes each feature into `COVERAGE[layer + '-' + f.properties.state]` instead
  of one layer. `excluded` and `uncounted` features both go in `excluded`.
- Add in this order, so weight and importance stack correctly:
  `excluded → road-run → road-not-run → cartpath-complete → cartpath-run →
  cartpath-not-run`, then `raiseNodes()` as today. Re-run that order after any
  toggle so a layer turned back on doesn't land on top.
- `cartpathPieces` (find-by-OID) is populated the same way in `onEachFeature`,
  regardless of which sub-layer the piece lands in.
- A `#legend` checkbox maps straight to its layer:
  `map[on ? 'addLayer' : 'removeLayer'](COVERAGE[input.dataset.layer])`.
- Popups are unchanged — `clickPopup` is still bound per feature.

Node, track, island and change layers stay exactly as they are.
