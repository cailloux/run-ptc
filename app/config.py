import os
from dataclasses import dataclass
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


def load_settings(path: Path = CONFIG_DIR / "settings.yaml") -> Settings:
    data = yaml.safe_load(path.read_text())
    data["cartpath_counted_types"] = tuple(data["cartpath_counted_types"])
    # Unknown or missing keys raise TypeError, so typos fail loudly.
    return Settings(**data)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url
