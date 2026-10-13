"""HVAC layer: scene model, compiler, validation, drawing registration and symbol detection, airflow
domain, simulated BMS, AI fix operations and the HVAC API.

Automated tests on hand-made data (tests/hvac_fixture.py, synthetic vector primitives). The real-drawing
regression on private drawings lives in tests/local (not in the repository).
"""

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from tests.hvac_fixture import sample_hvac, sample_scene_with_hvac
from twin.ai.fix import apply_operations, compact_scene
from twin.hvac import detect as det
from twin.hvac.airflow import build_domain, cold_room_temp
from twin.hvac.register import grid_lines, match_axis, register_grids
from twin.hvac.telemetry import RHO_CP, TelemetryBody, TelemetryStore, occupancy, outdoor_c, simulate
from twin.scene.compile import compile_scene, hvac_placement
from twin.scene.model import Scene
from twin.scene.validate import validate_scene



# ---- scene model ---------------------------------------------------------------------------------

def test_hvac_references_are_checked():
    d = sample_scene_with_hvac().model_dump(mode="json")
    d["hvac"]["terminals"][0]["unit_id"] = "U_NOPE"
    with pytest.raises(ValidationError, match="unknown unit"):
        Scene.model_validate(d)
    d = sample_scene_with_hvac().model_dump(mode="json")
    d["hvac"]["ducts"][0]["width_end"] = 0.3      # tapered transitions need 2-point paths
    with pytest.raises(ValidationError, match="2-point"):
        Scene.model_validate(d)


def test_old_scenes_load_without_hvac():
    from twin.fixtures import load_sample_apartment
    d = load_sample_apartment().model_dump(mode="json")
    d.pop("hvac")
    s = Scene.model_validate(d)
    assert s.hvac.units == [] and compile_scene(s)["objects"]


# ---- compiler + validation -------------------------------------------------------------------------

def test_compile_places_hvac_in_the_ceiling_void():
    s = sample_scene_with_hvac()
    plan = compile_scene(s)
    by = {o["name"]: o for o in plan["objects"]}
    unit = by["HVAC_Unit_U_FCU-1"]
    ceiling = 2.7
    (x0, y0, z0), (x1, y1, z1) = unit["expected_bounds"]
    assert z0 == pytest.approx(ceiling + s.hvac.plenum_clearance - 0.02, abs=1e-6)  # control box 2 cm below the casing
    assert z1 == pytest.approx(ceiling + s.hvac.plenum_clearance + 0.3, abs=1e-6)
    assert (x1 - x0, y1 - y0) == (pytest.approx(1.2), pytest.approx(0.7))
    dif = by["HVAC_Diffuser_SD1"]
    assert dif["expected_bounds"][0][2] == pytest.approx(ceiling - 0.025, abs=1e-6)
    duct = by["HVAC_Duct_D1"]
    assert duct["expected_bounds"][0][2] == pytest.approx(ceiling + 0.08, abs=1e-6)
    assert duct["system"] == "supply" and "FCU-1" in duct["label"]
    split = hvac_placement(s)["U_SPLIT-1"]
    assert split["z0"] == pytest.approx(2.2)
    assert {o["kind"] for o in plan["objects"] if o["name"].startswith("HVAC_")} == {"hvac_unit", "hvac_duct", "hvac_diffuser", "hvac_grille"}


def test_validation_aggregates_hvac_review_and_checks_balance():
    d = sample_scene_with_hvac().model_dump(mode="json")
    for t in d["hvac"]["terminals"]:
        t["certainty"] = {"status": "extracted", "source": "derived"}
    d["hvac"]["terminals"][1]["airflow_lps"] = 400      # 250 + 400 = 650 vs a 400 L/s unit
    rep = validate_scene(Scene.model_validate(d))
    review = rep.by_code("R_CONFIRM_HVAC")
    assert len(review) == 1 and "3 diffusers/grilles" in review[0].message and not rep.can_generate
    bal = rep.by_code("W_HVAC_AIRFLOW_BALANCE")
    assert len(bal) == 1 and "650 L/s" in bal[0].message
    ok = validate_scene(sample_scene_with_hvac())
    assert ok.can_generate and not ok.by_code("W_HVAC_AIRFLOW_BALANCE")


# ---- registration ----------------------------------------------------------------------------------

def _dashed(x=None, y=None, lo=100.0, hi=1500.0, dash=20.0, gap=4.0):
    out, t = [], lo
    while t < hi:
        a, b = t, min(hi, t + dash)
        out.append((x, a, x, b) if x is not None else (a, y, b, y))
        t = b + gap
    return out


def test_grid_registration_recovers_the_offset():
    xs, ys = [500.0, 726.8, 840.2, 953.6, 1067.0, 1180.4, 1407.2], [300.0, 351.0, 399.2, 498.4, 725.1, 838.5]
    plan = [s for x in xs for s in _dashed(x=x)] + [s for y in ys for s in _dashed(y=y, hi=2000)]
    plan += [(600, 600, 900, 600), (600, 650, 900, 650)]          # walls: continuous, not grid lines
    dx, dy = 115.25, -70.5
    hvac = [(a - dx, b - dy, c - dx, d - dy) for a, b, c, d in plan]
    P = np.array(plan, float)
    H = np.array(hvac, float)
    px, py = grid_lines(P, 2384, 1684)
    hx, hy = grid_lines(H, 2384, 1684)
    assert len(px) == 7 and len(py) == 6
    reg = register_grids(hx, hy, px, py)
    assert reg is not None and reg.matched_x == 7 and reg.matched_y == 6
    assert reg.dx == pytest.approx(dx, abs=0.05) and reg.dy == pytest.approx(dy, abs=0.05)


def test_match_axis_ignores_extra_lines():
    o, n, rms = match_axis([10.0, 50.0, 90.0, 300.0], [110.0, 150.0, 190.0, 7.0, 42.0])
    assert n == 3 and o == pytest.approx(100.0)


# ---- symbol detection (synthetic vector primitives, sheet points; 1 m = 28.35 pt at 1:100) ------------

M = 1 / (0.0254 / 72 * 100)


def _sq(cx, cy, s):
    return [[cx - s / 2, cy - s / 2], [cx + s / 2, cy - s / 2], [cx + s / 2, cy + s / 2], [cx - s / 2, cy + s / 2]]


def _synthetic_sheet():
    quads, segs = [], []
    # supply diffuser at (200, 200): three nested squares 0.3 / 0.2 / 0.1 m
    for s in (0.3, 0.2, 0.1):
        quads.append(_sq(200, 200, s * M))
    # return grille at (400, 200): 1.0 x 0.4 m with one diagonal
    g = [[400 - 0.5 * M, 200 - 0.2 * M], [400 + 0.5 * M, 200 - 0.2 * M], [400 + 0.5 * M, 200 + 0.2 * M], [400 - 0.5 * M, 200 + 0.2 * M]]
    quads.append(g)
    segs.append((g[0][0], g[0][1], g[2][0], g[2][1]))
    # fan coil at (300, 400): marker boxes 0.8 m apart on a horizontal axis + casing outline 1.4 x 0.8 m
    for mx in (300 - 0.4 * M, 300 + 0.4 * M):
        quads.append(_sq(mx, 400, 0.17 * M))
        quads.append(_sq(mx, 400, 0.13 * M))
    for yy in (400 - 0.4 * M, 400 + 0.4 * M):
        segs.append((300 - 0.7 * M, yy, 300 + 0.7 * M, yy))
    # supply duct 400 mm wide from (300, 600) to (600, 600)
    for yy in (600 - 0.2 * M, 600 + 0.2 * M):
        segs.append((300, yy, 600, yy))
    return np.array(segs, float), np.array(quads, float)


def test_detects_diffuser_grille_unit_and_duct():
    segs, quads = _synthetic_sheet()
    labels = [det.Label("tag", "1FC-07", 300, 400 - 1.2 * M), det.Label("flow", "500 L/s", 300, 400 - 0.8 * M, value=500),
              det.Label("number", "185", 200, 200 - 0.6 * M, value=185),
              det.Label("size", "SAD 400x200", 450, 600 + 1.0 * M, code="SAD", w_mm=400, h_mm=200)]
    found, notes = det.detect(segs, quads, labels, 1 / M)
    det.assign_flows(found, labels, M)
    kinds = sorted(f.kind for f in found)
    assert kinds == ["duct", "return_grille", "supply_diffuser", "unit"], kinds
    d = next(f for f in found if f.kind == "supply_diffuser")
    assert (d.x, d.y) == (pytest.approx(200), pytest.approx(200)) and d.flow_lps == 185
    g = next(f for f in found if f.kind == "return_grille")
    assert g.w / M == pytest.approx(1.0, abs=0.01) and g.d / M == pytest.approx(0.4, abs=0.01)
    u = next(f for f in found if f.kind == "unit")
    assert u.tag == "1FC-07" and u.flow_lps == 500
    assert (u.x, u.y) == (pytest.approx(300, abs=1), pytest.approx(400, abs=1))
    assert u.w / M == pytest.approx(1.4, abs=0.05) and u.d / M == pytest.approx(0.8, abs=0.05) and u.angle % 180 == 0
    duct = next(f for f in found if f.kind == "duct")
    assert duct.w / M == pytest.approx(0.4, abs=0.01) and duct.height_mm == 200 and duct.system == "supply"


def test_label_parsing_handles_merged_ocr_lines():
    from twin.hvac.ocr import TextItem
    labs = det.parse_labels([TextItem("395 L/s 455 L/s", 100, 50, 60, 4, 0, 0.9), TextItem("K 1FC-32", 10, 10, 20, 4, 0, 0.9),
                             TextItem("-SAD 550x200 (TYP.)", 0, 0, 40, 4, 0, 0.9), TextItem("185", 5, 5, 6, 4, 0, 0.9),
                             TextItem("1711", 5, 5, 6, 4, 0, 0.6)])
    flows = sorted((lab.value, lab.x) for lab in labs if lab.kind == "flow")
    assert [f[0] for f in flows] == [395, 455] and flows[0][1] < 100 < flows[1][1]   # left / right half of the box
    assert [lab.text for lab in labs if lab.kind == "tag"] == ["1FC-32"]
    size = next(lab for lab in labs if lab.kind == "size")
    assert (size.code, size.w_mm, size.h_mm) == ("SAD", 550, 200)
    nums = [lab for lab in labs if lab.kind == "number"]
    assert [det.plausible_terminal_flow(n) for n in nums] == [True, False]   # 1711 is not a design airflow


def test_single_missing_tag_is_inferred():
    assert det._single_gap([f"1FC-{n:02d}" for n in range(1, 41) if n != 4]) == "1FC-04"
    assert det._single_gap([f"1FC-{n:02d}" for n in range(1, 41) if n not in (4, 9)]) is None


# ---- airflow domain + simulated BMS ----------------------------------------------------------------

def test_airflow_domain_of_the_sample_apartment():
    s = sample_scene_with_hvac()
    d = build_domain(s, cell=0.25)
    g = d["grid"]
    solid = np.frombuffer(__import__("base64").b64decode(d["solid"]), np.uint8).reshape(g["nz"], g["ny"], g["nx"])
    # a wall column is solid at every height; the door O1 in W3 is open below its head
    def col(x, y):
        return int((x - g["x0"]) // g["h"]), int((y - g["y0"]) // g["h"])
    i, j = col(0.0, 1.0)                                         # west exterior wall (windows stay closed)
    assert solid[:, j, i].all()
    i, j = col(7.0, 2.0)                                         # inside the study: open air
    assert not solid[:, j, i].any()
    assert (solid[0] == 0).sum() > 0.7 * (10 * 8) / (0.25 * 0.25)  # most of the floor is open air
    fcu = next(u for u in d["units"] if u["id"] == "U_FCU-1")
    assert [s_["id"] for s_ in fcu["supplies"]] == ["SD1", "SD2"]
    assert sum(s_["q_lps"] for s_ in fcu["supplies"]) == pytest.approx(400)
    assert sum(r["q_lps"] for r in fcu["returns"]) == pytest.approx(400)
    split = next(u for u in d["units"] if u["id"] == "U_SPLIT-1")
    assert split["supplies"] == [] and split["discharge"] and split["discharge"]["dir"][1] < 0   # blows into the bedroom
    assert fcu["internal_w"] > 0 and fcu["served_m2"] > 0
    assert cold_room_temp("Cold Room D Freezer") == -18 and cold_room_temp("Cold Room A Chiller") == 3
    assert cold_room_temp("Cold Room Service Corridor", "hallway") is None


def test_airflow_domain_outlets_furniture_and_relief():
    d = build_domain(sample_scene_with_hvac(), cell=0.25)
    fcu = next(u for u in d["units"] if u["id"] == "U_FCU-1")
    sd1 = next(s_ for s_ in fcu["supplies"] if s_["id"] == "SD1")
    # a 0.6 m 4-way diffuser: face cells under it, jets on all four sides blowing outwards along the ceiling
    assert len(sd1["cells"]) >= 4
    dirs = {(round(j[3]), round(j[4])) for j in sd1["jets"]}
    assert dirs == {(1, 0), (-1, 0), (0, 1), (0, -1)} and all(j[5] < 0 for j in sd1["jets"])
    assert all(c[2] == d["grid"]["nz"] - 1 for c in sd1["cells"])          # in the top layer
    rg = fcu["returns"][0]
    assert len(rg["cells"]) >= 4                                           # a 1.0 x 0.4 m grille face
    split = next(u for u in d["units"] if u["id"] == "U_SPLIT-1")
    assert len(split["discharge"]["cells"]) >= 3 and len(split["discharge"]["jets"]) == len(split["discharge"]["cells"])
    # furniture blocks the air (sofa, beds, counters ...); chairs, plants, rugs do not
    assert d["stats"]["furniture_cells"] > 50
    g_ = d["grid"]
    solid = np.frombuffer(__import__("base64").b64decode(d["solid"]), np.uint8).reshape(g_["nz"], g_["ny"], g_["nx"])
    s = sample_scene_with_hvac()
    bed = next(f for f in s.furniture if f.asset_id == "bed_double")
    i, j = int((bed.position[0] - g_["x0"]) // g_["h"]), int((bed.position[1] - g_["y0"]) // g_["h"])
    assert solid[0, j, i] == 1 and solid[g_["nz"] - 1, j, i] == 0          # bed body solid, air above it
    plant = next(f for f in s.furniture if f.asset_id == "plant")              # see-through: stays air
    i, j = int((plant.position[0] - g_["x0"]) // g_["h"]), int((plant.position[1] - g_["y0"]) // g_["h"])
    assert solid[1, j, i] == 0
    assert d["relief"]                                                     # the entrance door (exterior wall)
    assert d["domain_version"] >= 2 and d["focus"] is None


def test_airflow_room_domain_is_only_that_room():
    s = sample_scene_with_hvac()
    d = build_domain(s, room_id="R2")                                      # kitchen: SD2 of FCU-1, nothing else
    assert d["focus"] == {"room_id": "R2", "name": "Kitchen"}
    assert d["grid"]["h"] <= 0.15                                          # fine cells for a room
    assert [u["id"] for u in d["units"]] == ["U_FCU-1"]
    fcu = d["units"][0]
    assert [x["id"] for x in fcu["supplies"]] == ["SD2"] and fcu["returns"] == []   # its return is in the living room
    assert d["relief"], "air supplied into the kitchen must be able to leave (door / open edge)"
    g_ = d["grid"]
    room_index = np.frombuffer(__import__("base64").b64decode(d["room_index"]), np.int16).reshape(g_["ny"], g_["nx"])
    solid = np.frombuffer(__import__("base64").b64decode(d["solid"]), np.uint8).reshape(g_["nz"], g_["ny"], g_["nx"])
    open_cols = solid[0] == 0
    kitchen = [r["id"] for r in d["rooms"]].index("R2")
    assert set(np.unique(room_index[open_cols])) == {kitchen}
    from twin.hvac.airflow import focus_rooms
    rooms = {r["id"]: r for r in focus_rooms(s)}
    assert set(rooms) == {"R1", "R2", "R3"} and rooms["R3"]["outlets"] == 1      # the split unit counts


def test_simulated_bms_is_consistent_with_the_heat_balance():
    d = build_domain(sample_scene_with_hvac(), cell=0.25)
    t = 1791900000.0 + 600                                          # outside the simulated fault window
    readings, site = simulate(d, t)
    assert 20 <= site["outdoor_c"] <= 36 and 0 <= site["occupancy"] <= 1
    for u in d["units"]:
        r = readings[u["id"]]
        if not r["on"]:
            continue
        load = u["internal_w"] * (0.35 + 0.65 * site["occupancy"]) + u["envelope_ua"] * (site["outdoor_c"] - r["setpoint_c"])
        removed = r["airflow_lps"] / 1000 * RHO_CP * (r["return_air_c"] - r["supply_air_c"])
        assert removed == pytest.approx(max(0.0, load), rel=0.02, abs=2.0)
    assert outdoor_c(t) == site["outdoor_c"] and occupancy(t) == site["occupancy"]


def test_telemetry_store_maps_tags_and_expires():
    st = TelemetryStore()
    body = TelemetryBody.model_validate({"readings": [{"unit": "1fc-23", "supply_air_c": 13.5, "return_air_c": 24.2},
                                                      {"unit": "nope", "supply_air_c": 14}],
                                         "site": {"outdoor_c": 33.1}})
    res = st.ingest("p1", body, lambda name: {"1FC-23": "U_1FC-23"}.get(name.upper()))
    assert res == {"accepted": 1, "unknown_units": ["nope"]}
    units, site, ts = st.live("p1")
    assert units["U_1FC-23"]["supply_air_c"] == 13.5 and site["outdoor_c"] == 33.1
    units["U_1FC-23"]["ts"] = 0       # copies are returned; the store is unchanged
    assert st.live("p1")[0]["U_1FC-23"]["ts"] > 0


# ---- AI fix: edit operations -------------------------------------------------------------------------

def test_fix_operations_update_add_delete_with_cascade():
    s = sample_scene_with_hvac()
    ops = [{"op": "update", "collection": "hvac_units", "id": "U_FCU-1", "data_json": json.dumps({"position": [3.5, 6.5]}), "reason": "move"},
           {"op": "add", "collection": "hvac_terminals", "id": "SD3",
            "data_json": json.dumps({"kind": "supply_diffuser", "position": [5.0, 5.0], "unit_id": "U_FCU-1", "airflow_lps": 50}), "reason": "add"},
           {"op": "delete", "collection": "hvac_units", "id": "U_SPLIT-1", "data_json": "", "reason": "remove"},
           {"op": "delete", "collection": "walls", "id": "W3", "data_json": "", "reason": "remove"}]
    data, changes = apply_operations(s, ops, "test request")
    out = Scene.model_validate(data)
    assert next(u for u in out.hvac.units if u.id == "U_FCU-1").position == (3.5, 6.5)
    assert next(t for t in out.hvac.terminals if t.id == "SD3").certainty.status == "accepted_estimate"
    assert not any(o.wall_id == "W3" for o in out.openings)          # openings of a deleted wall go too
    assert [c["op"] for c in changes] == ["update", "add", "delete", "delete"]


def test_fix_operations_reject_bad_edits():
    s = sample_scene_with_hvac()
    with pytest.raises(ValueError, match="does not exist"):
        apply_operations(s, [{"op": "update", "collection": "rooms", "id": "R99", "data_json": "{}", "reason": ""}], "x")
    with pytest.raises(ValueError, match="already exists"):
        apply_operations(s, [{"op": "add", "collection": "rooms", "id": "R1", "data_json": "{}", "reason": ""}], "x")
    with pytest.raises(ValueError, match="not valid JSON"):
        apply_operations(s, [{"op": "update", "collection": "rooms", "id": "R1", "data_json": "{oops", "reason": ""}], "x")
    c = compact_scene(s)
    assert "provenance" not in c and c["hvac"]["units"][0]["certainty"] == {"status": "confirmed"}


def test_fix_scene_retries_once_with_validation_errors():
    from twin.ai.client import AIError, AIResult
    from twin.ai.fix import fix_scene

    class FakeClient:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), []

        def structured(self, **kw):
            self.calls.append(kw)
            return AIResult(self.answers.pop(0), "", "fake", "req1", {})
    bad = {"summary": "s", "questions": [], "operations": [
        {"op": "update", "collection": "hvac_terminals", "id": "SD1", "data_json": json.dumps({"airflow_lps": -5}), "reason": ""}]}
    good = {"summary": "fixed", "questions": [], "operations": [
        {"op": "update", "collection": "hvac_terminals", "id": "SD1", "data_json": json.dumps({"airflow_lps": 200}), "reason": ""}]}
    fc = FakeClient([bad, good])
    out = fix_scene(fc, sample_scene_with_hvac(), "set SD1 to 200 L/s", ["hvac_terminal:SD1"])
    assert next(t for t in out.scene.hvac.terminals if t.id == "SD1").airflow_lps == 200
    assert len(fc.calls) == 2 and "REJECTED" in fc.calls[1]["parts"][-1].text
    assert "THE USER SELECTED" in fc.calls[0]["parts"][1].text
    with pytest.raises(AIError) as e:
        fix_scene(FakeClient([bad, bad]), sample_scene_with_hvac(), "x")
    assert e.value.code == "AI_FIX_INVALID"
    none = fix_scene(FakeClient([{"summary": "unclear", "questions": ["Which unit?"], "operations": []}]), sample_scene_with_hvac(), "fix it")
    assert none.changes == [] and none.questions == ["Which unit?"]


# ---- API ----------------------------------------------------------------------------------------------

def test_hvac_api_live_domain_and_telemetry(tmp_path):
    from tests.test_api import make_client, new_project
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": sample_scene_with_hvac().model_dump(mode="json")}).json()["number"]
    assert c.get(f"/api/projects/{pid}/hvac/versions").json()[0]["units"] == 2
    dom = c.get(f"/api/projects/{pid}/versions/{v}/airflow-domain").json()
    assert dom["stats"]["units"] == 2 and dom["grid"]["nz"] >= 4
    live = c.get(f"/api/projects/{pid}/hvac/live", params={"version": v}).json()
    assert live["source"] == "simulated" and set(live["units"]) == {"U_FCU-1", "U_SPLIT-1"}
    r = c.post(f"/api/projects/{pid}/hvac/telemetry", json={"readings": [{"unit": "FCU-1", "supply_air_c": 12.5, "return_air_c": 23.9}]})
    assert r.json() == {"accepted": 1, "unknown_units": []}
    live = c.get(f"/api/projects/{pid}/hvac/live", params={"version": v}).json()
    assert live["source"] == "live" and live["units"]["U_FCU-1"]["supply_air_c"] == 12.5
    forced = c.get(f"/api/projects/{pid}/hvac/live", params={"version": v, "simulated": True}).json()
    assert forced["source"] == "simulated"
    assert c.get(f"/api/projects/{pid}/versions/{v}/hvac-overlay").headers["content-type"] == "image/png"
    # an HVAC import needs a vector floor plan for automatic alignment
    up = c.post(f"/api/projects/{pid}/files", files={"file": ("ac.pdf", _tiny_pdf(), "application/pdf")}, data={"kind": "hvac"}).json()
    r = c.post(f"/api/projects/{pid}/versions/{v}/hvac", json={"file_id": up["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "HVAC_PLAN_NOT_VECTOR"


def test_ingest_token(tmp_path, monkeypatch):
    from tests.test_api import make_client, new_project
    monkeypatch.setenv("TWIN_HVAC_INGEST_TOKEN", "s3cret")
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions", json={"scene": sample_scene_with_hvac().model_dump(mode="json")})
    body = {"readings": [{"unit": "FCU-1", "supply_air_c": 13}]}
    assert c.post(f"/api/projects/{pid}/hvac/telemetry", json=body).status_code == 401
    assert c.post(f"/api/projects/{pid}/hvac/telemetry", json=body, headers={"X-Twin-Token": "s3cret"}).status_code == 200


def _tiny_pdf() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.draw_line((10, 10), (390, 10))
    return doc.tobytes()


# ---- browser solver (Node) -------------------------------------------------------------------------------

@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="Node.js not installed")
def test_browser_solver_physics(tmp_path):
    import shutil
    import subprocess
    d = build_domain(sample_scene_with_hvac(), cell=0.3)
    (tmp_path / "domain.json").write_text(json.dumps(d), encoding="utf-8")
    src = Path(__file__).parent.parent / "frontend" / "js" / "airflow" / "solver.js"
    shutil.copy(src, tmp_path / "solver.mjs")
    (tmp_path / "check.mjs").write_text(r'''
import { AirflowSolver, Particles } from "./solver.mjs";
import fs from "node:fs";
const domain = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const on = { "U_FCU-1": { on: true, supply_air_c: 13, airflow_lps: 400, return_air_c: 24 },
             "U_SPLIT-1": { on: true, supply_air_c: 14, airflow_lps: 150, return_air_c: 24 } };
const s = new AirflowSolver(domain);
s.setReadings(on, { outdoor_c: 33, occupancy: 1 });
const p = new Particles(s, 300);
const mean = () => { let a = 0, n = 0; for (let c = 0; c < s.N; c++) if (s.fluid[c]) { a += s.T[c]; n++; } return a / n; };
const t0 = mean();
for (let i = 0; i < 240; i++) { s.step(0.5); p.advance(0.5); }
const cooled = mean();
let nan = 0, solidMoving = 0;
for (let c = 0; c < s.N; c++) { if (Number.isNaN(s.T[c]) || Number.isNaN(s.u[c])) nan++; if (!s.fluid[c] && (s.u[c] || s.v[c] || s.w[c])) solidMoving++; }
const sup = domain.units[0].supplies[0].cell;
const supplyT = s.T[s.idx(sup[0], sup[1], sup[2])];
const st = s.stats();
s.setReadings({ "U_FCU-1": { on: false }, "U_SPLIT-1": { on: false } }, { outdoor_c: 33, occupancy: 1 });
for (let i = 0; i < 240; i++) s.step(0.5);
console.log(JSON.stringify({ t0, cooled, warmed: mean(), nan, solidMoving, supplyT, units: st.units, divergence: s.divergenceRms() }));
''', encoding="utf-8")
    out = subprocess.run(["node", str(tmp_path / "check.mjs"), str(tmp_path / "domain.json")], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["nan"] == 0 and r["solidMoving"] == 0
    assert r["cooled"] < r["t0"] - 1.0                  # 550 L/s of 13-14 C air cools an 80 m2 flat
    assert r["supplyT"] < 18                            # the supply cell carries the supply air
    assert r["warmed"] > r["cooled"] + 0.5             # units off: internal and envelope gains warm it up
    fcu = next(u for u in r["units"] if u["id"] == "U_FCU-1")
    assert fcu["on"] and fcu["return_c"] is not None and fcu["return_c"] > r["supplyT"]
    assert r["divergence"] < 0.5


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="Node.js not installed")
def test_browser_solver_room_domain_and_render_data(tmp_path):
    """A single room (fine cells, supply without its return): stable, mass-consistent through the relief cells,
    and the view's data (volume bytes, streamlines) is well formed."""
    import shutil
    import subprocess
    d = build_domain(sample_scene_with_hvac(), room_id="R2")
    (tmp_path / "domain.json").write_text(json.dumps(d), encoding="utf-8")
    shutil.copy(Path(__file__).parent.parent / "frontend" / "js" / "airflow" / "solver.js", tmp_path / "solver.mjs")
    (tmp_path / "check.mjs").write_text(r"""
import { AirflowSolver } from "./solver.mjs";
import fs from "node:fs";
const domain = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const s = new AirflowSolver(domain);
s.setReadings({ "U_FCU-1": { on: true, supply_air_c: 13, airflow_lps: 400, return_air_c: 24 } }, { outdoor_c: 33, occupancy: 1 });
let net = 0; for (let c = 0; c < s.N; c++) if (s.fluid[c]) net += s.src[c];
for (let i = 0; i < 160; i++) s.step();
let nan = 0, vmax = 0;
for (let c = 0; c < s.N; c++) { const v = Math.hypot(s.u[c], s.v[c], s.w[c]); if (Number.isNaN(v) || Number.isNaN(s.T[c])) nan++; if (s.fluid[c]) vmax = Math.max(vmax, v); }
const vol = s.packVolume(new Uint8Array(s.N * 4), 2, 16, 32);
let air = 0; for (let c = 0; c < s.N; c++) if (vol[c * 4 + 2] === 255) air++;
const L = s.streamlines(100, 200, () => 0.5);
let pts = 0; for (const n of L.counts) pts += n;
console.log(JSON.stringify({ net, nan, vmax, div: s.divergenceRms(), air, fluid: s.fluidCells.length, lines: L.counts.length,
  pts, posLen: L.pos.length, maxSpeed: s.stats().maxSpeed, dt: s.dt }));
""", encoding="utf-8")
    out = subprocess.run(["node", str(tmp_path / "check.mjs"), str(tmp_path / "domain.json")], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert abs(r["net"]) < 1e-3                       # supply without return is balanced by the relief cells
    assert r["nan"] == 0 and r["vmax"] <= 3.3 and r["maxSpeed"] > 0.3
    assert r["div"] < 0.5
    assert r["air"] == r["fluid"]
    assert r["lines"] > 10 and r["posLen"] == 3 * r["pts"]
    assert 0.25 <= r["dt"] <= 0.5


def test_grid_registration_across_paper_scales():
    """An HVAC sheet at 1:100 on a plan at 1:75: the plan's grid is 4/3 larger on paper; some plan lines are missing."""
    from twin.hvac.register import register_grids_scaled, scale_ratios
    hx = [540.7, 767.5, 880.9, 994.3, 1107.7, 1221.0, 1447.8]
    hy = [261.9, 312.9, 361.1, 460.3, 687.0, 800.4, 913.8]
    s, dx, dy = 100 / 75, -251.3, -354.7
    px = [s * x + dx for x in hx if x not in (880.9,)]
    py = [s * y + dy for y in hy[2:]]
    assert 1.0 in scale_ratios(75) and pytest.approx(100 / 75) in scale_ratios(75)
    r = register_grids_scaled(hx, hy, px, py, scale_ratios(75))
    assert r is not None and r.scale == pytest.approx(100 / 75) and r.dx == pytest.approx(dx, abs=0.05)
    assert r.dy == pytest.approx(dy, abs=0.05) and r.matched_x == 6 and r.matched_y == 5 and "ratio" in r.method
    same = register_grids_scaled(hx, hy, [x + 10 for x in hx], [y - 5 for y in hy], scale_ratios(100))
    assert same.scale == 1.0 and same.dx == pytest.approx(10, abs=0.05)
