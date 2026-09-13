# What changed, and why

## Layout
1. **The floating panel is gone.** Its contents split three ways: the numbers
   you watch go into a persistent top bar, the things you consult (find, stats,
   sync detail) go into a Data drawer, the legend becomes a Layers drawer. The
   panel can no longer grow past the viewport, and nothing covers the map.
2. **The Leaflet layers control is removed.** Its eight toggles move into the
   Layers drawer, merged with the legend, so the bottom-right corner is free and
   the same information is not stated twice.
3. **Legend rows are the toggles.** One row = checkbox + swatch + label
   (+ miles). Unchecked rows go gray; the swatch stays coloured.
4. **Only one drawer opens at a time.** That is the structural fix for the old
   overflow: there is no state in which two stacked panels fight for height.
5. **The banner sits in flow** between the bar and the map instead of floating
   over it, so it can never cover the map or a control, and long errors clamp to
   two lines instead of overflowing.

## Planning
6. **Planning is a mode.** The top bar is replaced by a dark route bar, the map
   gets a violet inset edge and a one-line hint, Esc exits. The tools are laid
   out in one row with real spacing instead of a wrapped cluster.
7. **Distance is the headline number** while planning — 24px, first thing in the
   bar.
8. **New miles vs already-run miles.** The route draws violet over ground you
   have not run and pale violet over ground you have, with a "3.10 mi new" chip
   next to the distance. This is the single change that makes the app answer
   "does this route actually get me anything".
9. **An elevation strip docks under the map**, collapsible, remembering its
   state. Not backed by the API yet; specced so it can arrive without moving
   anything.

## Type and colour
10. **14px base with a real scale** (11 / 12.5 / 13 / 14 / 15 / 16 / 22 / 24 /
    38), system fonts only. Labels are 11px uppercase; numbers are 22px+ and
    tabular.
11. **New coverage palette ("Graphite").** Everything finished is gray —
    #34332f for run-but-incomplete, #6b6862 for complete — and colour means
    unfinished: cart paths left #ff3b1f at 5px, roads left #ffb01f at 3px. The
    map reads as a to-do list. **Not colourblind-validated**, per your note that
    it is not a requirement. Palette A ("Ember") stays commented in
    `tokens.css` if you want to switch back.
12. **Route moves off magenta to blue** (#1b6ef3) — the only cool colour on the
    map, so it can't be read as coverage. The city-change band moves from yellow
    to pale blue for the same reason (amber now means "road not run").
12b. **Completion meters use ink**, not map colours: in this palette the "done"
    gray is too weak to work as a progress fill.
13. **Status colour is separate from map colour**, always paired with an icon
    and a word — unchanged rule, now tokenised.

## Status page
14. **The two source cards sit side by side**, each with a state edge colour, so
    both sources' health is one glance.
15. **Errors get a monospace block** that wraps (`pre-wrap` +
    `overflow-wrap:anywhere`) instead of overflowing, and keep the full URL.
16. **Running shows as a state**: edge colour, indeterminate bar, disabled
    button, tinted live row in the jobs table.
17. **Button errors (409/503) render under the fields** rather than replacing
    the card content.

## New surface
18. **Two share pages off one template**: `left.html` (what's still unrun — the
    framing you picked) and `progress.html` (the inverse). Header is title, both
    figures and the latest run date; no controls, no nodes, no toggles, no
    popups.
19. **Headline numbers are one decimal** (50.9%, 52.7 of 103.6 mi, 84.0 of
    248.5 mi). Planning keeps two (4.82 mi, 3.10 mi new), and the stats table
    keeps the full figures.
20. **Two exports** in the route bar: GPX solid, FIT outline. FIT is a
    placeholder — disabled until it exists, so the bar never re-flows when it
    lands.

## Decided
- **Coverage layers split per state** (three cart path layers, two road, one for
  excluded/uncounted) so each legend row is its own Leaflet layer. Draw order
  and the loader change are written out at the end of the component spec.
- **"New miles" stays in the design as a future feature.** The chip and the
  two-tone route are specced; until \`/route/leg\` returns segment identity, the
  route draws in one blue and \`#route-new\` is not rendered.

## Open questions for you
