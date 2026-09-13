import pytest
from fastapi.testclient import TestClient

from app.jobs import JOB_LOCK
from tests.test_sync import FakeIntervals, city, run, walk


@pytest.fixture
def api(conn, db_url, monkeypatch):
    """The real app against the test database, with a fake Intervals client."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    from app.main import app
    original = app.state.intervals_client_factory
    with TestClient(app) as client:
        yield client, app
    app.state.intervals_client_factory = original


def test_sync_button_runs_a_sync_and_reports_it(api, conn):
    client, app = api
    city(conn)
    app.state.intervals_client_factory = lambda: FakeIntervals(
        [run("i1")], {"i1": walk(conn, (0, 0), (190, 0))})

    resp = client.post("/sync")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # TestClient finishes background tasks before returning.
    status = client.get("/sync").json()
    assert status["running"] is False
    assert (status["latest"]["id"], status["latest"]["status"], status["latest"]["trigger"]) == (
        job_id, "ok", "button")
    assert status["last_ok"]["headline"].startswith("1 new city run, ")

    last_sync = client.get("/stats").json()["last_sync"]
    assert last_sync["finished_at"] is not None
    assert last_sync["headline"].startswith("1 new city run")
    assert conn.execute("SELECT count(*) FROM node WHERE hit_at IS NOT NULL").fetchone()[0] > 0


def test_sync_button_refuses_while_another_job_runs(api, conn, db_url):
    client, app = api
    city(conn)
    app.state.intervals_client_factory = lambda: FakeIntervals([], {})
    conn.execute("SELECT pg_advisory_lock(%s)", (JOB_LOCK,))
    try:
        resp = client.post("/sync")
        assert resp.status_code == 409
        assert client.get("/sync").json()["running"] is True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))
    assert conn.execute("SELECT count(*) FROM job_run").fetchone()[0] == 0


def test_sync_button_without_credentials(api):
    client, app = api

    def missing():
        raise RuntimeError("INTERVALS_ATHLETE_ID and INTERVALS_API_KEY must be set to sync")

    app.state.intervals_client_factory = missing
    resp = client.post("/sync")
    assert resp.status_code == 503
    assert "INTERVALS_API_KEY" in resp.json()["detail"]


def test_network_returns_coverage_states(api, conn):
    client, _ = api
    city(conn)
    resp = client.get("/network?layer=cartpath")
    states = {f["properties"]["state"] for f in resp.json()["features"]}
    assert states == {"not_run"}
    # The client asks for gzip, as browsers do; the map's GeoJSON compresses ~6x.
    assert resp.headers["content-encoding"] == "gzip"
