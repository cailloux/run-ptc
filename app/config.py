import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MIGRATIONS_DIR = ROOT / "db" / "migrations"


@dataclass(frozen=True)
class Settings:
    node_spacing_m: float
    min_segment_length_m: float
    min_part_length_m: float
    dedupe_grid_m: float
    cartpath_counted_types: tuple[str, ...]
    road_counted_city: str
    sync_run_types: tuple[str, ...]
    sync_backfill_start: date
    track_gap_split_m: float
    match_radius_m: float
    match_radius_wide_m: float
    match_radius_wide_road_classes: tuple[str, ...]


def load_settings(path: Path = CONFIG_DIR / "settings.yaml") -> Settings:
    data = yaml.safe_load(path.read_text())
    for key in ("cartpath_counted_types", "sync_run_types", "match_radius_wide_road_classes"):
        data[key] = tuple(data[key])
    if isinstance(data.get("sync_backfill_start"), str):
        data["sync_backfill_start"] = date.fromisoformat(data["sync_backfill_start"])
    # Unknown or missing keys raise TypeError, so typos fail loudly.
    return Settings(**data)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def intervals_credentials() -> tuple[str, str]:
    """(athlete_id, api_key). Only sync needs these; the map runs without them."""
    athlete_id = os.environ.get("INTERVALS_ATHLETE_ID")
    api_key = os.environ.get("INTERVALS_API_KEY")
    if not athlete_id or not api_key:
        raise RuntimeError("INTERVALS_ATHLETE_ID and INTERVALS_API_KEY must be set to sync")
    return athlete_id, api_key
