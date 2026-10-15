"""Rule-based problem fixes (twin.scene.autofix): each rule on the sample apartment with one injected problem."""

import pytest

from tests.conftest import mutate
from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.autofix import RULES, add_missing_walls, autofix
from twin.scene.validate import validate_scene


def codes(report, severities=("error", "warning")):
    return {i.code for i in report.issues if i.severity in severities}


def item(scene, fid):
    return next(f for f in scene.furniture if f.id == fid)


def furn(d, fid):
    return next(f for f in d["furniture"] if f["id"] == fid)


def opening(d, oid):
    return next(o for o in d["openings"] if o["id"] == oid)


def test_clean_scene_is_left_alone(scene, brief):
    fixed, actions, report = autofix(scene, brief)
    assert actions == [] and fixed == scene and report.can_generate


def test_stretched_shelf_becomes_a_row(scene_dict):
    shelf = load_catalog().get("bookshelf")
    w0, d0, h0 = shelf.size
    s = mutate(scene_dict, lambda d: furn(d, "F27").update(size={"width": 3 * w0, "depth": d0, "height": h0}))
    assert "W_FURNITURE_SCALE" in codes(validate_scene(s))
    fixed, actions, report = autofix(s)
    assert "W_FURNITURE_SCALE" not in codes(report)
    row = [f for f in fixed.furniture if f.asset_id == "bookshelf"]
    assert len(row) == 3 and "F27" in {f.id for f in row}
    assert all(f.size.width == pytest.approx(w0) and f.rotation_deg == 270 for f in row)
    # laid end to end along the shelf's own axis (rotated 270 deg: along y), centred where the long one was
    ys = sorted(f.position[1] for f in row)
    assert ys[1] - ys[0] == pytest.approx(w0, abs=1e-3) and sum(ys) / 3 == pytest.approx(2.0, abs=1e-3)
    assert {f.position[0] for f in row} == {9.715}
    assert "row of 3" in actions[0].text


def test_squashed_table_is_resized_into_range(scene_dict):
    table = load_catalog().get("coffee_table")
    w0, d0, h0 = table.size
    s = mutate(scene_dict, lambda d: furn(d, "F2").update(size={"width": 0.4 * w0, "depth": d0, "height": h0}))
    fixed, actions, report = autofix(s, codes={"W_FURNITURE_SCALE"})
    lo, _ = table.scale_range()
    assert item(fixed, "F2").size.width == pytest.approx(lo * w0, abs=1e-3)
    assert "squashed" in actions[0].text and "W_FURNITURE_SCALE" not in codes(report)


def test_door_at_a_corner_slides_clear(scene_dict):
    s = mutate(scene_dict, lambda d: opening(d, "O9").update(offset=3.6))   # 3.2-4.0 m: into wall W6 at x=4.0
    assert "E_OPENING_AT_JUNCTION" in codes(validate_scene(s))
    fixed, actions, report = autofix(s)
    o9 = next(o for o in fixed.openings if o.id == "O9")
    assert o9.offset == pytest.approx(3.5) and o9.width == pytest.approx(0.8)   # 10 cm back, same width
    assert report.ok and "moved it 10 cm" in actions[0].text


def test_door_between_close_walls_is_narrowed(scene_dict):
    # a 1.0 m door between two joining walls 1 m apart (0.8 m clear) must get narrower: it cannot move anywhere
    def inject(d):
        d["walls"].append({"id": "W8", "level_id": "L0", "start": [3.0, 3.6], "end": [3.0, 6.0], "thickness": 0.1})
        opening(d, "O9").update(offset=3.5, width=1.0)
    s = mutate(scene_dict, inject)
    fixed, actions, report = autofix(s, codes={"E_OPENING_AT_JUNCTION"})
    o9 = next(o for o in fixed.openings if o.id == "O9")
    assert o9.width == pytest.approx(0.8) and o9.offset == pytest.approx(3.5)
    assert [a.text for a in actions] == ["Door O9 cut into the corner with a joining wall: narrowed it 1.00 → 0.80 m."]
    assert "E_OPENING_AT_JUNCTION" not in codes(report)


def chamfered_room(cut: float, kind: str = "window", width: float = 0.45):
    """A 4 x 3 m room whose south-east corner is cut at 45 degrees by a wall `cut` m on each side; one opening on it."""
    from twin.scene.model import Scene
    pts = [(0, 0), (4 - cut, 0), (4, cut), (4, 3), (0, 3)]
    walls = [{"id": f"W{i + 1}", "level_id": "L0", "start": pts[i], "end": pts[(i + 1) % 5], "thickness": 0.25, "kind": "exterior"}
             for i in range(5)]
    length = 2 ** 0.5 * cut
    op = {"id": "O1", "kind": kind, "wall_id": "W2", "offset": length / 2, "width": width, "height": 2.1 if kind == "door" else 1.2,
          "sill_height": 0.0 if kind == "door" else 0.9}
    return Scene.model_validate({"project": {"name": "chamfer"}, "levels": [{"id": "L0", "name": "Ground"}],
                                 "rooms": [{"id": "R1", "name": "Room", "type": "living", "level_id": "L0", "polygon": pts}],
                                 "walls": walls, "openings": [op]})


def test_window_on_a_short_wall_is_shrunk_to_its_free_stretch():
    s = chamfered_room(0.48)                    # 0.68 m wall; the joints leave 0.33 m clear, under the 0.40 m minimum
    assert "E_OPENING_AT_JUNCTION" in codes(validate_scene(s))
    fixed, actions, report = autofix(s, codes={"E_OPENING_AT_JUNCTION"})
    o = fixed.openings[0]
    assert 0.25 <= o.width < 0.4 and "narrowed it 0.45" in actions[0].text and "clear of its corners" in actions[0].text
    assert "E_OPENING_AT_JUNCTION" not in codes(report) and report.can_generate


def test_window_with_no_room_is_removed_but_a_door_never_is():
    s = chamfered_room(0.38)                    # 0.54 m wall, 0.19 m clear: too little even for a narrow window
    assert "E_OPENING_AT_JUNCTION" in codes(validate_scene(s))
    fixed, actions, report = autofix(s, codes={"E_OPENING_AT_JUNCTION"})
    assert fixed.openings == [] and "removed it" in actions[0].text and report.can_generate
    fixed, actions, report = autofix(chamfered_room(0.38, "door", 0.5), codes={"E_OPENING_AT_JUNCTION"})
    assert [o.id for o in fixed.openings] == ["O1"] and not actions
    assert "E_OPENING_AT_JUNCTION" in codes(report)


def test_furniture_into_wall_is_nudged_back(scene_dict):
    s = mutate(scene_dict, lambda d: furn(d, "F25").update(position=[8.2, 0.3]))   # desk into the south wall
    assert "W_FURNITURE_CROSSES_BOUNDARY" in codes(validate_scene(s))
    fixed, actions, report = autofix(s)
    assert "W_FURNITURE_CROSSES_BOUNDARY" not in codes(report)
    assert item(fixed, "F25").position[1] > 0.3 and "moved" in actions[0].text


def test_item_standing_in_another_room_changes_room_not_place(scene_dict):
    s = mutate(scene_dict, lambda d: furn(d, "F28").update(position=[5.6, 2.4]))   # study armchair in the bathroom
    assert "E_FURNITURE_OUTSIDE_ROOM" in codes(validate_scene(s))
    fixed, actions, _ = autofix(s, codes={"E_FURNITURE_OUTSIDE_ROOM"})
    f = item(fixed, "F28")
    assert f.room_id == "R4" and f.position == (5.6, 2.4) and "now belongs" in actions[0].text


def test_item_in_a_door_swing_moves_out(scene_dict):
    s = mutate(scene_dict, lambda d: furn(d, "F29").update(position=[7.25, 3.15]))  # plant behind study door O11
    assert "W_DOOR_SWING_BLOCKED" in codes(validate_scene(s))
    fixed, actions, report = autofix(s)
    assert "W_DOOR_SWING_BLOCKED" not in codes(report) and "door O11" in actions[0].text


def test_overlapping_items_are_pulled_apart(scene_dict):
    s = mutate(scene_dict, lambda d: furn(d, "F29").update(position=[8.2, 0.75]))   # plant on the desk chair
    assert "W_FURNITURE_OVERLAP" in codes(validate_scene(s))
    _, actions, report = autofix(s, codes={"W_FURNITURE_OVERLAP"})
    assert "W_FURNITURE_OVERLAP" not in codes(report) and "overlapped" in actions[0].text


def test_wall_overshoot_is_trimmed(scene_dict):
    s = mutate(scene_dict, lambda d: next(w for w in d["walls"] if w["id"] == "W6").update(end=[4.0, 3.75]))
    assert "W_WALL_OVERSHOOT" in codes(validate_scene(s))
    fixed, actions, report = autofix(s)
    assert fixed.wall("W6").end == pytest.approx((4.0, 3.6)) and "W_WALL_OVERSHOOT" not in codes(report)


def test_too_tall_opening_is_lowered(scene_dict):
    s = mutate(scene_dict, lambda d: opening(d, "O9").update(height=2.7))
    fixed, actions, report = autofix(s)
    assert next(o for o in fixed.openings if o.id == "O9").height < 2.65 and report.ok


def test_only_requested_codes_or_targets_are_fixed(scene_dict):
    def inject(d):
        furn(d, "F25").update(position=[8.2, 0.3])
        opening(d, "O9").update(offset=3.6)
    s = mutate(scene_dict, inject)
    _, actions, report = autofix(s, codes={"E_OPENING_AT_JUNCTION"})
    assert [a.code for a in actions] == ["E_OPENING_AT_JUNCTION"]
    assert "W_FURNITURE_CROSSES_BOUNDARY" in codes(report)
    _, actions, _ = autofix(s, targets={"furniture:F25"})
    assert [a.target for a in actions] == ["furniture:F25"]


def test_open_edges_open_plan_flag_or_new_walls(scene_dict):
    def no_wall(d):
        d["walls"] = [w for w in d["walls"] if w["id"] != "W7"]
        d["openings"] = [o for o in d["openings"] if o["wall_id"] != "W7"]
    s = mutate(scene_dict, no_wall)
    edge = {i.targets[0] for i in validate_scene(s).issues if i.code == "W_ROOM_EDGE_NO_WALL"}
    assert {"room:R1", "room:R2"} <= edge
    # the user says it is open plan: no warning for those rooms
    flagged = s.model_copy(update={"rooms": [r.model_copy(update={"open_plan": r.id in ("R1", "R2")}) for r in s.rooms]})
    edge2 = {i.targets[0] for i in validate_scene(flagged).issues if i.code == "W_ROOM_EDGE_NO_WALL"}
    assert not edge2 & {"room:R1", "room:R2"}
    # the user says the wall is missing: it comes back as an interior wall along x = 6.4
    walled, notes = add_missing_walls(s, "R2")
    new = [w for w in walled.walls if w.id not in {x.id for x in s.walls}]
    assert new and all(w.kind == "interior" and w.start[0] == w.end[0] == pytest.approx(6.4) for w in new)
    assert "room:R2" not in {i.targets[0] for i in validate_scene(walled).issues if i.code == "W_ROOM_EDGE_NO_WALL"}
    assert notes and "added" in notes[0]


def test_every_rule_code_is_a_real_validation_code():
    import inspect
    import twin.scene.validate as v
    src = inspect.getsource(v)
    assert all(f'"{code}"' in src for code in RULES)
    assert g.JOIN_TOL > 0   # geometry tolerances the rules rely on
