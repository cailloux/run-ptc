from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx2
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from app import db, exclusions, health
from app.config import ROOT, intervals_credentials, load_settings
from app.coverage import coverage_geojson
from app.finish import finish_route, new_miles
from app.graph import main_component
from app.intervals import IntervalsClient
from app.jobs import JobBusy, start_job
from app.matching import METERS_PER_MILE, metrics
from app.refresh import refresh
from app.routing import GraphMissing, RouteError, leg, snap
from app.sync import sync, sync_window

router = APIRouter()

LayerName = Literal["cartpath", "road"]


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class LegRequest(BaseModel):
    start: LatLon = Field(alias="from")
    end: LatLon = Field(alias="to")


class CoverageRequest(BaseModel):
    latlngs: list[tuple[float, float]] = Field(max_length=50_000)


class FinishRequest(BaseModel):
    start: LatLon
    segment_ids: list[int] = Field(min_length=1, max_length=150)   # as route.js MAX_PICKS


def _route_errors(fn):
    """Map routing failures onto HTTP: the click was bad (422) or the graph is missing (503)."""
    try:
        return fn()
    except RouteError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except GraphMissing as e:
        raise HTTPException(status_code=503, detail=str(e))


def _geojson(sql: str, params: tuple | dict) -> Response:
    """Run a query that builds a FeatureCollection in PostGIS and return it as-is."""
    with db.connect() as conn:
        body = conn.execute(sql, params).fetchone()[0]
    return Response(body, media_type="application/geo+json")


def default_intervals_client() -> IntervalsClient:
    athlete_id, api_key = intervals_credentials()
    return IntervalsClient(athlete_id, api_key)


def default_city_client() -> httpx2.Client:
    return httpx2.Client(timeout=120)


BUSY = "another job is running (sync, import, refresh, or recompute)"


def _start_in_background(background: BackgroundTasks, name: str, fn, on_busy=None) -> dict:
    """Start a job from a button. The job's connection holds the lock until
    the background task finishes. Poll GET /status for the result."""
    try:
        job = start_job(name, "button")
    except JobBusy:
        if on_busy:
            on_busy()
        raise HTTPException(status_code=409, detail=BUSY)
    background.add_task(job.run, fn, reraise=False)
    return {"job_id": job.id}


@router.get("/health")
def health_check() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# Clean URLs for the two share pages; the .html paths still work too since
# they're plain static files (app/main.py mounts app/static/ last).
@router.get("/left", include_in_schema=False)
def left_page() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "left.html")


@router.get("/progress", include_in_schema=False)
def progress_page() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "progress.html")


@router.get("/network")
def network(layer: LayerName) -> Response:
    """Segments with coverage state per run of node intervals (see app/coverage.py)."""
    spacing_m = load_settings().node_spacing_m
    with db.connect() as conn:
        body = coverage_geojson(conn, layer, spacing_m)
    return Response(body, media_type="application/geo+json")


@router.get("/nodes")
def nodes(layer: LayerName, status: Literal["hit", "missed"] | None = None) -> Response:
    """Node points with a hit flag. Details load per node from /nodes/{id}."""
    return _geojson("""
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(n.geom, 4326), 6)::json,
                'properties', json_build_object('id', n.id, 'hit', n.hit_at IS NOT NULL)
            ) ORDER BY n.id), '[]'::json))::text
        FROM node n JOIN segment s ON s.id = n.segment_id
        WHERE s.layer = %(layer)s
          AND (%(status)s::text IS NULL OR (n.hit_at IS NOT NULL) = (%(status)s = 'hit'))
    """, {"layer": layer, "status": status})


@router.get("/nodes/{node_id}")
def node_detail(node_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("""
            SELECT n.id, n.radius_m, n.part_idx, n.seq, s.layer, s.source_oid, s.name, s.seg_type,
                   n.hit_at, a.intervals_id, a.name
            FROM node n JOIN segment s ON s.id = n.segment_id
            LEFT JOIN activity a ON a.id = n.hit_activity_id
            WHERE n.id = %s
        """, (node_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such node")
    (nid, radius_m, part_idx, seq, layer, source_oid, seg_name, seg_type,
     hit_at, intervals_id, run_name) = row
    return {
        "id": nid,
        "radius_m": radius_m,
        "segment": {"layer": layer, "source_oid": source_oid, "name": seg_name,
                    "seg_type": seg_type, "part_idx": part_idx, "seq": seq},
        "hit": None if hit_at is None else {
            "start_at": hit_at, "intervals_id": intervals_id, "name": run_name},
    }


@router.get("/activities")
def activities() -> Response:
    """City runs. Tracks are simplified to 3 m for display only; matching uses full geometry."""
    return _geojson("""
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(ST_Simplify(geom, 3), 4326), 6)::json,
                'properties', json_build_object(
                    'id', id,
                    'intervals_id', intervals_id,
                    'start_at', start_at,
                    'name', name,
                    'sport', sport,
                    'distance_m', round(distance_m::numeric),
                    'parts', ST_NumGeometries(geom))
            ) ORDER BY start_at), '[]'::json))::text
        FROM activity WHERE status = 'city'
    """, ())


@router.post("/route/snap")
def route_snap(point: LatLon) -> dict:
    """Where a click lands on the network: the start of a route."""
    max_m = load_settings().route_snap_max_m
    with db.connect() as conn:
        s = _route_errors(lambda: snap(conn, point.lat, point.lon, max_m))
    return {"lat": s.lat, "lon": s.lon}


@router.post("/route/leg")
def route_leg(body: LegRequest) -> dict:
    """Shortest path along paths and roads from one clicked point to another."""
    max_m = load_settings().route_snap_max_m
    with db.connect() as conn:
        r = _route_errors(lambda: leg(conn, (body.start.lat, body.start.lon),
                                      (body.end.lat, body.end.lon), max_m))
    return {
        "latlngs": r.latlngs,
        "length_m": round(r.length_m, 1),
        "from": {"lat": r.start.lat, "lon": r.start.lon},
        "to": {"lat": r.end.lat, "lon": r.end.lon},
    }


@router.post("/route/coverage")
def route_coverage(body: CoverageRequest) -> dict:
    """How much of a route is new ground (not-run intervals), and where."""
    with db.connect() as conn:
        r = new_miles(conn, body.latlngs)
    return {"new_m": round(r.new_m, 1), "new": r.lines}


@router.post("/route/finish")
def route_finish(body: FinishRequest) -> dict:
    """A loop from the start that runs every not-run stretch of these segments."""
    max_m = load_settings().route_snap_max_m
    with db.connect() as conn:
        f = _route_errors(lambda: finish_route(conn, (body.start.lat, body.start.lon),
                                               body.segment_ids, max_m))
    return {
        "from": {"lat": f.start[0], "lon": f.start[1]},
        "legs": [{"latlngs": lg["latlngs"], "length_m": round(lg["length_m"], 1),
                  "to": {"lat": lg["to"][0], "lon": lg["to"][1]}} for lg in f.legs],
        "length_m": round(f.length_m, 1),
        "skipped": f.skipped,
    }


@router.get("/graph/islands")
def graph_islands() -> Response:
    """Edges not connected to the main network, for review."""
    with db.connect() as conn:
        main = main_component(conn)
        body = conn.execute("""
            WITH island AS (
                SELECT v.component, sum(e.length_m) AS length_m
                FROM route_edge e JOIN route_vertex v ON v.id = e.source
                WHERE v.component IS DISTINCT FROM %(main)s
                GROUP BY v.component
            )
            SELECT json_build_object('type', 'FeatureCollection', 'features',
                coalesce(json_agg(json_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(ST_Transform(e.geom, 4326), 6)::json,
                    'properties', json_build_object(
                        'component', i.component,
                        'island_length_m', round(i.length_m::numeric),
                        'layer', s.layer,
                        'source_oid', s.source_oid,
                        'name', s.name,
                        'seg_type', s.seg_type)
                ) ORDER BY i.component, e.id), '[]'::json))::text
            FROM route_edge e
            JOIN route_vertex v ON v.id = e.source
            JOIN island i ON i.component = v.component
            JOIN segment s ON s.id = e.segment_id
        """, {"main": main}).fetchone()[0]
    return Response(body, media_type="application/geo+json")


@router.post("/sync", status_code=202)
def start_sync(request: Request, background: BackgroundTasks) -> dict:
    """Run the default incremental sync in the background."""
    try:
        client = request.app.state.intervals_client_factory()
    except RuntimeError as e:   # credentials not set
        raise HTTPException(status_code=503, detail=str(e))
    settings = load_settings()

    def run_sync(conn) -> list[str]:
        try:
            start, end = sync_window(conn, settings)
            report = sync(conn, client, start, end, settings)
        finally:
            client.close()
        return [report.headline(), *report.lines()]

    return _start_in_background(background, "sync", run_sync, on_busy=client.close)


@router.post("/refresh", status_code=202)
def start_refresh(request: Request, background: BackgroundTasks) -> dict:
    """Check the city's layers and import any that changed, in the background."""
    try:
        excl = exclusions.load()
    except exclusions.ExclusionError as e:
        raise HTTPException(status_code=503, detail=f"exclusions.yaml: {e}")
    settings = load_settings()
    client = request.app.state.city_client_factory()

    def run_refresh(conn) -> list[str]:
        try:
            return refresh(conn, client, settings, excl).lines()
        finally:
            client.close()

    return _start_in_background(background, "refresh", run_refresh, on_busy=client.close)


@router.get("/changes")
def changes() -> Response:
    """Segments from each layer's latest city change (added or changed)."""
    return _geojson("""
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(s.geom, 4326), 6)::json,
                'properties', json_build_object(
                    'layer', s.layer, 'source_oid', s.source_oid, 'name', s.name,
                    'seg_type', s.seg_type, 'change', s.city_change, 'changed_at', s.changed_at)
            ) ORDER BY s.layer, s.source_oid), '[]'::json))::text
        FROM segment s
        JOIN (SELECT layer, max(changed_at) AS at FROM segment GROUP BY layer) latest
          ON latest.layer = s.layer AND s.changed_at = latest.at
    """, ())


@router.get("/status")
def status() -> dict:
    """Everything the status page and the map's banner show."""
    settings = load_settings()
    now = datetime.now(UTC)
    with db.connect() as conn:
        sources = {k: h.as_dict() for k, h in health.all_health(conn, settings, now).items()}

        by_status = dict(conn.execute("SELECT status, count(*) FROM activity GROUP BY status").fetchall())
        sources["sync"]["counts"] = {
            "city": by_status.get("city", 0), "outside": by_status.get("outside", 0),
            "no_gps": by_status.get("no_gps", 0),
            "latest_run": conn.execute(
                "SELECT max(start_at) FROM activity WHERE status = 'city'").fetchone()[0],
        }

        layers = {}
        for (layer, city_count, city_edited, imported_at, stored, counted, counted_m,
             excluded) in conn.execute("""
            SELECT l.layer, g.feature_count, g.max_edited_at, g.imported_at,
                   count(s.id), count(s.id) FILTER (WHERE s.counted AND NOT s.excluded),
                   coalesce(sum(s.length_m) FILTER (WHERE s.counted AND NOT s.excluded), 0),
                   count(s.id) FILTER (WHERE s.excluded)
            FROM (VALUES ('cartpath'), ('road')) l (layer)
            LEFT JOIN source_signature g ON g.layer = l.layer
            LEFT JOIN segment s ON s.layer = l.layer
            GROUP BY 1, 2, 3, 4
        """):
            layers[layer] = {
                "city_features": city_count, "city_last_edited": city_edited,
                "imported_at": imported_at, "stored": stored, "counted": counted,
                "counted_mi": round(counted_m / METERS_PER_MILE, 2), "excluded": excluded,
            }
        sources["city"]["layers"] = layers

        # The segments /changes shows, and the job that made the latest of
        # those changes: changed_at is its import's transaction start, so it
        # falls inside that job's run.
        change = conn.execute("""
            WITH latest AS (
                SELECT s.city_change, s.changed_at FROM segment s
                JOIN (SELECT layer, max(changed_at) AS at FROM segment GROUP BY layer) m
                  ON m.layer = s.layer AND s.changed_at = m.at
            ), at AS (SELECT max(changed_at) AS at FROM latest)
            SELECT at.at,
                   (SELECT count(*) FROM latest WHERE city_change = 'added'),
                   (SELECT count(*) FROM latest WHERE city_change = 'changed'),
                   j.id, j.job, j.summary
            FROM at
            LEFT JOIN LATERAL (
                SELECT id, job, summary FROM job_run
                WHERE job IN ('refresh', 'import') AND status = 'ok'
                  AND started_at <= at.at AND finished_at >= at.at
                ORDER BY started_at DESC LIMIT 1
            ) j ON true
            WHERE at.at IS NOT NULL
        """).fetchone()
        sources["city"]["last_change"] = None if change is None else {
            "changed_at": change[0], "added": change[1], "changed": change[2],
            "job_id": change[3], "job": change[4], "report": change[5],
        }

        nightly = health.nightly_last_run(conn)
        jobs = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, job, trigger, status, started_at, finished_at, summary, error FROM job_run"
            " ORDER BY started_at DESC, id DESC LIMIT 20").fetchall()
        episodes = health.recent_episodes(conn)
    stale_after = timedelta(hours=settings.stale_after_hours)
    return {
        "now": now,
        "stale_after_hours": settings.stale_after_hours,
        "sources": sources,
        "nightly": {"last_run": nightly,
                    "overdue": nightly is not None and now - nightly > stale_after},
        "episodes": episodes,
        "last_alert_sent": max((e["alerted_at"] for e in episodes if e["alerted_at"]), default=None),
        "jobs": jobs,
    }


@router.get("/stats")
def stats() -> dict:
    with db.connect() as conn:
        runs, latest = conn.execute(
            "SELECT count(*), max(start_at) FROM activity WHERE status = 'city'"
        ).fetchone()
        completion = metrics(conn).as_dict()
        rows = conn.execute("""
            SELECT s.layer,
                   count(*) FILTER (WHERE counted AND NOT excluded) AS counted,
                   coalesce(sum(length_m) FILTER (WHERE counted AND NOT excluded), 0) AS counted_m,
                   (SELECT count(*) FROM node n JOIN segment s2 ON s2.id = n.segment_id
                    WHERE s2.layer = s.layer) AS nodes
            FROM segment s GROUP BY s.layer
        """).fetchall()
    result = {
        layer: {"counted": counted, "counted_mi": round(counted_m / METERS_PER_MILE, 2), "nodes": nodes}
        for layer, counted, counted_m, nodes in rows
    }
    result["runs"] = {"city": runs, "latest_start_at": latest}
    result["completion"] = completion
    return result
