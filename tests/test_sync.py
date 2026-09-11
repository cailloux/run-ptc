import math
from datetime import UTC, date, datetime

import pytest

from app.sync import default_window, month_windows, sync, today_eastern
from tests.helpers import SETTINGS, X0, Y0, cartpath, line, run_import


# ---- dates (no database) -----------------------------------------------------


def test_month_windows_cover_the_range_inclusively():
    assert month_windows(date(2024, 1, 15), date(2024, 3, 10)) == [
        (date(2024, 1, 15), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),   # leap year
        (date(2024, 3, 1), date(2024, 3, 10)),
    ]


def test_month_windows_roll_over_the_year():
    assert month_windows(date(2023, 12, 20), date(2024, 1, 5)) == [
        (date(2023, 12, 20), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 1, 5)),
    ]


def test_month_windows_single_day_and_empty():
    assert month_windows(date(2024, 5, 4), date(2024, 5, 4)) == [(date(2024, 5, 4), date(2024, 5, 4))]
    assert month_windows(date(2024, 5, 5), date(2024, 5, 4)) == []


def test_default_window_backfills_when_nothing_is_stored():
    assert default_window(date(2026, 9, 11), None, date(2023, 1, 1)) == (date(2023, 1, 1), date(2026, 9, 11))


def test_default_window_resumes_a_week_before_the_newest_run_in_eastern_time():
    latest = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)   # Aug 31, 11 pm Eastern
    assert default_window(date(2026, 9, 11), latest, date(2023, 1, 1)) == (date(2026, 8, 24), date(2026, 9, 11))


def test_today_is_the_eastern_date():
    assert today_eastern(datetime(2026, 9, 12, 2, 30, tzinfo=UTC)) == date(2026, 9, 11)


# ---- split_track --------------------------------------------------------------


def split(conn, points, gap_m=100):
    wkt = "LINESTRING(" + ", ".join(f"{X0 + x} {Y0 + y}" for x, y in points) + ")"
    parts, length = conn.execute(
        "SELECT ST_NumGeometries(g), ST_Length(g)"
        " FROM (SELECT split_track(ST_GeomFromText(%s, 32616), %s) AS g) s",
        (wkt, gap_m),
    ).fetchone()
    return parts, None if length is None else round(length, 6)


def test_gap_of_exactly_the_threshold_does_not_split(conn):
    assert split(conn, [(0, 0), (50, 0), (150, 0)]) == (1, 150.0)


def test_gap_just_over_the_threshold_splits(conn):
    assert split(conn, [(0, 0), (50, 0), (150.5, 0), (200, 0)]) == (2, 99.5)


def test_lone_fix_between_two_gaps_is_dropped(conn):
    assert split(conn, [(0, 0), (10, 0), (500, 0), (1000, 0), (1010, 0)]) == (2, 20.0)


def test_nothing_left_is_null(conn):
    assert split(conn, [(0, 0), (0, 0), (0, 0)]) == (None, None)   # standing still
    assert split(conn, [(0, 0), (500, 0)]) == (None, None)         # two lone fixes


# ---- sync against a fake Intervals ------------------------------------------


class FakeIntervals:
    def __init__(self, activities, tracks):
        self._activities = activities
        self._tracks = tracks
        self.windows = []
        self.fetches = []

    def activities(self, oldest, newest):
        self.windows.append((oldest, newest))
        return [a for a in self._activities
                if oldest <= date.fromisoformat((a.get("start_date") or a["start_date_local"])[:10]) <= newest]

    def latlng(self, activity_id):
        self.fetches.append(activity_id)
        return self._tracks[activity_id]

    def close(self):
        pass


def run(activity_id, start="2024-05-04T12:00:00Z", **fields):
    return {"id": activity_id, "start_date": start, "type": "Run", "name": f"run {activity_id}",
            "distance": 5000.0, "source": "GARMIN_CONNECT",
            "stream_types": ["time", "latlng", "heartrate"], **fields}


def walk(conn, start, end, step=10.0):
    """GPS fixes every `step` m from start to end (UTM offsets), as (lat, lon)."""
    (x0, y0), (x1, y1) = start, end
    n = max(1, round(math.dist(start, end) / step))
    xy = [(X0 + x0 + (x1 - x0) * i / n, Y0 + y0 + (y1 - y0) * i / n) for i in range(n + 1)]
    return [tuple(r) for r in conn.execute(
        "SELECT ST_Y(p), ST_X(p) FROM unnest(%s::float8[], %s::float8[]) AS u(x, y),"
        " LATERAL ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 32616), 4326) AS p",
        ([x for x, _ in xy], [y for _, y in xy]),
    ).fetchall()]


def city(conn):
    """Two cart paths whose bounding box is 0..1000 m on both axes."""
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (1000, 0))),
        cartpath(2, line((0, 1000), (1000, 1000))),
    ])


def activity(conn, intervals_id):
    return conn.execute("""
        SELECT status, start_at, ST_NumGeometries(geom), ST_NumPoints(track_raw), track_raw IS NULL
        FROM activity WHERE intervals_id = %s
    """, (intervals_id,)).fetchone()


def do_sync(conn, client, start=date(2024, 5, 1), end=date(2024, 5, 31), **kwargs):
    return sync(conn, client, start, end, SETTINGS, **kwargs)


def test_runs_are_classified_against_the_city_bounding_box(conn):
    city(conn)
    tracks = {
        "i1": walk(conn, (100, 100), (500, 100)),                              # inside
        "i2": walk(conn, (3000, 3000), (3400, 3000)),                          # outside
        "i3": walk(conn, (-2000, 500), (-1900, 500)) + walk(conn, (2000, 500), (2100, 500)),  # jump across
        "i4": walk(conn, (100, 100), (300, 100)) + walk(conn, (700, 100), (900, 100)),        # inside, gap
    }
    report = do_sync(conn, FakeIntervals([run(i) for i in tracks], tracks))
    assert report.statuses == {"city": 2, "outside": 2}

    status, start_at, parts, points, raw_null = activity(conn, "i1")
    assert (status, parts, points) == ("city", 1, 41)
    assert start_at == datetime(2024, 5, 4, 12, 0, tzinfo=UTC)
    assert activity(conn, "i2")[0] == "outside" and activity(conn, "i2")[4] is True
    # The straight line across the city is a GPS gap, not running.
    assert activity(conn, "i3")[0] == "outside"
    assert activity(conn, "i4")[:3:2] == ("city", 2)


def test_missing_and_zero_fixes_are_dropped(conn):
    city(conn)
    fixes = walk(conn, (100, 100), (200, 100))   # 11 fixes
    noisy = [None, fixes[0], (0.0, 0.0), *fixes[1:], None]
    do_sync(conn, FakeIntervals([run("i1")], {"i1": noisy}))
    assert activity(conn, "i1")[3] == 11


def test_run_with_no_usable_fixes_is_recorded_without_a_track(conn):
    city(conn)
    report = do_sync(conn, FakeIntervals([run("i1")], {"i1": [None, None, None]}))
    assert report.statuses == {"no_gps": 1}
    assert activity(conn, "i1")[0] == "no_gps" and activity(conn, "i1")[4] is True


def test_only_outdoor_gps_runs_are_fetched(conn):
    city(conn)
    listing = [
        run("i1"),
        run("i2", type="Ride"),
        run("i3", type="VirtualRun"),
        run("i4", stream_types=["time", "heartrate"], trainer=True),   # treadmill
        {"id": "i5", "type": "Run", "source": "STRAVA", "start_date_local": "2024-05-04T08:00:00"},
    ]
    client = FakeIntervals(listing, {"i1": walk(conn, (100, 100), (200, 100))})
    report = do_sync(conn, client)
    assert client.fetches == ["i1"]
    assert (report.listed, report.runs, report.without_gps, report.strava_stubs) == (5, 3, 1, ["i5"])
    assert report.skipped_types == {"Ride": 1, "VirtualRun": 1}
    assert conn.execute("SELECT array_agg(intervals_id) FROM activity").fetchone()[0] == ["i1"]


def test_resync_skips_known_runs_unless_refetching(conn):
    city(conn)
    tracks = {"i1": walk(conn, (100, 100), (200, 100)), "i2": walk(conn, (3000, 3000), (3100, 3000))}
    listing = [run(i) for i in tracks]
    do_sync(conn, FakeIntervals(listing, tracks))

    again = FakeIntervals(listing, tracks)
    report = do_sync(conn, again)
    assert (again.fetches, report.already, report.fetched) == ([], 2, 0)

    refetch = FakeIntervals(listing, tracks)
    report = do_sync(conn, refetch, refetch=True)
    assert sorted(refetch.fetches) == ["i1", "i2"]
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_dry_run_classifies_but_stores_nothing(conn):
    city(conn)
    tracks = {"i1": walk(conn, (100, 100), (200, 100)), "i2": walk(conn, (100, 100), (300, 100))}
    listing = [run("i1", start="2024-05-04T12:00:00Z"), run("i2", start="2024-06-04T12:00:00Z")]
    report = do_sync(conn, FakeIntervals(listing, tracks), end=date(2024, 6, 30), dry_run=True)
    assert report.statuses == {"city": 2}
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 0


def test_sync_asks_for_explicit_month_windows(conn):
    city(conn)
    client = FakeIntervals([], {})
    do_sync(conn, client, start=date(2024, 4, 20), end=date(2024, 6, 2))
    assert client.windows == [
        (date(2024, 4, 20), date(2024, 4, 30)),
        (date(2024, 5, 1), date(2024, 5, 31)),
        (date(2024, 6, 1), date(2024, 6, 2)),
    ]


def test_sync_needs_the_city_imported_first(conn):
    with pytest.raises(RuntimeError, match="city import"):
        do_sync(conn, FakeIntervals([], {}))
