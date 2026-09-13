# CLAUDE.md

## Project

Single-user planning tool for running every cart path and road in Peachtree City, GA. It tracks run coverage from Intervals.icu, measures cart path completion to a 100% ("Hard Mode") standard, and builds routes exported as GPX for Garmin.

**The spec is `docs/PLAN.md`. Read it before starting any work.** It records decisions already made; don't revisit them without asking.

## How to work

- Build one phase at a time, in the order in `docs/PLAN.md`. Stop at each phase's "done when" check and report results before starting the next phase.
- Start each phase in plan mode: propose files, schema changes, and steps before writing code.
- Ask before deciding anything the spec doesn't cover. Explain the reasoning behind a proposal briefly.
- Keep responses short. Lead with what changed and what to verify.
- If the spec looks wrong once you're in the code (a field name, a data shape, an API behavior), stop and say so. Don't silently work around it.

## Stack

- Python 3.14, FastAPI, psycopg 3. Raw SQL for spatial queries; no ORM. The Dockerfile's base image is the single source of the Python version; CI runs the tests inside that image.
- Postgres with PostGIS and pgRouting via the `pgrouting/pgrouting` image. The stock `postgres` and `postgis/postgis` images lack pgRouting.
- Plain SQL migrations in `db/migrations/`, numbered and applied in order.
- Frontend: Leaflet with plain JS and no build step, served by FastAPI from `app/static/`. Load libraries from cdnjs. Design tokens (type, color, spacing, and the map's line colors and weights) live in `app/static/tokens.css`; the map reads the `--map-*` properties at startup, so don't copy colors into JS. The design reference is `docs/design/`.
- pytest for tests.

## Geometry rules

- Store all geometry in EPSG:32616 (UTM 16N). Distances and buffers are in meters.
- Convert to EPSG:4326 only at the API boundary.
- The city server can return UTM directly with `outSR=32616`.
- GIST-index every geometry column.
- Both layers contain MultiLineString features (141 counted cart paths, 23 roads), and the parts are often separated by real gaps. Store one segment per source feature as MultiLineString, but never let nodes, coverage intervals, or routing edges span parts. Work per part (`node.part_idx`).
- Compute every length from geometry. Never read the city's length attributes (`LengthMile`, `LENGTH`, `Length`, `Shape.STLength()`).
- Don't measure "how much of a line is near something" with `ST_Length(ST_Intersection(line, ST_Buffer(...)))`. The pgrouting image ships GEOS 3.9.0, which returns EMPTY for that intersection when the line is exactly horizontal or vertical. Sample points and use `ST_DWithin` (the `share_within()` SQL function, or an indexed `EXISTS` per sample for large sets).
- Import clean-up (duplicates, slivers under 1 m, roads outside the city) is described in `docs/PLAN.md` under "Clean-up on import."

## External services

**City ArcGIS layers:** public, no auth. The URLs and paging rules are in `docs/PLAN.md` under "Data sources." The server caps each response at 1,000 records, so always page with `resultOffset`.
- The nightly refresh and real data updates are fine. While testing or debugging, ask before running anything that calls the city server: `refresh` (with or without `--force`), `import`, the status page's "Check city now", or a probe with curl. A forced refresh is about 7 requests. Test import and refresh logic against the tests' fake city instead.

**Intervals.icu:**
- Auth is HTTP Basic with the literal username `API_KEY` and the API key as the password.
- The athlete ID and key come from env vars.
- Check activity type names and the stream response shape against the Intervals OpenAPI spec, not memory.
- Keep sync read-only. This app never writes to Intervals.

## Secrets and config

- Secrets live in env vars only: `DATABASE_URL`, `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID` on the app, and `POSTGRES_PASSWORD` on the db. Commit `.env.example` with placeholders and never commit `.env`.
- Never put a key, token, or password in code, tests, fixtures, docs, or this file. A key once leaked through a CLAUDE.md in git history on another project.
- Tunables (node spacing, match radii, gap-split threshold) live in `config/settings.yaml`.
- Exclusions live in `config/exclusions.yaml`, using the format in `docs/PLAN.md`.

## Testing

Unit-test the geometry logic with small hand-built fixtures, not live city data:
- Node generation, including short segments and MultiLineStrings
- Track splitting at GPS gaps
- Node matching at the radius boundary
- The tunnel both-ends-same-run rule
- The completion metrics
- Exclusion loading, including orphan and name-mismatch warnings

Tests must run without network access.

## Timezone

Store timestamps in UTC. Display them in US Eastern. Dates passed to Intervals are always explicit, never "today" or relative.

## Git

- Stage files explicitly with `git add <file>`. Never `git add .` or `git add -A`.
- Use one branch per phase and open a PR when the phase passes its check.
- Keep commits small, with messages that say what changed and why.

## Deployment

- The app is called Run PTC. It runs in Docker on kirk (Unraid) and is reached on the local network. `ssh kirk` works from the dev Mac.
- Kirk has no docker-compose. The two containers, `run-ptc` (app, host port 8010) and `run-ptc-db` (`pgrouting/pgrouting`, no published port), are Unraid templates in `deploy/unraid/`, on the `services` bridge network. Favor env vars for all config so it can be set in the Unraid Docker UI.
- Appdata path: `/mnt/user/appdata/run-ptc/`. The db data is in `pgdata/`, the dev pair (`scripts/kirk-dev.sh`) in `dev/`, and the synced source for tests in `src/`.
- The dev pair (`run-ptc-dev` at http://192.168.50.2:8011, `run-ptc-dev-db`) is the standing development environment. Keep it running; don't tear it down after a phase. The user switches to the Unraid-managed containers manually.
- The app applies pending migrations and reapplies exclusions on startup, and marks any `job_run` rows left `running` by a crash as interrupted.
- Sync, import, refresh, graph, and recompute share one advisory lock (`app/jobs.py`) whether started from the CLI, the nightly script, or a button on the map or status page, and each run is logged in `job_run`. Run new data jobs through `start_job(...).run(fn)`.
- The app image has a `HEALTHCHECK` using curl against `/health`.
- CI: GitHub Actions builds the image and pushes it to GHCR. Dependabot covers pip, Docker, and Actions.
- Authelia and Traefik are not in v1. Don't add them unless asked.

## Commands

The dev Mac has no Docker or project Python, so tests and the dev stack run on kirk over SSH. `gh` is at `/opt/homebrew/bin/gh`.

```
# run tests (syncs source to kirk, runs pytest on an internet-less network)
scripts/kirk-test.sh
# start or refresh the dev pair on kirk (http://kirk:8011)
scripts/kirk-dev.sh
# apply migrations (also automatic on app startup)
ssh kirk docker exec run-ptc python -m app.cli migrate
# import city layers
ssh kirk docker exec run-ptc python -m app.cli import
# reapply exclusions after editing config/exclusions.yaml
ssh kirk docker exec run-ptc python -m app.cli exclusions
# sync runs from Intervals.icu (default: a week before the newest run through today;
# add --from/--to YYYY-MM-DD, --dry-run, or --refetch)
ssh kirk docker exec run-ptc python -m app.cli sync
# list synced runs, e.g. to check which were classified outside the city
ssh kirk docker exec run-ptc python -m app.cli activities --status outside
# re-split tracks, reassign radii, clear hits, and replay every run (after tuning settings)
ssh kirk docker exec run-ptc python -m app.cli recompute
# completion metrics and missed nodes by distance to the nearest run (read-only)
ssh kirk docker exec run-ptc python -m app.cli stats
# rebuild the routing graph and list its islands (import also rebuilds it)
ssh kirk docker exec run-ptc python -m app.cli graph
# import any city layer the city has changed since the last import, with a change report
# (--force imports regardless). Runs nightly from the User Script.
ssh kirk docker exec run-ptc python -m app.cli refresh
# data-health notices for the nightly script to send (it marks each one sent
# with --sent KEY); the status page is /status.html
ssh kirk docker exec run-ptc python -m app.cli alerts
```

Nightly jobs run from the Unraid User Scripts template in `deploy/unraid/user-scripts/run-ptc-nightly`, passing `--trigger schedule` so `job_run` records them as nightly. The app can't reach Unraid's `notify`, so it decides what to send (`app/health.py`: alert once per failing or stale episode, then a "recovered" notice) and the script sends it. A job that fails before the app records it (exit `1` without a "`<job> failed (job N)`" line) and a stopped container alert straight from the script. The dev pair has no nightly script, so its data shows as stale 36 h after the last manual sync or refresh.

Use `run-ptc-dev` in place of `run-ptc` to target the dev pair.
