import json
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

# The city server caps every response at this many records.
PAGE_SIZE = 1000
SRID = 32616


@dataclass(frozen=True)
class LayerSignature:
    """Enough to tell whether a layer changed: additions move the count and
    highest OID, deletions move the count, edits move the latest edit date."""
    feature_count: int
    max_oid: int
    max_edited_at: datetime | None


def layer_signature(client: httpx.Client, layer_url: str, oid_field: str) -> LayerSignature:
    """One statistics request. The layers publish no layer-level edit date."""
    stats = [
        {"statisticType": "count", "onStatisticField": oid_field, "outStatisticFieldName": "n"},
        {"statisticType": "max", "onStatisticField": oid_field, "outStatisticFieldName": "max_oid"},
        {"statisticType": "max", "onStatisticField": "last_edited_date",
         "outStatisticFieldName": "max_edited"},
    ]
    resp = client.get(f"{layer_url}/query", params={
        "where": "1=1", "f": "json", "outStatistics": json.dumps(stats),
    })
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error from {layer_url}: {data['error']}")
    # The server has been seen to answer with no rows for a while, then recover.
    if not data.get("features"):
        raise RuntimeError(f"city server returned no statistics for {layer_url}; try again later")
    # ArcGIS sometimes changes the case of output field names.
    attrs = {k.lower(): v for k, v in data["features"][0]["attributes"].items()}
    edited_ms = attrs.get("max_edited")
    return LayerSignature(
        feature_count=int(attrs["n"]),
        max_oid=int(attrs["max_oid"]),
        max_edited_at=datetime.fromtimestamp(edited_ms / 1000, tz=UTC) if edited_ms else None,
    )


def fetch_features(client: httpx.Client, layer_url: str, oid_field: str) -> list[dict]:
    """Fetch every feature of a layer as GeoJSON in UTM 16N, paging by resultOffset."""
    features: list[dict] = []
    while True:
        resp = client.get(
            f"{layer_url}/query",
            params={
                "where": "1=1",
                "outFields": "*",
                "f": "geojson",
                "outSR": SRID,
                "orderByFields": oid_field,
                "resultOffset": len(features),
                "resultRecordCount": PAGE_SIZE,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # ArcGIS reports query errors as HTTP 200 with an "error" body.
        if "error" in data:
            raise RuntimeError(f"ArcGIS error from {layer_url}: {data['error']}")
        crs = data.get("crs", {}).get("properties", {}).get("name")
        if crs != f"EPSG:{SRID}":
            raise RuntimeError(f"expected EPSG:{SRID} from {layer_url}, got {crs!r}")
        page = data["features"]
        features.extend(page)
        if len(page) < PAGE_SIZE:
            return features
