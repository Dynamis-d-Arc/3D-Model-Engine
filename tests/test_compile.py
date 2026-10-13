"""Deterministic geometry compilation: walls, real openings, floors, furniture placement."""

import json

import pytest
from shapely.geometry import Polygon

from tests.conftest import mutate
from twin.scene import geometry as g
from twin.scene.compile import compile_scene, prism_volume
from twin.scene.transforms import scene_to_unreal_location, scene_to_unreal_yaw

TOL = 1e-6


@pytest.fixture
def plan(scene):
    return compile_scene(scene)


def objs(plan, kind):
    return [o for o in plan["objects"] if o["kind"] == kind]


def test_compile_is_deterministic(scene):
    a = json.dumps(compile_scene(scene), sort_keys=True)
    b = json.dumps(compile_scene(scene), sort_keys=True)
    assert a == b


def test_wall_positions_match_centrelines(scene, plan):
    walls = {o["wall_id"]: o for o in objs(plan, "wall")}
    assert set(walls) == {w.id for w in scene.walls}
    for w in scene.walls:
        (minx, miny, minz), (maxx, maxy, maxz) = walls[w.id]["expected_bounds"]
        f = g.wall_frame(w)
        half = w.thickness / 2
        # every prism vertex must lie within thickness/2 of the centreline (plus junction extension)
        for pr in walls[w.id]["prisms"]:
            for x, y in pr["rings"][0]:
                s, d = f.to_local(x, y)
                assert abs(d) <= half + TOL
                assert -0.1 - TOL <= s <= f.length + 0.1 + TOL
        assert minz == pytest.approx(-0.02) and maxz == pytest.approx(2.7)


def test_wall_volume_matches_hand_calculation(plan):
    # footprint 9.295 m2 x 2.72 m minus the 11 opening voids (see docs/SCENE_MODEL.md)
    vol = sum(prism_volume(p) for o in objs(plan, "wall") for p in o["prisms"])
    assert vol == pytest.approx(25.2824 - 3.5816, abs=1e-6)


def test_wall_solids_do_not_overlap(plan):
    prisms = [(o["name"], p) for o in objs(plan, "wall") for p in o["prisms"]]
    for i, (na, a) in enumerate(prisms):
        pa = Polygon(a["rings"][0], a["rings"][1:])
        for nb, b in prisms[i + 1:]:
            if a["z1"] <= b["z0"] + TOL or b["z1"] <= a["z0"] + TOL:
                continue
            ov = pa.intersection(Polygon(b["rings"][0], b["rings"][1:])).area
            assert ov < 1e-8, f"{na} overlaps {nb} by {ov}"


def _solid_at(plan, x, y, z) -> bool:
    from shapely.geometry import Point
    for o in objs(plan, "wall"):
        for p in o["prisms"]:
            if p["z0"] + TOL < z < p["z1"] - TOL and Polygon(p["rings"][0], p["rings"][1:]).contains(Point(x, y)):
                return True
    return False


def test_openings_are_real_voids(scene, plan):
    """Inside each opening there is no wall; just beside and above it there is."""
    for o in scene.openings:
        w = scene.wall(o.wall_id)
        f = g.wall_frame(w)
        a, b = o.offset - o.width / 2, o.offset + o.width / 2
        z_mid = o.sill_height + o.height / 2
        for s in (a + 0.01, o.offset, b - 0.01):
            for d in (-w.thickness / 2 + 0.01, 0.0, w.thickness / 2 - 0.01):
                assert not _solid_at(plan, *f.to_world(s, d), z_mid), f"{o.id} void blocked at s={s}"
        assert _solid_at(plan, *f.to_world(a - 0.02, 0.0), z_mid), f"{o.id}: no wall left of opening"
        assert _solid_at(plan, *f.to_world(b + 0.02, 0.0), z_mid), f"{o.id}: no wall right of opening"
        head = o.sill_height + o.height
        if head < 2.7 - 0.02:
            assert _solid_at(plan, *f.to_world(o.offset, 0.0), head + 0.01), f"{o.id}: no lintel"
        if o.sill_height > 0.02:
            assert _solid_at(plan, *f.to_world(o.offset, 0.0), o.sill_height - 0.01), f"{o.id}: no wall below sill"


def test_window_glass_fills_opening(scene, plan):
    glass = {o["opening_id"]: o for o in objs(plan, "glass")}
    for o in [o for o in scene.openings if o.kind == "window"]:
        (_, _, z0), (_, _, z1) = glass[o.id]["expected_bounds"]
        assert z0 == pytest.approx(o.sill_height + 0.05) and z1 == pytest.approx(o.sill_height + o.height - 0.05)


def test_door_leaves_open_into_their_room(scene, plan):
    from shapely.geometry import Point
    leaves = {o["opening_id"]: o for o in objs(plan, "door_leaf")}
    for o in [o for o in scene.openings if o.kind == "door"]:
        pr = leaves[o.id]["prisms"][0]
        c = Polygon(pr["rings"][0]).centroid
        assert g.room_polygon(scene.room(o.opens_into)).contains(Point(c.x, c.y)), o.id


def test_cap_triangulation_covers_polygon(plan):
    for o in plan["objects"]:
        for p in o["prisms"]:
            flat = [pt for ring in p["rings"] for pt in ring]
            tri_area = sum(abs(g.signed_area([flat[i] for i in t])) for t in p["tris"])
            assert tri_area == pytest.approx(Polygon(p["rings"][0], p["rings"][1:]).area, rel=1e-6, abs=1e-9)
            for t in p["tris"]:  # counter-clockwise from above -> +Z top-cap normals
                assert g.signed_area([flat[i] for i in t]) > 0


def test_floors_and_ceilings_per_room(scene, plan):
    floors = {o["room_id"]: o for o in objs(plan, "floor")}
    ceilings = {o["room_id"]: o for o in objs(plan, "ceiling")}
    assert set(floors) == set(ceilings) == {r.id for r in scene.rooms}
    for r in scene.rooms:
        (_, _, fz0), (_, _, fz1) = floors[r.id]["expected_bounds"]
        (_, _, cz0), _ = ceilings[r.id]["expected_bounds"]
        assert (fz0, fz1, cz0) == pytest.approx((-0.02, 0.0, 2.7))


def test_furniture_placement_and_unreal_transform(scene, plan):
    by_id = {f["furniture_id"]: f for f in plan["furniture"]}
    assert len(by_id) == len(scene.furniture)
    sofa = by_id["F1"]
    assert sofa["location"] == [1.5, 4.2, 0.0]
    assert sofa["scale"] == [1.0, 1.0, 1.0] and sofa["size"] == [2.2, 0.95, 0.85]
    assert sofa["unreal"]["location_cm"] == pytest.approx(list(scene_to_unreal_location(1.5, 4.2, 0.0)))
    assert sofa["unreal"]["yaw_deg"] == pytest.approx(scene_to_unreal_yaw(180.0))
    assert sofa["name"] == "FURN_R1_F1_sofa_3seat"


def test_resized_furniture_scales(scene_dict):
    s = mutate(scene_dict, lambda d: d["furniture"][0].update(size={"width": 2.64, "depth": 0.95, "height": 0.85}))
    sofa = next(f for f in compile_scene(s)["furniture"] if f["furniture_id"] == "F1")
    assert sofa["scale"] == pytest.approx([1.2, 1.0, 1.0])


def test_object_naming(plan):
    names = [o["name"] for o in plan["objects"]] + [f["name"] for f in plan["furniture"]]
    assert len(names) == len(set(names))
    assert all(n.startswith(("ARCH_", "FURN_")) for n in names)


def test_room_edit_only_changes_that_rooms_hashes(scene_dict, scene):
    before = compile_scene(scene)
    after_scene = mutate(scene_dict, lambda d: next(f for f in d["furniture"] if f["id"] == "F17").update(position=[1.3, 1.75]))
    after = compile_scene(after_scene)
    h0 = {o["name"]: o["hash"] for o in before["objects"] + before["furniture"]}
    h1 = {o["name"]: o["hash"] for o in after["objects"] + after["furniture"]}
    assert h0.keys() == h1.keys()
    assert [n for n in h0 if h0[n] != h1[n]] == ["FURN_R3_F17_bed_double"]


def test_room_spawn_points_are_on_free_floor(scene, plan):
    """Teleport/navigation targets must not be inside furniture or walls."""
    from shapely.geometry import Point
    from twin.assets.catalog import load_catalog
    cat = load_catalog()
    for r in plan["rooms"]:
        p = Point(r["spawn_point"])
        room = scene.room(r["id"])
        assert g.room_polygon(room).contains(p)
        for f in scene.furniture:
            if f.room_id == room.id and cat.get(f.asset_id).placement != "floor_layer":
                assert g.furniture_footprint(f, cat.get(f.asset_id)).distance(p) >= 0.34, (r["name"], f.id)


def _set_bands(d):
    d["rooms"][0]["finishes"] = {"walls": "plaster_warm", "dado": "wall_tile_white", "dado_height": 1.0,
                                 "frieze": "wood_oak", "frieze_from": 2.2}


def test_wall_bands_split_faces_by_height(scene_dict):
    banded = mutate(scene_dict, _set_bands)
    plan = compile_scene(banded)
    room = banded.room("R1")
    faces = []  # (z0, z1, material facing R1) for every wall piece on R1's side
    for o in objs(plan, "wall"):
        nx, ny = o["axis"]
        for p in o["prisms"]:
            (x0, y0), (x1, y1) = p["rings"][0][0], p["rings"][0][1]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            for side, key in ((1, "mat_pos"), (-1, "mat_neg")):
                probe = g.room_at(banded, "L0", cx + side * nx * 0.2, cy + side * ny * 0.2)
                if probe is not None and probe.id == room.id and key in p:
                    faces.append((p["z0"], p["z1"], p[key]))
    assert faces
    for z0, z1, mat in faces:
        mid = (z0 + z1) / 2
        expected = "wall_tile_white" if mid < 1.0 else "wood_oak" if mid > 2.2 else "plaster_warm"
        assert mat == expected, (z0, z1, mat)
    assert any(z1 == pytest.approx(1.0) for _, z1, _ in faces) and any(z0 == pytest.approx(2.2) for z0, _, _ in faces)


def test_wall_bands_keep_volume_and_other_rooms(scene_dict, plan):
    banded = compile_scene(mutate(scene_dict, _set_bands))
    vol = sum(prism_volume(p) for o in objs(banded, "wall") for p in o["prisms"])
    assert vol == pytest.approx(25.2824 - 3.5816, abs=1e-6)
    used = {p.get(k) for o in objs(banded, "wall") for p in o["prisms"] for k in ("mat_pos", "mat_neg")}
    assert {"wall_tile_white", "wood_oak", "plaster_warm"} <= used
    before = {o["name"]: o["hash"] for o in objs(plan, "wall")}
    after = {o["name"]: o["hash"] for o in objs(banded, "wall")}
    assert any(before[n] == after[n] for n in before)  # walls not touching R1 are untouched


def test_wall_bands_must_be_ordered(scene_dict):
    def bad(d):
        d["rooms"][0]["finishes"] = {"dado": "wall_tile_white", "dado_height": 2.5, "frieze": "wood_oak",
                                     "frieze_from": 2.0}
    with pytest.raises(ValueError, match="dado_height must be below frieze_from"):
        mutate(scene_dict, bad)
