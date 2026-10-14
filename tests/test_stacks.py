"""Multi-floor buildings (twin.viewer.stacks): floors stacked into one building for the Unreal viewer.

AUTOMATED (no Blender, no Unreal): the floors are the synthetic builds of tests/test_viewer.py. The real
viewer opens a stack in tests/test_unreal_viewer.py (marker `unreal`).
"""

from __future__ import annotations

import pytest

from tests.hvac_fixture import sample_scene_with_hvac
from tests.test_viewer import bearer, completed_build, make_app
from twin.fixtures import load_sample_apartment
from twin.viewer import bundle, stacks
from twin.viewer.tokens import load_secret, mint


def test_grid_alignment_matches_the_shared_structural_grid():
    # floor 1's grid at x = 23.1 + (0, 8, 12, 16, 20, 24, 32) m; floor 2's sheet shows the same grid 10.7 m to the left
    bays = [0, 8, 12, 16, 20, 24, 32]
    ref = ([23.1 + b for b in bays], [6.0 + b for b in (0, 8, 12, 16, 24, 28)])
    floor = ([12.4 + b for b in bays] + [49.7], [-1.95 + b for b in (0, 8, 12, 16, 24, 28)])
    a = stacks.align_grids(ref, floor)
    assert a["method"] == "structural grid" and a["matched"] == [7, 6]
    assert a["offset_m"] == pytest.approx([10.7, 7.95], abs=1e-3)
    assert stacks.align_grids(ref, ([1.0, 2.0], [5.0])) is None and stacks.align_grids(None, floor) is None


def test_floor_to_floor_leaves_room_for_the_ceiling_void():
    lower = {"wall_height_m": 3.0, "top_m": 3.43}   # ducts and ducted units up to 3.43 m above the 3.0 m ceiling
    assert stacks.floor_to_floor(lower, {"slab_m": 0.2}) == pytest.approx(3.65)
    assert stacks.floor_to_floor({"wall_height_m": 2.7, "top_m": 2.0}, {"slab_m": 0.2}) == pytest.approx(2.95)


def test_merged_model_shifts_and_renames_every_floor(tmp_path, scene):
    from tests.test_viewer import write_build
    a, b = tmp_path / "b_a", tmp_path / "b_b"
    write_build(a, scene.model_dump(mode="json"))
    write_build(b, scene.model_dump(mode="json"))
    data, gltf = stacks.merge_glbs([("F1", a / bundle.MODEL_REL, (0.0, 0.0, 0.0)),
                                    ("F2", b / bundle.MODEL_REL, (1.5, 3.2, -2.0))])  # glTF metres: x, up, -y
    out = tmp_path / "m.glb"
    out.write_bytes(data)
    g, binary = bundle.read_glb(out)
    got = bundle.node_bounds_ue(g, binary)
    one = bundle.node_bounds_ue(*bundle.read_glb(a / bundle.MODEL_REL))
    assert set(got) == {f"F1_{n}" for n in one} | {f"F2_{n}" for n in one}
    # Unreal cm = 100 * (x, z, y) of glTF: the F2 translation (1.5, 3.2, -2.0) m is (150, -200, 320) cm
    for name, (lo, hi) in one.items():
        assert got[f"F1_{name}"][0] == pytest.approx(lo, abs=1e-3)
        assert got[f"F2_{name}"][0] == pytest.approx([lo[0] + 150, lo[1] - 200, lo[2] + 320], abs=1e-3)
        assert got[f"F2_{name}"][1] == pytest.approx([hi[0] + 150, hi[1] - 200, hi[2] + 320], abs=1e-3)
    assert [n["name"] for n in g["nodes"] if "children" in n] == ["Floor_F1", "Floor_F2"]


def test_stack_api_viewer_routes_and_scope(tmp_path):
    c, storage, settings = make_app(tmp_path)
    p1, b1 = completed_build(storage, sample_scene_with_hvac())
    p2, b2 = completed_build(storage, sample_scene_with_hvac())
    other, ob = completed_build(storage, load_sample_apartment())
    assert c.post("/api/stacks", json={"name": "Tower", "floors": [{"project_id": "p_nope"}]}).json()["error"]["code"] == "FLOOR_NOT_FOUND"
    st = c.post("/api/stacks", json={"name": "Tower", "floors": [{"project_id": p1}, {"project_id": p2, "label": "Roof"}]}).json()
    sid = st["id"]
    assert sid.startswith("s_") and [x["id"] for x in c.get("/api/stacks", params={"project_id": p2}).json()] == [sid]
    plan = st["plan"]
    f1, f2 = plan["floors"]
    assert (f1["label"], f2["label"]) == ("Level 1", "Roof") and f1["elevation_m"] == 0.0
    # synthetic builds have no floor-plan PDF: placed by their drawing origins, with a warning
    assert f2["alignment"]["method"].startswith("none") and plan["warnings"]
    assert f2["elevation_source"] == "stacked"
    assert f2["elevation_m"] == pytest.approx(stacks.floor_to_floor({"wall_height_m": f1["wall_height_m"], "top_m": f1["top_m"]},
                                                                    {"slab_m": 0.2}))

    r = c.post(f"/api/stacks/{sid}/viewer/launch", json={}).json()
    doc = r["launch"]
    assert doc["project_id"] == sid and doc["build_id"] == plan["composite_id"] and [f["key"] for f in r["floors"]] == ["F1", "F2"]
    h = bearer(doc["token"])
    base = f"/api/viewer/v1/buildings/{sid}/builds/{doc['build_id']}"
    man = c.get(base + "/manifest", headers=h).json()
    assert man["preflight"]["ok"] and man["preflight"]["checked"] == 2 * 4
    assert [f["key"] for f in man["floors"]] == ["F1", "F2"] and man["build"]["kind"] == "stack"
    z2 = man["floors"][1]["floor_z_cm"]
    assert z2 == pytest.approx(f2["elevation_m"] * 100)
    assert {r_["id"] for r_ in man["rooms"]} == {"F1_R1", "F2_R1"} and man["rooms"][1]["name"] == "Roof · Living"
    assert man["rooms"][1]["floor_z_cm"] == pytest.approx(z2)
    wall2 = next(o for o in man["objects"] if o["name"] == "F2_ARCH_Wall_W1")
    assert wall2["bounds_cm"][0][2] == pytest.approx(z2) and wall2["floor"] == "F2"
    assert {u["id"] for u in man["hvac"]["units"]} == {"F1_U_FCU-1", "F1_U_SPLIT-1", "F2_U_FCU-1", "F2_U_SPLIT-1"}
    glb = c.get(base + "/model.glb", headers=h)
    assert glb.status_code == 200 and len(glb.content) == man["model"]["bytes"]
    live = c.get(base + "/hvac/live", headers=h).json()
    assert live["source"] == "simulated" and {u["unit_id"] for u in live["units"]} == {u["id"] for u in man["hvac"]["units"]}
    assert all(u["object"] == f"{u['floor']}_HVAC_Unit_{u['unit_id'][3:]}" for u in live["units"])
    assert "F2_HVAC_Unit_U_FCU-1" in {o["name"] for o in man["objects"]}
    af = c.get(base + "/hvac/airflow", headers=h).json()
    assert [f["floor"] for f in af["floors"]] == ["F1", "F2"] and af["floors"][1]["offset_cm"][2] == pytest.approx(z2)
    assert af["floors"][1]["unit_prefix"] == "F2_" and af["floors"][1]["airflow"]["zones"]
    listing = c.get("/api/viewer/v1/buildings", headers=h).json()["buildings"]
    assert listing[0]["project_id"] == sid and listing[0]["kind"] == "stack" and {b["project_id"] for b in listing} == {sid, p1, p2}
    # the token covers this stack and its floors only
    assert c.get(f"/api/viewer/v1/buildings/{other}/builds/{ob}/manifest", headers=h).status_code == 403
    other_tok, _ = mint(load_secret(None, settings.data_dir), projects=[other])
    assert c.get(base + "/manifest", headers=bearer(other_tok)).status_code == 403

    # a given height and offset win; the composite id changes and the old link keeps working
    upd = c.put(f"/api/stacks/{sid}", json={"name": "Tower", "floors": [
        {"project_id": p1}, {"project_id": p2, "label": "Roof", "elevation_m": 4.0, "offset_m": [1.0, -2.0]}]}).json()
    g2 = upd["plan"]["floors"][1]
    assert g2["elevation_m"] == 4.0 and g2["alignment"] == {"offset_m": [1.0, -2.0], "method": "given"}
    assert upd["plan"]["composite_id"] != doc["build_id"]
    assert c.get(base + "/manifest", headers=h).status_code == 200
    new = c.post(f"/api/stacks/{sid}/viewer/launch", json={}).json()["launch"]
    man2 = c.get(f"/api/viewer/v1/buildings/{sid}/builds/{new['build_id']}/manifest", headers=bearer(new["token"])).json()
    w = next(o for o in man2["objects"] if o["name"] == "F2_ARCH_Wall_W1")["bounds_cm"][0]
    assert w == pytest.approx([wall2["bounds_cm"][0][0] + 100, wall2["bounds_cm"][0][1] + 200, 400.0], abs=1e-2)
    assert c.get(f"/api/viewer/v1/buildings/{sid}/builds/c_000000000000/manifest", headers=h).json()["error"]["code"] == "STACK_CHANGED"
    assert c.delete(f"/api/stacks/{sid}").json() == {"deleted": sid}
    assert c.get(f"/api/stacks/{sid}").status_code == 404


def test_stack_with_an_unbuilt_floor_is_reported_not_served(tmp_path, scene):
    c, storage, settings = make_app(tmp_path)
    p1, _ = completed_build(storage, scene)
    p2, _ = completed_build(storage, scene, status="processing")
    st = c.post("/api/stacks", json={"name": "Half", "floors": [{"project_id": p1}, {"project_id": p2}]}).json()
    assert st["plan_error"]["code"] == "FLOOR_NOT_BUILT" and "plan" not in st
    assert c.post(f"/api/stacks/{st['id']}/viewer/launch", json={}).json()["error"]["code"] == "FLOOR_NOT_BUILT"
    tok, _ = mint(load_secret(None, settings.data_dir), projects=None)
    assert st["id"] not in {b["project_id"] for b in c.get("/api/viewer/v1/buildings", headers=bearer(tok)).json()["buildings"]}
