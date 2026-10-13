"""Wall derivation from rooms, layout fitting, and room-scoped change isolation."""

import pytest

from tests.conftest import mutate
from twin.scene.derive import derive_walls_from_rooms, fit_to_overall
from twin.scene.diff import ScopeViolation, apply_room_update, assert_only_rooms_changed, diff_scenes
from twin.scene.model import FurnitureItem, RoomFinishes
from twin.scene.validate import validate_scene


def _segments(scene):
    return {tuple(sorted((tuple(w.start), tuple(w.end)))): (w.kind, w.thickness) for w in scene.walls}


def test_derive_walls_reproduces_fixture(scene):
    derived, notes = derive_walls_from_rooms(scene)
    assert notes == []
    assert _segments(derived) == _segments(scene)
    assert {w.id for w in derived.walls} == {w.id for w in scene.walls}       # ids preserved
    assert [(o.id, o.wall_id, o.offset) for o in derived.openings] == \
           [(o.id, o.wall_id, o.offset) for o in scene.openings]


def test_derive_walls_without_any_walls(scene_dict):
    s = mutate(scene_dict, lambda d: d.update(walls=[], openings=[]))
    derived, _ = derive_walls_from_rooms(s)
    assert len(derived.walls) == 7
    kinds = sorted(w.kind for w in derived.walls)
    assert kinds.count("exterior") == 4 and kinds.count("interior") == 3
    assert validate_scene(derived).ok


def test_fit_to_overall_scales_layout(scene):
    bigger = fit_to_overall(scene, 12.2, 8.2)   # stretch x only: centreline 10 -> 12
    report = {r.name: r for r in validate_scene(bigger).rooms}
    assert report["Bedroom"].width_centreline == pytest.approx(4.8)
    assert report["Bedroom"].depth_centreline == pytest.approx(3.6)
    assert next(w for w in bigger.walls if w.id == "W5").thickness == 0.1


def test_room_update_changes_only_that_room(scene):
    new_items = [FurnitureItem(id="X1", asset_id="bed_single", room_id="R3", position=(1.0, 1.8), rotation_deg=90),
                 FurnitureItem(id="X2", asset_id="desk", room_id="R3", position=(2.8, 0.5), rotation_deg=180)]
    out = apply_room_update(scene, "R3", new_items, finishes=RoomFinishes(floor="walnut_planks"))
    d = diff_scenes(scene, out)
    assert set(d) <= {"furniture", "rooms"}
    assert list(d["rooms"]["changed"]) == ["R3"]
    unchanged = [f for f in scene.furniture if f.room_id != "R3"]
    assert [f for f in out.furniture if f.room_id != "R3"] == unchanged
    assert sorted(f.asset_id for f in out.furniture if f.room_id == "R3") == ["bed_single", "desk"]


def test_room_update_rejects_furniture_for_other_rooms(scene):
    with pytest.raises(ScopeViolation):
        apply_room_update(scene, "R3", [FurnitureItem(id="X1", asset_id="sofa_3seat", room_id="R1",
                                                      position=(2.0, 5.0))])


def test_scope_check_catches_wall_changes(scene_dict, scene):
    moved = mutate(scene_dict, lambda d: d["walls"][4].update(thickness=0.15))
    with pytest.raises(ScopeViolation, match="walls changed"):
        assert_only_rooms_changed(scene, moved, {"R3"})


def test_scope_check_catches_other_room_furniture(scene_dict, scene):
    moved = mutate(scene_dict, lambda d: d["furniture"][0].update(position=[1.6, 4.2]))   # F1 is in R1
    with pytest.raises(ScopeViolation, match="R1"):
        assert_only_rooms_changed(scene, moved, {"R3"})
