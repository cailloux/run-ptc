import logging
import time
from pathlib import Path

import psycopg

from app.config import MIGRATIONS_DIR, database_url

log = logging.getLogger(__name__)

# Arbitrary key so concurrent migrators (app startup and the CLI) serialize.
MIGRATION_LOCK = 7_316_240


def connect(url: str | None = None) -> psycopg.Connection:
    """Autocommit connection. Group multi-statement work with conn.transaction().

    JIT is off: the planner's cost estimates for the spatial queries run far
    too high, so Postgres spent ~1.8 s compiling the coverage query (/network)
    that then ran in 0.1-0.3 s. The jobs run no slower without it.
    """
    return psycopg.connect(url or database_url(), autocommit=True, options="-c jit=off")


def connect_with_retry(timeout_s: float = 60) -> psycopg.Connection:
    """Wait for the database. Unraid starts containers without ordering."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return connect()
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            log.info("database not ready, retrying")
            time.sleep(2)


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in filename order, each in its own transaction."""
    applied_now = []
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            log.info("applied migration %s", path.name)
            applied_now.append(path.name)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    return applied_now
