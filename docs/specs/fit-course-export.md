# Spec: FIT course export with junction-only turn cues

Status: **specced, not started.** Written 2026-09-13. Start with the test course in Step 0 before building anything else.

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

**Unknowns** (Step 0 answers them):
1. Does Garmin Connect keep FIT course points on import, or reprocess them?
2. Which Forerunner 955 setting turns off the watch's own turn guidance while keeping course point alerts? Record the menu path.
3. Do course point alerts fire with the prompt text we give them (e.g. "Left onto path")?

## Step 0: test course (do first)

1. **Generate one FIT course by script.** Use a short loop you'd happily run through the 301/B03 stretch, with course points at its real junctions only. Use the same encoder planned for the real feature (below), so the script isn't throwaway work.
2. **Check the file** by decoding it with Garmin's official FIT SDK for Python (`garmin-fit-sdk`, decode only), and compare it with a Connect-exported course.
3. **You import it** through Connect and sync to the 955, with map-based turn guidance off, then run or walk it.
4. **Record the result here:**
   - Which prompts fired.
   - What they said.
   - The exact watch setting used.
   - Whether Connect preserved the course points (compare the course in Connect with the file).
5. **If Connect drops or regenerates course points,** try loading the file over USB into `GARMIN/NewFiles`. That bypasses Connect and tells us whether Connect is the cause.

**Decision point:**
- **Only our cues fire:** build the feature.
- **Phantom turns remain:** stop and rethink, e.g. a TCX course, or different watch settings.

Step 0 needs no requests to the city's GIS server. It uses the stored routing graph.

## Design

### Where cues come from

Legs are routed with `pgr_withPoints` (`app/routing.py`), which already returns the sequence of graph vertices the leg passes through. A vertex is a **decision point** when its degree in `route_edge` is 3 or more. At each decision point the leg passes:

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

3. **Forks:** if the other branches at the vertex include one within ~30° of the one taken, use `left_fork` / `right_fork`, so a split between two near-parallel paths reads as a fork, not a slight turn. This is optional and can be added after the first version.

**Degree-2 vertices** (one path running straight into the next) get no cue, since there's no choice to make there. This is exactly the case the phantom prompts get wrong.

### Straight cues

Every side path would otherwise produce a "straight". The tunable `fit_straight_cues` takes one of three values:
- **`ambiguous`** (default): emit "straight" only when another branch leaves within ±45° of straight ahead, where you could plausibly drift onto it.
- **`all`:** every decision point the route passes straight through.
- **`none`.**

Crossing a road at grade is always a decision point: the crossing vertex has degree 4.

### Names

A course point's name is short text; the watch shows about 15 characters. Use:
- The target's name when it's a road, e.g. "L Crosstown Dr".
- "L path" / "R path" for unnamed cart paths.
- "Straight" and "U-turn".

Check what the 955 actually displays during Step 0.

### Data flow

- **`POST /route/leg`** also returns `junctions`: the decision-point vertex ids with each one's index into `latlngs`. It's the same `pgr_withPoints` result, so it adds no cost.
- **The route builder** keeps each leg's junctions alongside its points:
  - **Splitting a leg** at a shift-click point splits its junction list.
  - **A retrace** reverses its junction list and adds a `u_turn` at the turnaround.
  - **A drag** reroutes, which returns fresh junctions.
- **Export:** the client POSTs `{name, points, junctions: [{vertex, index}], u_turns: [index]}` to a new **`POST /route/fit`**. The server classifies each junction from the polyline around the index (the bearings) and from the graph (the vertex's other branches, and the target segment's name and layer). It then returns the FIT file as a download.
- **Classification stays on the server,** next to the graph, and is unit-tested like the rest of the geometry code.

### FIT encoding

This is a small encoder written by us in `app/fit.py`. The FIT protocol is documented and a course needs only a handful of messages, so no new dependency is needed to write files. Tests decode the output with `garmin-fit-sdk`. Message order follows what Garmin's own course exports use; confirm it against a Connect-exported course in Step 0.

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

- The route bar gains **Export FIT** next to Export GPX. It's enabled when the route has two or more points.
- Both downloads are named `run-ptc-YYYY-MM-DD-X.XXmi`.
- Map preview of cues, optional: small arrows at the cue points while planning. This is worth doing if cue placement is hard to judge from the watch alone.

### Settings (`config/settings.yaml`)

```yaml
fit_turn_thresholds_deg: {straight: 20, slight: 45, turn: 135, sharp: 170}
fit_bearing_window_m: 15
fit_straight_cues: ambiguous      # ambiguous | all | none
fit_course_pace_min_per_mi: 9.0
```

## Tests (no network)

Unit tests on hand-built graphs in `tests/`:
- A T-junction: left and right turns classify correctly, and going straight at a T emits nothing when `fit_straight_cues` is `ambiguous` and no branch is near straight ahead.
- A winding degree-2 path emits no cues, even with 60° bends.
- A crossroads carried straight through emits a "straight" cue under `all` and `ambiguous`, and none under `none`.
- A retrace emits a `u_turn` at the turnaround, and its reversed junctions flip left and right.
- A route that starts or ends mid-edge has no cue at the click points.
- `/route/leg` returns junction indices that match the positions of their vertices.
- The FIT encoder:
  - Decodes cleanly with `garmin-fit-sdk`: the header and file CRCs, the message counts, and course point types and positions round-trip.
  - The record distances increase and the total equals the route length.

## Done when

1. Step 0 has passed, and its findings (watch setting, Connect behavior) are recorded in this file.
2. Export FIT produces a course that runs on the 955 with prompts only at real junctions and turnarounds, checked on at least one run over a winding stretch.
3. Tests pass on kirk and in CI.
