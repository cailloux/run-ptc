# Peachtree City Cart Path Tracker: Implementation Plan

## Goal

A single-user planning tool on kirk. It shows where you have and haven't run across Peachtree City's cart path and road networks, tracks cart path completion to a 100% ("Hard Mode") standard, and builds routes you export as GPX for Garmin. It approximates CityStrides for planning purposes; it doesn't replace it.

## Decisions

| Area | Decision |
|---|---|
| Users and access | Single user, local network. Authelia can be added later. |
| Activities | Runs only, pulled from the Intervals.icu API, full history backfilled. |
| Cart paths that count | Path, Bridge, Tunnel, including private segments. 780 segments, 103.6 mi as of Sept 2026, after removing duplicates. |
| Cart paths that don't count | Walking Path, Road Crossing, Road Entrance Exit, Parking Lot, Park, and any other types. They stay in the routing graph. |
| Roads that count | All centerlines inside Peachtree City (`City = PEACHTREE CITY`), including private roads and state routes (state routes assumed), with divided roads counted once. 2,392 segments, 248.5 mi, after removing duplicates, slivers, and second carriageways. |
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

Query each layer's `/query` endpoint with `where=1=1&outFields=*&f=geojson&orderByFields=OBJECTID` (the cart path layer's ID field is `OBJECTID_1`; its `OBJECTID` column is null on every row). The server caps each response at 1,000 records, so page with `resultOffset` until a page comes back with fewer than 1,000 features. Request `outSR=32616` to get UTM directly. The server allows cross-origin requests.

### Clean-up on import

The raw layers need four corrections before anything is counted:

- **Duplicates.** Both layers repeat some features with identical geometry under a new ID, apparently batch re-adds (`OBJECTID_1` from 22510 in cart paths, `OBJECTID` from 10876 in roads). Features are deduplicated by a direction-independent geometry hash. The lowest object ID is kept, and dropped IDs are logged and recorded so exclusions that reference them map to the kept feature. As of Sept 2026 this drops 35 cart path features (25 Path, 10 Road Crossing; 5.2 mi of counted path) and 51 roads (3.7 mi).
- **Slivers.** Any feature shorter than 1 m is dropped with a warning (3 roads), because zero-length lines create degenerate nodes and edges. Parts shorter than 1 m inside a multipart feature (18 cart path parts) stay in the geometry for routing but get no nodes, so they never affect completion.
- **Lengths.** Every length is computed from geometry. Never read the city's length attributes (`LengthMile`, `LENGTH`, `Length`, `Shape.STLength()`). They're null, zero, or in other units on many features; the original 263 mi road figure came from one of them.
- **Outside the city.** The road layer includes 6 features outside Peachtree City (`City` is TYRONE, SENOIA, or Unknown). They're imported with `counted = false`, so they're usable for routing but don't count. This is a data rule, not an exclusion.

- **Divided roads.** SR-74 (N and S), SR-54, N and S Peachtree Pkwy, and MacDuff Pkwy (`divided_roads` in settings) are drawn as two carriageways 12–16 m apart. Only one carriageway counts: a segment lying at least 80% within 40 m of an already-kept segment of the same road, which it doesn't touch, is its twin. Longest chains of touching segments go first, so where the carriageways are separate chains one whole side is kept; MacDuff's connect, so it's paired segment by segment. Twins get `counted = false` with `uncounted_reason = 'second carriageway'`, keep no nodes, stay routable, and on the map show the kept side's coverage (each point borrows the nearest counted node's status). The kept side gets the wide match radius so running the far sidewalk still completes it. As of Sept 2026 this marks 104 segments, 20.6 mi.

After clean-up: 1,528 cart path features stored (780 counted, 103.6 mi) and 2,500 roads stored (2,392 counted, 248.5 mi). Totals measured in UTM may differ by about 0.1 mi. Every uncounted segment records why in `uncounted_reason`: `type`, `outside city`, or `second carriageway`.

The Intervals.icu athlete ID and API key come from environment variables, as in fitness-api.

## Architecture

Two containers on kirk (Unraid), `run-ptc` and `run-ptc-db`, defined as Unraid Docker templates in `deploy/unraid/` rather than docker-compose, since kirk has no compose plugin. Both sit on the `services` bridge network, and all configuration comes from env vars set in the Unraid Docker UI. Appdata lives under `/mnt/user/appdata/run-ptc/`. The database container uses the `pgrouting/pgrouting` image, which bundles PostGIS and pgRouting. The stock Postgres and `postgis/postgis` images don't include pgRouting. Running it as its own container keeps the existing Postgres untouched for your other apps.

The app container is a FastAPI service. It runs the import and sync jobs, exposes the API, and serves a static Leaflet frontend. FastAPI matches fitness-api, so the same GitHub Actions to GHCR pipeline, env-var secrets, and healthcheck pattern carry over. Keeping it separate from fitness-api avoids pulling a PostGIS dependency into an unrelated service.

All geometry is stored in EPSG:32616 (UTM 16N), so buffers and lengths are in meters with no extra math. The API converts to EPSG:4326 on output.

## Data model

```
segment          id, layer (cartpath | road), source_key, source_oid, name, seg_type,
                 uncounted_reason,
                 counted, excluded, exclusion_reason, length_m, source_edited_at,
                 geom_hash, props, geom (MultiLineString)
source_duplicate layer, dropped_key, canonical_key
node             id, segment_id, part_idx, seq, radius_m, hit_activity_id, hit_at, geom
activity         id, intervals_id, start_at, sport, name, distance_m, source, status
                 (city | outside | no_gps), track_raw (LineString), geom (MultiLineString),
                 synced_at
route_edge       id, source, target, length_m, cost, reverse_cost, segment_id, part_idx,
                 part_from, part_to, geom (LineString)
route_vertex     id, component, geom (Point)
```

For cart paths, `source_key` is the `GlobalID`, which is unique and stable across city edits. Roads have no `GlobalID`, so they use `OBJECTID` with the road name stored alongside as a check. `props` keeps every source attribute, so later rules (like radius classes) don't need a re-import.

One `segment` row is one source feature, and its geometry is always a MultiLineString. 141 counted cart paths (140 Path, 1 Bridge, no Tunnels) and 23 roads have more than one part. The parts are not just digitizing splits: many are separated by real gaps, some over 250 m. Anything built on consecutive nodes (nodes, coverage intervals, road miles covered) therefore works within a single part, identified by `part_idx`, and never spans parts.

## Nodes

Nodes are generated along each part of each counted segment, evenly spaced at 20 m or slightly less, plus both endpoints, so even short parts get at least two. Parts shorter than 1 m get none. That comes to about 9,800 cart path nodes and 25,400 road nodes. The city's own vertices are too uneven to use directly: curves have dozens and straight stretches have two.

## Activity sync

The sync job lists activities from Intervals.icu by date range, keeps outdoor runs (types `Run` and `TrailRun`, per the Intervals OpenAPI spec; `VirtualRun` is left out), and skips anything without GPS. A run has GPS when its `stream_types` includes `latlng`, so treadmill runs are skipped without fetching anything. For each run it fetches the `latlng` stream (latitudes in `data`, longitudes in `data2`), builds a line, splits it at gaps, and discards the track if no part intersects the city's bounding box (the extent of every imported segment). The initial backfill runs in monthly chunks. After that, a nightly job and a "Sync now" button pick up new runs.

Every date sent to Intervals is explicit: each month window sends `oldest=YYYY-MM-DD` and `newest=YYYY-MM-DDT23:59:59`. By default a sync resumes a week before the newest stored run and ends today in US Eastern; with nothing stored it starts at `sync_backfill_start`.

Every GPS run gets an `activity` row, so repeated syncs never refetch its stream, but tracks are stored only for city runs (`status = city`). Intervals serves Strava-sourced activities as empty stubs with no GPS; sync reports them loudly and stores nothing. As of Sept 2026 the backfill finds 551 runs: 532 with GPS, 328 in the city, and no Strava stubs.

Tracks are split wherever consecutive points are more than 100 m apart. Without this, a GPS jump or a pause-and-resume draws a straight line across town and credits nodes you never ran. The split happens before the city test, so a GPS jump across the city can't pull in an outside run. Tunnels are handled by their own rule, so splitting doesn't cost you tunnel credit.

Tracks are stored permanently, both raw (`track_raw`, every fix in order) and split (`geom`). That makes matching recomputable whenever the radius, node spacing, gap threshold, or city data changes; splitting is the SQL function `split_track(track, gap_m)`.

## Matching

A node is hit when a run's track passes within the node's radius (`ST_DWithin`). Radius is set by class: 10 m for cart paths, 20 m for most roads, and 30 m for roads with `CLASS = Arterial`, where sidewalks sit farther from the centerline.

The cart path radius was tuned down from 20 m. GPS tracks sit close to the paths (93% of hits at 20 m came from within 5 m), and with nodes 20 m apart, a 20 m radius credited the second node up any side path you merely ran past. A 5 m trial removed those false hits but missed corner-cutting at junctions, so cart path end nodes (the first and last node of each part) have their own setting, `match_radius_cartpath_end_m`. Both are currently 10 m. In the city's data `Arterial` is SR-74 and SR-54 (124 segments, 28.3 mi) plus five 0.01 mi intersection stubs; it also catches 2 SR-54 segments that lack GDOT tags, which `DOT_RDTYPE = 1` would miss.

Each node records the first run that hit it (`hit_activity_id`, which links to the run in Intervals) and that run's start time (`hit_at`). "First" means earliest by date, not first synced: matching a late-synced older run replaces a newer run's hit, and matching a newer run never replaces an older one.

Whole tracks usually span most of the city, so their bounding boxes can't narrow a spatial join. Matching runs against `activity_piece`, each city track cut into ~200 m pieces with a GiST index. A full match of every node takes a few seconds this way, against roughly two minutes on whole tracks.

Tunnel segments get one extra rule. If a single run hits both the first and last node of a Tunnel segment, every node in that tunnel is credited to the earliest such run, unless a node already has an earlier direct hit.

Matching is incremental. Sync matches only the city runs it just stored, and a city import matches only the segments whose nodes it regenerated. A `recompute` command re-splits every track with the current gap threshold, rebuilds the pieces, reassigns radii from the settings, clears all hits, and replays every stored run. Tuning happens there: look at missed nodes along routes you know you ran, adjust the radius, and recompute. `recompute` and `stats` also print missed nodes bucketed by distance to the nearest run, which shows how many a larger radius would pick up.

Metrics:

- **Cart path miles complete** (headline): the total length of counted, non-excluded cart path segments with every node hit, shown with the total and a percentage. A segment with no nodes is never complete. Partial segments show on the map but add nothing, which matches Hard Mode.
- **Segments complete:** a count, shown alongside the headline.
- **Road miles covered:** the length of node-to-node intervals, within a single part, where both nodes are hit.

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

Exclusions are reapplied from the YAML on every city import, on app startup, and by a standalone command, since they change more often than city data. Metrics are computed live, so they reflect exclusions as soon as they're applied. An entry that references a dropped duplicate is mapped to the kept feature, with a warning.

Excluded segments stay on the map, greyed out, and drop out of all totals. A road entry with only a name excludes every segment of that road. An entry with an `object_id` excludes one segment, and the import warns if that segment's name no longer matches. The import also warns about entries that point to segments the city has deleted.

## City data refresh

Re-import runs monthly or on demand. Segments are compared by `source_key` and geometry hash. New segments get nodes and are matched against stored runs. Segments with changed geometry get their nodes regenerated and rematched. Deleted segments are removed. Each refresh produces a short change report listing what was added, changed, or removed, plus any orphaned exclusions.

## Routing graph

The graph includes every cart path type, since crossings, entrance stubs, and parking lot segments are real connections even though they don't count toward completion, plus all roads. Edge cost is length, and both directions are allowed on every edge. The `OneWay` field is ignored because you're on foot.

Multipart segments are exploded so each part (1 m or longer) becomes its own edge. Connections are made only at line endpoints:

- **Ends join ends.** Line ends within 3 m of each other (`graph_snap_m`) snap onto one shared junction.
- **Ends split lines.** An end that lands within 3 m of another line's middle splits that line there, making a T-junction. This applies to cart paths as well as roads: the city rarely breaks a through path at a junction, and 393 cart paths meet another cart path mid-line. Splitting only roads, as first specified, left 68 components and 19.7 mi stranded. As a guard, tunnels and bridges are never split by a road's end.
- **At-grade crossings join; tunnels and bridges never do.** Where two lines cross and neither is a Tunnel or Bridge, both split at the crossing and share a junction: 8 road intersections the city drew as through lines (Archway Ln × Gates Entry had made Gates Entry an island), 273 path-road crosswalks, and 8 path-path crossings. The 57 crossings involving a tunnel or bridge stay apart, so the router can never turn from a tunnel onto the road above it. (The original rule, that no crossing joins, was meant for tunnels and bridges but also cut those at-grade intersections.)

pgRouting 4.0 has no topology builder that fits these rules. `pgr_separateTouching` failed on this data (16 overlapping road pairs) and can't tell a tunnel from an at-grade crossing. So the splitting and the end snapping are our own SQL (`app/graph.py`), while pgRouting assigns junctions (`pgr_extractVertices`) and finds islands (`pgr_connectedComponents`). The build takes under a second and runs after every city import, or on demand with `python -m app.cli graph`.

Each edge also records the stretch of its segment's part that it covers (`part_from`, `part_to`), which maps nodes onto edges. That's what a future "finish these segments for me" planner needs: the graph is a standard pgRouting edges table, so `pgr_dijkstraCostMatrix`, `pgr_TSP`, and `pgr_dijkstraVia` (all installed) can run on it directly.

As of Sept 2026 the graph has 5,652 edges, 4,424 junctions, and 8 components: a main network of 381.2 mi plus 7 islands totaling 1.39 mi, which are shown on the map for review.

**Routes prefer cart paths alongside roads.** A road edge with a cart path (not a tunnel or bridge) within the road's own match radius along at least 80% of its length costs `route_parallel_road_factor` (3) times its length, so the router takes the path. Because the path lies within the road's match radius, running it still completes the road. `cost` carries the preference and `length_m` the true length, which every displayed distance uses. As of Sept 2026, 659 road edges (37.1 mi) have a path alongside.

## Route builder

The route is a start point plus an ordered list of legs, and each leg holds its geometry and length. The coverage layer stays visible underneath, so unrun segments are obvious while you draw.

**Click to route.** Each click snaps to the nearest edge and routes from the end of the current route using `pgr_withPoints`. That routes from the exact clicked point on the line, not the nearest intersection. Routing to the nearest intersection is what creates the extra spurs other route builders add on out-and-backs.

**Waypoints and dragging.** Every click is a waypoint marker. While planning, dragging a waypoint reroutes the legs on either side of it; retraces that follow are rebuilt from the new geometry, and the leg after a moved turnaround reroutes too. A drag dropped too far from any path or road reverts, and each drag is one undo step.

**Retrace.** "Retrace to start" appends a reversed copy of the whole route. Shift-clicking an earlier point on the route appends a reversed copy back to that point, and the point becomes a waypoint (the leg is split there, not rerouted), so the turnaround can be dragged later. Retracing copies the existing geometry and never calls the router, so it can't drift or add spurs.

**Undo and redo.** Every edit (click, retrace, clear) pushes a snapshot onto a history stack. Undo and redo step through it with buttons or Ctrl+Z / Ctrl+Shift+Z. Clear is undoable too.

**Distance.** The route's total distance in miles updates after every edit.

**Export.** Export produces a GPX file with a single track, named by date and distance. You import it into Garmin Connect as a course and sync it to the watch.

## Map UI

The map stays on Leaflet, since the viewer already validated it with this data. Its canvas renderer handles the node and segment counts involved. The basemap is OpenStreetMap's standard tiles, used within the OSMF tile policy (visible attribution, normal Referer, no bulk or prefetching) and rendered in grayscale so OSM's orange roads and green parks don't compete with the coverage colors. They also render to zoom 19, where the earlier Esri basemap stopped at 16. Layers:

- Cart path coverage in three states: complete segments; run intervals on segments that aren't complete yet; intervals not run. Road coverage in two: run and not run. An interval is run when both of its end nodes are hit, and consecutive intervals with the same state are drawn as one line, never across parts.
- Missed nodes as dots (cart paths on by default, roads off; shown from zoom 15)
- Hit nodes as dots, off by default; clicking one shows the run's date, name, and Intervals link
- Excluded segments, greyed out
- Run tracks, off by default

Since Phase 8 the coverage palette is "Graphite": finished ground is gray and color means unfinished (cart paths not run in red, the heaviest line; roads not run in amber). Its colors and line weights live in `app/static/tokens.css`, which the map reads at startup.

A top bar shows cart path completion (percentage, miles, segments) and road miles covered, the last sync, and the ways in: Plan a route, a Layers drawer (the legend, whose rows are the layer toggles), a Data drawer (find by OID, the stats table, sync detail), and the status page. The UI is desktop-first and usable on a phone.

The sync button runs the same incremental sync as the CLI (a week before the newest run through today) in the background, and the panel polls until it finishes, then reloads the coverage. Sync, import, and recompute share one lock, so only one runs at a time from any trigger; a second request gets a 409 from the API or an "another job is running" exit from the CLI. Every run of a data job is recorded in `job_run` (trigger, times, status, summary, error), which the panel reads for the last sync time.

```
GET  /stats                            metrics, counts, last sync
GET  /network?layer=cartpath|road      GeoJSON with coverage state
GET  /nodes?layer=&status=missed|hit
GET  /nodes/{id}                       hit run's date, name, Intervals id
GET  /activities
GET  /sync                             latest sync, last success, running?
POST /sync                             202 started, 409 busy, 503 no credentials
POST /refresh                          city check in the background; 202, 409 busy
GET  /status                           data health, city layers, alert episodes, recent jobs
GET  /changes                          segments from each layer's latest city change
POST /route/snap  {lat, lon} -> the point on the network
POST /route/leg   {from, to} -> latlngs, length_m, snapped from/to
                                 422 too far from the network or unreachable, 503 no graph
GET  /graph/islands                    edges not connected to the main network
```

Retrace, undo, redo, and GPX export run client-side.

## Phases

### Phase 1: Foundation

Stand up both containers, the repo, and CI. Import both city layers with filtering and exclusions, generate nodes, and render them on a map. The phase is done when the nodes look right and the totals match the cleaned city data: 780 counted cart path segments and 103.6 mi.

### Phase 2: Activity sync

Backfill runs from Intervals.icu and render the tracks. The phase is done when every Peachtree City run appears and nothing from elsewhere does.

### Phase 3: Matching and stats

Implement hits, the tunnel rule, metrics, and `recompute`. Then tune the radius against runs you remember. The phase is done when the remaining missed nodes are places you genuinely haven't run.

### Phase 4: Coverage map

Build the full map UI and stats panel.

### Phase 5: Routing and route builder

Build the graph, review islands, then add click-to-route, retrace, undo/redo, distance, and GPX export. The phase is done when an exported GPX imports into Garmin Connect as a course and follows the paths.

### Phase 6: Automation

Add the nightly sync and the city refresh with its change report. Record every job run (start, finish, status, summary, error) in a job log. Schedule jobs so they check daily whether there's anything to do and only work when their data is out of date. That way a failed run retries itself the next day.

As built:

- **Scheduling** is the Unraid User Scripts plugin, not the app: one nightly script (`deploy/unraid/user-scripts/run-ptc-nightly`, suggested 3:15 AM) runs `docker exec run-ptc python -m app.cli sync`, then `refresh`. The dev pair has no schedule.
- **The refresh replaces a monthly timer.** The city layers publish no layer-level edit date, so each night one statistics request per layer fetches a signature: feature count, highest OID, and latest `last_edited_date`. A layer whose signature differs from the one stored at its last successful import (`source_signature`) is imported, the graph rebuilt, and the new signature stored. Signatures are written only after everything succeeds, so a failed night retries. `refresh --force` imports regardless. The roads layer appears to be re-saved in bulk now and then (every road carries an edit date within the past year), which just produces a harmless 0-change import.
- **The change report** lists each added, changed, or removed segment (OID, name, type, length), then the impact before and after: cart path miles and segments complete, road miles covered, counted segments and miles per layer, second carriageways, and graph components and islands. It's the job's summary in `job_run`, the output in the User Scripts log, and the headline of the "city data updated" notice.
- **Alerts** use Unraid's `notify`, which reaches email through Unraid's own notification settings: a failed job is an alert, a job skipped because another was running (CLI exit code 75) is a warning, and new city data is a normal notice. Quiet nights send nothing.

### Phase 7: Admin and data health

Modeled on the Plane States admin page, minus its auth:

- A status page per data source (city layers, Intervals sync): last success, last attempt, last error, record counts, and a stale or current flag, with buttons to run a job now and refresh.
- Alerts on failures and staleness, sent only after a grace period and at most once per stale episode, with a stored marker preventing repeats. Each alert includes the last error and what to do. A failure to send an alert never breaks the job, and a "send test alert" button proves the alert path works. The channel (SMTP, ntfy, Pushover, a Discord webhook, or Apprise, which covers them all) is to be decided, since Cloudflare Email Routing isn't available on kirk.
- Freshness on the map: a banner when data is stale or the last job failed, and a layer highlighting the segments the last city refresh added or changed.

As built:

- **Health** (`app/health.py`) covers two sources: Intervals sync (the `sync` job) and city data (`refresh` and `import`). A source is *failing* when its latest finished attempt failed (including one interrupted by a restart), *stale* when its last success is older than `stale_after_hours` (36), and *current* otherwise. Each state carries advice, e.g. a 401 from Intervals points at the key.
- **The alert channel is Unraid's `notify`**, already set up to email. The app container can't reach it, so the app decides and the nightly User Script sends: after its jobs, the script runs `python -m app.cli alerts`, sends each notice, and confirms it with `alerts --sent KEY` only after `notify` succeeds, so a failed send retries the next night. An `alert_episode` row per problem is the stored marker: the first failure (or staleness) alerts at once, nothing repeats while it lasts, and a "recovered" notice closes it. The only alerts that repeat nightly come straight from the script, when the container is down or a job fails before the app can record it. The busy (exit 75) warning was dropped; repeated skips surface as staleness.
- **No "send test alert" button**, since the app can't call `notify`. The User Script's `TEST_ALERT=1` tests the whole path, and the status page says so and shows when the last alert went out.
- **Status page** (`/status.html`, from `GET /status`): a card per source with a Current/Stale/Failing badge, last success and last attempt, the error and advice, counts (runs by status; per layer the city's feature count and last edit, last import, counted and excluded), and a Run now button (`POST /sync`, `POST /refresh`). Plus the latest city change report, the nightly script's last run (jobs it starts are recorded with trigger `schedule`), recent alert episodes, and the last 20 jobs.
- **Map:** a banner (amber for stale, red for failing, each with an icon) links to the status page. The "Latest city changes" overlay (`GET /changes`, off by default, `#changes` turns it on) draws a yellow highlighter band under each segment from each layer's latest city change. The importer marks added and changed segments (`segment.city_change`, `changed_at`), except on a layer's first import; segment popups show when the city last changed them. Removed segments appear only in the change report.

### Phase 8: UI design and polish

One holistic design pass over the whole UI once every feature is in place, instead of styling each phase piecemeal: layout (a fixed sidebar was deferred from Phase 4), typography, the coverage palette in context, legend and layer controls, and the route builder's controls. Also consider collapsing each divided road to a single line on the map (a centerline between the carriageways) for both coverage and nodes; today both carriageways show the kept side's coverage and nodes sit on the kept side.

As built, from the Claude Design artifacts in `docs/design/` (direction 1b, palette B "Graphite"):

- **Layout:** the floating panel is gone. A top bar holds the numbers you watch (cart path completion with a meter, road coverage, the last sync) and the ways in. The Layers and Data drawers open one at a time under it, and the freshness banner sits between the bar and the map instead of over it. Leaflet's layer control is gone: each Layers row is a checkbox with its swatch, and each coverage state is its own Leaflet layer so it can be toggled.
- **Tokens:** `app/static/tokens.css` holds type, color, spacing, and the map's line colors and weights; the map reads the `--map-*` properties at startup, so colors live in one place.
- **Planning is a mode:** a dark route bar replaces the top bar at the same height (distance as the headline number, the tools, a help and error line, Done or Esc), the map gets a blue edge and a first-click hint, and the route is blue, the only cool color on the map.
- **Popups** share one shape: a swatch naming the line clicked, a headline, and label/value rows, with skeleton rows while loading.
- **Status page:** the two source cards side by side with a state edge color, errors in a wrapping monospace block, a running state (indeterminate bar, when it started), and friendlier trigger names (manual, nightly, command line).
- **Share pages:** `left.html` (what's still unrun) and `progress.html` (what's done), one template with one loud layer each and no controls. Linked from the Data drawer; like the rest of the app, they're reachable on the local network only.
- **Phone:** the bar stacks, the actions dock at the bottom, the drawers become bottom sheets, and the route bar becomes a two-row dock.

Each phase can be verified on the map before the next builds on it. Routing comes last because it doesn't depend on coverage, and graph topology is the fiddliest part to get right.

## Later

Designed or discussed but not built:

- **New miles on a route:** draw the route in two blues (new ground and ground already run) with a "3.10 mi new" figure. Needs `/route/leg` to return per-stretch coverage.
- **Elevation profile** under the map while planning, with gain and loss. Needs an elevation source (e.g. a USGS DEM loaded into PostGIS).
- **FIT course export** next to GPX, with turn cues only at real junctions, so the watch stops prompting turns on winding paths. Specced in [docs/specs/fit-course-export.md](specs/fit-course-export.md); starts with a test course on the Forerunner 955.
- **Divided roads as one centerline** on the map, for coverage and nodes; today both carriageways show the kept side's coverage and nodes sit on the kept side.

## Tunable defaults

State routes are counted. Node spacing is 20 m. Match radius is 10 m for cart paths (interior and end nodes set separately, both 10 m), 20 m for most roads, and 30 m for roads with `CLASS = Arterial` (SR-74 and SR-54). Tracks split at gaps over 100 m. Tunnels require both ends in the same run. All of these live in config and take effect on the next `recompute`.
