from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app import db
from app.config import intervals_credentials, load_settings
from app.coverage import coverage_geojson
from app.intervals import IntervalsClient
from app.jobs import JobBusy, last_runs, start_job
from app.matching import METERS_PER_MILE, metrics
from app.sync import sync, sync_window

router = APIRouter()

LayerName = Literal["cartpath", "road"]


def _geojson(sql: str, params: tuple | dict) -> Response:
    """Run a query that builds a FeatureCollection in PostGIS and return it as-is."""
    with db.connect() as conn:
        body = conn.execute(sql, params).fetchone()[0]
    return Response(body, media_type="application/geo+json")


def default_intervals_client() -> IntervalsClient:
    athlete_id, api_key = intervals_credentials()
    return IntervalsClient(athlete_id, api_key)


@router.get("/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/network")
def network(layer: LayerName) -> Response:
    """Segments with coverage state per run of node intervals (see app/coverage.py)."""
    with db.connect() as conn:
        body = coverage_geojson(conn, layer)
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


@router.get("/sync")
def sync_status() -> dict:
    """The latest sync (any trigger), the latest successful one, and whether a job is running."""
    with db.connect() as conn:
        return last_runs(conn, "sync")


@router.post("/sync", status_code=202)
def start_sync(request: Request, background: BackgroundTasks) -> dict:
    """Run the default incremental sync in the background. Poll GET /sync for the result."""
    try:
        client = request.app.state.intervals_client_factory()
    except RuntimeError as e:   # credentials not set
        raise HTTPException(status_code=503, detail=str(e))
    try:
        job = start_job("sync", "button")
    except JobBusy:
        client.close()
        raise HTTPException(status_code=409,
                            detail="another job is running (sync, import, or recompute)")
    settings = load_settings()

    def run_sync(conn) -> list[str]:
        try:
            start, end = sync_window(conn, settings)
            report = sync(conn, client, start, end, settings)
        finally:
            client.close()
        return [report.headline(), *report.lines()]

    # The job's connection holds the lock until the background task finishes.
    background.add_task(job.run, run_sync, reraise=False)
    return {"job_id": job.id}


@router.get("/stats")
def stats() -> dict:
    with db.connect() as conn:
        runs, latest = conn.execute(
            "SELECT count(*), max(start_at) FROM activity WHERE status = 'city'"
        ).fetchone()
        completion = metrics(conn).as_dict()
        sync_runs = last_runs(conn, "sync")
        rows = conn.execute("""
            SELECT s.layer,
                   count(*) AS stored,
                   count(*) FILTER (WHERE counted AND NOT excluded) AS counted,
                   coalesce(sum(length_m) FILTER (WHERE counted AND NOT excluded), 0) AS counted_m,
                   count(*) FILTER (WHERE excluded) AS excluded,
                   coalesce(sum(length_m) FILTER (WHERE excluded), 0) AS excluded_m,
                   (SELECT count(*) FROM node n JOIN segment s2 ON s2.id = n.segment_id
                    WHERE s2.layer = s.layer) AS nodes
            FROM segment s GROUP BY s.layer
        """).fetchall()
    result = {
        layer: {
            "stored": stored,
            "counted": counted,
            "counted_mi": round(counted_m / METERS_PER_MILE, 2),
            "excluded": excluded,
            "excluded_mi": round(excluded_m / METERS_PER_MILE, 2),
            "nodes": node_count,
        }
        for layer, stored, counted, counted_m, excluded, excluded_m, node_count in rows
    }
    result["runs"] = {"city": runs, "latest_start_at": latest}
    result["completion"] = completion
    latest_sync = sync_runs["latest"]
    result["last_sync"] = {
        "finished_at": sync_runs["last_ok"]["finished_at"] if sync_runs["last_ok"] else None,
        "headline": sync_runs["last_ok"]["headline"] if sync_runs["last_ok"] else None,
        "latest_status": latest_sync["status"] if latest_sync else None,
        "latest_error": latest_sync["error"] if latest_sync else None,
        "running": sync_runs["running"],
    }
    return result
