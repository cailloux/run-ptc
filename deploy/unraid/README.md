# Unraid templates

Kirk has no docker-compose, so the two containers are Unraid templates.

1. Copy both XML files to `/boot/config/plugins/dockerMan/templates-user/` as
   `my-run-ptc-db.xml` and `my-run-ptc.xml`.
2. Docker tab → Add Container → pick `run-ptc-db`, set `POSTGRES_PASSWORD`, apply.
3. Add Container → pick `run-ptc`, set `DATABASE_URL` with the same password, apply.
4. `docker exec run-ptc python -m app.cli import`, then open `http://kirk:8010`.

Secrets are entered in the UI only; the templates in git hold placeholders.

To edit exclusions without rebuilding the image, add a path mapping on `run-ptc`
from a folder holding `settings.yaml` and `exclusions.yaml` to `/app/config`
(read-only), then run `docker exec run-ptc python -m app.cli exclusions`.
