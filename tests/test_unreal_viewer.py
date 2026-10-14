"""REAL packaged Unreal viewer tests (markers `unreal` + `blender`).

Nothing is mocked: real Blender builds the sample apartment twice (without / with the test HVAC layer) in a
backend process (`python -m twin.api`), and the PACKAGED viewer (dist/TwinViewer/Windows/TwinWalkthrough.exe,
`python -m twin.unreal.package`) loads them at runtime from that backend with -TwinTour, switches between them,
shows real BMS readings pushed to the backend, and must fail cleanly with a bad token or no server.
Skipped when the packaged viewer or Blender is not available.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.hvac_fixture import sample_scene_with_hvac
from tests.viewer_harness import VIEWER_EXE, Backend, free_port, run_viewer, wait_job
from twin.config import load_settings

SETTINGS = load_settings()


def _reason() -> str | None:
    if not VIEWER_EXE.is_file():
        return f"packaged viewer not found ({VIEWER_EXE}); run python -m twin.unreal.package"
    if not SETTINGS.blender_path or not Path(SETTINGS.blender_path).is_file():
        return "Blender not configured"
    return None


REASON = _reason()
pytestmark = [pytest.mark.unreal, pytest.mark.blender, pytest.mark.skipif(REASON is not None, reason=REASON or "")]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("viewer_e2e")
    be = Backend(root / "data").start()
    try:
        with be.client() as c:
            pid = c.post("/api/projects", json={"name": "Viewer test apartment"}).json()["id"]
            v1 = c.post(f"/api/projects/{pid}/versions/fixture").json()["number"]
            v2 = c.post(f"/api/projects/{pid}/versions", json={"scene": sample_scene_with_hvac().model_dump(mode="json"),
                                                               "parent": v1, "note": "with HVAC", "source": "edit"}).json()["number"]
            builds = {}
            for v in (v1, v2):
                job = c.post(f"/api/projects/{pid}/versions/{v}/build", json={"render_previews": False}).json()
                done = wait_job(c, job["id"])
                assert done["status"] == "completed", done
                builds[v] = job["build_id"]
            manifests = {v: c.get(f"/api/projects/{pid}/builds/{b}").json()["manifest"] for v, b in builds.items()}
        yield {"backend": be, "root": root, "pid": pid, "plain": builds[v1], "hvac": builds[v2], "manifests": manifests,
               "v1": v1, "v2": v2}
    finally:
        be.stop()


@pytest.fixture(scope="module")
def tour(served):
    """One packaged run: the HVAC build with real BMS readings, then a switch to the plain build."""
    be, pid = served["backend"], served["pid"]
    with be.client() as c:
        r = c.post(f"/api/projects/{pid}/hvac/telemetry", json={"readings": [
            {"unit": "FCU-1", "on": True, "mode": "cool", "setpoint_c": 24, "supply_air_c": 13.5, "return_air_c": 24.2,
             "fan_speed": 0.7, "airflow_lps": 280},
            {"unit": "SPLIT-1", "on": False, "mode": "off", "setpoint_c": 25, "return_air_c": 26.0}]}).json()
        assert r["accepted"] == 2
    launch = be.launch(pid, served["hvac"])["launch"]
    out = served["root"] / "tour_main"
    code, report = run_viewer(launch, out, extra=[f"-TwinSwitchTo={pid}/{served['plain']}"])
    return code, report, out


def test_packaged_viewer_loads_and_validates_in_unreal(served, tour):
    code, r, _ = tour
    assert code == 0 and r is not None, r
    assert r["runtime"].startswith("viewer") and r["with_editor"] is False  # the packaged game, not the editor
    m = served["manifests"][served["v2"]]
    v = r["viewer"]["validation"]
    assert v["ok"], v
    assert v["checked"] == len(m["objects"]) + len(m["furniture"]) and v["max_deviation_cm"] <= 1.0
    assert v["rooms_with_floor"] == v["rooms_expected"] == len(m["rooms"])
    assert v["viewer_materials_found"] is True and v["server_preflight"]["ok"] is True
    assert r["twin_actors"] == r["viewer"]["actors"] + 1  # + the project info actor


def test_packaged_viewer_walkthrough(tour):
    _, r, out = tour
    nav = r["navigation"]
    assert nav["valid"] is True and all(x["reachable"] for x in nav["reachability"][1:]), nav
    assert r["walk"]["arrived"] is True, r["walk"]
    assert r["wall_collision"]["stayed_inside_room"] is True, r["wall_collision"]
    assert r["look"]["ok"] is True, r["look"]   # mouse / arrow-key look turns the view and the turn sticks
    assert r["selection"]["selected"] is True and r["selection"]["kind"] == "furniture"
    for shot in r["screenshots"]:
        assert (out / shot).stat().st_size > 10_000, shot


def test_packaged_viewer_hvac_identity_and_live_data(tour):
    _, r, _ = tour
    h = r["hvac"]
    assert h["selected"] is True and h["selected_kind"] == "hvac_unit", h
    assert h["unit_actors"] == h["unit_actors_matched"] == h["build_units"] == 2, h
    assert h["telemetry_source"] == "live" and h["telemetry_measured"] is True
    # the reading shown for the selected unit is the one pushed for that tag
    sel = h["selected_reading"]
    assert sel["tag"] == h["selected_tag"]
    if sel["tag"] == "FCU-1":
        assert sel["supply_air_c"] == pytest.approx(13.5) and sel["on"] is True
    else:
        assert sel["on"] is False
    a = r["airflow"]
    assert a["kind"] == "illustration" and "not a CFD result" in a["disclaimer"] and a["dots"] > 0 and a["units"] == 2
    # the mist (the web Live tab's temperature cloud): one zone per unit, driven by the feed; SPLIT-1 is off, so
    # neither its mist nor its tracers are drawn; FCU-1 takes its own return-air temperature
    mist = a["mist"]
    assert mist["material"] is True and mist["params_ok"] is True and mist["zones"] == 2 and mist["driver"] == "feed", mist
    assert mist["visible"] == 1, mist
    assert mist["temp_min_c"] == pytest.approx(24.2, abs=0.01) and mist["temp_max_c"] == pytest.approx(24.2, abs=0.01)
    assert mist["mask_w"] > 2 and mist["mask_h"] > 2
    assert a["dots_moving"] * 2 == a["dots"], a


def test_packaged_viewer_switches_buildings(served, tour):
    _, r, _ = tour
    s = r["switch"]
    m = served["manifests"][served["v1"]]
    assert s["ready"] is True and s["build_id"] == served["plain"], s
    assert s["validation"]["ok"] and s["actors"] == len(m["objects"]) + len(m["furniture"])
    assert s["twin_actors_in_world"] == s["actors"] + 1  # nothing of the previous building is left


def test_packaged_viewer_reuses_its_cache(served, tour):
    launch = served["backend"].launch(served["pid"], served["hvac"])["launch"]
    code, r = run_viewer(launch, served["root"] / "tour_cache")
    assert code == 0 and r["viewer"]["from_cache"] is True and r["viewer"]["validation"]["ok"]


@pytest.mark.parametrize("case", ["bad_token", "expired", "no_server"])
def test_packaged_viewer_load_errors_exit_cleanly(served, case):
    launch = dict(served["backend"].launch(served["pid"], served["hvac"])["launch"])
    expected = {"bad_token": "TOKEN_INVALID", "expired": "TOKEN_EXPIRED", "no_server": "SERVER_UNREACHABLE"}[case]
    if case == "bad_token":
        launch["token"] = launch["token"][:-4] + ("AAAA" if not launch["token"].endswith("AAAA") else "BBBB")
    elif case == "expired":
        launch["expires_at"] = int(time.time()) - 60
    else:
        launch["server"] = f"http://127.0.0.1:{free_port()}"
    code, r = run_viewer(launch, served["root"] / f"err_{case}", timeout_s=300)
    assert code == 3 and r is not None and r["load_error"]["code"] == expected, r


def test_package_contains_no_backend_secrets():
    from twin.unreal.package import scan_for_secrets
    assert scan_for_secrets(VIEWER_EXE.parent, SETTINGS) == []
