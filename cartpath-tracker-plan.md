# Peachtree City Cart Path Tracker: Implementation Plan

## Goal

A single-user planning tool on kirk. It shows where you have and haven't run across Peachtree City's cart path and road networks, tracks cart path completion to a 100% ("Hard Mode") standard, and builds routes you export as GPX for Garmin. It approximates CityStrides for planning purposes; it doesn't replace it.

## Decisions

| Area | Decision |
|---|---|
| Users and access | Single user, local network. Authelia can be added later. |
| Activities | Runs only, pulled from the Intervals.icu API, full history backfilled. |
| Cart paths that count | Path, Bridge, Tunnel, including private segments. 805 segments, 108.8 mi as of Sept 2026. |
| Cart paths that don't count | Walking Path, Road Crossing, Road Entrance Exit, Parking Lot, and the remaining types. They stay in the routing graph. |
| Roads that count | All city centerlines, including private roads and state routes (state routes assumed). 2,554 segments, about 263 mi. |
| Cart path completion | A segment is complete when 100% of its nodes are hit. |
| Tunnels | Credited when both ends are hit in the same run. |
| Headline metric | Cart path miles complete. |
| Road progress | Run / not run coverage only. No street-level completion. |
| Exclusions | YAML file, empty at start. Parking loops and gated roads get added as found. |
| Routing | Manual click-to-route, shortest path, snapped to paths and roads within city limits. Retrace, undo/redo, live distance, GPX export. |
| Out of scope for v1 | Saved routes, route stats beyond distance, mobile UI, routing outside city limits, automatic route generation. |

## Data sources

Both city layers come from the same public ArcGIS server and use the same download method.

- Cart paths: `https://gis.peachtree-city.org/arcgis/rest/services/Public_Services/PeachtreeCityGolfCartPath/MapServer/3` (1,563 segments; filter on the `Type` field)
- Roads: `https://gis.peachtree-city.org/arcgis/rest/services/BASE/Road_Centerlines/MapServer/0` (2,554 segments)

Query each layer's `/query` endpoint with `where=1=1&outFields=*&f=geojson&orderByFields=OBJECTID` (the cart path layer's ID field is `OBJECTID_1`). The server caps each response at 1,000 records, so page with `resultOffset` until a page comes back with fewer than 1,000 features. Request `outSR=32616` to get UTM directly, or `outSR=4326` and transform in PostGIS. The server allows cross-origin requests.

The Intervals.icu athlete ID and API key come from environment variables, as in fitness-api.

## Architecture

Two containers on kirk. The database container uses the `pgrouting/pgrouting` image, which bundles PostGIS and pgRouting. The stock Postgres and `postgis/postgis` images don't include pgRouting. Running it as its own container keeps the existing Postgres untouched for your other apps.

The app container is a FastAPI service. It runs the import and sync jobs, exposes the API, and serves a static Leaflet frontend. FastAPI matches fitness-api, so the same GitHub Actions to GHCR pipeline, env-var secrets, and healthcheck pattern carry over. Keeping it separate from fitness-api avoids pulling a PostGIS dependency into an unrelated service.

All geometry is stored in EPSG:32616 (UTM 16N), so buffers and lengths are in meters with no extra math. The API converts to EPSG:4326 on output.

## Data model

```
segment    id, layer (cartpath | road), source_key, name, seg_type, counted,
           excluded, length_m, source_edited_at, geom_hash, geom
node       id, segment_id, seq, radius_m, hit_activity_id, hit_at, geom
activity   id, intervals_id, start_at, sport, distance_m, geom (MultiLineString)
route_edge id, source, target, cost, reverse_cost, segment_id, geom
```

For cart paths, `source_key` is the `GlobalID`, which is unique and stable across city edits. Roads have no `GlobalID`, so they use `OBJECTID` with the road name stored alongside as a check.

## Nodes

Nodes are generated every 20 m along each counted segment, plus both endpoints, so even short segments get at least two. That comes to roughly 8,750 cart path nodes and 21,000 road nodes. The city's own vertices are too uneven to use directly: curves have dozens and straight stretches have two.

## Activity sync

The sync job lists activities from Intervals.icu by date range, keeps outdoor runs (Run and TrailRun; verify the exact type names in the Intervals OpenAPI spec), and skips anything without GPS. For each run it fetches the lat/lon stream, builds a line, and discards it if it doesn't intersect the city's bounding box. The initial backfill runs in monthly chunks. After that, a nightly job and a "Sync now" button pick up new runs.

Tracks are split wherever consecutive points are more than 100 m apart. Without this, a GPS jump or a pause-and-resume draws a straight line across town and credits nodes you never ran. Tunnels are handled by their own rule, so splitting doesn't cost you tunnel credit.

Tracks are stored permanently. That makes matching recomputable whenever the radius, node spacing, or city data changes.

## Matching

A node is hit when a run's track passes within the node's radius (`ST_DWithin` against the track, with GiST indexes on both). Radius is set by class: 20 m for cart paths and residential roads, 30 m for arterials and state routes, where sidewalks sit farther from the centerline. Each node records the first run that hit it and when.

Tunnel segments get one extra rule. If a single run hits both the first and last node of a Tunnel segment, every node in that tunnel is marked hit.

A `recompute` command clears all hits and replays every stored run. Tuning happens there: look at missed nodes along routes you know you ran, adjust the radius, and recompute.

Metrics:

- **Cart path miles complete** (headline): the total length of counted, non-excluded cart path segments with every node hit, shown with the total and a percentage. Partial segments show on the map but add nothing, which matches Hard Mode.
- **Segments complete:** a count, shown alongside the headline.
- **Road miles covered:** the length of node-to-node intervals where both nodes are hit.

## Exclusions

Exclusions live in a YAML file in the repo, so they're versioned and reviewable:

```yaml
cartpaths:
  - global_id: "{17F7645E-B49A-46BA-9BD6-4BFD4CFB5395}"
    reason: "Gated off"
roads:
  - name: "MEADE FIELD COMPLEX"
    reason: "Parking loop"
  - object_id: 1234
    name: "TWIGGS COR"
    reason: "Gated"
```

Excluded segments stay on the map, greyed out, and drop out of all totals. A road entry with only a name excludes every segment of that road. An entry with an `object_id` excludes one segment, and the import warns if that segment's name no longer matches. The import also warns about entries that point to segments the city has deleted.

## City data refresh

Re-import runs monthly or on demand. Segments are compared by `source_key` and geometry hash. New segments get nodes and are matched against stored runs. Segments with changed geometry get their nodes regenerated and rematched. Deleted segments are removed. Each refresh produces a short change report listing what was added, changed, or removed, plus any orphaned exclusions.

## Routing graph

The graph includes every cart path type, since crossings, entrance stubs, and parking lot segments are real connections even though they don't count toward completion, plus all roads. Edge cost is length, and both directions are allowed on every edge. The `OneWay` field is ignored because you're on foot.

Connections are made only at line endpoints. A cart path endpoint within about 3 m of another endpoint snaps to it. An endpoint that lands mid-road splits the road there. Lines that merely cross are never joined, because tunnels pass under roads and bridges pass over them. Splitting at every crossing would let the router turn from a tunnel onto the road above it. After the graph is built, `pgr_connectedComponents` lists any disconnected islands for review.

## Route builder

The route is a start point plus an ordered list of legs, and each leg holds its geometry and length. The coverage layer stays visible underneath, so unrun segments are obvious while you draw.

**Click to route.** Each click snaps to the nearest edge and routes from the end of the current route using `pgr_withPoints`. That routes from the exact clicked point on the line, not the nearest intersection. Routing to the nearest intersection is what creates the extra spurs other route builders add on out-and-backs.

**Retrace.** "Retrace to start" appends a reversed copy of the whole route. Shift-clicking an earlier point on the route appends a reversed copy back to that point. Retracing copies the existing geometry and never calls the router, so it can't drift or add spurs.

**Undo and redo.** Every edit (click, retrace, clear) pushes a snapshot onto a history stack. Undo and redo step through it with buttons or Ctrl+Z / Ctrl+Shift+Z. Clear is undoable too.

**Distance.** The route's total distance in miles updates after every edit.

**Export.** Export produces a GPX file with a single track, named by date and distance. You import it into Garmin Connect as a course and sync it to the watch.

## Map UI

The map stays on Leaflet, since the viewer already validated it with this data. Its canvas renderer handles the node and segment counts involved. Layers:

- Cart path coverage and road coverage, colored run / not run per node interval
- Missed nodes as dots
- Excluded segments, greyed out
- Run tracks, off by default

A side panel shows cart path miles complete, the percentage, segments complete, road miles covered, the last sync time, and a sync button. The UI is desktop-first.

```
GET  /stats
GET  /network?layer=cartpath|road      GeoJSON with coverage state
GET  /nodes?status=missed
GET  /activities
POST /sync
POST /route/leg   {from, to} -> geometry, length_m
```

Retrace, undo, redo, and GPX export run client-side.

## Phases

### Phase 1: Foundation

Stand up both containers, the repo, and CI. Import both city layers with filtering and exclusions, generate nodes, and render them on a map. The phase is done when the nodes look right and the totals match the city data: 805 counted cart path segments and 108.8 mi.

### Phase 2: Activity sync

Backfill runs from Intervals.icu and render the tracks. The phase is done when every Peachtree City run appears and nothing from elsewhere does.

### Phase 3: Matching and stats

Implement hits, the tunnel rule, metrics, and `recompute`. Then tune the radius against runs you remember. The phase is done when the remaining missed nodes are places you genuinely haven't run.

### Phase 4: Coverage map

Build the full map UI and stats panel.

### Phase 5: Routing and route builder

Build the graph, review islands, then add click-to-route, retrace, undo/redo, distance, and GPX export. The phase is done when an exported GPX imports into Garmin Connect as a course and follows the paths.

### Phase 6: Automation

Add the nightly sync and the monthly city refresh with its change report.

Each phase can be verified on the map before the next builds on it. Routing comes last because it doesn't depend on coverage, and graph topology is the fiddliest part to get right.

## Tunable defaults

State routes are counted. Node spacing is 20 m. Match radius is 20 m for cart paths and residential roads, 30 m for arterials and state routes. Tracks split at gaps over 100 m. Tunnels require both ends in the same run. All of these live in config and take effect on the next `recompute`.
