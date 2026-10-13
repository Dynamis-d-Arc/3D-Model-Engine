"""Geometric validation: known dimensions, overlaps, openings, connectivity, furniture."""

import pytest

from tests.conftest import mutate
from twin.scene.derive import fit_furniture_into_room
from twin.scene.validate import validate_scene

# the sample apartment's known measurements (metres)
EXPECTED = {
    "Living / Dining": {"centreline": (6.4, 4.4), "clear": (6.25, 4.25)},
    "Kitchen": {"centreline": (3.6, 4.4), "clear": (3.45, 4.25)},
    "Bedroom": {"centreline": (4.0, 3.6), "clear": (3.85, 3.45)},
    "Bathroom": {"centreline": (2.4, 3.6), "clear": (2.30, 3.45)},
    "Study": {"centreline": (3.6, 3.6), "clear": (3.45, 3.45)},
}
TOL = 1e-3


def codes(report):
    return {i.code for i in report.issues}


def test_fixture_is_valid_and_generatable(scene, brief):
    report = validate_scene(scene, brief)
    assert report.ok and report.can_generate, [i.message for i in report.issues]
    assert {i.severity for i in report.issues} <= {"info"}


def test_known_plan_room_dimensions(scene):
    report = validate_scene(scene)
    got = {r.name: r for r in report.rooms}
    assert set(got) == set(EXPECTED)
    for name, exp in EXPECTED.items():
        r = got[name]
        assert (r.width_centreline, r.depth_centreline) == pytest.approx(exp["centreline"], abs=TOL)
        assert (r.width_clear, r.depth_clear) == pytest.approx(exp["clear"], abs=TOL)
        assert r.area_centreline == pytest.approx(exp["centreline"][0] * exp["centreline"][1], abs=TOL)
        assert r.ceiling_height == pytest.approx(2.7)


def test_brief_mismatch_is_reported(scene, brief):
    brief.known_rooms[0].width = 7.0
    brief.overall_width = 11.0
    report = validate_scene(scene, brief)
    assert {"W_ROOM_DIM_MISMATCH", "W_OVERALL_MISMATCH"} <= codes(report)
    assert report.ok  # warnings only


def test_overlapping_walls(scene_dict):
    s = mutate(scene_dict, lambda d: d["walls"].append(
        {"id": "W8", "level_id": "L0", "start": [2.0, 0.0], "end": [5.0, 0.0], "thickness": 0.1}))
    assert "E_WALL_OVERLAP" in codes(validate_scene(s))


def test_overlapping_rooms(scene_dict):
    s = mutate(scene_dict, lambda d: d["rooms"][2].update(polygon=[[0, 0], [4, 0], [4, 4.0], [0, 4.0]]))
    assert "E_ROOM_OVERLAP" in codes(validate_scene(s))


def test_self_intersecting_room(scene_dict):
    s = mutate(scene_dict, lambda d: d["rooms"][2].update(polygon=[[0, 0], [4, 3.6], [4, 0], [0, 3.6]]))
    assert "E_ROOM_POLYGON_INVALID" in codes(validate_scene(s))


@pytest.mark.parametrize("patch, code", [
    ({"offset": 9.8, "width": 0.8}, "E_OPENING_OUTSIDE_WALL"),     # W5 is 10 m; 9.4..10.2
    ({"offset": 4.1}, "E_OPENING_AT_JUNCTION"),                    # crosses W6 at x=4
    ({"height": 2.7}, "E_OPENING_TOO_TALL"),
    ({"offset": 4.6}, "E_OPENING_OVERLAP"),                        # 4.2..5.0 overlaps O10 4.8..5.6
])
def test_misplaced_openings(scene_dict, patch, code):
    s = mutate(scene_dict, lambda d: next(o for o in d["openings"] if o["id"] == "O9").update(patch))
    assert code in codes(validate_scene(s))


def test_overlapping_openings(scene_dict):
    s = mutate(scene_dict, lambda d: next(o for o in d["openings"] if o["id"] == "O11").update(offset=5.5))
    assert "E_OPENING_OVERLAP" in codes(validate_scene(s))  # O11 5.1..5.9 vs O10 4.8..5.6


def test_disconnected_room(scene_dict):
    s = mutate(scene_dict, lambda d: d.update(openings=[o for o in d["openings"] if o["id"] != "O11"]))
    report = validate_scene(s)
    unreachable = [i for i in report.issues if i.code == "W_ROOM_UNREACHABLE"]
    assert [i.targets for i in unreachable] == [["room:R5"]]


def test_no_entrance(scene_dict):
    s = mutate(scene_dict, lambda d: d.update(openings=[o for o in d["openings"] if o["id"] != "O1"]))
    assert "W_NO_ENTRANCE" in codes(validate_scene(s))


def test_furniture_outside_room(scene_dict):
    s = mutate(scene_dict, lambda d: d["furniture"][0].update(position=[8.0, 6.0]))  # sofa moved into the kitchen
    report = validate_scene(s)
    assert any(i.code == "E_FURNITURE_OUTSIDE_ROOM" and "furniture:F1" in i.targets for i in report.issues)
    assert not report.ok


def test_furniture_into_wall_is_flagged_and_fixable(scene_dict):
    s = mutate(scene_dict, lambda d: d["furniture"][0].update(position=[1.5, 3.9]))  # sofa pushed into W5
    report = validate_scene(s)
    assert any(i.code == "W_FURNITURE_CROSSES_BOUNDARY" and "furniture:F1" in i.targets for i in report.issues)
    fixed, shift = fit_furniture_into_room(s, "F1")
    assert shift == pytest.approx(0.225, abs=0.01)  # sofa bottom edge 3.425 -> wall face 3.65
    assert not any("furniture:F1" in i.targets and i.code.startswith(("W_FURNITURE_CROSSES", "E_FURNITURE"))
                   for i in validate_scene(fixed).issues)


def test_door_swing_blocked(scene_dict):
    s = mutate(scene_dict, lambda d: d["furniture"].append(
        {"id": "F90", "asset_id": "armchair", "room_id": "R3", "position": [3.2, 3.0], "rotation_deg": 0}))
    assert "W_DOOR_SWING_BLOCKED" in codes(validate_scene(s))


def test_unknown_asset(scene_dict):
    s = mutate(scene_dict, lambda d: d["furniture"][0].update(asset_id="hot_tub"))
    assert "E_UNKNOWN_ASSET" in codes(validate_scene(s))


def test_estimates_require_review(scene_dict):
    s = mutate(scene_dict, lambda d: d["rooms"][1]["certainty"].update(status="estimated", source="ai", confidence=0.4))
    report = validate_scene(s)
    assert report.ok and not report.can_generate
    assert "R_CONFIRM_MEASUREMENT" in codes(report)
    accepted = mutate(s.model_dump(mode="json"), lambda d: d["rooms"][1]["certainty"].update(status="accepted_estimate"))
    assert validate_scene(accepted).can_generate


def test_unresolved_blocks(scene_dict):
    s = mutate(scene_dict, lambda d: d["walls"][4]["certainty"].update(status="unresolved", note="label illegible"))
    report = validate_scene(s)
    assert not report.ok and "E_UNRESOLVED_MEASUREMENT" in codes(report)


def test_blocking_question(scene_dict):
    s = mutate(scene_dict, lambda d: d["open_questions"].append(
        {"id": "Q1", "target": "room:R2", "question": "Is the kitchen 3.6 or 4.6 m wide?", "severity": "blocking"}))
    assert not validate_scene(s).ok
    resolved = mutate(s.model_dump(mode="json"), lambda d: d["open_questions"][0].update(resolved=True, answer="3.6"))
    assert validate_scene(resolved).can_generate
