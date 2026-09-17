"""End-to-end proof of Phase A's own "done when" #2: a synthetic second
network's data coexists in the same live database as PTC's, and neither
network's coverage/stats/graph are affected by the other's presence.

Deliberately uses a genuinely different real SRID (Watopia's own UTM zone,
32758) rather than reusing 32616 like every other two-network test this
phase -- the one thing no other test exercised: the cross-SRID transform
paths in app/sync.py's classification and app/matching.recompute's resplit,
both no-ops until 016_network_enforce.sql relaxed the geometry typmods
enough for a second real SRID to exist at all. Segments are built directly
(not via app/importer.py's import_layer), since that staging pipeline is
PTC-ArcGIS-specific by design -- Phase B/C's real importers will have their
own pipelines too, not this one.
"""

from datetime import date

from app.graph import build_graph, graph_report
from app.importer import regenerate_nodes
from app.matching import metrics
from app.sync import sync
from tests.helpers import SETTINGS, add_network, cartpath, line, network_id, run_import
from tests.test_api_sync import api  # noqa: F401  (fixture)
from tests.test_sync import FakeIntervals, run

WATOPIA_SRID = 32758
WATOPIA_LON, WATOPIA_LAT = 166.94, -11.65


def insert_watopia_segment(conn, oid, source_key, lon0, lat0, lon1, lat1):
    net_id = network_id(conn, "testworld")
    return conn.execute("""
        INSERT INTO segment (network_id, layer, source_key, source_oid, counted,
                             length_m, geom_hash, props, geom)
        SELECT %(net)s, 'cartpath', %(key)s, %(oid)s, true, ST_Length(g),
               md5(ST_AsBinary(g)), '{}', g
        FROM (SELECT ST_Multi(ST_Transform(ST_SetSRID(
                  ST_MakeLine(ST_MakePoint(%(lon0)s, %(lat0)s), ST_MakePoint(%(lon1)s, %(lat1)s)), 4326),
                %(srid)s)) AS g) t
        RETURNING id
    """, {"net": net_id, "key": source_key, "oid": oid, "lon0": lon0, "lat0": lat0,
          "lon1": lon1, "lat1": lat1, "srid": WATOPIA_SRID}).fetchone()[0]


def watopia_track(lon0, lat0, lon1, lat1, n=10):
    """(lat, lon) points, the shape client.latlng() returns."""
    return [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]


def test_second_network_coexists_without_affecting_ptc(conn):
    # PTC: a small real fixture, metrics captured before testworld exists.
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    build_graph(conn, SETTINGS)
    ptc_before = metrics(conn, "ptc").as_dict()

    add_network(conn, "testworld", srid=WATOPIA_SRID)
    seg_id = insert_watopia_segment(conn, 1, "tw-1", WATOPIA_LON, WATOPIA_LAT,
                                    WATOPIA_LON + 0.001, WATOPIA_LAT)
    regenerate_nodes(conn, [seg_id], SETTINGS, network="testworld")
    build_graph(conn, SETTINGS, network="testworld")

    # A synthetic ride, classified through the real sync() pipeline -- proves
    # network_box's per-network SRID transform actually works, not just that
    # it's syntactically present.
    track = watopia_track(WATOPIA_LON, WATOPIA_LAT, WATOPIA_LON + 0.001, WATOPIA_LAT)
    report = sync(conn, FakeIntervals([run("z1")], {"z1": track}),
                  date(2024, 5, 4), date(2024, 5, 4), SETTINGS)
    assert report.statuses == {"city": 1}
    status, net_id = conn.execute(
        "SELECT status, network_id FROM activity WHERE intervals_id = 'z1'").fetchone()
    assert (status, net_id) == ("city", network_id(conn, "testworld"))

    hit_count = conn.execute(
        "SELECT count(*) FROM node WHERE segment_id = %s AND hit_at IS NOT NULL", (seg_id,)
    ).fetchone()[0]
    assert hit_count > 0

    # The actual isolation proof: ptc's own numbers are bit-identical to
    # before testworld existed, and testworld's numbers reflect only itself.
    assert metrics(conn, "ptc").as_dict() == ptc_before
    tw_metrics = metrics(conn, "testworld").as_dict()
    assert tw_metrics["segments_total"] == 1
    assert tw_metrics["cartpath_complete_mi"] > 0

    assert graph_report(conn, "ptc").edges == 1
    assert graph_report(conn, "testworld").edges == 1


def test_second_network_isolated_through_the_real_api(api, conn):
    """Same proof, but through the actual HTTP layer -- the "same live
    database" the spec's done-when criterion actually means."""
    client, _ = api
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    build_graph(conn, SETTINGS)
    ptc_before = client.get("/stats").json()

    add_network(conn, "testworld", srid=WATOPIA_SRID)
    seg_id = insert_watopia_segment(conn, 1, "tw-1", WATOPIA_LON, WATOPIA_LAT,
                                    WATOPIA_LON + 0.001, WATOPIA_LAT)
    regenerate_nodes(conn, [seg_id], SETTINGS, network="testworld")
    build_graph(conn, SETTINGS, network="testworld")

    assert client.get("/stats").json() == ptc_before
    tw_stats = client.get("/stats", params={"network": "testworld"}).json()
    assert tw_stats["cartpath"]["counted"] == 1
