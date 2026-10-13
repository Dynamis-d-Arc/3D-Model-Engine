"""REAL integration tests: launch headless Blender (marker `blender`).

Skipped automatically when Blender is not installed/configured (TWIN_BLENDER_PATH).
"""

import json
from pathlib import Path

import pytest

from tests.conftest import mutate
from twin.config import load_settings
from twin.pipeline import run_build

SETTINGS = load_settings()
pytestmark = [
    pytest.mark.blender,
    pytest.mark.skipif(not SETTINGS.blender_path or not Path(SETTINGS.blender_path).is_file(),
                       reason="Blender not configured (set TWIN_BLENDER_PATH)"),
]


@pytest.fixture(scope="module")
def first_build(tmp_path_factory):
    from twin.fixtures import load_sample_apartment
    out = tmp_path_factory.mktemp("build_a")
    result = run_build(load_sample_apartment(), out, SETTINGS, build_id="test-a", render=False)
    return result


def test_build_produces_verified_outputs(first_build):
    out = first_build.build_dir
    m = first_build.manifest
    for rel in ("scene.blend", "export/fbx/scene_full.fbx", "export/web/scene.glb", "manifest.json"):
        assert (out / rel).stat().st_size > 1000, rel
    assert len(m["exports"]) == 60
    for e in m["exports"]:
        assert (out / e["file"]).is_file()
    verify = json.loads((out / "verify_report.json").read_text())
    assert verify["failures"] == []
    assert verify["max_delta_m"] <= 0.001
    assert verify["blend"]["missing"] == [] and verify["blend"]["unpacked_images"] == []


def test_manifest_room_dimensions_match_fixture(first_build):
    rooms = {r["name"]: r for r in first_build.manifest["rooms"]}
    assert rooms["Bedroom"]["dims_clear"] == pytest.approx([3.85, 3.45], abs=1e-3)
    assert rooms["Bathroom"]["dims_centreline"] == pytest.approx([2.4, 3.6], abs=1e-3)


def test_blender_report_matches_plan(first_build):
    report = json.loads((first_build.build_dir / "blender_report.json").read_text())
    plan = json.loads((first_build.build_dir / "plan.json").read_text())
    w1 = report["objects"]["ARCH_Wall_W1"]["bounds"]
    expected = next(o for o in plan["objects"] if o["name"] == "ARCH_Wall_W1")["expected_bounds"]
    assert w1[0] + w1[1] == pytest.approx(expected[0] + expected[1], abs=1e-4)
    sofa = report["furniture"]["FURN_R1_F1_sofa_3seat"]
    assert sofa["dims"] == pytest.approx([2.2, 0.95, 0.85], abs=1e-4)
    assert sofa["location"] == pytest.approx([1.5, 4.2, 0.0], abs=1e-4)


def test_room_edit_rebuild_reuses_unrelated_exports(first_build, scene_dict, tmp_path):
    """Change only the bedroom floor finish: every other export must be reused byte-for-byte."""
    edited = mutate(scene_dict, lambda d: d["rooms"][2].update(finishes={"floor": "walnut_planks"}))
    out = tmp_path / "build_b"
    result = run_build(edited, out, SETTINGS, build_id="test-b", previous_build=first_build.build_dir, render=False)
    rebuilt = {e["file"] for e in result.manifest["exports"]} - set(result.reused)
    assert rebuilt == {"export/unreal/arch/ARCH_Floor_R3.fbx"}
    before = {e["file"]: e["sha256"] for e in first_build.manifest["exports"]}
    after = {e["file"]: e["sha256"] for e in result.manifest["exports"]}
    changed = sorted(f for f in after if after[f] != before.get(f))
    assert changed == ["export/unreal/arch/ARCH_Floor_R3.fbx"]
    # the first build's files are untouched (new build -> new directory)
    assert (first_build.build_dir / "manifest.json").read_text() == json.dumps(first_build.manifest, indent=1)
