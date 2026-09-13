# Map style sheet — Leaflet style objects

Palette **B "Graphite"** (your pick). Changed values are marked **changed**;
unmarked lines match today's app. Colours mirror `tokens.css`.

## Coverage lines

```js
const STATE_STYLE = {
  cartpath: {
    complete: { color: '#6b6862', weight: 2,   opacity: 1 },   // changed: #199e70 / 2.5
    run:      { color: '#34332f', weight: 2.5, opacity: 1 },   // changed: #1c5cab
    not_run:  { color: '#ff3b1f', weight: 5,   opacity: 1 },   // changed: #d95926 / 4
  },
  road: {
    run:      { color: '#c4c1ba', weight: 1.5, opacity: 1 },   // changed: #5598e7
    not_run:  { color: '#ffb01f', weight: 3,   opacity: 1 },   // changed: #8f8e89
  },
  excluded:   { color: '#9a978f', weight: 2.5, opacity: 1, dashArray: '4 6' },  // changed: #5f5e5a
  uncounted:  { color: '#dedcd6', weight: 1.5, opacity: 1 },   // changed: #cac9c4
};
```

The rule: **finished ground is gray, colour means unfinished.** Cart paths left
are red and the heaviest line; roads left are amber one step down; run-but-
incomplete is the darker graphite (more work remains), complete the lighter.
Not colourblind-validated, per your note that it is not a requirement.

## Nodes

```js
const NODE_STYLE = {
  hit:        { radius: 2,   weight: 0,   fillColor: '#333333', fillOpacity: 0.75 },
  missed:     { radius: 3.5, weight: 1.5, color: '#2b2a27', fillColor: '#ffffff', fillOpacity: 1 },  // changed ring: #3a3936
  missedRoad: { radius: 3,   weight: 1.5, color: '#2b2a27', fillColor: '#ffb01f', fillOpacity: 1 },  // changed fill: #ffd84d (now matches "road not run")
};
```

Unchanged: nodes render from zoom 15 and load on first toggle.

## Route — three polylines instead of two

```js
const ROUTE_CASING = { renderer, color: '#ffffff', weight: 11, opacity: 0.95 };  // changed: weight 9
const ROUTE_NEW    = { renderer, color: '#1b6ef3', weight: 5, opacity: 1 };      // changed: #e87ba4
const ROUTE_KNOWN  = { renderer, color: '#9dc2fb', weight: 5, opacity: 1 };      // new
```

Split rule: a leg's points inherit the coverage state of the segment they were
routed over. Points on a `not_run` segment (either layer) go in `ROUTE_NEW`;
everything else in `ROUTE_KNOWN`. If the router can't return per-point state
yet, draw the whole route with `ROUTE_NEW` and hide the "new mi" chip.

Blue is the only cool colour on the map, so the route can never be mistaken for
coverage.

## Waypoints

`L.divIcon`, class `waypoint` (+ `start` / `end`), 14x14, anchor 7,7.

```css
.waypoint { border-radius: 50%; box-sizing: border-box; background: #fff;
            border: 3px solid var(--map-route-new);      /* changed from #e87ba4 */
            box-shadow: 0 0 0 1.5px #fff, 0 1px 3px rgba(0,0,0,.4); }
.waypoint.start { background: var(--color-text); border-color: #fff; }
.waypoint.end   { background: var(--map-route-new); border-color: #fff; }
```

## Run tracks

```js
{ color: '#e6550d', weight: 2, opacity: 0.5 }   // unchanged
```

## Graph islands

```js
{ color: '#111111', weight: 5, dashArray: '2 6', opacity: 0.9 }   // unchanged
```

## Latest city changes

```js
map.createPane('changes').style.zIndex = 390;
const CHANGE_STYLE = { color: '#9fd8ff', weight: 12, opacity: 1, interactive: false };  // changed: #e8c547
```

Changed from yellow because amber now means "road not run" — the band would
have read as coverage. Pale blue sits under the lines without competing.

## Share pages — left.html and progress.html

One styles object with two entries; each page picks one. No nodes, no popups,
no interaction, and excluded/uncounted segments are not drawn at all.

```js
const SHARE_STYLE = {
  left: {   // left.html — what is still unrun carries all the weight
    cartpath: { not_run: { color: '#ff3b1f', weight: 4.5 },
                run:      { color: '#e6e4de', weight: 1.5 },
                complete: { color: '#e6e4de', weight: 1.5 } },
    road:     { not_run: { color: '#8b8983', weight: 2.5 },
                run:      { color: '#e6e4de', weight: 1.5 } },
  },
  done: {   // progress.html — the inverse
    cartpath: { complete: { color: '#34332f', weight: 3.5 },
                run:      { color: '#34332f', weight: 3.5 },
                not_run:  { color: '#dad8d2', weight: 2 } },
    road:     { run:      { color: '#8b8983', weight: 2 },
                not_run:  { color: '#dad8d2', weight: 2 } },
  },
};
```

## Map interaction

- Planning: `.leaflet-container.routing { cursor: crosshair }` (unchanged) plus
  `box-shadow: inset 0 0 0 2px var(--map-route-new)` on the map container.
- Popups suppressed while `routeMode` is true (unchanged).
- `boxZoom` and `doubleClickZoom` disabled while planning (unchanged).
