import math

from tests.helpers import cartpath, line, nodes_of, run_import, segment


def xs(nodes):
    return [x for _, _, x, _ in nodes]


def test_straight_line_gets_nodes_every_20m_including_endpoints(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (100, 0)))])
    assert nodes_of(conn, 1) == [(0, i, 20.0 * i, 0.0) for i in range(6)]


def test_spacing_shrinks_so_the_far_endpoint_is_a_node(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (45, 0)))])
    # ceil(45 / 20) = 3 intervals of 15 m.
    assert xs(nodes_of(conn, 1)) == [0.0, 15.0, 30.0, 45.0]


def test_segment_shorter_than_spacing_gets_both_endpoints(conn):
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (4.7, 0)))])
    assert xs(nodes_of(conn, 1)) == [0.0, 4.7]


def test_nodes_follow_curves_not_chords(conn):
    # An L shape: 30 m east then 30 m north. Nodes are measured along the line.
    run_import(conn, "cartpath", [cartpath(1, line((0, 0), (30, 0), (30, 30)))])
    points = [(x, y) for _, _, x, y in nodes_of(conn, 1)]
    assert points == [(0.0, 0.0), (20.0, 0.0), (30.0, 10.0), (30.0, 30.0)]


def test_multipart_nodes_never_span_the_gap(conn):
    run_import(conn, "cartpath", [cartpath(
        1,
        line((0, 0), (60, 0)),
        line((460, 0), (500, 0)),   # 400 m gap
    )])
    nodes = nodes_of(conn, 1)
    assert nodes == [
        (0, 0, 0.0, 0.0), (0, 1, 20.0, 0.0), (0, 2, 40.0, 0.0), (0, 3, 60.0, 0.0),
        (1, 0, 460.0, 0.0), (1, 1, 480.0, 0.0), (1, 2, 500.0, 0.0),
    ]
    assert not [x for x in xs(nodes) if 60 < x < 460]


def test_tiny_part_keeps_its_geometry_but_gets_no_nodes(conn):
    report = run_import(conn, "cartpath", [cartpath(
        1,
        line((0, 0), (40, 0)),
        line((40.5, 0), (40.6, 0)),   # 0.1 m part
        line((50, 0), (70, 0)),
    )])
    assert {part for part, *_ in nodes_of(conn, 1)} == {0, 2}
    assert segment(conn, 1)["parts"] == 3
    [(oid, part, length)] = report.tiny_parts
    assert (oid, part) == (1, 1) and math.isclose(length, 0.1, abs_tol=1e-6)


def test_uncounted_types_are_stored_without_nodes(conn):
    run_import(conn, "cartpath", [
        cartpath(1, line((0, 0), (50, 0)), type_="Road Crossing"),
        cartpath(2, line((0, 10), (50, 10)), type_="Tunnel"),
    ])
    assert segment(conn, 1)["counted"] is False
    assert nodes_of(conn, 1) == []
    assert segment(conn, 2)["counted"] is True
    assert len(nodes_of(conn, 2)) == 4


def test_counted_segment_made_only_of_tiny_parts_is_reported(conn):
    report = run_import(conn, "cartpath", [cartpath(
        1,
        *[line((i * 10, 0), (i * 10 + 0.5, 0)) for i in range(4)],   # 4 x 0.5 m = 2 m total
    )])
    assert report.nodeless == [1]
