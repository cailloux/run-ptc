# Unraid templates

Kirk has no docker-compose, so the two containers are Unraid templates, and
scheduled jobs are User Scripts (see `user-scripts/`).

## Setting up prod

1. Copy both XML files to `/boot/config/plugins/dockerMan/templates-user/` as
   `my-run-ptc-db.xml` and `my-run-ptc.xml`.
2. Docker tab → Add Container → pick `run-ptc-db`, set `POSTGRES_PASSWORD`, apply.
3. Add Container → pick `run-ptc`, set `DATABASE_URL` with the same password
   and the `INTERVALS_*` variables, apply. On startup the app applies
   migrations; the map loads empty until the next step.
4. Import the city data. This also builds the routing graph and records the
   city layers' signatures for the nightly refresh (about 30 s):

   ```sh
   docker exec run-ptc python -m app.cli import
   ```

5. Backfill runs from Intervals.icu. Each month's runs are matched as they
   arrive, so no recompute is needed afterward (about 3 minutes):

   ```sh
   docker exec run-ptc python -m app.cli sync
   ```

6. Open `http://kirk:8010` and check the totals against the dev pair.
7. Install the nightly job from `user-scripts/run-ptc-nightly` (see that
   README), run it once with `TEST_ALERT=1` to confirm alerts reach you, then
   set it back to `0`.

Secrets are entered in the Unraid UI only; the templates in git hold
placeholders.

## Editing exclusions without a rebuild

Add a path mapping on `run-ptc` from a folder holding `settings.yaml` and
`exclusions.yaml` to `/app/config` (read-only), then run
`docker exec run-ptc python -m app.cli exclusions`.
