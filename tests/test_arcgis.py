import httpx2
import pytest

from app.arcgis import PAGE_SIZE, fetch_features

URL = "https://gis.example/arcgis/rest/services/Layer/MapServer/0"
CRS = {"type": "name", "properties": {"name": "EPSG:32616"}}


def server(total: int, calls: list, body_override: dict | None = None):
    """A fake ArcGIS layer holding `total` features, honoring resultOffset."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        params = request.url.params
        calls.append(params)
        if body_override is not None:
            return httpx2.Response(200, json=body_override)
        offset, count = int(params["resultOffset"]), int(params["resultRecordCount"])
        ids = range(offset, min(offset + count, total))
        return httpx2.Response(200, json={
            "type": "FeatureCollection",
            "crs": CRS,
            "features": [{"type": "Feature", "properties": {"OID": i}, "geometry": None} for i in ids],
        })
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_pages_until_a_short_page():
    calls = []
    features = fetch_features(server(2250, calls), URL, "OID")
    assert [f["properties"]["OID"] for f in features] == list(range(2250))
    assert [c["resultOffset"] for c in calls] == ["0", "1000", "2000"]


def test_exact_multiple_of_page_size_needs_one_empty_page():
    calls = []
    features = fetch_features(server(PAGE_SIZE, calls), URL, "OID")
    assert len(features) == PAGE_SIZE
    assert [c["resultOffset"] for c in calls] == ["0", "1000"]


def test_query_parameters():
    calls = []
    fetch_features(server(3, calls), URL, "OBJECTID_1")
    params = calls[0]
    assert params["where"] == "1=1"
    assert params["outFields"] == "*"
    assert params["f"] == "geojson"
    assert params["outSR"] == "32616"
    assert params["orderByFields"] == "OBJECTID_1"


def test_arcgis_error_body_raises():
    body = {"error": {"code": 400, "message": "Invalid query"}}
    with pytest.raises(RuntimeError, match="Invalid query"):
        fetch_features(server(0, [], body), URL, "OID")


def test_wrong_spatial_reference_raises():
    body = {"type": "FeatureCollection", "features": []}   # no crs: server ignored outSR
    with pytest.raises(RuntimeError, match="EPSG:32616"):
        fetch_features(server(0, [], body), URL, "OID")
