"""Sync runs from Intervals.icu (read-only) and store Peachtree City tracks.

Rules are in docs/PLAN.md under "Activity sync". Gap splitting and the city
test run in PostGIS; Python only filters the activity list and moves rows.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from app.config import Settings
from app.intervals import IntervalsClient
from app.matching import match, rebuild_pieces

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
# Re-list this many days before the newest stored run, to catch late uploads.
OVERLAP_DAYS = 7


def today_eastern(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(EASTERN).date()


def default_window(today: date, latest_start: datetime | None, backfill_start: date) -> tuple[date, date]:
    """Backfill from the configured start, or resume a week before the newest stored run."""
    if latest_start is None:
        return backfill_start, today
    return latest_start.astimezone(EASTERN).date() - timedelta(days=OVERLAP_DAYS), today


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split start..end (inclusive) into calendar-month windows."""
    windows = []
    cur = start
    while cur <= end:
        next_month = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        windows.append((cur, min(next_month - timedelta(days=1), end)))
        cur = next_month
    return windows


@dataclass
class SyncReport:
    start: date
    end: date
    listed: int = 0
    skipped_types: Counter = field(default_factory=Counter)
    runs: int = 0
    without_gps: int = 0          # no latlng stream at all (treadmill, indoor)
    strava_stubs: list[str] = field(default_factory=list)
    already: int = 0
    fetched: int = 0
    statuses: Counter = field(default_factory=Counter)   # city / outside / no_gps
    newly_hit: int = 0
    dry_run: bool = False
    refetch: bool = False

    def lines(self) -> list[str]:
        skipped = ", ".join(f"{t} {n}" for t, n in self.skipped_types.most_common()) or "none"
        prefix = "DRY RUN " if self.dry_run else ""
        lines = [
            f"{prefix}sync {self.start}..{self.end}: {self.listed} listed, {self.runs} runs; "
            f"other types skipped: {skipped}",
            f"runs: {self.fetched} fetched, {self.already} already synced, "
            f"{self.without_gps} without GPS, {len(self.strava_stubs)} Strava stubs",
            f"fetched: {self.statuses['city']} city, {self.statuses['outside']} outside, "
            f"{self.statuses['no_gps']} with no usable track; {self.newly_hit} nodes newly hit",
        ]
        if self.refetch and self.fetched:
            lines.append("refetched tracks can't remove hits from their old versions; run recompute")
        return lines


UPSERT_SQL = """
WITH raw AS (
    SELECT CASE WHEN count(*) >= 2 THEN
               ST_Transform(ST_SetSRID(ST_MakeLine(ST_MakePoint(lon, lat) ORDER BY seq), 4326), 32616)
           END AS track
    FROM sync_fix
), split AS (
    SELECT track, split_track(track, %(gap_m)s) AS parts FROM raw
), classified AS (
    SELECT CASE
               WHEN parts IS NULL THEN 'no_gps'
               WHEN ST_Intersects(parts, (SELECT box FROM sync_city_box)) THEN 'city'
               ELSE 'outside'
           END AS status,
           track, parts
    FROM split
)
INSERT INTO activity (intervals_id, start_at, sport, name, distance_m, source,
                      status, track_raw, geom, synced_at)
SELECT %(intervals_id)s, %(start_at)s, %(sport)s, %(name)s, %(distance_m)s, %(source)s,
       status,
       CASE WHEN status = 'city' THEN track END,
       CASE WHEN status = 'city' THEN parts END,
       now()
FROM classified
ON CONFLICT (intervals_id) DO UPDATE SET
    start_at = EXCLUDED.start_at,
    sport = EXCLUDED.sport,
    name = EXCLUDED.name,
    distance_m = EXCLUDED.distance_m,
    source = EXCLUDED.source,
    status = EXCLUDED.status,
    track_raw = EXCLUDED.track_raw,
    geom = EXCLUDED.geom,
    synced_at = EXCLUDED.synced_at
RETURNING id, status
"""


def _prepare(conn: psycopg.Connection) -> None:
    # Session temp tables, created outside any transaction so a dry run's
    # rollback doesn't take them with it.
    conn.execute("DROP TABLE IF EXISTS pg_temp.sync_fix, pg_temp.sync_city_box")
    conn.execute("CREATE TEMP TABLE sync_fix (seq integer, lat float8, lon float8)")
    conn.execute("""
        CREATE TEMP TABLE sync_city_box AS
        SELECT ST_SetSRID(ST_Extent(geom)::geometry, 32616) AS box FROM segment
    """)
    if conn.execute("SELECT box FROM sync_city_box").fetchone()[0] is None:
        raise RuntimeError("no city segments; run the city import before syncing")


def _store_run(conn: psycopg.Connection, activity: dict, points: list, gap_m: float) -> tuple[int, str]:
    """Upsert the run and recut its matching pieces. Returns (activity id, status)."""
    conn.execute("TRUNCATE sync_fix")
    with conn.cursor().copy("COPY sync_fix (seq, lat, lon) FROM STDIN") as copy:
        for seq, (lat, lon) in enumerate(points):
            copy.write_row((seq, lat, lon))
    activity_id, status = conn.execute(UPSERT_SQL, {
        "gap_m": gap_m,
        "intervals_id": activity["id"],
        "start_at": datetime.fromisoformat(activity["start_date"]),
        "sport": activity["type"],
        "name": activity.get("name"),
        "distance_m": activity.get("distance"),
        "source": activity.get("source"),
    }).fetchone()
    rebuild_pieces(conn, [activity_id])
    return activity_id, status


def sync(conn: psycopg.Connection, client: IntervalsClient, start: date, end: date,
         settings: Settings, *, refetch: bool = False, dry_run: bool = False) -> SyncReport:
    report = SyncReport(start, end, dry_run=dry_run, refetch=refetch)
    _prepare(conn)
    known = {r[0] for r in conn.execute("SELECT intervals_id FROM activity")}

    for oldest, newest in month_windows(start, end):
        fetched_before = report.fetched
        city_ids: list[int] = []
        # One transaction per month keeps a long backfill's progress if it
        # fails partway; a dry run rolls every month back.
        with conn.transaction(force_rollback=dry_run):
            for a in client.activities(oldest, newest):
                report.listed += 1
                if a.get("type") not in settings.sync_run_types:
                    report.skipped_types[a.get("type")] += 1
                    continue
                report.runs += 1
                if a.get("source") == "STRAVA" or not a.get("start_date"):
                    # Intervals serves Strava-sourced activities as empty stubs.
                    report.strava_stubs.append(a.get("id"))
                    continue
                if "latlng" not in (a.get("stream_types") or []):
                    report.without_gps += 1
                    continue
                if a["id"] in known and not refetch:
                    report.already += 1
                    continue
                points = [p for p in client.latlng(a["id"]) if p and p != (0.0, 0.0)]
                report.fetched += 1
                activity_id, status = _store_run(conn, a, points, settings.track_gap_split_m)
                report.statuses[status] += 1
                if status == "city":
                    city_ids.append(activity_id)
                known.add(a["id"])
            # Match only this month's new city runs; recompute replays everything.
            report.newly_hit += match(conn, settings, activity_ids=city_ids)
        if report.fetched > fetched_before:
            log.info("%s: fetched %d runs", oldest.strftime("%Y-%m"), report.fetched - fetched_before)

    if report.strava_stubs:
        log.warning("%d Strava-sourced runs have no GPS in Intervals: %s",
                    len(report.strava_stubs), report.strava_stubs)
    return report
