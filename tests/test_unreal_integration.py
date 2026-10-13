"""REAL Unreal Engine integration tests (marker `unreal`).

Builds the sample apartment with Blender, imports it into the TwinWalkthrough template with the
headless editor, then plays the map with -TwinTour and checks navigation, wall collision,
selection and screenshots. Skipped when UE or the compiled template module is not available.
"""

from pathlib import Path

import pytest

from twin.config import load_settings
from twin.unreal.runner import UnrealError, run_import, run_tour, unreal_paths

SETTINGS = load_settings()


def _ue_ready() -> str | None:
    try:
        p = unreal_paths(SETTINGS)
    except UnrealError as exc:
        return str(exc)
    if not p.module_dll.is_file():
        return f"walkthrough C++ module not compiled ({p.module_dll.name}); run python -m twin.unreal.setup"
    if not SETTINGS.blender_path or not Path(SETTINGS.blender_path).is_file():
        return "Blender not configured"
    return None


REASON = _ue_ready()
pytestmark = [pytest.mark.unreal, pytest.mark.blender, pytest.mark.skipif(REASON is not None, reason=REASON or "")]


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    from twin.fixtures import load_sample_apartment
    from twin.pipeline import run_build
    out = tmp_path_factory.mktemp("ue_build")
    build = run_build(load_sample_apartment(), out, SETTINGS, build_id="b_ue_test", render=False)
    report = run_import(SETTINGS, build.build_dir, "p_uetest")
    return build, report


def test_import_matches_scene_model(imported):
    build, report = imported
    assert report["status"] == "ok"
    assert report["actors"] == len(build.manifest["objects"]) + len(build.manifest["furniture"])
    assert report["max_bounds_deviation_cm"] <= 1.0
    assert not report["warnings"] or all("complex collision" in w for w in report["warnings"])


def test_reimport_reuses_meshes(imported):
    build, _ = imported
    again = run_import(SETTINGS, build.build_dir, "p_uetest")
    assert again["imported"] == [] and len(again["reused"]) == len(build.manifest["exports"])


def test_walkthrough_tour(imported, tmp_path):
    build, report = imported
    tour = run_tour(SETTINGS, report["map"], tmp_path / "tour")
    assert tour["twin_actors"] == report["actors"] + 1  # + the project info actor
    assert len(tour["rooms"]) == len(build.manifest["rooms"])
    assert tour["navigation"]["valid"] is True, tour["navigation"]
    others = tour["navigation"]["reachability"][1:]  # every other room from the first room's spawn point
    assert others and all(r["reachable"] for r in others), others
    assert tour["walk"]["arrived"] is True, tour["walk"]
    assert tour["wall_collision"]["stayed_inside_room"] is True, tour["wall_collision"]
    assert tour["selection"]["selected"] is True and tour["selection"]["kind"] == "furniture"
    for shot in tour["screenshots"]:
        assert (tmp_path / "tour" / shot).stat().st_size > 10_000, shot


def test_unreal_import_job_through_api(tmp_path):
    """The backend route runs import + tour as a job and reports the checks."""
    import time

    from fastapi.testclient import TestClient

    from twin.api.app import create_app
    env = {"TWIN_DATA_DIR": str(tmp_path / "data"), "TWIN_BLENDER_PATH": SETTINGS.blender_path,
           "TWIN_UNREAL_ENGINE_DIR": SETTINGS.unreal_engine_dir, "TWIN_UNREAL_PROJECT": str(SETTINGS.unreal_project),
           "TWIN_RENDER_PREVIEWS": "0"}
    c = TestClient(create_app(load_settings(env=env, env_file=None)))

    def wait(jid, timeout=1200):
        end = time.time() + timeout
        while time.time() < end:
            j = c.get(f"/api/jobs/{jid}").json()
            if j["status"] in ("completed", "failed", "needs_review"):
                return j
            time.sleep(1)
        raise AssertionError("job timed out")

    pid = c.post("/api/projects", json={"name": "UE API test"}).json()["id"]
    c.post(f"/api/projects/{pid}/versions/fixture")
    build = c.post(f"/api/projects/{pid}/versions/1/build", json={"render_previews": False}).json()
    assert wait(build["id"])["status"] == "completed"
    job = c.post(f"/api/projects/{pid}/builds/{build['build_id']}/unreal/import").json()
    done = wait(job["id"])
    assert done["status"] == "completed", done.get("error")
    r = done["result"]
    assert r["max_bounds_deviation_cm"] <= 1.0
    assert r["tour"]["navigation"]["valid"] and r["tour"]["walk"]["arrived"] and r["tour"]["wall_collision"]["stayed_inside_room"]
    shot = c.get(f"/api/projects/{pid}/builds/{build['build_id']}/files/unreal/tour/overview.png")
    assert shot.status_code == 200 and len(shot.content) > 10_000
