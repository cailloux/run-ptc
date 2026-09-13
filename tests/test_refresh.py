from datetime import UTC, datetime

import httpx
import pytest

from app import cli
from app.arcgis import LayerSignature, layer_signature
from app.exclusions import Exclusions
from app.jobs import JOB_LOCK
from app.refresh import LIST_CAP, refresh, stored_signature
from tests.helpers import SETTINGS, cartpath, line, road

EDITED = datetime(2026, 4, 21, 12, 21, 35, tzinfo=UTC)


class FakeCity:
    """Stands in for the ArcGIS server: a signature and features per layer."""

    def __init__(self, cartpaths, roads):
        self.features = {"cartpath": cartpaths, "road": roads}
        self.sigs = {layer: LayerSignature(len(f), 1000 + len(f), EDITED) for layer, f in self.features.items()}
        self.fetched = []
        self.fail = set()

    def signature(self, client, url, oid_field):
        return self.sigs["cartpath" if "GolfCartPath" in url else "road"]

    def fetch(self, client, url, oid_field):
        layer = "cartpath" if "GolfCartPath" in url else "road"
        if layer in self.fail:
            raise RuntimeError(f"{layer} download failed")
        self.fetched.append(layer)
        return self.features[layer]

    def run(self, conn, force=False):
        return refresh(conn, None, SETTINGS, Exclusions(), force=force,
                       signature=self.signature, fetch=self.fetch)


@pytest.fixture
def city():
    return FakeCity(
        cartpaths=[cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 50), (100, 50)))],
        roads=[road(1, line((0, 200), (100, 200)), name="FIRST ST")],
    )


# ---- signature request --------------------------------------------------------


def test_layer_signature_parses_statistics_in_any_case():
    def handler(request):
        assert request.url.params["where"] == "1=1"
        return httpx.Response(200, json={"features": [{"attributes": {
            "N": 1563, "MAX_OID": 22558, "MAX_EDITED": int(EDITED.timestamp() * 1000)}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert layer_signature(client, "https://gis.example/layer/3", "OBJECTID_1") == \
        LayerSignature(1563, 22558, EDITED)


def test_layer_signature_raises_on_arcgis_errors():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"error": {"code": 400, "message": "bad field"}})))
    with pytest.raises(RuntimeError, match="bad field"):
        layer_signature(client, "https://gis.example/layer/3", "OBJECTID_1")


def test_layer_signature_raises_when_the_server_returns_no_rows():
    # Seen from the city server on 2026-09-12: the fields, but no features.
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"fields": [{"name": "n"}], "features": []})))
    with pytest.raises(RuntimeError, match="returned no statistics"):
        layer_signature(client, "https://gis.example/layer/3", "OBJECTID_1")


# ---- refresh decisions ----------------------------------------------------------


def test_first_refresh_imports_everything_and_records_signatures(conn, city):
    report = city.run(conn)
    assert sorted(city.fetched) == ["cartpath", "road"]
    assert report.headline() == "cart paths: 2 added, 0 changed, 0 removed; roads: 1 added, 0 changed, 0 removed"
    assert stored_signature(conn, "road") == city.sigs["road"]
    assert conn.execute("SELECT count(*) FROM route_edge").fetchone()[0] == 3   # graph rebuilt


def test_unchanged_city_imports_nothing(conn, city):
    city.run(conn)
    city.fetched.clear()
    report = city.run(conn)
    assert city.fetched == []
    assert report.headline() == \
        "city data unchanged (cart paths last edited 2026-04-21, roads last edited 2026-04-21)"
    assert report.lines() == [report.headline()]


@pytest.mark.parametrize("column, value", [
    ("feature_count", 99), ("max_oid", 1), ("max_edited_at", datetime(2026, 9, 1, tzinfo=UTC))])
def test_any_signature_change_imports_only_that_layer(conn, city, column, value):
    city.run(conn)
    city.fetched.clear()
    conn.execute(f"UPDATE source_signature SET {column} = %s WHERE layer = 'road'", (value,))
    report = city.run(conn)
    assert city.fetched == ["road"]
    assert report.headline() == "cart paths: unchanged; roads: 0 added, 0 changed, 0 removed"


def test_force_imports_every_layer(conn, city):
    city.run(conn)
    city.fetched.clear()
    city.run(conn, force=True)
    assert sorted(city.fetched) == ["cartpath", "road"]


def test_a_failed_refresh_keeps_the_old_signature_so_the_next_run_retries(conn, city):
    city.run(conn)
    old = stored_signature(conn, "road")
    city.sigs["road"] = LayerSignature(1, 5000, EDITED)
    city.fail.add("road")
    with pytest.raises(RuntimeError, match="road download failed"):
        city.run(conn)
    assert stored_signature(conn, "road") == old

    city.fail.clear()
    city.fetched.clear()
    city.run(conn)
    assert city.fetched == ["road"]
    assert stored_signature(conn, "road") == city.sigs["road"]


# ---- change report ----------------------------------------------------------------


def test_report_lists_segments_and_the_impact_on_totals(conn, city):
    city.run(conn)
    city.features["cartpath"] = [cartpath(1, line((0, 0), (100, 0))),        # 2 removed
                                 cartpath(3, line((0, 90), (60, 90)))]        # 3 added
    city.sigs["cartpath"] = LayerSignature(2, 1003, EDITED)
    lines = city.run(conn).lines()
    assert lines[0] == "cart paths: 1 added, 0 changed, 1 removed; roads: unchanged"
    assert "    3 (no name) (Path, 60 m)" in lines
    assert "    2 (no name) (Path, 100 m)" in lines
    assert "  counted cart paths: 2 (0.12 mi) -> 2 (0.10 mi)" in lines


def test_long_lists_are_capped(conn, city):
    city.run(conn)
    city.features["cartpath"] += [cartpath(100 + i, line((0, 300 + i * 10), (50, 300 + i * 10)))
                                  for i in range(LIST_CAP + 5)]
    city.sigs["cartpath"] = LayerSignature(len(city.features["cartpath"]), 9999, EDITED)
    lines = city.run(conn).lines()
    assert "    … and 5 more" in lines


def test_a_download_short_of_the_signature_count_is_refused(conn, city):
    city.run(conn)
    before = conn.execute("SELECT count(*) FROM segment").fetchone()[0]
    # The city says 3 roads, but a page went missing and only 1 arrived.
    city.sigs["road"] = LayerSignature(3, 5000, EDITED)
    with pytest.raises(RuntimeError, match="road: downloaded 1 features but the city reports 3"):
        city.run(conn)
    assert conn.execute("SELECT count(*) FROM segment").fetchone()[0] == before


# ---- CLI exit codes -----------------------------------------------------------------


def test_failed_jobs_end_with_one_clean_line(conn, db_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def broken(*args, **kwargs):
        raise RuntimeError("city server said no\nsecond line of detail")

    monkeypatch.setattr(cli, "refresh", broken)
    assert cli.main(["refresh"]) == 1
    last = capsys.readouterr().err.strip().splitlines()[-1]
    job_id = conn.execute("SELECT max(id) FROM job_run").fetchone()[0]
    assert last == f"refresh failed (job {job_id}): RuntimeError: city server said no"


def test_import_is_a_forced_refresh_logged_as_import(conn, db_url, monkeypatch, city):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(cli, "refresh", lambda conn, client, settings, excl, force: city.run(conn, force=force))
    assert cli.main(["import"]) == 0
    assert cli.main(["import"]) == 0   # unchanged city: still imports every layer
    assert sorted(city.fetched) == ["cartpath", "cartpath", "road", "road"]
    assert conn.execute("SELECT job, status FROM job_run").fetchall() == [("import", "ok")] * 2


def test_busy_jobs_exit_75(conn, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    conn.execute("SELECT pg_advisory_lock(%s)", (JOB_LOCK,))
    try:
        assert cli.main(["recompute"]) == cli.EXIT_BUSY == 75
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (JOB_LOCK,))
