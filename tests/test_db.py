from datetime import date

from app import db
from app.config import load_settings


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == []
    tables = {r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"segment", "node", "source_duplicate", "activity", "schema_migrations"} <= tables


def test_repo_settings_load():
    s = load_settings()
    assert s.node_spacing_m == 20
    assert s.cartpath_counted_types == ("Path", "Bridge", "Tunnel")
    assert s.sync_run_types == ("Run", "TrailRun")
    assert s.sync_backfill_start == date(2023, 1, 1)
