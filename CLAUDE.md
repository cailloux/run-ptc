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

- Python 3.12, FastAPI, psycopg 3. Raw SQL for spatial queries; no ORM.
- Postgres with PostGIS and pgRouting via the `pgrouting/pgrouting` image. The stock `postgres` and `postgis/postgis` images lack pgRouting.
- Plain SQL migrations in `db/migrations/`, numbered and applied in order.
- Frontend: Leaflet with plain JS and no build step, served by FastAPI from `app/static/`. Load libraries from cdnjs.
- pytest for tests.

## Geometry rules

- Store all geometry in EPSG:32616 (UTM 16N). Distances and buffers are in meters.
- Convert to EPSG:4326 only at the API boundary.
- The city server can return UTM directly with `outSR=32616`.
- GIST-index every geometry column.
- Road features arrive as MultiLineString. Normalize them before generating nodes or routing edges.

## External services

**City ArcGIS layers:** public, no auth. The URLs and paging rules are in `docs/PLAN.md` under "Data sources." The server caps each response at 1,000 records, so always page with `resultOffset`.

**Intervals.icu:**
- Auth is HTTP Basic with the literal username `API_KEY` and the API key as the password.
- The athlete ID and key come from env vars.
- Check activity type names and the stream response shape against the Intervals OpenAPI spec, not memory.
- Keep sync read-only. This app never writes to Intervals.

## Secrets and config

- Secrets live in env vars only: `DATABASE_URL`, `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`. Commit `.env.example` with placeholders and never commit `.env`.
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

- The app runs in Docker on kirk (Unraid) and is reached on the local network.
- `docker-compose.yml` defines two services: `db` and `app`.
- Appdata path: `/mnt/user/appdata/cartpath-tracker/` (confirm before creating).
- The app image has a `HEALTHCHECK` using curl against `/health`.
- CI: GitHub Actions builds the image and pushes it to GHCR. Dependabot covers pip, Docker, and Actions.
- Authelia and Traefik are not in v1. Don't add them unless asked.

## Commands

Fill these in as Phase 1 sets them up:

```
# start stack
# apply migrations
# import city layers
# sync runs from Intervals.icu
# recompute all matches
# run tests
```
