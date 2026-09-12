from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cli
from app.health import FIELD_SEP, RECORD_SEP, alerts, mark_sent, source_health
from app.jobs import JOB_LOCK
from tests.helpers import SETTINGS, cartpath, line, road, run_import

NOW = datetime(2026, 9, 12, 7, 30, tzinfo=UTC)   # 3:30 AM Eastern
HOUR = timedelta(hours=1)


def job(conn, name, status, finished, error=None, summary="done", trigger="cli"):
    conn.execute("""
        INSERT INTO job_run (job, trigger, started_at, finished_at, status, summary, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (name, trigger, finished - timedelta(seconds=5), None if status == "running" else finished,
          status, summary if status == "ok" else None, error))


def state(conn, source, now=NOW):
    return source_health(conn, SETTINGS, source, now).state


# ---- health ---------------------------------------------------------------------


def test_recent_success_is_ok_and_an_old_one_stale(conn):
    job(conn, "sync", "ok", NOW - 2 * HOUR)
    assert state(conn, "sync") == "ok"
    assert state(conn, "sync", NOW + 33 * HOUR) == "ok"       # 35 h old
    assert state(conn, "sync", NOW + 35 * HOUR) == "stale"    # 37 h old


def test_never_succeeded_is_stale(conn):
    h = source_health(conn, SETTINGS, "city", NOW)
    assert (h.state, h.problem()) == ("stale", "City data has never succeeded")


def test_a_failed_latest_attempt_is_failing_even_after_a_success(conn):
    job(conn, "sync", "ok", NOW - 25 * HOUR)
    job(conn, "sync", "failed", NOW - HOUR, error="HTTPStatusError: Client error '401 Unauthorized'\nmore")
    h = source_health(conn, SETTINGS, "sync", NOW)
    assert h.state == "failing"
    assert h.problem() == "Intervals sync failing: HTTPStatusError: Client error '401 Unauthorized'"
    assert "INTERVALS_API_KEY" in h.advice


def test_a_running_job_doesnt_hide_the_last_result(conn):
    job(conn, "sync", "failed", NOW - HOUR, error="boom")
    job(conn, "sync", "running", NOW)
    assert state(conn, "sync") == "failing"


def test_city_counts_both_refresh_and_import(conn):
    job(conn, "refresh", "failed", NOW - 3 * HOUR, error="RuntimeError: city server returned no statistics")
    assert state(conn, "city") == "failing"
    job(conn, "import", "ok", NOW - HOUR)
    h = source_health(conn, SETTINGS, "city", NOW)
    assert (h.state, h.last_ok["job"]) == ("ok", "import")
    job(conn, "sync", "failed", NOW, error="boom")   # another source's job
    assert state(conn, "city") == "ok"


def test_an_interrupted_job_is_failing_with_its_own_advice(conn):
    job(conn, "refresh", "failed", NOW - HOUR, error="interrupted: the process running it stopped")
    h = source_health(conn, SETTINGS, "city", NOW)
    assert h.state == "failing" and h.advice.startswith("The app restarted")


# ---- alert episodes -----------------------------------------------------------------


def notices(conn, now=NOW):
    return alerts(conn, SETTINGS, now)


def test_a_failure_alerts_once_then_recovers_once(conn):
    job(conn, "import", "ok", NOW - HOUR)
    error = ("HTTPStatusError: Client error '401 Unauthorized' for url 'https://intervals.icu/api/v1/x?y=1'\n"
             "For more information check: https://developer.mozilla.org/")
    job(conn, "sync", "failed", NOW - HOUR, error=error)

    [alert] = notices(conn)
    assert (alert.key, alert.severity, alert.subject) == ("1:alert", "alert", "Run PTC: intervals sync failing")
    # The one-liner drops the URL; the body keeps the whole error.
    assert alert.description == "Intervals sync failing: HTTPStatusError: Client error '401 Unauthorized'"
    assert alert.body.startswith("Intervals sync is failing.\n")
    assert error in alert.body
    assert "What to do: Intervals rejected the credentials" in alert.body

    # Not marked sent (notify failed?): it comes back the next night.
    assert [n.key for n in notices(conn, NOW + 24 * HOUR)] == ["1:alert"]
    mark_sent(conn, "1:alert")
    assert notices(conn, NOW + 24 * HOUR) == []
    # Still failing days later, now stale too: the same episode, no repeat.
    job(conn, "refresh", "ok", NOW + 71 * HOUR)   # keeps city data current
    assert notices(conn, NOW + 72 * HOUR) == []

    job(conn, "sync", "ok", NOW + 73 * HOUR, summary="3 new city runs, 120 nodes newly hit\ndetail")
    [recovered] = notices(conn, NOW + 73 * HOUR)
    assert (recovered.key, recovered.severity) == ("1:recovered", "normal")
    assert recovered.description == "Intervals sync is working again: 3 new city runs, 120 nodes newly hit"
    mark_sent(conn, "1:recovered")
    assert notices(conn, NOW + 74 * HOUR) == []
    assert conn.execute("SELECT count(*) FROM alert_episode WHERE closed_at IS NULL").fetchone()[0] == 0


def test_staleness_alerts_on_its_own(conn):
    job(conn, "import", "ok", NOW - HOUR)
    job(conn, "sync", "ok", NOW - 40 * HOUR)
    [alert] = notices(conn)
    assert alert.subject == "Run PTC: intervals sync stale"
    assert alert.description == "Intervals sync stale: no success since Sep 10, 11:30 AM"
    assert alert.body.startswith("Intervals sync hasn't succeeded in over 36 hours.")


def test_a_recovery_before_the_alert_went_out_closes_quietly(conn):
    job(conn, "import", "ok", NOW - HOUR)
    job(conn, "sync", "failed", NOW - HOUR, error="boom")
    assert len(notices(conn)) == 1                 # never marked sent
    job(conn, "sync", "ok", NOW)
    assert notices(conn) == []
    assert conn.execute("SELECT closed_at IS NOT NULL FROM alert_episode").fetchone()[0]


def test_breaking_again_before_the_recovery_notice_keeps_one_episode(conn):
    job(conn, "import", "ok", NOW - HOUR)
    job(conn, "sync", "failed", NOW - 3 * HOUR, error="boom")
    mark_sent(conn, notices(conn)[0].key)
    job(conn, "sync", "ok", NOW - 2 * HOUR)
    assert [n.key for n in notices(conn)] == ["1:recovered"]   # not sent
    job(conn, "sync", "failed", NOW - HOUR, error="boom again")
    assert notices(conn) == []
    assert conn.execute("SELECT count(*), bool_and(recovered_at IS NULL) FROM alert_episode").fetchone() == \
        (1, True)


def test_cli_prints_records_and_marks_them_sent(conn, db_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)
    job(conn, "sync", "ok", datetime.now(UTC) - HOUR)
    assert cli.main(["alerts"]) == 0             # city has never run
    out = capsys.readouterr().out
    [record] = out.split(RECORD_SEP)[:-1]
    key, severity, subject, description, body = record.split(FIELD_SEP)
    assert (key, severity, subject, description) == (
        "1:alert", "alert", "Run PTC: city data stale", "City data has never succeeded")
    assert "\n" in body
    assert cli.main(["alerts", "--sent", key]) == 0
    cli.main(["alerts"])
    assert capsys.readouterr().out == ""


def test_the_trigger_flag_is_recorded(conn, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    assert cli.main(["--trigger", "schedule", "graph"]) == 0
    assert conn.execute("SELECT job, trigger FROM job_run").fetchone() == ("graph", "schedule")


# ---- city changes ---------------------------------------------------------------------


def changes(conn):
    return dict(conn.execute(
        "SELECT source_oid, city_change FROM segment WHERE changed_at IS NOT NULL").fetchall())


def test_imports_mark_added_and_changed_segments_but_not_the_first_import(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 50), (100, 50)))])
    assert changes(conn) == {}
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0))),        # unchanged
                                  cartpath(2, line((0, 50), (120, 50))),       # changed
                                  cartpath(3, line((0, 90), (60, 90)))])       # added
    assert changes(conn) == {2: "changed", 3: "added"}


# ---- API ------------------------------------------------------------------------------


class MockCity:
    """The city's ArcGIS server over httpx.MockTransport: statistics and features."""

    def __init__(self, cartpaths, roads):
        self.features = {"cartpath": cartpaths, "road": roads}

    def handler(self, request):
        layer = "cartpath" if "GolfCartPath" in request.url.path else "road"
        feats = self.features[layer]
        if "outStatistics" in request.url.params:
            return httpx.Response(200, json={"features": [{"attributes": {
                "n": len(feats), "max_oid": 1000 + len(feats), "max_edited": 1767225600000}}]})
        return httpx.Response(200, json={
            "type": "FeatureCollection", "features": feats,
            "crs": {"type": "name", "properties": {"name": "EPSG:32616"}}})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def api(conn, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from app.main import app
    original = app.state.city_client_factory
    with TestClient(app) as client:
        yield client, app
    app.state.city_client_factory = original


def test_refresh_button_runs_a_refresh_and_status_reports_it(api, conn):
    client, app = api
    city = MockCity([cartpath(1, line((0, 0), (100, 0)))], [road(1, line((0, 200), (100, 200)))])
    app.state.city_client_factory = city.client
    assert client.post("/refresh").status_code == 202

    city.features["cartpath"].append(cartpath(2, line((0, 50), (100, 50))))
    assert client.post("/refresh").status_code == 202

    s = client.get("/status").json()
    assert s["sources"]["city"]["state"] == "ok"
    assert s["sources"]["city"]["last_ok"]["headline"].startswith("cart paths: 1 added")
    assert s["sources"]["city"]["layers"]["cartpath"]["city_features"] == 2
    change = s["sources"]["city"]["last_change"]
    assert (change["added"], change["changed"], change["job"]) == (1, 0, "refresh")
    assert change["report"].startswith("cart paths: 1 added")
    assert s["sources"]["sync"]["state"] == "stale"
    assert [j["trigger"] for j in s["jobs"]] == ["button", "button"]
    assert s["nightly"] == {"last_run": None, "overdue": False}

    [f] = client.get("/changes").json()["features"]
    assert (f["properties"]["source_oid"], f["properties"]["change"]) == (2, "added")


def test_refresh_button_refuses_while_another_job_runs(api, conn):
    client, app = api
    app.state.city_client_factory = MockCity([], []).client
    conn.execute("SELECT pg_advisory_lock(%s)", (JOB_LOCK,))
    try:
        assert client.post("/refresh").status_code == 409
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))


def test_changes_shows_each_layers_latest_change_only(api, conn):
    client, _ = api
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 50), (100, 50)))])
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 50), (100, 50))),
                                  cartpath(3, line((0, 90), (60, 90)))])
    assert [f["properties"]["source_oid"] for f in client.get("/changes").json()["features"]] == [3]
