import httpx

# The city server caps every response at this many records.
PAGE_SIZE = 1000
SRID = 32616


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
