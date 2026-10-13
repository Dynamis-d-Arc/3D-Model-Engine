"""Airflow illustration: zone model, MATLAB results, API, and the browser
modules (paths, thermal drivers) in Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.hvac_fixture import sample_scene_with_hvac
from twin.hvac.illustration import ThermalResultsError, airflow_zones, validate_thermal_results

JS = Path(__file__).parent.parent / "frontend" / "js" / "airflow"
NODE = shutil.which("node")


def fake_results(floor_tags=("FCU-1", "SPLIT-1")) -> dict:
    """TEST DATA in the study's results format: 2 setpoints, one floor, 481 minutes cooling from 30 C."""
    def series(sp):
        return [[30 - (30 - sp) * min(1, m / 120), 30 - (30 - sp - 0.5) * min(1, m / 120), 4.0 - m / 480, m * 0.05, 1.5]
                for m in range(481)]
    return {"engine": {"name": "MATLAB", "version": "test", "generatedAt": "2026-10-13T00:00:00Z"},
            "options": {"coolingKW": 7, "inputKW": 2},
            "building": {"floors": [{"floor": 1, "splitX": 0.0, "unitCount": 2, "airflowLs": 550, "tags": list(floor_tags)}]},
            "scenarios": [{"setpoint": sp, "floors": [{"floor": 1, "series": series(sp)}]} for sp in (22, 25)]}


def test_zone_model_of_the_sample_apartment():
    m = airflow_zones(sample_scene_with_hvac())
    assert len(m["outline"]) <= 64 and m["ceiling"] == pytest.approx(2.7, abs=0.6)
    x0, z0, x1, z1 = m["bounds"]
    assert x1 > x0 and z1 > z0 and z1 <= 0.5           # viewer frame: z = -plan y
    assert m["floors"][0]["splitX"] == pytest.approx((x0 + x1) / 2) and m["floors"][0]["unitCount"] == 2
    zones = {z["tag"]: z for z in m["zones"]}
    for z in zones.values():
        bx0, bz0, bx1, bz1 = z["b"]
        assert x0 < bx0 < bx1 < x1 and z0 < bz0 < bz1 < z1 and bx1 - bx0 <= 6.0 + 1e-6
    fcu = zones["FCU-1"]
    # the ducted unit's supply duct leaves its +x end: the jet leaves the discharge face 8 cm clear, along +x
    assert fcu["outlet"]["dir"] == pytest.approx([1.0, 0.0, 0.0], abs=1e-6)
    assert fcu["outlet"]["o"][0] == pytest.approx(3.0 + 0.6 + 0.08, abs=1e-3)
    assert fcu["outlet"]["o"][2] == pytest.approx(-6.5, abs=1e-3)
    split = zones["SPLIT-1"]
    assert split["outlet"]["dir"][2] > 0.9               # a wall unit blows into its own room (plan -y)
    assert {r["id"] for r in m["rooms"]} == {r.id for r in sample_scene_with_hvac().rooms}


def test_thermal_results_validation():
    s = validate_thermal_results(fake_results())
    assert s["setpoints"] == [22, 25] and s["floors"] == [1] and s["minutes"] == 480 and s["tags"][1] == ["FCU-1", "SPLIT-1"]
    bad = fake_results()
    bad["engine"]["name"] = "Excel"
    with pytest.raises(ThermalResultsError):
        validate_thermal_results(bad)
    bad = fake_results()
    bad["scenarios"][0]["floors"][0]["series"] = [[25]]
    with pytest.raises(ThermalResultsError):
        validate_thermal_results(bad)
    with pytest.raises(ThermalResultsError):
        validate_thermal_results({"engine": {"name": "MATLAB"}})


def test_airflow_zone_and_thermal_results_api(tmp_path):
    from tests.test_api import make_client, new_project
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": sample_scene_with_hvac().model_dump(mode="json")}).json()["number"]
    z = c.get(f"/api/projects/{pid}/versions/{v}/airflow-zones").json()
    assert len(z["zones"]) == 2 and z["floors"][0]["unitCount"] == 2
    assert c.get(f"/api/projects/{pid}/hvac/thermal-results").status_code == 404
    r = c.put(f"/api/projects/{pid}/hvac/thermal-results", files={"file": ("results.json", json.dumps(fake_results()), "application/json")})
    assert r.status_code == 200 and r.json()["setpoints"] == [22, 25]
    assert c.get(f"/api/projects/{pid}/hvac/thermal-results").json()["engine"]["name"] == "MATLAB"
    r = c.put(f"/api/projects/{pid}/hvac/thermal-results", files={"file": ("x.json", b"{not json", "application/json")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_JSON"
    assert c.delete(f"/api/projects/{pid}/hvac/thermal-results").json() == {"deleted": True}
    assert c.get(f"/api/projects/{pid}/hvac/thermal-results").status_code == 404


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_paths_and_thermal_drivers_in_node(tmp_path):
    model = airflow_zones(sample_scene_with_hvac())
    (tmp_path / "model.json").write_text(json.dumps(model), encoding="utf-8")
    (tmp_path / "results.json").write_text(json.dumps(fake_results()), encoding="utf-8")
    for name in ("school-flow.js", "thermal.js"):
        shutil.copy(JS / name, tmp_path / name.replace(".js", ".mjs"))
    (tmp_path / "check.mjs").write_text(r"""
import fs from "node:fs";
import { paths, sources, inside, SAMPLES } from "./school-flow.mjs";
import { zoneValues, matlabState, liveState, matchFloor, FULL_DAY } from "./thermal.mjs";
const model = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const results = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = { zones: [] };
for (const r of model.zones) {
  const ps = paths(r), src = sources(r);
  let outside = 0, outBox = 0, yBad = 0;
  const top = r.outlet.ceiling - 0.05;
  for (const p of ps) for (let k = 0; k < SAMPLES; k++) {
    const x = p[k * 3], y = p[k * 3 + 1], z = p[k * 3 + 2];
    if (!inside(r, x, z)) outside++;
    if (x < r.b[0] || x > r.b[2] || z < r.b[1] || z > r.b[3]) outBox++;
    if (y < 0.22 - 1e-6 || y > top + 1e-6) yBad++;
  }
  const o = src[0].o, first = [ps[0][0], ps[0][1], ps[0][2]];
  out.zones.push({ tag: r.tag, loops: ps.length, len: ps[0].length, outside, outBox, yBad, outletY: o[1], top,
    startGap: Math.hypot(first[0] - o[0], first[2] - o[2]), dirs: src[0].dirs });
  // without a known discharge face: the study's rule (away from the nearest box edge, along an axis)
  const bare = { ...r, outlet: undefined };
  out.bareDirs = sources(bare)[0].dirs;
  out.bareLoops = paths(bare).length;
}
const floor = matchFloor(results, model);
const d = matlabState(results, 25, 60, model, floor);
const { values, drops } = zoneValues(model, d);
out.floor = floor; out.values = values; out.drops = drops; out.row = d.floorSeries.series[60];
const readings = { "U_FCU-1": { on: true, room_temp_c: 26.5 }, "U_SPLIT-1": { on: false, room_temp_c: 29 } };
const L = liveState(model, readings);
out.live = { hidden: [...L.hidden], values: L.values, drops: L.drops, running: L.running, zones: L.zones, minute: L.minute, full: FULL_DAY };
console.log(JSON.stringify(out));
""", encoding="utf-8")
    res = subprocess.run([NODE, str(tmp_path / "check.mjs"), str(tmp_path / "model.json"), str(tmp_path / "results.json")],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    r = json.loads(res.stdout.strip().splitlines()[-1])
    for z in r["zones"]:
        assert z["loops"] == 6 and z["len"] == 192 * 3                      # 6 loops per outlet, 192 samples each
        assert z["outside"] == 0 and z["outBox"] == 0 and z["yBad"] == 0     # inside floor polygon, box, 0.22..ceiling
        assert z["outletY"] <= z["top"] + 1e-6 and z["startGap"] < 0.6        # loops start at the (contained) outlet
    assert r["zones"][0]["dirs"] == [[1, 0, 0]]
    assert len(r["bareDirs"]) == 1 and sum(abs(v) for v in r["bareDirs"][0]) == 1 and r["bareLoops"] == 6
    # MATLAB mode: both units are on the same floor; values follow west/east of the split; the drop formula
    assert r["floor"] == 1
    west, east, power = r["row"][0], r["row"][1], r["row"][2]
    split = model["floors"][0]["splitX"]
    for z in model["zones"]:
        t = west if z["units"][0][0] < split else east
        assert r["values"][z["key"]] == pytest.approx([t, t])
        cooling = max(0, power - 2 * 0.06) * (7 / 2) * 1000
        assert r["drops"][z["key"]] == pytest.approx(min(max(0, t - 12), cooling / (550 / 1000 * 1.2 * 1005)))
    # live mode: a unit that is off is not drawn; a running one takes its own temperature and 7 kW of cooling
    lv = r["live"]
    assert lv["hidden"] == ["U_SPLIT-1"] and lv["running"] == 1 and lv["minute"] == lv["full"] == 480
    assert lv["values"]["U_FCU-1"] == [26.5, 26.5]
    assert lv["drops"]["U_FCU-1"] == pytest.approx(min(26.5 - 12, 7000 / (400 / 1000 * 1.2 * 1005)))
    assert lv["zones"][0]["powerKW"] == pytest.approx(1 * 2 + 2 * 0.06)
