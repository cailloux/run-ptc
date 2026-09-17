"""Sync runs from Intervals.icu (read-only) and classify each track by network.

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


def sync_window(conn: psycopg.Connection, settings: Settings,
                start: date | None = None, end: date | None = None) -> tuple[date, date]:
    """The explicit dates a sync will ask for: the given ones, or the defaults."""
    latest = conn.execute("SELECT max(start_at) FROM activity").fetchone()[0]
    default_start, default_end = default_window(today_eastern(), latest, settings.sync_backfill_start)
    return start or default_start, end or default_end


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

    def headline(self) -> str:
        """One line for the map panel."""
        city = self.statuses["city"]
        prefix = "dry run: " if self.dry_run else ""
        return f"{prefix}{city} new city run{'' if city == 1 else 's'}, {self.newly_hit} nodes newly hit"


UPSERT_SQL = """
WITH points_4326 AS (
    SELECT seq, ST_SetSRID(ST_MakePoint(lon, lat), 4326) AS pt FROM sync_fix
), raw AS (
    SELECT CASE WHEN count(*) >= 2 THEN ST_MakeLine(pt ORDER BY seq) END AS track_4326
    FROM points_4326
), matched AS (
    -- A real GPS fix landing in a network's box counts; the straight line
    -- connecting two fixes across a GPS gap does not (it may cross a box
    -- neither fix is actually in).
    SELECT r.track_4326, b.network_id,
           CASE WHEN b.network_id IS NOT NULL THEN
               split_track(ST_Transform(r.track_4326, b.srid), %(gap_m)s)
           END AS parts
    FROM raw r
    LEFT JOIN LATERAL (
        SELECT nb.network_id, nb.srid FROM network_box nb
        WHERE r.track_4326 IS NOT NULL
          AND EXISTS (SELECT 1 FROM points_4326 p WHERE ST_Intersects(p.pt, nb.box_4326))
        ORDER BY nb.network_id LIMIT 1
    ) b ON true
), classified AS (
    SELECT CASE
               WHEN track_4326 IS NULL THEN 'no_gps'
               WHEN network_id IS NULL THEN 'outside'
               ELSE 'city'
           END AS status,
           track_4326, network_id, parts
    FROM matched
)
INSERT INTO activity (intervals_id, start_at, sport, name, distance_m, source,
                      status, track_raw, geom, network_id, synced_at)
SELECT %(intervals_id)s, %(start_at)s, %(sport)s, %(name)s, %(distance_m)s, %(source)s,
       status, track_4326, parts, network_id, now()
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
    network_id = EXCLUDED.network_id,
    synced_at = EXCLUDED.synced_at
RETURNING id, status
"""


def _prepare(conn: psycopg.Connection) -> None:
    # A session temp table, created outside any transaction so a dry run's
    # rollback doesn't take it with it.
    conn.execute("DROP TABLE IF EXISTS pg_temp.sync_fix")
    conn.execute("CREATE TEMP TABLE sync_fix (seq integer, lat float8, lon float8)")
    if conn.execute("SELECT count(*) FROM network_box").fetchone()[0] == 0:
        raise RuntimeError("no segments for any network; run the city import before syncing")


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
            #
            # ponytail: match() here defaults to network="ptc", but city_ids
            # can span multiple networks (each activity is classified
            # independently against every network in _store_run). Currently
            # dormant: match()'s own node<->activity_piece join is already
            # network-correlated regardless of this param, and every node's
            # radius_m is set correctly at import time (app/importer.py's
            # regenerate_nodes), so nothing is ever missing for this call's
            # assign_radii(missing_only=True) step to backfill. It reactivates
            # the moment a second network has its own settings.yaml block and
            # real segments (Phase B/C) -- fix then by grouping city_ids by
            # their network and calling match() once per group with that
            # network's own settings, not just its slug.
            report.newly_hit += match(conn, settings, activity_ids=city_ids)
        if report.fetched > fetched_before:
            log.info("%s: fetched %d runs", oldest.strftime("%Y-%m"), report.fetched - fetched_before)

    if report.strava_stubs:
        log.warning("%d Strava-sourced runs have no GPS in Intervals: %s",
                    len(report.strava_stubs), report.strava_stubs)
    return report
