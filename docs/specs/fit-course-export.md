# Spec: FIT course export with junction-only turn cues

Status: **built and tested on the backend** (`app/fit.py`, `POST /route/fit`); UI wiring (Export FIT next to Export GPX) is next. Written 2026-09-13, revised 2026-09-16 after Step 0 and after building past it -- see "What changed from the original design" below.

## Problem

Routes are exported as GPX tracks: a line of points with no turn information. Garmin fills that gap on its own. On winding cart paths the watch prompts turns that aren't real, for example on path 301 near the B03 junction (Crosstown Drive). On a Forerunner 955 the prompts most likely come from the watch matching the course to its own map (Garmin's map is based on OpenStreetMap) or from sharp heading changes in the line. Either way, the turn cues come from geometry the watch guesses at, not from the route's actual decisions.

## Goal

A course where the only prompts are:
- **A turn** where the route leaves one path or road for another at a junction.
- **"Straight"** where the route carries on through a junction that has other branches.
- **"U-turn"** at a retrace turnaround.

No cues on bends within a single path or road.

**Not goals:**
- Replacing GPX export. It stays, alongside FIT.
- Elevation.
- Distance-based pacing features.
- Sending courses to Garmin Connect directly. You import them yourself, through the Connect web or iOS app.

## Why this can work

A FIT course file carries **course points** (FIT message `course_point`): a position, a distance along the course, a type, and a short name. The FIT profile's `course_point` types include `left`, `right`, `straight`, `slight_left`, `slight_right`, `sharp_left`, `sharp_right`, `u_turn`, `left_fork`, and `right_fork`. With course points in the file, the watch can alert on them. Its own map-based turn guidance can be switched off on the watch, which you believe the 955 allows. Then the course points are the only prompts.

**Unknowns, answered by Step 0:**
1. **Does Garmin Connect keep FIT course points on import, or reprocess them?** Reprocesses them: importing through Connect Web does not keep the real navigation types (`left`, `right`, `slight_left`, ...) usable. The fallback is the `generic` course point type, with the real direction carried only in the point's `name` text -- Connect Web leaves those alone. See "Two output flavors" below.
2. Which Forerunner 955 setting turns off the watch's own turn guidance while keeping course point alerts, and whether alert text matches what we send -- not yet recorded. Confirm before relying on this for a real run.

Not yet tried: loading a file over USB into `GARMIN/NewFiles`, which may keep the real navigation types since it bypasses Connect entirely -- that's the `flavor=garmin` output.

## What changed from the original design

The original design (below, "Data flow") had `/route/leg` return junctions and the route builder carry them through every edit, so `/route/fit` would classify from a junction list the client already had. Built instead: `find_course_points` (`app/fit.py`) takes only the finished route's `latlngs` and re-derives the junctions itself, by snapping each point back onto the nearest `route_edge` within `snap_m` and looking for vertices of degree ≥ 3 (`app/fit.py`, `ponytail:` comment on `find_course_points`). This needed no changes to `/route/leg` or the route builder's edit paths (split, retrace, drag) -- `POST /route/fit` takes `{name, latlngs, flavor}` and nothing else. Trade-off: reruns the snap on every export instead of carrying junctions through edits; revisit if the route builder ever needs junctions for something else.

Also added, not in the original design: two output **flavors** (below), since Connect Web doesn't keep real navigation types.

### Where cues come from

At each decision point (a graph vertex the route passes with degree ≥ 3 in `route_edge`):

1. **Measure the turn:**
   - Take the incoming bearing over the last ~15 m before the vertex, and the outgoing bearing over the first ~15 m after it. Measuring over a distance keeps snap jitter and tiny zig-zags from counting as turns.
   - The turn angle is `outgoing − incoming`, normalized to −180°…180°, where negative means left.
2. **Classify it.** The thresholds are tunables in `config/settings.yaml`:

   | Angle | Cue |
   |---|---|
   | under 20° | `straight` (see "Straight cues") |
   | 20–45° | `slight_left` / `slight_right` |
   | 45–135° | `left` / `right` |
   | 135–170° | `sharp_left` / `sharp_right` |
   | over 170°, or a retrace turnaround | `u_turn` |

3. **Forks:** if the other branches at the vertex include one within ~30° of the one taken, use `left_fork` / `right_fork`, so a split between two near-parallel paths reads as a fork, not a slight turn. Built (`_refine` in `app/fit.py`).

**Degree-2 vertices** (one path running straight into the next) get no cue, since there's no choice to make there. This is exactly the case the phantom prompts get wrong.

### Straight cues

Every side path would otherwise produce a "straight". The tunable `fit_straight_cues` takes one of three values:
- **`ambiguous`** (default): emit "straight" only when another branch leaves within ±45° of straight ahead, where you could plausibly drift onto it.
- **`all`:** every decision point the route passes straight through.
- **`none`.**

Crossing a road at grade is always a decision point: the crossing vertex has degree 4.

### Names

A course point's name is short text; the watch shows about 15 characters. Built (`_CUE_NAME` in `app/fit.py`): "L path" / "R path" / "Bear L" / "Bear R" / "Sharp L" / "Sharp R" / "Straight" / "U-turn", the same text regardless of whether the target is a path or a road. **Not built:** using the road's real name (e.g. "L Crosstown Dr") when turning onto a named road -- `_CUE_NAME` doesn't look up `segment.name`. Add if plain "L path" / "R path" onto a road proves confusing on the watch.

Check what the 955 actually displays once Step 0's remaining unknowns are checked.

### Two output flavors

Since Connect Web only keeps the `generic` course point type (see "Unknowns" above), `encode_course` (`app/fit.py`) and `POST /route/fit` take a `flavor`:
- **`generic`** (default): every course point's `type` is `generic`; the real direction is in `name` only. What you export for Connect Web.
- **`garmin`**: the real navigation types (`left`, `right`, `slight_left`, ...). For side-loading over USB, or a device (e.g. an Edge) that may not filter them -- unconfirmed.

### Data flow (as built)

- `find_course_points(conn, latlngs, settings)` (`app/fit.py`) takes the finished route's points, snaps them back onto `route_edge` to find the decision points, and classifies each one. No junctions need to flow through `/route/leg` or the route builder's edits (see "What changed from the original design" above).
- **`POST /route/fit`** takes `{name, latlngs, flavor?}` (`flavor` defaults to `generic`) and returns the FIT file as `application/vnd.ant.fit`.
- **Classification stays on the server,** next to the graph, and is unit-tested like the rest of the geometry code.

### FIT encoding

Built in `app/fit.py`. The FIT protocol is documented and a course needs only a handful of messages, so no new dependency is needed to write files (`garmin-fit-sdk` is a dev/test-only dependency, used to decode and round-trip in tests). Message order follows what Garmin's own course exports use; not yet confirmed against a real Connect-exported course.

1. **File header:** 14 bytes, with a header CRC.
2. **`file_id`:** `type = course`, manufacturer `development`, `time_created`.
3. **`course`:** `sport = running`, `name`.
4. **`lap`:**
   - Start and end positions.
   - `total_distance`.
   - `total_elapsed_time` and `total_timer_time`.
   - `start_time` and `timestamp`.
5. **`event`:** timer start.
6. **`record`, one per point:**
   - `position_lat` and `position_long` in semicircles (degrees × 2³¹ / 180).
   - `distance` (m, scale 100).
   - `timestamp`, synthesized from a nominal pace. The watch uses it for the virtual partner. Make the pace a tunable, `fit_course_pace_min_per_mi`, default 9:00.
7. **`course_point`, one per cue:**
   - `position_lat` and `position_long`.
   - `distance`.
   - `timestamp`.
   - `type` and `name`.
8. **`event`:** timer stop.
9. **File CRC-16.**

Take every field number, type, scale, and enum value from the FIT SDK's profile (`Profile.xlsx` or the SDK's generated profile), not from memory. This includes the full `course_point` type list.

### UI

Built: the route bar gains **Export FIT** (generic, default) next to Export GPX, with a caret opening **Export Garmin** (the `garmin` flavor). Both FIT buttons are enabled when the route has two or more points, same as Export GPX. Downloads: `run-ptc-YYYY-MM-DD-X.XXmi.fit` (generic), `run-ptc-YYYY-MM-DD-X.XXmi-garmin.fit`.

**Not built:** map preview of cues (small arrows at the cue points while planning). Worth adding if cue placement is hard to judge from the watch alone.

### Settings (`config/settings.yaml`)

```yaml
fit_turn_thresholds_deg: {straight: 20, slight: 45, turn: 135, sharp: 170}
fit_bearing_window_m: 15
fit_straight_cues: ambiguous      # ambiguous | all | none
fit_cluster_m: 20
fit_course_pace_min_per_mi: 9.0
```

## Tests (no network)

`tests/test_fit.py`, on hand-built graphs:
- `classify_turn` thresholds and `turn_angle` on straight and turning lines.
- `_refine`: all three `fit_straight_cues` modes, the near-parallel-branch fork relabeling, real turns passing through unchanged.
- `find_course_points`: a real turn at a junction; a plain bend with no cue; straight-through with and without a close branch; a dead-straight continuation not mistaken for its own branch; a near-parallel fork; decision vertices merging across a short connector and staying separate across a long one; the same vertex revisited close together (merges) and far apart (stays separate); a road crossed at grade with and without a real turn there.
- `encode_course`: round-trips through `garmin-fit-sdk` for both flavors -- `garmin` decodes the real type (`left`), `generic` decodes `generic` with the real direction still in `name`.

`tests/test_api_route.py`: `POST /route/fit` returns `application/vnd.ant.fit` for both flavors.

## Done when

1. Backend and API built and tested (`app/fit.py`, `POST /route/fit`) -- done.
2. UI wired (Export FIT / Export Garmin next to Export GPX) -- done.
3. Tests pass on kirk and in CI -- done (`scripts/kirk-test.sh`, 2026-09-16).
4. A real run on the 955 confirms: map-based turn guidance off, course point alerts fire, and the alert text is legible -- not yet done. Do this before relying on the generic flavor for a real run; if Connect Web still shows phantom turns even with only generic points, rethink (e.g. try the `garmin` flavor over USB, or a TCX course).
