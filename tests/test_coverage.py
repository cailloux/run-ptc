import json

from app import exclusions as excl
from app.coverage import coverage_geojson
from tests.helpers import add_network, cartpath, global_id, line, road, run_import
from tests.test_matching import set_hits

# Nodes on a 100 m straight part sit at x = 0, 20, ... 100 (seq 0-5), so every
# interval is 20 m.
STRAIGHT = line((0, 0), (100, 0))


def pieces(conn, layer, oid):
    """(state, length_m) of every feature for one segment, in drawing order along x."""
    feats = [f for f in json.loads(coverage_geojson(conn, layer))["features"]
             if f["properties"]["source_oid"] == oid]
    feats.sort(key=lambda f: min(pt[0] for pt in _coords(f["geometry"])))
    return [(f["properties"]["state"], f["properties"]["length_m"]) for f in feats]


def _coords(geom):
    return geom["coordinates"] if geom["type"] == "LineString" else [
        pt for part in geom["coordinates"] for pt in part]


def test_fully_hit_cart_path_is_one_complete_feature(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    set_hits(conn, "cartpath", 1, [(0, s) for s in range(6)])
    assert pieces(conn, "cartpath", 1) == [("complete", 100.0)]


def test_untouched_cart_path_is_one_not_run_feature(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    assert pieces(conn, "cartpath", 1) == [("not_run", 100.0)]


def test_consecutive_intervals_with_the_same_state_merge(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    set_hits(conn, "cartpath", 1, [(0, 0), (0, 1), (0, 2), (0, 4)])
    # (0-1) and (1-2) are run; (2-3), (3-4), (4-5) each lack a hit end.
    assert pieces(conn, "cartpath", 1) == [("run", 40.0), ("not_run", 60.0)]


def test_separate_stretches_stay_separate(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    set_hits(conn, "cartpath", 1, [(0, 0), (0, 1), (0, 3), (0, 4)])
    assert pieces(conn, "cartpath", 1) == [
        ("run", 20.0), ("not_run", 40.0), ("run", 20.0), ("not_run", 20.0)]


def test_pieces_never_span_parts(conn):
    # Part 0: x = 0..40, part 1: x = 100..140. Every node hit except the last.
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)), line((100, 0), (140, 0)))])
    set_hits(conn, "cartpath", 1, [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)])
    assert pieces(conn, "cartpath", 1) == [("run", 40.0), ("run", 20.0), ("not_run", 20.0)]


def test_excluded_and_uncounted_segments_come_back_whole(conn):
    rules = excl.parse({"cartpaths": [{"global_id": global_id(2), "reason": "Gated"}]})
    run_import(conn, "cartpath", [
        cartpath(1, STRAIGHT, type_="Road Crossing"),
        cartpath(2, line((0, 50), (100, 50))),
    ], exclusions=rules)
    set_hits(conn, "cartpath", 2, [(0, 0), (0, 1)])
    assert pieces(conn, "cartpath", 1) == [("uncounted", 100.0)]
    assert pieces(conn, "cartpath", 2) == [("excluded", 100.0)]


def test_roads_are_never_complete(conn):
    run_import(conn, "road", [road(1, STRAIGHT)])
    set_hits(conn, "road", 1, [(0, s) for s in range(6)])
    assert pieces(conn, "road", 1) == [("run", 100.0)]


def test_popup_fields(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    set_hits(conn, "cartpath", 1, [(0, 0), (0, 1)])
    props = json.loads(coverage_geojson(conn, "cartpath"))["features"][0]["properties"]
    assert (props["nodes_hit"], props["nodes_total"], props["segment_length_m"], props["parts"]) == (
        2, 6, 100.0, 1)


def test_networks_never_see_each_others_coverage(conn):
    run_import(conn, "cartpath", [cartpath(1, STRAIGHT)])
    add_network(conn, "testworld")
    run_import(conn, "cartpath", [cartpath(101, STRAIGHT)], network="testworld")

    ptc_oids = {f["properties"]["source_oid"] for f in
                json.loads(coverage_geojson(conn, "cartpath", "ptc"))["features"]}
    tw_oids = {f["properties"]["source_oid"] for f in
               json.loads(coverage_geojson(conn, "cartpath", "testworld"))["features"]}
    assert ptc_oids == {1}
    assert tw_oids == {101}
