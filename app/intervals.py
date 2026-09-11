"""Read-only Intervals.icu client.

This app never writes to Intervals, so the client only ever sends GET.
Endpoint and field names follow https://intervals.icu/api/v1/docs.
"""

import time
from datetime import date

import httpx

BASE_URL = "https://intervals.icu/api/v1"
LIST_FIELDS = ("id", "start_date", "type", "name", "distance", "source", "stream_types", "trainer")
RETRY_STATUSES = {429, 500, 502, 503, 504}

LatLng = tuple[float, float]


class IntervalsShapeError(RuntimeError):
    """A response didn't have the shape the spec (and our probe) led us to expect."""


class IntervalsClient:
    def __init__(self, athlete_id: str, api_key: str, *, transport: httpx.BaseTransport | None = None,
                 tries: int = 3, sleep=time.sleep):
        self._athlete_id = athlete_id
        self._http = httpx.Client(base_url=BASE_URL, auth=("API_KEY", api_key), timeout=60,
                                  transport=transport)
        self._tries = tries
        self._sleep = sleep

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict) -> object:
        for attempt in range(1, self._tries + 1):
            resp = self._http.get(path, params=params)
            if resp.status_code in RETRY_STATUSES and attempt < self._tries:
                self._sleep(_retry_delay(resp, attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    def activities(self, oldest: date, newest: date) -> list[dict]:
        """Activities starting on oldest..newest inclusive, in the athlete's local dates."""
        data = self._get(f"/athlete/{self._athlete_id}/activities", {
            "oldest": oldest.isoformat(),
            "newest": f"{newest.isoformat()}T23:59:59",
            "fields": ",".join(LIST_FIELDS),
        })
        if not isinstance(data, list):
            raise IntervalsShapeError(f"activity list: expected a list, got {type(data).__name__}")
        return data

    def latlng(self, activity_id: str) -> list[LatLng | None]:
        """GPS fixes in order; None where the device recorded no position."""
        streams = self._get(f"/activity/{activity_id}/streams.json", {"types": "latlng"})
        return parse_latlng(streams, activity_id)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return 2.0 ** attempt


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_latlng(streams: object, activity_id: str) -> list[LatLng | None]:
    """The latlng stream holds latitudes in `data` and longitudes in `data2`."""
    where = f"activity {activity_id} streams"
    if not isinstance(streams, list):
        raise IntervalsShapeError(f"{where}: expected a list, got {type(streams).__name__}")
    found = [s for s in streams if isinstance(s, dict) and s.get("type") == "latlng"]
    if not found:
        raise IntervalsShapeError(f"{where}: no latlng stream")
    lats, lons = found[0].get("data"), found[0].get("data2")
    if not isinstance(lats, list) or not isinstance(lons, list) or len(lats) != len(lons):
        raise IntervalsShapeError(f"{where}: latlng data/data2 must be equal-length lists")

    points: list[LatLng | None] = []
    for lat, lon in zip(lats, lons):
        if lat is None or lon is None:
            points.append(None)
        elif _is_number(lat) and _is_number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append((float(lat), float(lon)))
        else:
            raise IntervalsShapeError(f"{where}: bad latlng value {lat!r}, {lon!r}")
    return points
