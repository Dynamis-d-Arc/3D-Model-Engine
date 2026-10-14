"""Sensor layer: model, validation, marker geometry, glTF composition and in-place updates of a saved build.

Everything here runs without Blender: a saved "full" build is fabricated from the compiled plan (its glTF
written by the same composer, standing in for Blender's export), then updated through the real API.
"""

import json
import math
import os
from pathlib import Path

import pytest

from tests.test_api import make_client, new_project, wait_job
from twin.layers import GEOMETRY_REL, MODEL_REL, append_layer, compose, verify, write_glb
from twin.pipeline import prepare_plan
from twin.scene.compile import compile_scene
from twin.scene.derive import fit_to_overall
from twin.scene.diff import change_summary
from twin.scene.model import Scene
from twin.scene.sensors import (CEILING_GAP, MARKER_RADIUS, MARKER_TOTAL, compile_sensors, geometry_key,
                                import_sheet, layer_changes, relink_sensor_units, sensor_placement)
from twin.scene.validate import validate_scene
from twin.viewer.bundle import build_viewer_manifest, read_glb


def with_sensors(scene: Scene, *sensors: dict) -> Scene:
    data = scene.model_dump(mode="json")
    data["sensors"] = data["sensors"] + [{"level_id": "L0", **s} for s in sensors]
    return Scene.model_validate(data)


def a_room(scene: Scene, idx: int = 0):
    r = scene.rooms[idx]
    p = r.polygon
    return r, (round(sum(x for x, _ in p) / len(p), 3), round(sum(y for _, y in p) / len(p), 3))


# ---- model + validation ----------------------------------------------------------------------

def test_sensor_references_are_checked(scene):
    r, c = a_room(scene)
    with_sensors(scene, {"id": "S1", "tag": "TS-01", "room_id": r.id, "position": c})
    for bad, msg in (({"room_id": "NOPE"}, "unknown room"), ({"unit_id": "NOPE"}, "unknown unit"),
                     ({"level_id": "L9"}, "unknown level")):
        with pytest.raises(ValueError, match=msg):
            with_sensors(scene, {"id": "S1", "tag": "TS-01", "position": c, **bad})
    with pytest.raises(ValueError, match="duplicate sensor id"):
        with_sensors(scene, {"id": "S1", "tag": "A", "position": c}, {"id": "S1", "tag": "B", "position": c})


def test_validation_of_the_sensor_layer(scene):
    r, c = a_room(scene)
    other, oc = a_room(scene, 1)
    ok = with_sensors(scene, {"id": "S1", "tag": "TS-01", "room_id": r.id, "position": c, "bms_point": "P1"})
    rep = validate_scene(ok)
    assert rep.can_generate and not [i for i in rep.issues if i.code.startswith(("E_SENSOR", "W_SENSOR"))]

    bad = with_sensors(scene,
                       {"id": "S1", "tag": "TS-01", "room_id": r.id, "position": oc},             # in another room
                       {"id": "S2", "tag": "TS-01", "position": c},                                # duplicate tag
                       {"id": "S3", "tag": "TS-03", "position": c, "elevation": 2.68},             # into the ceiling
                       {"id": "S4", "tag": "TS-04", "position": (-50.0, -50.0)},                   # outdoors
                       {"id": "S5", "tag": "TS-05", "position": c, "certainty": {"status": "estimated"}})
    codes = {i.code: i for i in validate_scene(bad).issues}
    assert "sensor:S1" in codes["E_SENSOR_OUTSIDE_ROOM"].targets
    assert set(codes["E_SENSOR_DUPLICATE_TAG"].targets) == {"sensor:S1", "sensor:S2"}
    assert codes["E_SENSOR_ABOVE_CEILING"].targets == ["sensor:S3"]
    assert codes["W_SENSOR_OUTSIDE_ROOMS"].targets == ["sensor:S4"]
    assert codes["R_CONFIRM_SENSORS"].targets == ["sensor:S5"]
    assert not validate_scene(bad).can_generate


def test_marker_geometry_and_heights(scene):
    r, c = a_room(scene)
    s = with_sensors(scene, {"id": "S1", "tag": "TS-01", "position": c},
                     {"id": "S2", "tag": "CO2-01", "kind": "co2", "position": c, "mounting": "ceiling"},
                     {"id": "S3", "tag": "TS-03", "position": c, "elevation": 0.4})
    objs, mats = compile_sensors(s)
    by = {o["sensor_id"]: o for o in objs}
    ceil = scene.levels[0].wall_height
    assert by["S1"]["expected_bounds"][0][2] == pytest.approx(1.5 - MARKER_TOTAL / 2)
    assert by["S1"]["expected_bounds"][1][2] == pytest.approx(1.5 + MARKER_TOTAL / 2)
    assert by["S2"]["expected_bounds"][1][2] == pytest.approx(ceil - CEILING_GAP)
    assert by["S3"]["height_m"] == pytest.approx(0.4)
    half = MARKER_RADIUS * math.cos(math.radians(22.5))
    assert by["S1"]["expected_bounds"][0][0] == pytest.approx(c[0] - half, abs=1e-5)   # prisms snap to 0.01 mm
    assert by["S1"]["room_id"] == r.id and by["S1"]["label"].startswith("Temperature sensor TS-01")
    assert {"sensor_body", "sensor_temperature", "sensor_co2"} == set(mats)
    assert sensor_placement(s)["S2"]["room_id"] == r.id
    plan = compile_scene(s)   # the layer is in the plan, but never among the objects Blender builds
    assert len(plan["sensors"]) == 3 and not any(o["kind"] == "sensor" for o in plan["objects"])


def test_geometry_key_ignores_layers_only(scene):
    r, c = a_room(scene)
    s = with_sensors(scene, {"id": "S1", "tag": "TS-01", "position": c})
    assert geometry_key(s) == geometry_key(scene)
    d = scene.model_dump(mode="json")
    d["walls"][0]["thickness"] += 0.01
    moved = Scene.model_validate(d)
    assert geometry_key(moved) != geometry_key(scene)
    summary = change_summary(scene, s)
    assert summary["layers_only"] and not summary["geometry_changed"]
    assert summary["counts"]["sensors"] == {"added": 1, "removed": 0, "changed": 0}
    assert change_summary(scene, moved)["geometry_changed"]


# ---- glTF composition ------------------------------------------------------------------------

def fake_geometry_glb(plan: dict, path: Path) -> None:
    """Stand-in for Blender's glTF export: every architectural object of the plan as a mesh node."""
    gltf, binary = append_layer({"asset": {"version": "2.0"}}, b"", plan["objects"], plan["materials"], "geometry")
    write_glb(path, gltf, binary)


def test_compose_appends_without_touching_the_geometry(tmp_path, scene):
    r, c = a_room(scene)
    s = with_sensors(scene, {"id": "S1", "tag": "TS-01", "position": c},
                     {"id": "S2", "tag": "CO2-01", "kind": "co2", "position": c, "mounting": "ceiling"})
    plan = compile_scene(s)
    geo, out = tmp_path / "geometry.glb", tmp_path / "scene.glb"
    fake_geometry_glb(plan, geo)
    rep = compose(geo, out, plan["sensors"], plan["sensor_materials"])
    assert rep["ok"], rep["failures"]
    assert rep["model_nodes"] == rep["geometry_nodes"] + 2 and rep["max_deviation_cm"] < 0.01
    g_json, g_bin = read_glb(geo)
    m_json, m_bin = read_glb(out)
    assert m_bin[:len(g_bin)] == g_bin and m_json["nodes"][:len(g_json["nodes"])] == g_json["nodes"]
    node = next(n for n in m_json["nodes"] if n["name"] == "SENSOR_S1")
    assert node["extras"]["twin_kind"] == "sensor" and node["extras"]["twin_tag"] == "TS-01"
    # a changed byte in the building part is caught
    data = bytearray(out.read_bytes())
    data[-len(m_bin) + 64] ^= 0xFF
    out.write_bytes(bytes(data))
    assert not verify(geo, out, plan["sensors"])["ok"]


# ---- in-place updates of a saved build through the API ------------------------------------------

def fabricate_full_build(c, pid: str, version: int) -> str:
    """A completed full build of `version` as Blender would leave it (with a stand-in glTF)."""
    from twin.layers import apply_layers
    from twin.pipeline import write_manifest
    storage = c.app.state.storage
    scene = storage.load_scene(pid, version)
    job = storage.create_job(pid, "build", {}, version)
    bid = storage.create_build(pid, version, job["id"], None)["id"]
    d = storage.build_dir(pid, bid)
    plan = prepare_plan(scene)
    (d / "export/web").mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (d / "scene.json").write_text(scene.model_dump_json(), encoding="utf-8")
    (d / "scene.blend").write_bytes(b"BLENDER-v400" + os.urandom(64))
    for rel in [f"export/unreal/arch/{o['name']}.fbx" for o in plan["objects"]] + \
               [f"export/unreal/furniture/SM_{f['variant']}.fbx" for f in plan["furniture"]] + ["export/fbx/scene_full.fbx"]:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b"FBX" + rel.encode())
    fake_geometry_glb(plan, d / MODEL_REL)
    layer = apply_layers(d, plan)
    write_manifest(scene, plan, d, bid, {"renders": []}, {"checked": 1, "max_delta_m": 0.0}, "Blender 5.2 (fake)", [],
                   layer)
    storage.update_build(bid, status="completed", summary={"kind": "full"})
    storage.update_job(job["id"], status="completed")
    return bid


def save_version(c, pid: str, scene: Scene, parent: int, note: str) -> dict:
    r = c.post(f"/api/projects/{pid}/versions", json={"scene": scene.model_dump(mode="json"), "parent": parent,
                                                      "note": note})
    assert r.status_code == 201, r.text
    return r.json()


def test_sensor_update_reuses_the_saved_model(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    v1 = Scene.model_validate(c.post(f"/api/projects/{pid}/versions/fixture").json()["scene"])
    base = fabricate_full_build(c, pid, 1)
    opts = c.get(f"/api/projects/{pid}/versions/1/build-options").json()
    assert opts["layer"]["possible"] and opts["layer"]["up_to_date"]

    # step 7-8: add sensors, save -> v2 records that only the sensor layer changed
    r, cen = a_room(v1)
    unit_free = with_sensors(v1, {"id": "S1", "tag": "TS-01", "room_id": r.id, "position": cen, "bms_point": "AHU1.T"},
                             {"id": "S2", "tag": "CO2-01", "kind": "co2", "position": cen, "mounting": "ceiling"})
    v2 = save_version(c, pid, unit_free, 1, "add two sensors")
    assert v2["changes"]["layers_only"] and v2["changes"]["sensors"]["added"] == ["S1", "S2"]
    listed = next(v for v in c.get(f"/api/projects/{pid}/versions").json() if v["number"] == 2)
    assert listed["changes"]["counts"]["sensors"]["added"] == 2

    opts = c.get(f"/api/projects/{pid}/versions/2/build-options").json()
    assert opts["layer"]["possible"] and opts["layer"]["base_build"]["id"] == base
    assert opts["layer"]["changes"]["added"] == ["S1", "S2"]

    # step 9: update in place - no Blender configured, still completes
    job = c.post(f"/api/projects/{pid}/versions/2/build", json={"mode": "layer"}).json()
    assert job["build_kind"] == "layer"
    done = wait_job(c, job["id"], timeout=60)
    assert done["status"] == "completed", done
    bid = done["result"]["build_id"]
    b = c.get(f"/api/projects/{pid}/builds/{bid}").json()
    assert b["kind"] == "layer" and b["previous_build"] == base
    m = b["manifest"]
    assert m["build_kind"] == "layer" and m["geometry_build"] == base and m["base_build"] == base
    assert [s["tag"] for s in m["sensors"]] == ["TS-01", "CO2-01"]
    store = c.app.state.storage
    new_dir, base_dir = store.build_dir(pid, bid), store.build_dir(pid, base)
    # the building is the saved one, byte for byte (hard links: nothing was rewritten)
    assert (new_dir / GEOMETRY_REL).read_bytes() == (base_dir / GEOMETRY_REL).read_bytes()
    assert (new_dir / "scene.blend").read_bytes() == (base_dir / "scene.blend").read_bytes()
    assert os.stat(new_dir / "scene.blend").st_nlink >= 2
    g_json, _ = read_glb(new_dir / GEOMETRY_REL)
    m_json, _ = read_glb(new_dir / MODEL_REL)
    assert {"SENSOR_S1", "SENSOR_S2"} <= {n["name"] for n in m_json["nodes"]}
    assert not any(n["name"].startswith("SENSOR_") for n in g_json["nodes"])
    report = json.loads((new_dir / "layer_report.json").read_text(encoding="utf-8"))
    assert report["ok"] and report["changes"]["added"] == ["S1", "S2"]
    # the base build is untouched
    assert not any(n["name"].startswith("SENSOR_") for n in read_glb(base_dir / MODEL_REL)[0]["nodes"])
    assert c.get(f"/api/projects/{pid}/builds/{bid}/files/export/web/scene.glb").status_code == 200

    # the packaged viewer sees and checks the sensors
    vm = build_viewer_manifest(new_dir, pid, "Test flat", 2)
    sens = [o for o in vm["objects"] if o["kind"] == "sensor"]
    assert len(sens) == 2 and vm["sensors"] == 2 and vm["build"]["kind"] == "layer"
    assert not vm["preflight"]["failures"] and not any(n.startswith("SENSOR_") for n in vm["preflight"]["missing"])

    # move one sensor -> updates the newest compatible build, the geometry still comes from the first
    d3 = unit_free.model_dump(mode="json")
    d3["sensors"][0]["position"] = [cen[0] + 0.5, cen[1]]
    save_version(c, pid, Scene.model_validate(d3), 2, "move TS-01")
    done3 = wait_job(c, c.post(f"/api/projects/{pid}/versions/3/build", json={"mode": "auto"}).json()["id"], 60)
    assert done3["status"] == "completed" and done3["result"]["kind"] == "layer"
    assert done3["result"]["changes"]["moved"] == ["S1"] and done3["result"]["geometry_build"] == base

    # changing the building is refused for an in-place update
    d4 = unit_free.model_dump(mode="json")
    d4["walls"][0]["thickness"] = round(d4["walls"][0]["thickness"] + 0.02, 3)
    save_version(c, pid, Scene.model_validate(d4), 2, "thicker wall")
    opts = c.get(f"/api/projects/{pid}/versions/4/build-options").json()
    assert not opts["layer"]["possible"] and "walls 1 changed" in opts["layer"]["reason"]
    r4 = c.post(f"/api/projects/{pid}/versions/4/build", json={"mode": "layer"})
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "LAYER_UPDATE_NOT_POSSIBLE"

    ch = c.get(f"/api/projects/{pid}/versions/4/changes").json()
    assert ch["from"] == 2 and ch["geometry_changed"] and "walls" in ch["detail"]


def test_import_placement_sheet(scene):
    r, c = a_room(scene)
    unitless = scene
    text = ("Tag,Type,X,Y,Height,Room,BMS Point\n"
            f"TS-01,temp,{c[0]},{c[1]},1.4,,AHU.T1\n"
            f"CO2-01,co2,,,,{r.name},\n"
            "BAD-01,laser,1,1,,,\n"
            "NOPOS,temp,,,,,\n")
    out, info = import_sheet(unitless, text)
    assert info["added"] == ["TS-01", "CO2-01"]
    assert any("unknown sensor kind 'laser'" in p for p in info["problems"])
    assert any("NOPOS" in p and "no position" in p for p in info["problems"])
    by = {s.tag: s for s in out.sensors}
    assert by["TS-01"].room_id == r.id and by["TS-01"].elevation == 1.4 and by["TS-01"].bms_point == "AHU.T1"
    assert by["CO2-01"].mounting == "ceiling" and by["CO2-01"].certainty.status == "estimated"
    again, info2 = import_sheet(out, f"tag,x,y\nTS-01,{c[0] + 0.2},{c[1]}\n")
    assert info2["updated"] == ["TS-01"] and info2["added"] == []
    assert again.sensors[0].id == by["TS-01"].id and again.sensors[0].bms_point == "AHU.T1"
    assert layer_changes(out, again)["moved"] == [by["TS-01"].id]
    assert import_sheet(scene, "kind,x\n")[1]["problems"][0].startswith("no 'tag' column")


def test_live_readings_for_the_sensor_layer(tmp_path):
    from tests.test_hvac import sample_scene_with_hvac
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    base = sample_scene_with_hvac()
    unit = base.hvac.units[0]
    s = with_sensors(base, {"id": "S1", "tag": "TS-01", "position": unit.position, "unit_id": unit.id, "bms_point": "B.T1"},
                     {"id": "S2", "tag": "CO2-01", "kind": "co2", "position": unit.position, "mounting": "ceiling"})
    v = save_version(c, pid, s, None, "with sensors")["number"]
    live = c.get(f"/api/projects/{pid}/hvac/live", params={"version": v}).json()
    t1, co2 = live["sensors"]["S1"], live["sensors"]["S2"]
    assert t1["source"] == "simulated" and t1["unit"] == "°C" and 10 < t1["value"] < 40
    assert co2["unit"] == "ppm" and 400 < co2["value"] < 2000
    r = c.post(f"/api/projects/{pid}/hvac/telemetry", json={"sensors": [{"sensor": "b.t1", "value": 24.6},
                                                                       {"sensor": "nope", "value": 1}]}).json()
    assert r["sensors_accepted"] == 1 and r["unknown_sensors"] == ["nope"]
    live = c.get(f"/api/projects/{pid}/hvac/live", params={"version": v}).json()
    assert live["sensors"]["S1"] == {**live["sensors"]["S1"], "source": "live", "value": 24.6}
    assert live["sensors"]["S2"]["source"] == "simulated"


def test_sensors_follow_rescaling_and_hvac_reimport(scene):
    r, c = a_room(scene)
    data = scene.model_dump(mode="json")
    data["hvac"]["units"] = [{"id": "U1", "tag": "FC-1", "level_id": "L0", "position": list(c)}]
    data["sensors"] = [{"id": "S1", "tag": "TS-01", "level_id": "L0", "position": list(c), "unit_id": "U1"}]
    s = Scene.model_validate(data)
    new = s.model_dump(mode="json")
    new["hvac"]["units"] = [{"id": "U_FC-1", "tag": "FC-1", "level_id": "L0", "position": list(c)}]
    relink_sensor_units(new, s)
    assert Scene.model_validate(new).sensors[0].unit_id == "U_FC-1"
    big = fit_to_overall(s, 11.0, 9.0)
    assert big.sensors[0].position != s.sensors[0].position
