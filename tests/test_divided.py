import pytest

from app.graph import build_graph
from tests.helpers import SETTINGS, line, road, run_import
from tests.test_coverage import pieces
from tests.test_matching import set_hits


def seg(conn, oid):
    return conn.execute("""
        SELECT s.counted, s.uncounted_reason, count(n.id), min(n.radius_m)
        FROM segment s LEFT JOIN node n ON n.segment_id = s.id
        WHERE s.layer = 'road' AND s.source_oid = %s GROUP BY s.id
    """, (oid,)).fetchone()


def carriageways(conn):
    """Two carriageways of a divided road, 15 m apart, drawn in opposite directions."""
    return run_import(conn, "road", [
        road(10, line((0, 0), (200, 0)), name="DIVIDED RD"),
        road(11, line((200, 15), (0, 15)), name="DIVIDED RD"),
    ])


def test_one_carriageway_counts_with_the_wide_radius(conn):
    report = carriageways(conn)
    assert seg(conn, 10) == (True, None, 11, 30)          # kept: nodes, wide radius
    assert seg(conn, 11) == (False, "second carriageway", 0, None)
    assert report.second_carriageways == [11]
    assert report.counted_m == pytest.approx(200)


def test_the_second_carriageway_is_still_routable(conn):
    carriageways(conn)
    build_graph(conn, SETTINGS)
    oids = {r[0] for r in conn.execute(
        "SELECT s.source_oid FROM route_edge e JOIN segment s ON s.id = e.segment_id")}
    assert oids == {10, 11}


def test_roads_not_on_the_list_keep_both_sides(conn):
    run_import(conn, "road", [
        road(10, line((0, 0), (200, 0)), name="OTHER RD"),
        road(11, line((200, 15), (0, 15)), name="OTHER RD"),
    ])
    assert seg(conn, 10)[0] is True and seg(conn, 11)[0] is True
    assert seg(conn, 10)[3] == 20


def test_connected_carriageways_pair_segment_by_segment(conn):
    # MacDuff-style: both carriageways and the connectors at each end form one chain.
    report = run_import(conn, "road", [
        road(20, line((0, 0), (100, 0)), name="DIVIDED RD"),
        road(21, line((100, 0), (200, 0)), name="DIVIDED RD"),
        road(22, line((200, 15), (100, 15)), name="DIVIDED RD"),
        road(23, line((100, 15), (0, 15)), name="DIVIDED RD"),
        road(24, line((200, 0), (200, 15)), name="DIVIDED RD"),
        road(25, line((0, 15), (0, 0)), name="DIVIDED RD"),
    ])
    assert report.second_carriageways == [22, 23]


def test_reimport_is_idempotent(conn):
    carriageways(conn)
    report = carriageways(conn)
    assert (report.added, report.changed, report.removed) == (0, 0, 0)
    assert report.second_carriageways == [11]


def test_the_second_carriageway_has_no_coverage_feature(conn):
    carriageways(conn)
    set_hits(conn, "road", 10, [(0, s) for s in range(6)])   # x = 0 ... 100 run
    assert pieces(conn, "road", 11) == []
