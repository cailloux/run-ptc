import dataclasses
import math

import pytest

from app.fit import (
    CoursePoint,
    _refine,
    classify_turn,
    encode_course,
    find_course_points,
    turn_angle,
)
from app.graph import build_graph
from tests.helpers import SETTINGS, add_network, cartpath, line, road, run_import
from tests.test_routing import latlon

THRESHOLDS = SETTINGS.fit_turn_thresholds_deg   # {straight: 20, slight: 45, turn: 135, sharp: 170}


def trace(conn, *waypoints, step=2.0):
    """Dense (lat, lon) points along straight UTM segments, like a real GPS track."""
    pts = []
    for (x0, y0), (x1, y1) in zip(waypoints, waypoints[1:]):
        dist = math.hypot(x1 - x0, y1 - y0)
        n = max(1, round(dist / step))
        for i in range(n):
            t = i / n
            pts.append(latlon(conn, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    pts.append(latlon(conn, *waypoints[-1]))
    return pts


def build(conn, cartpaths=(), roads=()):
    if cartpaths:
        run_import(conn, "cartpath", list(cartpaths))
    if roads:
        run_import(conn, "road", list(roads))
    build_graph(conn, SETTINGS)


# ---- pure geometry: classify_turn, turn_angle -------------------------------------------


@pytest.mark.parametrize("angle, expected", [
    (0, "straight"), (15, "straight"), (-15, "straight"),
    (30, "slight_right"), (-30, "slight_left"),
    (90, "right"), (-90, "left"),
    (150, "sharp_right"), (-150, "sharp_left"),
    (175, "u_turn"), (-175, "u_turn"),
])
def test_classify_turn_thresholds(angle, expected):
    assert classify_turn(angle, THRESHOLDS) == expected


def test_turn_angle_is_zero_on_a_straight_line(conn):
    pts = trace(conn, (0, 0), (200, 0))
    assert turn_angle(pts, len(pts) // 2, window_m=15) == pytest.approx(0, abs=1)


def test_turn_angle_is_negative_ninety_turning_left(conn):
    # East then north: a left turn.
    pts = trace(conn, (0, 0), (100, 0), (100, 100))
    corner = next(i for i, p in enumerate(pts) if p == latlon(conn, 100, 0))
    assert turn_angle(pts, corner, window_m=15) == pytest.approx(-90, abs=2)


# ---- pure refinement: _refine -----------------------------------------------------------


def test_straight_all_mode_always_keeps_it():
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="all")
    assert _refine("straight", 90, [], settings) == "straight"


def test_straight_none_mode_always_drops_it():
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="none")
    assert _refine("straight", 90, [45], settings) is None


def test_straight_ambiguous_mode_keeps_it_with_a_nearby_branch():
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="ambiguous")
    assert _refine("straight", 90, [120], settings) == "straight"   # 30 deg away


def test_straight_ambiguous_mode_drops_it_with_no_nearby_branch():
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="ambiguous")
    assert _refine("straight", 90, [270], settings) is None   # 180 deg away


def test_slight_turn_with_a_near_parallel_branch_becomes_a_fork():
    # Taken path heads 60 deg; an unused branch at 75 deg is a 15 deg fork.
    assert _refine("slight_left", 60, [75], SETTINGS) == "right_fork"
    assert _refine("slight_right", 60, [45], SETTINGS) == "left_fork"


def test_slight_turn_with_no_near_branch_stays_a_slight_turn():
    assert _refine("slight_left", 60, [200], SETTINGS) == "slight_left"


def test_real_turns_pass_through_unchanged():
    for cue in ("left", "right", "sharp_left", "sharp_right", "u_turn"):
        assert _refine(cue, 90, [91], SETTINGS) == cue


# ---- find_course_points: real decision points --------------------------------------------


def test_a_real_turn_at_a_junction_gets_a_turn_cue(conn):
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (100, 60))),      # taken: a left turn
        cartpath(3, line((100, 0), (100, -60))),     # unused branch
    ])
    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (100, 60)), SETTINGS)
    assert [cp.type for cp in cps] == ["left"]


def test_two_networks_course_points_without_cross_contamination(conn):
    """find_course_points joins route_edge/route_vertex directly; identical
    geometry between two networks (step 7's composite PK means edge/vertex
    ids repeat across networks) is the real stress case."""
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (100, 60))),
        cartpath(3, line((100, 0), (100, -60))),
    ])
    add_network(conn, "testworld")
    run_import(conn, "cartpath", [
        cartpath(101, line((0, 0), (100, 0))),
        cartpath(102, line((100, 0), (100, 60))),
        cartpath(103, line((100, 0), (100, -60))),
    ], network="testworld")
    build_graph(conn, SETTINGS, network="testworld")

    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (100, 60)), SETTINGS, network="testworld")
    assert [cp.type for cp in cps] == ["left"]


def test_a_plain_bend_with_no_branch_gets_no_cue(conn):
    # Degree 2 at the bend: nothing to decide, so nothing to say.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (100, 60))),
    ])
    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (100, 60)), SETTINGS)
    assert cps == []


def test_straight_through_stays_straight_with_a_close_branch(conn):
    # cartpath2 bends well past the bearing window, so its own far end
    # doesn't read as a branch at all; only cartpath3's ~34 deg branch can
    # make this ambiguous. Its endpoint (130, 20) is kept off both of
    # cartpath2's lines so it doesn't accidentally split one into a T.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (150, 0), (150, 150))),
        cartpath(3, line((100, 0), (130, 20))),
    ])
    cps = find_course_points(
        conn, trace(conn, (0, 0), (100, 0), (150, 0), (150, 150)), SETTINGS)
    assert [cp.type for cp in cps] == ["straight"]


def test_straight_through_drops_with_no_close_branch(conn):
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (150, 0), (150, 150))),
        cartpath(3, line((100, 0), (100, -60))),   # perpendicular, not ambiguous
    ])
    cps = find_course_points(
        conn, trace(conn, (0, 0), (100, 0), (150, 0), (150, 150)), SETTINGS)
    assert cps == []


def test_a_dead_straight_continuation_is_not_mistaken_for_its_own_branch(conn):
    # cartpath2 continues dead straight, so its own far end has exactly the
    # route's own out-bearing. Before excluding the route's own entry/exit
    # neighbours from "other branches", this self-matched and the straight
    # cue never dropped even though cartpath3 is nowhere near ambiguous.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (200, 0))),
        cartpath(3, line((100, 0), (100, -60))),
    ])
    cps = find_course_points(conn, trace(conn, (0, 0), (200, 0)), SETTINGS)
    assert cps == []


def test_a_slight_turn_with_a_near_parallel_branch_is_a_fork(conn):
    # cartpath2 (taken) bends sharply after its first leg, so its own far
    # end reads nothing like the first leg's bearing; only cartpath3's
    # near-parallel branch can trigger the fork relabeling.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (150, 30), (150, 200))),   # taken: a slight left, then a hard bend
        cartpath(3, line((100, 0), (200, 20))),                # unused, close to the first leg's bearing
    ])
    cps = find_course_points(
        conn, trace(conn, (0, 0), (100, 0), (150, 30), (150, 200)), SETTINGS)
    assert [cp.type for cp in cps] == ["right_fork"]


# ---- find_course_points: clustering -------------------------------------------------------


def test_two_decision_vertices_joined_by_a_short_edge_merge(conn):
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="all")
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (115, 0))),    # 15 m connector, within fit_cluster_m
        cartpath(3, line((115, 0), (215, 0))),
        cartpath(4, line((100, 0), (100, 60))),   # branch at the first vertex
        cartpath(5, line((115, 0), (115, -60))),  # branch at the second vertex
    ])
    cps = find_course_points(conn, trace(conn, (0, 0), (215, 0)), settings)
    assert [cp.type for cp in cps] == ["straight"]


def test_two_decision_vertices_joined_by_a_long_edge_stay_separate(conn):
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="all")
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (140, 0))),    # 40 m connector, past fit_cluster_m
        cartpath(3, line((140, 0), (240, 0))),
        cartpath(4, line((100, 0), (100, 60))),
        cartpath(5, line((140, 0), (140, -60))),
    ])
    cps = find_course_points(conn, trace(conn, (0, 0), (240, 0)), settings)
    assert [cp.type for cp in cps] == ["straight", "straight"]


def test_the_same_vertex_visited_close_together_merges(conn):
    # A short out-and-back spur: the junction is visited twice, 10 m apart
    # along the route, well within fit_cluster_m. fit_straight_cues="all"
    # makes the merge itself the thing under test, not the separate
    # question of whether a "straight" cue is ambiguous enough to keep.
    settings = dataclasses.replace(SETTINGS, fit_straight_cues="all")
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (200, 0))),
        cartpath(3, line((100, 0), (100, 5))),   # a 5 m dead-end spur
    ])
    cps = find_course_points(
        conn, trace(conn, (0, 0), (100, 0), (100, 5), (100, 0), (200, 0)), settings)
    assert len(cps) == 1


def test_the_same_vertex_visited_far_apart_stays_separate(conn):
    # A long out-and-back spur: the junction is visited twice, 120 m apart
    # along the route (there and back), well past fit_cluster_m.
    build(conn, [
        cartpath(1, line((0, 0), (100, 0))),
        cartpath(2, line((100, 0), (200, 0))),
        cartpath(3, line((100, 0), (100, 60))),   # a 60 m dead-end spur
    ])
    cps = find_course_points(
        conn, trace(conn, (0, 0), (100, 0), (100, 60), (100, 0), (200, 0)), SETTINGS)
    assert len(cps) == 2


# ---- find_course_points: roads crossed at grade --------------------------------------------


def test_a_road_crossed_straight_through_gets_no_cue(conn):
    build(conn,
          cartpaths=[cartpath(1, line((0, 0), (100, 0))), cartpath(2, line((100, 0), (200, 0)))],
          roads=[road(1, line((100, 0), (100, 60))), road(2, line((100, 0), (100, -60)))])
    cps = find_course_points(conn, trace(conn, (0, 0), (200, 0)), SETTINGS)
    assert cps == []


def test_a_road_crossed_on_a_slight_bend_gets_no_cue(conn):
    build(conn,
          cartpaths=[cartpath(1, line((0, 0), (100, 0))),
                     cartpath(2, line((100, 0), (200, 50)))],   # ~27 deg bend, still "slight"
          roads=[road(1, line((100, 0), (100, 60))), road(2, line((100, 0), (100, -60)))])
    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (200, 50)), SETTINGS)
    assert cps == []


def test_a_real_turn_at_a_road_crossing_still_gets_a_cue(conn):
    # The path itself makes a real turn right at the crossing -- unrelated
    # to the road -- so it must not be muted.
    build(conn,
          cartpaths=[cartpath(1, line((0, 0), (100, 0))),
                     cartpath(2, line((100, 0), (100, 60))),
                     cartpath(3, line((100, 0), (100, -60)))],
          roads=[road(1, line((100, 0), (160, 0)))])
    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (100, 60)), SETTINGS)
    assert [cp.type for cp in cps] == ["left"]


def test_turning_onto_the_road_at_a_crossing_still_gets_a_cue(conn):
    # No cart path continues on the far side -- the route genuinely
    # transitions onto the road, so that's real guidance, not a crossing.
    build(conn,
          cartpaths=[cartpath(1, line((0, 0), (100, 0)))],
          roads=[road(1, line((100, 0), (100, 60))), road(2, line((100, 0), (100, -60)))])
    cps = find_course_points(conn, trace(conn, (0, 0), (100, 0), (100, 60)), SETTINGS)
    assert [cp.type for cp in cps] == ["left"]


# ---- FIT binary encoding: round-trip through the official SDK -----------------------------


def _decode(tmp_path, data):
    from garmin_fit_sdk import Decoder, Stream

    path = tmp_path / "test.fit"
    path.write_bytes(data)
    messages, errors = Decoder(Stream.from_file(str(path))).read()
    assert errors == []
    return messages


def test_encode_course_round_trips_through_the_garmin_sdk(conn, tmp_path):
    latlngs = trace(conn, (0, 0), (100, 0), (100, 60))
    course_points = [CoursePoint(latlngs[5][0], latlngs[5][1], 10.0, "left", "L path")]
    data = encode_course("Test course", latlngs, course_points, pace_min_per_mi=9.0,
                         created_at=1_700_000_000, flavor="garmin")
    messages = _decode(tmp_path, data)

    assert len(messages["record_mesgs"]) == len(latlngs)
    assert len(messages["course_point_mesgs"]) == 1
    cp = messages["course_point_mesgs"][0]
    assert cp["type"] == "left"   # the SDK decodes the enum to its name
    assert cp["name"] == "L path"


def test_encode_course_defaults_to_generic_course_points(conn, tmp_path):
    # Garmin Connect Web drops or reprocesses the real turn types on import,
    # so the default flavor tags every course point generic; the real
    # direction still shows up in the name.
    latlngs = trace(conn, (0, 0), (100, 0), (100, 60))
    course_points = [CoursePoint(latlngs[5][0], latlngs[5][1], 10.0, "left", "L path")]
    data = encode_course("Test course", latlngs, course_points, pace_min_per_mi=9.0,
                         created_at=1_700_000_000)
    messages = _decode(tmp_path, data)

    cp = messages["course_point_mesgs"][0]
    assert cp["type"] == "generic"
    assert cp["name"] == "L path"
