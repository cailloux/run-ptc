import pytest

from app.graph import build_graph
from tests.helpers import SETTINGS, cartpath, line, run_import
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
