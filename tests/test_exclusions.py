import pytest

from app import exclusions as excl
from app.config import CONFIG_DIR
from tests.helpers import add_network, cartpath, global_id, line, road, run_import, segment

# ---- loading (no database) -------------------------------------------------


def test_repo_exclusions_file_loads():
    assert excl.load(CONFIG_DIR / "exclusions.yaml") == excl.Exclusions()


def test_parse_spec_example():
    parsed = excl.parse({
        "cartpaths": [{"global_id": "{17F7645E-B49A-46BA-9BD6-4BFD4CFB5395}", "reason": "Gated off"}],
        "roads": [
            {"name": "MEADE FIELD COMPLEX", "reason": "Parking loop"},
            {"object_id": 1234, "name": "TWIGGS COR", "reason": "Gated"},
        ],
    })
    assert parsed.cartpaths == (
        excl.CartpathExclusion("{17F7645E-B49A-46BA-9BD6-4BFD4CFB5395}", "Gated off"),
    )
    assert parsed.roads == (
        excl.RoadExclusion("Parking loop", None, "MEADE FIELD COMPLEX"),
        excl.RoadExclusion("Gated", 1234, "TWIGGS COR"),
    )


def test_empty_file_and_empty_lists():
    assert excl.parse(None) == excl.Exclusions()
    assert excl.parse({"cartpaths": None, "roads": []}) == excl.Exclusions()


def test_global_id_is_normalized():
    assert excl.normalize_global_id(" 17f7645e-b49a-46ba-9bd6-4bfd4cfb5395 ") == \
        "{17F7645E-B49A-46BA-9BD6-4BFD4CFB5395}"


@pytest.mark.parametrize("data, message", [
    ({"cartpath": []}, "unknown top-level"),
    ({"roads": {"name": "X"}}, "list of entries"),
    ({"roads": [{"name": "X"}]}, "reason is required"),
    ({"roads": [{"reason": "r"}]}, "needs object_id or name"),
    ({"roads": [{"object_id": "12", "reason": "r"}]}, "object_id must be an integer"),
    ({"roads": [{"objectid": 12, "reason": "r"}]}, "unknown keys"),
    ({"cartpaths": [{"reason": "r"}]}, "global_id is required"),
])
def test_invalid_entries_fail_loudly(data, message):
    with pytest.raises(excl.ExclusionError, match=message):
        excl.parse(data)


# ---- applying ----------------------------------------------------------------


def roads_fixture(conn):
    run_import(conn, "road", [
        road(1, line((0, 0), (30, 0)), name="TWIGGS COR"),
        road(2, line((0, 10), (30, 10)), name="MEADE FIELD COMPLEX"),
        road(3, line((0, 20), (30, 20)), name="Meade Field Complex "),
        road(4, line((0, 30), (30, 30)), name="OTHER RD"),
    ])


def test_cartpath_exclusion_by_global_id(conn):
    run_import(conn, "cartpath", [cartpath(5, line((0, 0), (30, 0)))])
    gid = global_id(5).strip("{}").lower()   # pasted without braces, lowercase
    warnings = excl.apply(conn, excl.parse({"cartpaths": [{"global_id": gid, "reason": "Gated off"}]}))
    assert warnings == []
    s = segment(conn, 5)
    assert (s["excluded"], s["reason"]) == (True, "Gated off")


def test_road_name_excludes_every_segment_of_that_road(conn):
    roads_fixture(conn)
    excl.apply(conn, excl.parse({"roads": [{"name": "meade field complex", "reason": "Parking loop"}]}))
    assert [segment(conn, i, "road")["excluded"] for i in (1, 2, 3, 4)] == [False, True, True, False]


def test_object_id_with_changed_name_warns_but_still_excludes(conn):
    roads_fixture(conn)
    warnings = excl.apply(conn, excl.parse(
        {"roads": [{"object_id": 1, "name": "TWIGGS CT", "reason": "Gated"}]}))
    assert len(warnings) == 1 and "'TWIGGS COR'" in warnings[0]
    assert segment(conn, 1, "road")["excluded"] is True


def test_object_id_with_matching_name_is_quiet(conn):
    roads_fixture(conn)
    assert excl.apply(conn, excl.parse(
        {"roads": [{"object_id": 1, "name": "twiggs cor", "reason": "Gated"}]})) == []


def test_exclusions_apply_only_to_one_network(conn):
    """A real bug caught while wiring --network through: apply()'s reset step
    and _exclude()'s WHERE both had no network filter -- reapplying ptc's
    exclusions would reset or exclude another network's segments."""
    roads_fixture(conn)
    add_network(conn, "testworld")
    run_import(conn, "road", [road(101, line((0, 40), (30, 40)), name="MEADE FIELD COMPLEX")],
              network="testworld")
    excl.apply(conn, excl.parse({"roads": [{"name": "meade field complex", "reason": "Parking loop"}]}),
              network="ptc")
    assert [segment(conn, i, "road")["excluded"] for i in (1, 2, 3, 4)] == [False, True, True, False]
    assert segment(conn, 101, "road")["excluded"] is False

    # testworld's own exclusion must also survive a later reapply for ptc.
    excl.apply(conn, excl.parse({"roads": [{"object_id": 101, "reason": "closed"}]}), network="testworld")
    assert segment(conn, 101, "road")["excluded"] is True
    excl.apply(conn, excl.parse({}), network="ptc")
    assert segment(conn, 101, "road")["excluded"] is True


def test_orphans_warn(conn):
    roads_fixture(conn)
    warnings = excl.apply(conn, excl.parse({
        "cartpaths": [{"global_id": global_id(999), "reason": "gone"}],
        "roads": [
            {"object_id": 999, "reason": "gone"},
            {"name": "NO SUCH RD", "reason": "gone"},
        ],
    }))
    assert len(warnings) == 3
    assert all("matches no segment" in w for w in warnings)


def test_exclusion_of_dropped_duplicate_maps_to_kept_feature(conn):
    geom = line((0, 0), (30, 0))
    run_import(conn, "road", [road(3, geom), road(10900, geom)])
    warnings = excl.apply(conn, excl.parse({"roads": [{"object_id": 10900, "reason": "Gated"}]}))
    assert len(warnings) == 1 and "dropped duplicate" in warnings[0]
    assert segment(conn, 3, "road")["excluded"] is True


def test_reapply_clears_removed_entries(conn):
    roads_fixture(conn)
    excl.apply(conn, excl.parse({"roads": [{"object_id": 4, "reason": "Gated"}]}))
    excl.apply(conn, excl.Exclusions())
    assert segment(conn, 4, "road")["excluded"] is False


def test_import_applies_exclusions_and_drops_them_from_totals(conn):
    rules = excl.parse({"roads": [{"object_id": 4, "reason": "Gated"}]})
    report = run_import(conn, "road", [
        road(1, line((0, 0), (30, 0))),
        road(4, line((0, 30), (50, 30))),
    ], exclusions=rules)
    assert (report.stored, report.counted, report.excluded) == (2, 1, 1)
    assert report.counted_m == pytest.approx(30.0)


def test_import_applies_exclusions_to_its_own_network_only(conn):
    """A real bug: import_layer's own exclusion-reapply call used to ignore
    which network was importing, always defaulting to ptc -- a second
    network's import would look for its exclusion rule's object_id among
    ptc's segments, find nothing, and warn instead of excluding."""
    run_import(conn, "road", [road(1, line((0, 0), (30, 0)))])   # ptc, unrelated

    add_network(conn, "testworld")
    rules = excl.parse({"roads": [{"object_id": 101, "reason": "Gated"}]})
    report = run_import(conn, "road", [road(101, line((0, 0), (30, 0)))],
                        exclusions=rules, network="testworld")
    assert report.excluded == 1
    assert report.exclusion_warnings == []
    assert segment(conn, 101, "road")["excluded"] is True
    assert segment(conn, 1, "road")["excluded"] is False
