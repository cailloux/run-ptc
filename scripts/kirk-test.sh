#!/usr/bin/env bash
# Sync the working tree to kirk and run pytest there against a throwaway
# PostGIS container. Both containers sit on an --internal Docker network, so
# the tests physically cannot reach the internet.
#
# Usage: scripts/kirk-test.sh [pytest args...]
set -euo pipefail

HOST=${KIRK_HOST:-kirk}
SRC=/mnt/user/appdata/run-ptc/src
PG_IMAGE=pgrouting/pgrouting:17-3.5-4.0.1

cd "$(dirname "$0")/.."
rsync -a --delete --exclude .git --exclude .env --exclude __pycache__ --exclude .pytest_cache \
    ./ "$HOST:$SRC/"

ssh "$HOST" bash -s -- "$SRC" "$PG_IMAGE" "$@" <<'REMOTE'
set -euo pipefail
SRC=$1 PG_IMAGE=$2
shift 2
NET=run-ptc-test
DB=run-ptc-test-db

cleanup() {
    docker rm -f "$DB" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker build -q --target test -t run-ptc:test "$SRC" >/dev/null
docker network create --internal "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --tmpfs /var/lib/postgresql/data \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=test "$PG_IMAGE" >/dev/null

# The image's init phase listens only on a socket; TCP readiness means done.
for _ in $(seq 60); do
    docker exec "$DB" pg_isready -h 127.0.0.1 -U postgres -d test -q && break
    sleep 1
done

docker run --rm --network "$NET" \
    -e TEST_DATABASE_URL="postgresql://postgres@$DB:5432/test" \
    run-ptc:test pytest -q -p no:cacheprovider tests "$@"
REMOTE
