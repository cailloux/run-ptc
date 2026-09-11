"""Import a city layer: clean up, upsert segments, and regenerate nodes.

Clean-up rules are in docs/PLAN.md under "Clean-up on import". All geometry
work happens in PostGIS; Python only shapes rows and reports.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

import psycopg

from app import exclusions as excl
from app.config import Settings
from app.matching import METERS_PER_MILE, assign_radii, match

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Layer:
    name: str
    url: str
    oid_field: str
    # Maps source properties to (source_oid, source_key, name, seg_type, counted).
    attrs: Callable[[dict, Settings], tuple]


def _clean(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _cartpath_name(value: object) -> str | None:
    # The cart path layer fills unused name fields with the string "N/A".
    name = _clean(value)
    return None if name and name.upper() == "N/A" else name


def _cartpath_attrs(p: dict, s: Settings) -> tuple:
    name = _cartpath_name(p.get("TunnelName")) or _cartpath_name(p.get("BridgeName"))
    seg_type = _clean(p.get("Type"))
    return (p["OBJECTID_1"], excl.normalize_global_id(p["GlobalID"]), name, seg_type,
            seg_type in s.cartpath_counted_types)


def _road_attrs(p: dict, s: Settings) -> tuple:
    # Roads outside the city stay in the table for routing but never count.
    return (p["OBJECTID"], str(p["OBJECTID"]), _clean(p.get("RoadName")), _clean(p.get("CLASS")),
            _clean(p.get("City")) == s.road_counted_city)


LAYERS = {
    "cartpath": Layer(
        "cartpath",
        "https://gis.peachtree-city.org/arcgis/rest/services/Public_Services/PeachtreeCityGolfCartPath/MapServer/3",
        "OBJECTID_1",
        _cartpath_attrs,
    ),
    "road": Layer(
        "road",
        "https://gis.peachtree-city.org/arcgis/rest/services/BASE/Road_Centerlines/MapServer/0",
        "OBJECTID",
        _road_attrs,
    ),
}


@dataclass
class ImportReport:
    layer: str
    raw: int = 0
    no_geometry: list[int] = field(default_factory=list)
    slivers: list[tuple] = field(default_factory=list)      # (oid, name, length_m)
    duplicates: list[tuple] = field(default_factory=list)   # (dropped_oid, kept_oid)
    added: int = 0
    changed: int = 0
    removed: int = 0
    stored: int = 0
    counted: int = 0
    counted_m: float = 0.0
    excluded: int = 0
    nodes: int = 0
    tiny_parts: list[tuple] = field(default_factory=list)   # (oid, part_idx, length_m)
    nodeless: list[int] = field(default_factory=list)       # counted oids with no nodes
    exclusion_warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [
            f"{self.layer}: {self.raw} raw, {len(self.duplicates)} duplicates dropped, "
            f"{len(self.slivers)} slivers dropped, {len(self.no_geometry)} without geometry",
            f"{self.layer}: {self.stored} stored, {self.counted} counted "
            f"({self.counted_m / METERS_PER_MILE:.2f} mi), {self.excluded} excluded",
            f"{self.layer}: {self.added} added, {self.changed} changed, {self.removed} removed",
            f"{self.layer}: {self.nodes} nodes, {len(self.tiny_parts)} tiny parts without nodes",
        ]


def _edited_at(p: dict) -> datetime | None:
    ms = p.get("last_edited_date")
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


def _staging_rows(layer: Layer, features: list[dict], settings: Settings, report: ImportReport):
    for f in features:
        p = f["properties"]
        oid, key, name, seg_type, counted = layer.attrs(p, settings)
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            report.no_geometry.append(oid)
            continue
        yield (oid, key, name, seg_type, counted, _edited_at(p), json.dumps(p), json.dumps(geom))


NODES_SQL = """
INSERT INTO node (segment_id, part_idx, seq, geom)
SELECT s.id, d.path[1] - 1, i, ST_LineInterpolatePoint(d.geom, i::float8 / k.n)
FROM segment s
CROSS JOIN LATERAL ST_Dump(s.geom) d
CROSS JOIN LATERAL (
    SELECT GREATEST(1, CEIL(ST_Length(d.geom) / %(spacing)s))::int AS n
) k
CROSS JOIN LATERAL generate_series(0, k.n) i
WHERE s.id = ANY(%(ids)s) AND s.counted AND ST_Length(d.geom) >= %(min_part)s
"""


def regenerate_nodes(conn: psycopg.Connection, segment_ids: list[int], settings: Settings) -> None:
    """Replace the nodes of these segments and match them against every stored run."""
    if not segment_ids:
        return
    conn.execute("DELETE FROM node WHERE segment_id = ANY(%s)", (segment_ids,))
    conn.execute(NODES_SQL, {
        "ids": segment_ids,
        "spacing": settings.node_spacing_m,
        "min_part": settings.min_part_length_m,
    })
    assign_radii(conn, settings, segment_ids=segment_ids)
    match(conn, settings, segment_ids=segment_ids)


def import_layer(conn: psycopg.Connection, layer: Layer, features: list[dict],
                 settings: Settings, exclusions: excl.Exclusions) -> ImportReport:
    report = ImportReport(layer.name, raw=len(features))
    if not features:
        raise RuntimeError(f"{layer.name}: city returned no features; refusing to import")

    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS pg_temp.staging")
        conn.execute("""
            CREATE TEMP TABLE staging (
                source_oid integer NOT NULL,
                source_key text NOT NULL,
                name       text,
                seg_type   text,
                counted    boolean NOT NULL,
                edited_at  timestamptz,
                props      jsonb NOT NULL,
                geojson    text NOT NULL,
                geom       geometry(MultiLineString, 32616),
                length_m   double precision,
                geom_hash  text
            ) ON COMMIT DROP
        """)
        with conn.cursor().copy(
            "COPY staging (source_oid, source_key, name, seg_type, counted, edited_at, props, geojson)"
            " FROM STDIN"
        ) as copy:
            for row in _staging_rows(layer, features, settings, report):
                copy.write_row(row)

        dup_keys = conn.execute(
            "SELECT source_key FROM staging GROUP BY 1 HAVING count(*) > 1"
        ).fetchall()
        if dup_keys:
            raise RuntimeError(f"{layer.name}: repeated source keys {[k for k, in dup_keys]}")

        # The city sends UTM coordinates without an SRID, and lines as either
        # LineString or MultiLineString. Store everything as 2D MultiLineString.
        conn.execute("""
            UPDATE staging
            SET geom = ST_Multi(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(geojson), 32616)))
        """)
        # ST_Normalize fixes vertex order and part order, so reversed or
        # reordered copies hash the same.
        conn.execute("""
            UPDATE staging
            SET length_m = ST_Length(geom),
                geom_hash = md5(ST_AsBinary(ST_Normalize(ST_SnapToGrid(geom, %s))))
        """, (settings.dedupe_grid_m,))

        report.slivers = conn.execute(
            "DELETE FROM staging WHERE length_m < %s RETURNING source_oid, name, length_m",
            (settings.min_segment_length_m,),
        ).fetchall()

        dups = conn.execute("""
            WITH ranked AS (
                SELECT source_oid, source_key,
                       first_value(source_oid) OVER w AS kept_oid,
                       first_value(source_key) OVER w AS kept_key,
                       row_number() OVER w AS rn
                FROM staging
                WINDOW w AS (PARTITION BY geom_hash ORDER BY source_oid)
            )
            SELECT source_oid, source_key, kept_oid, kept_key
            FROM ranked WHERE rn > 1 ORDER BY source_oid
        """).fetchall()
        report.duplicates = [(d[0], d[2]) for d in dups]
        conn.execute("DELETE FROM source_duplicate WHERE layer = %s", (layer.name,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_duplicate (layer, dropped_key, canonical_key) VALUES (%s, %s, %s)",
                [(layer.name, d[1], d[3]) for d in dups],
            )
        conn.execute("DELETE FROM staging WHERE source_key = ANY(%s)", ([d[1] for d in dups],))

        changed_ids = [r[0] for r in conn.execute("""
            SELECT s.id FROM segment s
            JOIN staging t ON t.source_key = s.source_key
            WHERE s.layer = %s AND (s.geom_hash <> t.geom_hash OR s.counted <> t.counted)
        """, (layer.name,))]
        report.changed = len(changed_ids)

        report.removed = len(conn.execute("""
            DELETE FROM segment s
            WHERE s.layer = %s
              AND NOT EXISTS (SELECT 1 FROM staging t WHERE t.source_key = s.source_key)
            RETURNING s.id
        """, (layer.name,)).fetchall())

        upserted = conn.execute("""
            INSERT INTO segment (layer, source_key, source_oid, name, seg_type, counted,
                                 length_m, source_edited_at, geom_hash, props, geom)
            SELECT %s, source_key, source_oid, name, seg_type, counted,
                   length_m, edited_at, geom_hash, props, geom
            FROM staging
            ON CONFLICT (layer, source_key) DO UPDATE SET
                source_oid = EXCLUDED.source_oid,
                name = EXCLUDED.name,
                seg_type = EXCLUDED.seg_type,
                counted = EXCLUDED.counted,
                length_m = EXCLUDED.length_m,
                source_edited_at = EXCLUDED.source_edited_at,
                geom_hash = EXCLUDED.geom_hash,
                props = EXCLUDED.props,
                geom = EXCLUDED.geom
            RETURNING id, (xmax = 0) AS inserted
        """, (layer.name,)).fetchall()
        added_ids = [r[0] for r in upserted if r[1]]
        report.added = len(added_ids)

        regenerate_nodes(conn, added_ids + changed_ids, settings)
        report.exclusion_warnings = excl.apply(conn, exclusions)

        report.tiny_parts = conn.execute("""
            SELECT s.source_oid, d.path[1] - 1, ST_Length(d.geom)
            FROM segment s CROSS JOIN LATERAL ST_Dump(s.geom) d
            WHERE s.layer = %s AND s.counted AND ST_Length(d.geom) < %s
            ORDER BY 1, 2
        """, (layer.name, settings.min_part_length_m)).fetchall()
        report.nodeless = [r[0] for r in conn.execute("""
            SELECT source_oid FROM segment s
            WHERE layer = %s AND counted
              AND NOT EXISTS (SELECT 1 FROM node n WHERE n.segment_id = s.id)
            ORDER BY 1
        """, (layer.name,))]
        (report.stored, report.counted, report.counted_m, report.excluded,
         report.nodes) = conn.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE counted AND NOT excluded),
                   coalesce(sum(length_m) FILTER (WHERE counted AND NOT excluded), 0),
                   count(*) FILTER (WHERE excluded),
                   (SELECT count(*) FROM node n JOIN segment s2 ON s2.id = n.segment_id
                    WHERE s2.layer = %(layer)s)
            FROM segment WHERE layer = %(layer)s
        """, {"layer": layer.name}).fetchone()

    _log_warnings(report)
    return report


def _log_warnings(r: ImportReport) -> None:
    if r.no_geometry:
        log.warning("%s: skipped features without geometry: %s", r.layer, r.no_geometry)
    for oid, name, length in r.slivers:
        log.warning("%s: dropped sliver %s (%s, %.2f m)", r.layer, oid, name, length)
    if r.duplicates:
        pairs = ", ".join(f"{d}->{k}" for d, k in r.duplicates)
        log.warning("%s: dropped %d duplicates (dropped->kept): %s", r.layer, len(r.duplicates), pairs)
    for oid, part, length in r.tiny_parts:
        log.warning("%s: %s part %d is %.2f m; no nodes", r.layer, oid, part, length)
    if r.nodeless:
        log.warning("%s: counted segments with no nodes: %s", r.layer, r.nodeless)
