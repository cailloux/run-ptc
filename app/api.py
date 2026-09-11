from typing import Literal

from fastapi import APIRouter, Response

from app import db
from app.importer import METERS_PER_MILE

router = APIRouter()

LayerName = Literal["cartpath", "road"]


def _geojson(sql: str, params: tuple) -> Response:
    """Run a query that builds a FeatureCollection in PostGIS and return it as-is."""
    with db.connect() as conn:
        body = conn.execute(sql, params).fetchone()[0]
    return Response(body, media_type="application/geo+json")


@router.get("/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/network")
def network(layer: LayerName) -> Response:
    return _geojson("""
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(geom, 4326), 6)::json,
                'properties', json_build_object(
                    'id', id,
                    'source_key', source_key,
                    'source_oid', source_oid,
                    'name', name,
                    'seg_type', seg_type,
                    'counted', counted,
                    'excluded', excluded,
                    'exclusion_reason', exclusion_reason,
                    'length_m', round(length_m::numeric, 1),
                    'parts', ST_NumGeometries(geom))
            ) ORDER BY id), '[]'::json))::text
        FROM segment WHERE layer = %s
    """, (layer,))


@router.get("/nodes")
def nodes(layer: LayerName) -> Response:
    return _geojson("""
        SELECT json_build_object('type', 'FeatureCollection', 'features',
            coalesce(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(n.geom, 4326), 6)::json,
                'properties', json_build_object(
                    'segment_id', n.segment_id, 'part_idx', n.part_idx, 'seq', n.seq)
            ) ORDER BY n.id), '[]'::json))::text
        FROM node n JOIN segment s ON s.id = n.segment_id
        WHERE s.layer = %s
    """, (layer,))


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


@router.get("/stats")
def stats() -> dict:
    with db.connect() as conn:
        runs, latest = conn.execute(
            "SELECT count(*), max(start_at) FROM activity WHERE status = 'city'"
        ).fetchone()
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
    return result
