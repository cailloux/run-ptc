"""Hand-built fixtures in UTM 16N. Coordinates are offsets in meters from an
origin inside Peachtree City, so expected node positions are easy to read."""

from datetime import date

from app.config import Settings
from app.exclusions import Exclusions
from app.importer import LAYERS, import_layer

X0, Y0 = 730000.0, 3692000.0

SETTINGS = Settings(
    node_spacing_m=20,
    min_segment_length_m=1,
    min_part_length_m=1,
    dedupe_grid_m=0.01,
    cartpath_counted_types=("Path", "Bridge", "Tunnel"),
    road_counted_city="PEACHTREE CITY",
    sync_run_types=("Run", "TrailRun"),
    sync_backfill_start=date(2023, 1, 1),
    track_gap_split_m=100,
)


def line(*points: tuple[float, float]) -> list[list[float]]:
    return [[X0 + x, Y0 + y] for x, y in points]


def _geometry(parts: tuple) -> dict:
    if len(parts) == 1:
        return {"type": "LineString", "coordinates": parts[0]}
    return {"type": "MultiLineString", "coordinates": list(parts)}


def global_id(oid: int) -> str:
    return f"{{00000000-0000-0000-0000-{oid:012d}}}"


def cartpath(oid: int, *parts, type_: str = "Path", **props) -> dict:
    return {
        "type": "Feature",
        "geometry": _geometry(parts),
        "properties": {
            "OBJECTID_1": oid,
            "OBJECTID": None,
            "GlobalID": global_id(oid),
            "Type": type_,
            "last_edited_date": 1767225600000,
            **props,
        },
    }


def road(oid: int, *parts, name: str = "TEST RD", city: str = "PEACHTREE CITY", **props) -> dict:
    return {
        "type": "Feature",
        "geometry": _geometry(parts),
        "properties": {
            "OBJECTID": oid,
            "RoadName": name,
            "CLASS": "Residential",
            "City": city,
            "last_edited_date": 1767225600000,
            **props,
        },
    }


def run_import(conn, layer: str, features: list[dict], exclusions: Exclusions = Exclusions(),
               settings: Settings = SETTINGS):
    return import_layer(conn, LAYERS[layer], features, settings, exclusions)


def nodes_of(conn, oid: int, layer: str = "cartpath") -> list[tuple]:
    """(part_idx, seq, x, y) for one segment, as offsets from the origin."""
    return [
        (part, seq, round(x - X0, 3), round(y - Y0, 3))
        for part, seq, x, y in conn.execute(
            """
            SELECT n.part_idx, n.seq, ST_X(n.geom), ST_Y(n.geom)
            FROM node n JOIN segment s ON s.id = n.segment_id
            WHERE s.layer = %s AND s.source_oid = %s
            ORDER BY n.part_idx, n.seq
            """,
            (layer, oid),
        )
    ]


def segment(conn, oid: int, layer: str = "cartpath") -> dict | None:
    row = conn.execute(
        """
        SELECT source_key, counted, excluded, exclusion_reason, length_m,
               ST_NumGeometries(geom), GeometryType(geom), ST_SRID(geom)
        FROM segment WHERE layer = %s AND source_oid = %s
        """,
        (layer, oid),
    ).fetchone()
    if row is None:
        return None
    keys = ("source_key", "counted", "excluded", "reason", "length_m", "parts", "type", "srid")
    return dict(zip(keys, row))
