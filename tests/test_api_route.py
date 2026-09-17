import pytest

from app.graph import build_graph
from tests.helpers import SETTINGS, add_network, cartpath, line, run_import
from tests.test_api_sync import api  # noqa: F401  (fixture)
from tests.test_routing import latlon


def point(conn, x, y):
    lat, lon = latlon(conn, x, y)
    return {"lat": lat, "lon": lon}


@pytest.fixture
def network(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((50, 0), (50, 60))),
        cartpath(3, line((0, 300), (100, 300))),   # an island
    ])
    build_graph(conn, SETTINGS)


def test_snap_returns_the_point_on_the_network(api, conn, network):
    client, _ = api
    resp = client.post("/route/snap", json=point(conn, 30, 4))
    assert resp.status_code == 200
    assert resp.json() == pytest.approx(point(conn, 30, 0), abs=1e-7)


def test_leg_shape(api, conn, network):
    client, _ = api
    resp = client.post("/route/leg", json={"from": point(conn, 10, 0), "to": point(conn, 50, 50)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["length_m"] == pytest.approx(90, abs=0.1)
    assert body["latlngs"][0] == pytest.approx([body["from"]["lat"], body["from"]["lon"]])
    assert body["latlngs"][-1] == pytest.approx([body["to"]["lat"], body["to"]["lon"]])


def test_click_far_from_the_network_is_422(api, conn, network):
    client, _ = api
    resp = client.post("/route/snap", json=point(conn, 50, 150))
    assert resp.status_code == 422
    assert "more than 50 m" in resp.json()["detail"]


def test_unreachable_island_is_422(api, conn, network):
    client, _ = api
    resp = client.post("/route/leg", json={"from": point(conn, 10, 0), "to": point(conn, 10, 300)})
    assert resp.status_code == 422
    assert "island" in resp.json()["detail"]


def test_missing_graph_is_503(api, conn):
    client, _ = api
    resp = client.post("/route/snap", json=point(conn, 0, 0))
    assert resp.status_code == 503


def test_islands_endpoint_lists_island_edges(api, conn, network):
    client, _ = api
    features = client.get("/graph/islands").json()["features"]
    assert [f["properties"]["source_oid"] for f in features] == [3]


def test_fit_export_defaults_to_generic_course_points(api, conn, network):
    client, _ = api
    latlngs = [list(latlon(conn, x, 0)) for x in range(0, 51, 5)]
    resp = client.post("/route/fit", json={"name": "Test course", "latlngs": latlngs})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.ant.fit"


def test_fit_export_accepts_a_garmin_flavor(api, conn, network):
    client, _ = api
    latlngs = [list(latlon(conn, x, 0)) for x in range(0, 51, 5)]
    resp = client.post("/route/fit",
                       json={"name": "Test course", "latlngs": latlngs, "flavor": "garmin"})
    assert resp.status_code == 200


def test_networks_endpoint_lists_every_network(api, conn):
    client, _ = api
    add_network(conn, "testworld")
    slugs = {n["slug"] for n in client.get("/networks").json()}
    assert slugs == {"ptc", "testworld"}


def test_read_endpoints_stay_isolated_per_network(api, conn, network, monkeypatch):
    """/nodes, /activities, /changes, /stats all gained real WHERE-clause
    scoping in this step -- no test exercised any of them with a second
    network present."""
    client, _ = api
    # config/settings.yaml only has a real "ptc" block; /status's own
    # load_settings() call needs a stand-in so this test can use a
    # network slug that only exists as a DB row, not a settings block.
    monkeypatch.setattr("app.api.load_settings", lambda network="ptc": SETTINGS)
    add_network(conn, "testworld")
    run_import(conn, "cartpath", [cartpath(101, line((0, 0), (100, 0)))], network="testworld")
    run_import(conn, "cartpath", [cartpath(101, line((0, 0), (100, 0))),
                                  cartpath(102, line((0, 50), (100, 50)))], network="testworld")
    build_graph(conn, SETTINGS, network="testworld")

    ptc_nodes = client.get("/nodes", params={"layer": "cartpath"}).json()["features"]
    tw_nodes = client.get("/nodes", params={"layer": "cartpath", "network": "testworld"}).json()["features"]
    assert {n["properties"]["id"] for n in ptc_nodes}.isdisjoint({n["properties"]["id"] for n in tw_nodes})

    ptc_stats = client.get("/stats").json()
    tw_stats = client.get("/stats", params={"network": "testworld"}).json()
    assert (ptc_stats["cartpath"]["counted"], tw_stats["cartpath"]["counted"]) == (3, 2)

    tw_changes = client.get("/changes", params={"network": "testworld"}).json()["features"]
    assert [f["properties"]["source_oid"] for f in tw_changes] == [102]

    ptc_status = client.get("/status").json()
    tw_status = client.get("/status", params={"network": "testworld"}).json()
    assert (ptc_status["sources"]["city"]["layers"]["cartpath"]["stored"],
            tw_status["sources"]["city"]["layers"]["cartpath"]["stored"]) == (3, 2)


def test_route_endpoints_use_the_requested_network(api, conn, network, monkeypatch):
    """The already-scoped functions behind /route/* and /graph/islands were
    proven correct at the unit level in steps 7/8/10 -- what's untested is
    the API layer's own network param: it could silently default to ptc
    while looking like it worked, no matter what a caller asks for."""
    client, _ = api
    # config/settings.yaml only has a real "ptc" block, same limitation as
    # test_read_endpoints_stay_isolated_per_network above.
    monkeypatch.setattr("app.api.load_settings", lambda network="ptc": SETTINGS)
    add_network(conn, "testworld")
    # Far from ptc's fixture (near 0,0): if ?network= were ignored, this
    # click would be nowhere near ptc's network at all.
    run_import(conn, "cartpath", [cartpath(101, line((1000, 1000), (1100, 1000)))], network="testworld")
    build_graph(conn, SETTINGS, network="testworld")

    resp = client.post("/route/snap", params={"network": "testworld"},
                       json=point(conn, 1050, 1004))
    assert resp.status_code == 200
    assert resp.json() == pytest.approx(point(conn, 1050, 1000), abs=1e-7)

    resp = client.post("/route/leg", params={"network": "testworld"},
                       json={"from": point(conn, 1010, 1000), "to": point(conn, 1090, 1000)})
    assert resp.status_code == 200
    assert resp.json()["length_m"] == pytest.approx(80, abs=0.1)

    # ptc's fixture has one island (source_oid 3); testworld's single line has none.
    islands = client.get("/graph/islands", params={"network": "testworld"}).json()["features"]
    assert islands == []
