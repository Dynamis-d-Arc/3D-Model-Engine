"""Runtime-viewer backend: tokens, viewer manifest + glTF pre-flight, the /api/viewer/v1 routes.

AUTOMATED (no Blender, no Unreal): builds are synthesised here (a small GLB written by the test with the same
conventions as Blender's exporter) so every rule can be checked quickly. The real Blender export is checked
by test_blender_glb_matches_scene_model (marker `blender`); the real Unreal runtime by
tests/test_unreal_viewer.py (marker `unreal`).
"""

from __future__ import annotations

import json
import math
import struct
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.hvac_fixture import sample_scene_with_hvac
from twin.api.app import create_app
from twin.api.storage import Storage
from twin.config import RedactSecrets, load_settings
from twin.fixtures import load_sample_apartment
from twin.scene.model import Brief
from twin.viewer import bundle
from twin.viewer.routes import thermal_state
from twin.viewer.tokens import TokenError, load_secret, mint, verify

SECRET = b"x" * 40


# ---- a synthetic build: GLB (glTF Y-up, metres) + manifest (scene frame) ---------------------------

def _box_tris(lo, hi):
    """Box in glTF frame from a Blender-frame AABB (x, y, z) -> glTF (x, z, -y)."""
    corners = []
    for i in range(8):
        x = hi[0] if i & 1 else lo[0]
        y = hi[1] if i & 2 else lo[1]
        z = hi[2] if i & 4 else lo[2]
        corners.append((x, z, -y))
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    idx = [i for a, b, c, d in faces for i in (a, b, c, a, c, d)]
    return np.array(corners, dtype=np.float32), np.array(idx, dtype=np.uint16)


def write_glb(path: Path, nodes: list[dict]) -> None:
    """nodes: {name, lo, hi (local, Blender frame), t (Blender frame, optional), yaw_deg (optional), extras}"""
    blob = bytearray()
    gl = {"asset": {"version": "2.0", "generator": "twin test"}, "scene": 0, "scenes": [{"nodes": []}], "nodes": [],
          "meshes": [], "accessors": [], "bufferViews": [], "buffers": []}
    for n in nodes:
        pos, idx = _box_tris(n["lo"], n["hi"])
        for arr, target, ctype, typ in ((pos, 34962, 5126, "VEC3"), (idx, 34963, 5123, "SCALAR")):
            while len(blob) % 4:
                blob.append(0)
            gl["bufferViews"].append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes, "target": target})
            acc = {"bufferView": len(gl["bufferViews"]) - 1, "componentType": ctype, "count": len(arr), "type": typ}
            if typ == "VEC3":
                acc["min"], acc["max"] = arr.min(axis=0).tolist(), arr.max(axis=0).tolist()
            gl["accessors"].append(acc)
            blob += arr.tobytes()
        gl["meshes"].append({"primitives": [{"attributes": {"POSITION": len(gl["accessors"]) - 2},
                                             "indices": len(gl["accessors"]) - 1}]})
        node = {"name": n["name"], "mesh": len(gl["meshes"]) - 1, "extras": n.get("extras", {})}
        if "t" in n:
            tx, ty, tz = n["t"]
            node["translation"] = [tx, tz, -ty]
        if n.get("yaw_deg"):
            a = math.radians(n["yaw_deg"])  # Blender rotation about +Z == glTF rotation about +Y
            node["rotation"] = [0.0, math.sin(a / 2), 0.0, math.cos(a / 2)]
        gl["nodes"].append(node)
        gl["scenes"][0]["nodes"].append(len(gl["nodes"]) - 1)
    while len(blob) % 4:
        blob.append(0)
    gl["buffers"].append({"byteLength": len(blob)})
    js = json.dumps(gl).encode()
    js += b" " * (-len(js) % 4)
    data = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(blob)) + struct.pack("<II", len(js), 0x4E4F534A) + js \
        + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_build(folder: Path, scene_dict: dict, *, move_wall_cm: float = 0.0) -> dict:
    """A completed-looking build: two rooms' floors, a wall, a rotated table, an HVAC unit."""
    room = {"id": "R1", "name": "Living", "type": "living", "level_id": "L0", "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]],
            "dims_clear": [4.9, 3.9], "area_net": 19.11, "floor_z": 0.0, "ceiling_z": 2.7, "floor_material": "oak",
            "label_point_cm": [250.0, -200.0, 0.0], "spawn_point_cm": [250.0, -200.0, 0.0], "area_centreline": 20.0}
    objects = [
        {"name": "ARCH_Floor_R1", "kind": "floor", "room_id": "R1", "collision": "complex", "bounds_m": [[0, 0, -0.05], [5, 4, 0.0]]},
        {"name": "ARCH_Wall_W1", "kind": "wall", "wall_id": "W1", "collision": "complex", "bounds_m": [[0, -0.1, 0.0], [5, 0.1, 2.7]]},
        {"name": "HVAC_Unit_U_FCU-1", "kind": "hvac_unit", "room_id": "R1", "collision": "none", "hvac_id": "U_FCU-1",
         "tag": "FCU-1", "unit_kind": "fcu_ducted", "airflow_lps": 400.0, "label": "FCU-1 · 400 L/s",
         "bounds_m": [[2.4, 6.15, 2.78], [3.6, 6.85, 3.08]]},
    ]
    table = {"name": "FURN_R1_F1_table", "furniture_id": "F1", "room_id": "R1", "room_name": "Living", "asset_id": "table",
             "label": "Dining table", "category": "table", "size_m": [1.6, 0.9, 0.75], "materials": {}, "collision": "box",
             "location_m": [2.5, 2.0, 0.0], "rotation_deg": 30.0, "status": "confirmed",
             "unreal": {"location_cm": [250.0, -200.0, 0.0], "yaw_deg": -30.0, "scale": [1, 1, 1]}}
    manifest = {"manifest_version": 1, "build_id": folder.name, "project": {"name": "Test", "style": "modern"},
                "scene_hash": "x", "created_at": "2026-01-01T00:00:00Z", "blender": "test", "bounds_m": [[0, -0.1, -0.05], [5, 4, 2.7]],
                "rooms": [room], "objects": objects, "furniture": [table], "lights": [],
                "player_start": {"location": [2.5, 2.0, 0], "rotation_deg": 0, "room_id": "R1",
                                 "unreal": {"location_cm": [250.0, -200.0, 0.0], "yaw_deg": 0.0}}}
    nodes = [{"name": o["name"], "lo": o["bounds_m"][0], "hi": o["bounds_m"][1]} for o in objects]
    nodes[1]["lo"] = [nodes[1]["lo"][0] + move_wall_cm / 100, *nodes[1]["lo"][1:]]
    nodes[1]["hi"] = [nodes[1]["hi"][0] + move_wall_cm / 100, *nodes[1]["hi"][1:]]
    nodes.append({"name": table["name"], "lo": [-0.8, -0.45, 0.0], "hi": [0.8, 0.45, 0.75], "t": table["location_m"], "yaw_deg": 30.0})
    write_glb(folder / bundle.MODEL_REL, nodes)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "scene.json").write_text(json.dumps(scene_dict), encoding="utf-8")
    return manifest


def make_app(tmp_path, **env):
    settings = load_settings(env={"TWIN_DATA_DIR": str(tmp_path / "data"), "TWIN_BLENDER_PATH": "", **env}, env_file=None)
    app = create_app(settings)
    return TestClient(app), app.state.storage, settings


def completed_build(storage: Storage, scene, *, move_wall_cm: float = 0.0, status: str = "completed") -> tuple[str, str]:
    p = storage.create_project("Test building", Brief())
    v = storage.add_version(p["id"], scene, source="fixture", note="", parent=None, validation={"can_generate": True}, scene_hash="h")
    job = storage.create_job(p["id"], "build", {}, v["number"])
    b = storage.create_build(p["id"], v["number"], job["id"], None)
    write_build(storage.build_dir(p["id"], b["id"]), scene.model_dump(mode="json"), move_wall_cm=move_wall_cm)
    storage.update_build(b["id"], status=status, summary={"rooms": 1, "furniture": 1})
    return p["id"], b["id"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- tokens --------------------------------------------------------------------------------------

def test_token_roundtrip_scope_and_expiry():
    tok, grant = mint(SECRET, projects=["p_a"], ttl_hours=1, now=1000.0)
    assert tok.startswith("tvw1.") and grant.allows("p_a") and not grant.allows("p_b")
    g = verify(SECRET, tok, now=1000.0 + 3599)
    assert g.projects == ("p_a",) and g.build is None
    with pytest.raises(TokenError) as e:
        verify(SECRET, tok, now=1000.0 + 3600)
    assert e.value.code == "TOKEN_EXPIRED"
    with pytest.raises(TokenError) as e:
        verify(b"y" * 40, tok, now=1000.0)
    assert e.value.code == "TOKEN_INVALID"
    head, payload, sig = tok.split(".")
    forged = json.loads(bundle_b64d(payload)) | {"prj": "*"}
    with pytest.raises(TokenError):
        verify(SECRET, f"{head}.{bundle_b64e(json.dumps(forged).encode())}.{sig}", now=1000.0)
    for bad in ("", "abc", "tvw1.x", "tvw2.a.b"):
        with pytest.raises(TokenError):
            verify(SECRET, bad)
    all_tok, all_grant = mint(SECRET, projects=None, ttl_hours=1)
    assert all_grant.allows("p_anything", "b_x") and verify(SECRET, all_tok).projects is None
    _, b_grant = mint(SECRET, projects=["p_a"], build="b_1", ttl_hours=1)
    assert b_grant.allows("p_a", "b_1") and not b_grant.allows("p_a", "b_2")
    with pytest.raises(ValueError):
        mint(SECRET, projects=["p_a", "p_b"], build="b_1")
    with pytest.raises(ValueError):
        mint(SECRET, projects=["p_a"], ttl_hours=0)


def bundle_b64d(s: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def bundle_b64e(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_secret_is_generated_once_per_data_dir(tmp_path):
    a = load_secret(None, tmp_path)
    assert len(a) >= 32 and load_secret(None, tmp_path) == a
    assert load_secret(None, tmp_path / "other") != a
    with pytest.raises(ValueError):
        load_secret("short", tmp_path)
    assert load_secret("z" * 32, tmp_path) == b"z" * 32


def test_viewer_tokens_are_redacted_from_logs():
    r = RedactSecrets([])
    tok, _ = mint(SECRET, projects=["p_a"])
    assert tok not in r.scrub(f"launch with {tok} now") and "***" in r.scrub(tok)


# ---- glTF reading + pre-flight against the scene model ---------------------------------------------

def test_glb_reader_rejects_garbage(tmp_path):
    for data in (b"", b"nope" * 10, struct.pack("<4sII", b"glTF", 1, 12) + b"\0" * 8):
        f = tmp_path / "bad.glb"
        f.write_bytes(data)
        with pytest.raises(bundle.BundleError):
            bundle.read_glb(f)


def test_gltf_to_unreal_conversion_and_preflight(tmp_path, scene):
    folder = tmp_path / "b_1"
    write_build(folder, scene.model_dump(mode="json"))
    m = bundle.build_viewer_manifest(folder, "p_x", "Test", 3)
    pre = m["preflight"]
    assert pre["ok"] and pre["checked"] == 4 and pre["max_deviation_cm"] < 1e-3, pre
    # glTF (x, y_up, z) -> Unreal 100 * (x, z, y): the wall from y=-0.1..0.1 m sits at Unreal y = -10..10 cm
    got = bundle.node_bounds_ue(*bundle.read_glb(folder / bundle.MODEL_REL))
    assert got["ARCH_Wall_W1"][0] == pytest.approx([0.0, -10.0, 0.0], abs=1e-3)
    assert got["ARCH_Wall_W1"][1] == pytest.approx([500.0, 10.0, 270.0], abs=1e-3)
    # the rotated table: node rotation (+30 deg about Blender Z) lands where the manifest's yaw -30 says
    t = next(f for f in m["furniture"] if f["name"] == "FURN_R1_F1_table")
    assert got["FURN_R1_F1_table"][0] == pytest.approx(t["bounds_cm"][0], abs=1e-3)
    assert t["yaw_deg"] == -30.0 and t["size_text"] == "1.60 x 0.90 x 0.75 m"
    floor = next(o for o in m["objects"] if o["kind"] == "floor")
    assert floor["label"] == "Living" and m["rooms"][0]["polygon_cm"][2] == [500.0, -400.0]
    assert m["model"]["sha256"] and m["model"]["nodes"] == 4 and m["format"] == "twin-viewer-manifest"


def test_preflight_detects_a_misplaced_object(tmp_path, scene):
    folder = tmp_path / "b_2"
    write_build(folder, scene.model_dump(mode="json"), move_wall_cm=2.5)
    pre = bundle.build_viewer_manifest(folder, "p_x", "Test", 1)["preflight"]
    assert not pre["ok"] and pre["failures"][0]["object"] == "ARCH_Wall_W1"
    assert pre["failures"][0]["deviation_cm"] == pytest.approx(2.5, abs=1e-3)


def test_thermal_state_matches_floor_by_tags_and_clamps():
    series = [[30 - i * 0.01, 31 - i * 0.01, 10.0, i * 0.1, 3.0] for i in range(481)]
    res = {"engine": {"name": "MATLAB"}, "method": "zones", "building": {"floors": [
        {"floor": 1, "tags": ["1FC-01", "1FC-02"]}, {"floor": 2, "tags": ["2FC-01", "FCU-1"]}]},
        "scenarios": [{"setpoint": s, "floors": [{"floor": 1, "series": series}, {"floor": 2, "series": series}]} for s in (22, 24, 26)]}
    st = thermal_state(res, {"FCU-1"}, 25.2, 9999, 123.0)
    assert st["available"] and st["floor"] == 2 and st["setpoint_c"] == 26.0 and st["minute"] == 480
    assert st["measured"] is False and "simulation" in st["label"]
    assert thermal_state(res, {"XX-9"}, None, 0, None)["available"] is False


# ---- API -------------------------------------------------------------------------------------------

def test_viewer_api_auth_scope_and_errors(tmp_path, scene):
    c, storage, settings = make_app(tmp_path)
    pid, bid = completed_build(storage, scene)
    other, obid = completed_build(storage, scene)
    ppid, pending = completed_build(storage, scene, status="processing")
    assert c.get("/api/viewer/v1/info").json()["api_version"] == 1
    base = f"/api/viewer/v1/buildings/{pid}/builds/{bid}"
    assert c.get(base + "/manifest").json()["error"]["code"] == "TOKEN_MISSING"
    assert c.get(base + "/manifest", headers=bearer("tvw1.a.b")).status_code == 401
    secret = load_secret(None, settings.data_dir)
    tok, _ = mint(secret, projects=[pid])
    r = c.get(base + "/manifest", headers=bearer(tok))
    assert r.status_code == 200 and r.json()["preflight"]["ok"]
    glb = c.get(base + "/model.glb", headers=bearer(tok))
    assert glb.status_code == 200 and glb.content[:4] == b"glTF" and len(glb.content) == r.json()["model"]["bytes"]
    assert c.get(f"/api/viewer/v1/buildings/{other}/builds/{obid}/manifest", headers=bearer(tok)).json()["error"]["code"] == "OUT_OF_SCOPE"
    listing = c.get("/api/viewer/v1/buildings", headers=bearer(tok)).json()["buildings"]
    assert [b["project_id"] for b in listing] == [pid] and listing[0]["builds"][0]["build_id"] == bid
    all_tok, _ = mint(secret, projects=None)
    assert c.get(f"/api/viewer/v1/buildings/{pid}/builds/b_20990101_000000_abcdef/manifest", headers=bearer(all_tok)).status_code == 404
    assert c.get(f"/api/viewer/v1/buildings/{ppid}/builds/{pending}/manifest", headers=bearer(all_tok)).json()["error"]["code"] == "BUILD_NOT_COMPLETED"
    expired, _ = mint(secret, projects=[pid], ttl_hours=1, now=time.time() - 7200)
    assert c.get(base + "/manifest", headers=bearer(expired)).json()["error"]["code"] == "TOKEN_EXPIRED"


def test_viewer_api_hvac_live_is_labelled_and_matched(tmp_path):
    c, storage, settings = make_app(tmp_path)
    pid, bid = completed_build(storage, sample_scene_with_hvac())
    tok, _ = mint(load_secret(None, settings.data_dir), projects=[pid])
    live = c.get(f"/api/viewer/v1/buildings/{pid}/builds/{bid}/hvac/live", headers=bearer(tok)).json()
    assert live["source"] == "simulated" and live["measured"] is False and "not measured" in live["label"]
    assert {u["unit_id"] for u in live["units"]} == {"U_FCU-1", "U_SPLIT-1"} and live["matching"]["missing"] == []
    assert all(u["object"] == f"HVAC_Unit_{u['unit_id']}" for u in live["units"])
    # real BMS readings take over and are flagged as measured; unknown tags are reported, not shown
    r = c.post(f"/api/projects/{pid}/hvac/telemetry", json={"readings": [
        {"unit": "FCU-1", "supply_air_c": 13.5, "return_air_c": 24.0, "fan_speed": 0.7},
        {"unit": "NOPE-9", "supply_air_c": 10}]}).json()
    assert r["accepted"] == 1 and r["unknown_units"] == ["NOPE-9"]
    live = c.get(f"/api/viewer/v1/buildings/{pid}/builds/{bid}/hvac/live", headers=bearer(tok)).json()
    assert live["source"] == "live" and live["measured"] is True
    assert [u["supply_air_c"] for u in live["units"] if u["tag"] == "FCU-1"] == [13.5]
    assert live["matching"]["missing"] == ["U_SPLIT-1"]


def test_viewer_api_airflow_is_an_illustration(tmp_path):
    c, storage, settings = make_app(tmp_path)
    pid, bid = completed_build(storage, sample_scene_with_hvac())
    tok, _ = mint(load_secret(None, settings.data_dir), projects=[pid])
    af = c.get(f"/api/viewer/v1/buildings/{pid}/builds/{bid}/hvac/airflow", headers=bearer(tok)).json()
    assert af["kind"] == "illustration" and "not a CFD result" in af["disclaimer"] and "not measured" in af["disclaimer"]
    z = {zn["unit_id"]: zn for zn in af["zones"]}
    assert set(z) == {"U_FCU-1", "U_SPLIT-1"}
    assert [t["id"] for t in z["U_FCU-1"]["supply"]] == ["SD1", "SD2"] and [t["id"] for t in z["U_FCU-1"]["return"]] == ["RG1"]
    # Unreal cm: scene (2.0, 5.0) -> (200, -500)
    assert z["U_FCU-1"]["supply"][0]["location_cm"][:2] == [200.0, -500.0]
    assert af["thermal"]["available"] is False
    # a build without HVAC has no airflow; live data says unavailable
    p2, b2 = completed_build(storage, load_sample_apartment())
    tok2, _ = mint(load_secret(None, settings.data_dir), projects=[p2])
    assert c.get(f"/api/viewer/v1/buildings/{p2}/builds/{b2}/hvac/airflow", headers=bearer(tok2)).json()["error"]["code"] == "HVAC_MISSING"
    assert c.get(f"/api/viewer/v1/buildings/{p2}/builds/{b2}/hvac/live", headers=bearer(tok2)).json()["source"] == "unavailable"


def test_launch_endpoint_mints_a_scoped_link(tmp_path, scene):
    c, storage, settings = make_app(tmp_path, TWIN_PUBLIC_URL="https://twin.example.org/")
    empty = storage.create_project("Nothing built", Brief())
    assert c.post(f"/api/projects/{empty['id']}/viewer/launch", json={}).json()["error"]["code"] == "NO_COMPLETED_BUILD"
    pid, bid = completed_build(storage, scene)
    other, obid = completed_build(storage, scene)
    r = c.post(f"/api/projects/{pid}/viewer/launch", json={}).json()
    doc = r["launch"]
    assert doc["format"] == "twin-viewer-launch" and doc["server"] == "https://twin.example.org"
    assert doc["project_id"] == pid and doc["build_id"] == bid and r["file_name"].endswith(".twinview")
    assert abs(doc["expires_at"] - (time.time() + 12 * 3600)) < 60
    h = bearer(doc["token"])
    assert c.get(f"/api/viewer/v1/buildings/{pid}/builds/{bid}/manifest", headers=h).status_code == 200
    assert c.get(f"/api/viewer/v1/buildings/{other}/builds/{obid}/manifest", headers=h).status_code == 403
    wide = c.post(f"/api/projects/{pid}/viewer/launch", json={"all_projects": True, "ttl_hours": 1}).json()["launch"]
    assert c.get(f"/api/viewer/v1/buildings/{other}/builds/{obid}/manifest", headers=bearer(wide["token"])).status_code == 200
    assert doc["token"] not in c.get("/api/health").text  # tokens are only ever handed out by the launch route


# ---- the real Blender export -----------------------------------------------------------------------

@pytest.mark.blender
def test_blender_glb_matches_scene_model(tmp_path, scene):
    """REAL Blender: the build's glTF export, converted to Unreal space, matches the scene model within 1 cm."""
    settings = load_settings()
    if not settings.blender_path or not Path(settings.blender_path).is_file():
        pytest.skip("Blender not configured")
    from twin.pipeline import run_build
    res = run_build(scene, tmp_path / "b_glb", settings, build_id="b_glb", render=False)
    m = bundle.build_viewer_manifest(res.build_dir, "p_x", "Sample", 1)
    pre = m["preflight"]
    assert pre["ok"], pre
    assert pre["checked"] == len(res.manifest["objects"]) + len(res.manifest["furniture"])
    assert pre["max_deviation_cm"] < 0.05
