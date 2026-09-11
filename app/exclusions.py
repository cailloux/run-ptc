"""Load config/exclusions.yaml and apply it to the segment table.

Exclusions are judgment calls about valid city data. They're reapplied on
every import, on app startup, and by `python -m app.cli exclusions`.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml

from app.config import CONFIG_DIR

log = logging.getLogger(__name__)

EXCLUSIONS_PATH = CONFIG_DIR / "exclusions.yaml"


class ExclusionError(ValueError):
    pass


@dataclass(frozen=True)
class CartpathExclusion:
    global_id: str
    reason: str


@dataclass(frozen=True)
class RoadExclusion:
    reason: str
    object_id: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class Exclusions:
    cartpaths: tuple[CartpathExclusion, ...] = ()
    roads: tuple[RoadExclusion, ...] = ()


def normalize_global_id(value: str) -> str:
    """Match the city's format, {UPPERCASE-GUID}, whatever form was pasted in."""
    return "{" + value.strip().strip("{}").upper() + "}"


def _entries(data: dict, key: str) -> list[dict]:
    entries = data.get(key) or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ExclusionError(f"{key}: expected a list of entries")
    return entries


def _check_keys(entry: dict, allowed: set[str], where: str) -> None:
    unknown = set(entry) - allowed
    if unknown:
        raise ExclusionError(f"{where}: unknown keys {sorted(unknown)}")
    if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
        raise ExclusionError(f"{where}: reason is required")


def parse(data: object) -> Exclusions:
    if data is None:
        return Exclusions()
    if not isinstance(data, dict):
        raise ExclusionError("expected a mapping with cartpaths and roads")
    unknown = set(data) - {"cartpaths", "roads"}
    if unknown:
        raise ExclusionError(f"unknown top-level keys {sorted(unknown)}")

    cartpaths = []
    for i, e in enumerate(_entries(data, "cartpaths")):
        where = f"cartpaths[{i}]"
        _check_keys(e, {"global_id", "reason"}, where)
        if not isinstance(e.get("global_id"), str):
            raise ExclusionError(f"{where}: global_id is required")
        cartpaths.append(CartpathExclusion(normalize_global_id(e["global_id"]), e["reason"]))

    roads = []
    for i, e in enumerate(_entries(data, "roads")):
        where = f"roads[{i}]"
        _check_keys(e, {"object_id", "name", "reason"}, where)
        object_id, name = e.get("object_id"), e.get("name")
        if object_id is not None and (isinstance(object_id, bool) or not isinstance(object_id, int)):
            raise ExclusionError(f"{where}: object_id must be an integer")
        if name is not None and not isinstance(name, str):
            raise ExclusionError(f"{where}: name must be a string")
        if object_id is None and not name:
            raise ExclusionError(f"{where}: needs object_id or name")
        roads.append(RoadExclusion(e["reason"], object_id, name))

    return Exclusions(tuple(cartpaths), tuple(roads))


def load(path: Path = EXCLUSIONS_PATH) -> Exclusions:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ExclusionError(f"{path}: {e}") from e
    return parse(data)


def _same_name(a: str | None, b: str | None) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()


def _canonical_key(conn: psycopg.Connection, layer: str, key: str, label: str,
                   warnings: list[str]) -> str:
    row = conn.execute(
        "SELECT canonical_key FROM source_duplicate WHERE layer = %s AND dropped_key = %s",
        (layer, key),
    ).fetchone()
    if row is None:
        return key
    warnings.append(f"{label} is a dropped duplicate; applied to kept feature {row[0]}")
    return row[0]


def _exclude(conn: psycopg.Connection, where: str, params: tuple, reason: str) -> list[tuple]:
    return conn.execute(
        f"UPDATE segment SET excluded = true, exclusion_reason = %s WHERE {where}"
        " RETURNING source_key, name",
        (reason, *params),
    ).fetchall()


def apply(conn: psycopg.Connection, exclusions: Exclusions) -> list[str]:
    """Reset and reapply all exclusions in one transaction. Returns warnings."""
    warnings: list[str] = []
    with conn.transaction():
        conn.execute(
            "UPDATE segment SET excluded = false, exclusion_reason = NULL WHERE excluded"
        )

        for e in exclusions.cartpaths:
            label = f"cartpath {e.global_id}"
            key = _canonical_key(conn, "cartpath", e.global_id, label, warnings)
            if not _exclude(conn, "layer = 'cartpath' AND source_key = %s", (key,), e.reason):
                warnings.append(f"{label} matches no segment (deleted by the city?)")

        for e in exclusions.roads:
            if e.object_id is not None:
                label = f"road object_id {e.object_id}"
                key = _canonical_key(conn, "road", str(e.object_id), label, warnings)
                rows = _exclude(conn, "layer = 'road' AND source_key = %s", (key,), e.reason)
                if not rows:
                    warnings.append(f"{label} matches no segment (deleted by the city?)")
                elif e.name and not _same_name(rows[0][1], e.name):
                    warnings.append(
                        f"{label} is named {rows[0][1]!r} now, not {e.name!r}; check the entry"
                    )
            else:
                rows = _exclude(
                    conn,
                    "layer = 'road' AND upper(trim(name)) = upper(trim(%s))",
                    (e.name,),
                    e.reason,
                )
                if not rows:
                    warnings.append(f"road name {e.name!r} matches no segment")

    for w in warnings:
        log.warning("exclusions: %s", w)
    return warnings
