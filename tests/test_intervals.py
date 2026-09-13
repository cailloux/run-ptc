import base64
from datetime import date

import httpx2
import pytest

from app.intervals import IntervalsClient, IntervalsShapeError, parse_latlng


FAKE_KEY = "not-a-real-key"


def client_with(handler, **kwargs):
    return IntervalsClient("i12345", FAKE_KEY, transport=httpx2.MockTransport(handler), **kwargs)


def latlng_stream(lats, lons):
    # Shape observed from the real API: one stream, latitudes in data, longitudes in data2.
    return [{"type": "latlng", "name": None, "data": lats, "data2": lons,
             "valueType": "java.lang.Float", "valueTypeIsArray": False, "custom": False,
             "allNull": False, "anomalies": None}]


def test_auth_and_list_parameters():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json=[])

    client_with(handler).activities(date(2024, 2, 1), date(2024, 2, 29))
    [req] = seen
    assert req.url.path == "/api/v1/athlete/i12345/activities"
    assert req.url.params["oldest"] == "2024-02-01"
    assert req.url.params["newest"] == "2024-02-29T23:59:59"
    assert req.url.params["fields"].split(",") == [
        "id", "start_date", "type", "name", "distance", "source", "stream_types", "trainer"]
    scheme, token = req.headers["Authorization"].split()
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == f"API_KEY:{FAKE_KEY}"


def test_client_only_ever_sends_get():
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.url.path.endswith("/streams.json"):
            return httpx2.Response(200, json=latlng_stream([33.4], [-84.6]))
        return httpx2.Response(200, json=[])

    c = client_with(handler)
    c.activities(date(2024, 1, 1), date(2024, 1, 31))
    c.latlng("i1")
    assert set(methods) == {"GET"}
    # No write verbs exist on the client at all.
    assert not [m for m in ("post", "put", "patch", "delete") if hasattr(c, m)]


def test_latlng_request_and_parse():
    def handler(request):
        assert request.url.path == "/api/v1/activity/i99/streams.json"
        assert request.url.params["types"] == "latlng"
        return httpx2.Response(200, json=latlng_stream([33.39, None, 33.40], [-84.57, None, -84.58]))

    assert client_with(handler).latlng("i99") == [(33.39, -84.57), None, (33.40, -84.58)]


@pytest.mark.parametrize("streams, message", [
    ({"type": "latlng"}, "expected a list"),
    ([{"type": "heartrate", "data": [1]}], "no latlng stream"),
    ([{"type": "latlng", "data": [33.4]}], "equal-length"),
    ([{"type": "latlng", "data": [33.4, 33.5], "data2": [-84.6]}], "equal-length"),
    ([{"type": "latlng", "data": ["33.4"], "data2": [-84.6]}], "bad latlng value"),
    ([{"type": "latlng", "data": [133.4], "data2": [-84.6]}], "bad latlng value"),
])
def test_unexpected_stream_shapes_fail_loudly(streams, message):
    with pytest.raises(IntervalsShapeError, match=message):
        parse_latlng(streams, "i1")


def test_list_must_be_a_list():
    c = client_with(lambda r: httpx2.Response(200, json={"error": "nope"}))
    with pytest.raises(IntervalsShapeError, match="expected a list"):
        c.activities(date(2024, 1, 1), date(2024, 1, 31))


def test_rate_limit_is_retried_with_retry_after():
    responses = [httpx2.Response(429, headers={"Retry-After": "7"}), httpx2.Response(200, json=[])]
    slept = []
    c = client_with(lambda r: responses.pop(0), sleep=slept.append)
    assert c.activities(date(2024, 1, 1), date(2024, 1, 31)) == []
    assert slept == [7.0]


def test_persistent_errors_raise_after_retries():
    slept = []
    c = client_with(lambda r: httpx2.Response(503), sleep=slept.append, tries=3)
    with pytest.raises(httpx2.HTTPStatusError):
        c.activities(date(2024, 1, 1), date(2024, 1, 31))
    assert slept == [2.0, 4.0]


def test_client_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(401)

    with pytest.raises(httpx2.HTTPStatusError):
        client_with(handler, sleep=lambda s: None).activities(date(2024, 1, 1), date(2024, 1, 31))
    assert len(calls) == 1
