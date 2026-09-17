import math

import pytest

from app.exclusions import Exclusions
from app.importer import LAYERS, import_layer
from tests.helpers import (SETTINGS, add_network, cartpath, global_id, line, network_id, nodes_of, road,
                           run_import, segment)


def reversed_line(coords):
    return list(reversed(coords))


def test_linestring_is_stored_as_utm_multilinestring(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (30, 40)))])
    s = segment(conn, 1)
    assert (s["type"], s["srid"], s["parts"]) == ("MULTILINESTRING", 32616, 1)
    assert s["source_key"] == global_id(1)


def test_wrong_srid_geometry_is_rejected_by_the_trigger(conn):
    with pytest.raises(Exception, match="does not match"):
        conn.execute("""
            INSERT INTO segment (network_id, layer, source_key, source_oid, counted,
                                 length_m, geom_hash, props, geom)
            VALUES (%(network_id)s, 'cartpath', 'bad', 999, true, 10, 'x', '{}',
                    ST_GeomFromText('MULTILINESTRING((0 0, 10 0))', 4326))
        """, {"network_id": network_id(conn)})


def test_imported_segment_gets_the_network_id(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (30, 40)))])
    stored = conn.execute("SELECT network_id FROM segment WHERE source_oid = 1").fetchone()[0]
    assert stored == network_id(conn)


def test_unknown_network_slug_is_refused(conn):
    with pytest.raises(RuntimeError, match="nope"):
        import_layer(conn, LAYERS["cartpath"], [cartpath(1, line((0, 0), (30, 40)))],
                     SETTINGS, Exclusions(), network="nope")


def test_cartpath_name_skips_na_placeholders(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (30, 0)), type_="Bridge", TunnelName="N/A", BridgeName="Ardenlee"),
        cartpath(2, line((0, 9), (30, 9)), type_="Tunnel", TunnelName="Georgian Park", BridgeName="N/A"),
        cartpath(3, line((0, 18), (30, 18)), TunnelName="N/A", BridgeName=" n/a "),
    ])
    names = dict(conn.execute("SELECT source_oid, name FROM segment ORDER BY 1").fetchall())
    assert names == {1: "Ardenlee", 2: "Georgian Park", 3: None}


def test_length_comes_from_geometry_not_city_attributes(conn):
    run_import(conn, "road", [
        road(1, line((0, 0), (30, 40)), LengthMile=0, LENGTH=999.0, **{"SHAPE.STLength()": 1.0}),
    ])
    assert math.isclose(segment(conn, 1, "road")["length_m"], 50.0)


def test_reversed_duplicate_keeps_lowest_oid(conn):
    a = line((0, 0), (10, 5), (40, 5))
    report = run_import(conn, "cartpath", [
        cartpath(22510, reversed_line(a)),
        cartpath(7, a),
    ])
    assert report.duplicates == [(22510, 7)]
    assert segment(conn, 22510) is None
    assert segment(conn, 7) is not None
    assert conn.execute("SELECT dropped_key, canonical_key FROM source_duplicate").fetchall() == [
        (global_id(22510), global_id(7)),
    ]


def test_multipart_duplicate_with_parts_reordered_and_reversed(conn):
    p1, p2 = line((0, 0), (30, 0)), line((100, 0), (130, 0))
    report = run_import(conn, "road", [
        road(3, p1, p2),
        road(10900, reversed_line(p2), reversed_line(p1)),
    ])
    assert report.duplicates == [(10900, 3)]
    assert report.stored == 1


def test_near_duplicate_is_kept(conn):
    report = run_import(conn, "road", [
        road(3, line((0, 0), (30, 0))),
        road(4, line((0, 1), (30, 1))),   # parallel, 1 m away
    ])
    assert report.duplicates == []
    assert report.stored == 2


def test_sliver_under_1m_is_dropped(conn):
    report = run_import(conn, "road", [
        road(1, line((0, 0), (0.34, 0)), name="PEACHTREE CROSSINGS SHOPPING CENTER"),
        road(2, line((0, 10), (30, 10))),
    ])
    [(oid, name, length)] = report.slivers
    assert (oid, name) == (1, "PEACHTREE CROSSINGS SHOPPING CENTER")
    assert math.isclose(length, 0.34)
    assert segment(conn, 1, "road") is None


def test_roads_outside_the_city_are_stored_but_not_counted(conn):
    report = run_import(conn, "road", [
        road(1, line((0, 0), (30, 0)), city="TYRONE"),
        road(2, line((0, 10), (30, 10)), city="Unknown"),
        road(3, line((0, 20), (30, 20))),
    ])
    assert [segment(conn, i, "road")["counted"] for i in (1, 2, 3)] == [False, False, True]
    assert nodes_of(conn, 1, "road") == []
    assert (report.stored, report.counted) == (3, 1)
    assert math.isclose(report.counted_m, 30.0)


def test_reimport_is_idempotent(conn):
    features = [cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((0, 10), (50, 10)))]
    first = run_import(conn, "cartpath", features)
    node_ids = conn.execute("SELECT array_agg(id ORDER BY id) FROM node").fetchone()[0]
    second = run_import(conn, "cartpath", features)
    assert (first.added, first.changed, first.removed) == (2, 0, 0)
    assert (second.added, second.changed, second.removed) == (0, 0, 0)
    assert conn.execute("SELECT array_agg(id ORDER BY id) FROM node").fetchone()[0] == node_ids


def test_changed_geometry_regenerates_nodes(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    report = run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    assert report.changed == 1
    assert len(nodes_of(conn, 1)) == 6


def test_type_change_to_uncounted_removes_nodes(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    report = run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)), type_="Parking Lot")])
    assert report.changed == 1
    assert nodes_of(conn, 1) == []


def test_removed_feature_deletes_segment_and_nodes(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0))), cartpath(2, line((0, 9), (40, 9)))])
    report = run_import(conn, "cartpath", [cartpath(2, line((0, 9), (40, 9)))])
    assert report.removed == 1
    assert segment(conn, 1) is None
    assert conn.execute("SELECT count(*) FROM node").fetchone()[0] == 3


def test_layers_are_imported_independently(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    run_import(conn, "road", [road(1, line((0, 0), (40, 0)))])
    assert segment(conn, 1) is not None
    assert segment(conn, 1, "road") is not None


def test_networks_are_imported_independently(conn):
    """A second network's import of the same layer name must not treat the
    first network's segments as removed, changed, or absent."""
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])

    add_network(conn, "testworld")
    report = run_import(conn, "cartpath", [cartpath(101, line((0, 0), (40, 0)))], network="testworld")

    assert (report.removed, report.changed, report.added) == (0, 0, 1)
    assert segment(conn, 1) is not None
    assert segment(conn, 101) is not None
    assert conn.execute("SELECT network_id FROM segment WHERE source_oid = 101").fetchone()[0] \
        == network_id(conn, "testworld")


def test_import_assigns_node_radii_for_its_own_network_without_a_recompute(conn):
    """A real bug: regenerate_nodes() (called by import_layer after every
    import) called assign_radii()/match() without threading network
    through, so they silently defaulted to "ptc". For any other network,
    assign_radii's WHERE (network_id = ptc's id AND id IN these segments)
    matched nothing -- radius_m stayed NULL, and a NULL radius makes
    ST_DWithin's match condition NULL (not true), so those nodes could
    never be hit until a full recompute happened to fix it up."""
    add_network(conn, "testworld")
    run_import(conn, "cartpath", [cartpath(101, line((0, 0), (100, 0)))], network="testworld")
    radii = [r for (r,) in conn.execute(
        "SELECT n.radius_m FROM node n JOIN segment s ON s.id = n.segment_id WHERE s.source_oid = 101")]
    assert radii and all(r is not None for r in radii)


def test_empty_response_refuses_to_import(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (40, 0)))])
    with pytest.raises(RuntimeError, match="no features"):
        run_import(conn, "cartpath", [])
    assert segment(conn, 1) is not None


def test_repeated_source_keys_abort_the_import(conn):
    a = cartpath(1, line((0, 0), (40, 0)))
    b = cartpath(2, line((0, 9), (40, 9)), GlobalID=global_id(1))
    with pytest.raises(RuntimeError, match="repeated source keys"):
        run_import(conn, "cartpath", [a, b])


def test_feature_without_geometry_is_skipped(conn):
    missing = cartpath(2, line((0, 0), (40, 0)))
    missing["geometry"] = None
    report = run_import(conn, "cartpath", [cartpath(1, line((0, 9), (40, 9))), missing])
    assert report.no_geometry == [2]
    assert report.stored == 1
