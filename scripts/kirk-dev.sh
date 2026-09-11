#!/usr/bin/env bash
# Build the working tree on kirk and (re)start the dev pair:
#   run-ptc-dev-db  PostGIS + pgRouting, data in /mnt/user/appdata/run-ptc/dev/pgdata
#   run-ptc-dev     the app, at http://kirk:8011
# The dev database password is generated on kirk and never leaves it.
set -euo pipefail

HOST=${KIRK_HOST:-kirk}
BASE=/mnt/user/appdata/run-ptc
PG_IMAGE=pgrouting/pgrouting:17-3.5-4.0.1

cd "$(dirname "$0")/.."
rsync -a --delete --exclude .git --exclude .env --exclude __pycache__ --exclude .pytest_cache \
    ./ "$HOST:$BASE/src/"

ssh "$HOST" bash -s -- "$BASE" "$PG_IMAGE" <<'REMOTE'
set -euo pipefail
BASE=$1 PG_IMAGE=$2
DEV=$BASE/dev
mkdir -p "$DEV/pgdata"

if [ ! -f "$DEV/db.env" ]; then
    (
        umask 077
        pw=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
        printf 'POSTGRES_USER=runptc\nPOSTGRES_DB=runptc\nPOSTGRES_PASSWORD=%s\n' "$pw" > "$DEV/db.env"
        printf 'DATABASE_URL=postgresql://runptc:%s@run-ptc-dev-db:5432/runptc\nTZ=America/New_York\n' \
            "$pw" > "$DEV/app.env"
    )
fi

docker build -q -t run-ptc:dev "$BASE/src" >/dev/null

if ! docker container inspect run-ptc-dev-db >/dev/null 2>&1; then
    docker run -d --name run-ptc-dev-db --network services --env-file "$DEV/db.env" \
        -v "$DEV/pgdata:/var/lib/postgresql/data" "$PG_IMAGE" >/dev/null
fi

docker rm -f run-ptc-dev >/dev/null 2>&1 || true
docker run -d --name run-ptc-dev --network services -p 8011:8000 --env-file "$DEV/app.env" \
    run-ptc:dev >/dev/null
echo "run-ptc-dev started on http://kirk:8011"
REMOTE
