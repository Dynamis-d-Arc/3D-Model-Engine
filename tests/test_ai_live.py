"""LIVE AI test (marker `live_ai`): sends the sample plan drawing to the configured provider.

Opt-in because it costs tokens:  TWIN_RUN_LIVE_AI=1 pytest -m live_ai -s
Checks that the real model returns schema-valid output that converts into a scene, and prints
how close its room dimensions are to the known measurements (accuracy is reported, the hard
assertions are only structural).
"""

import os
from pathlib import Path

import pytest

from twin.ai.client import AIClient
from twin.ai.interpret import interpret_plan
from twin.config import load_settings
from twin.fixtures import SAMPLE_BRIEF
from twin.fixtures.render_plan import render
from twin.scene.validate import validate_scene

SETTINGS = load_settings()
pytestmark = [
    pytest.mark.live_ai,
    pytest.mark.skipif(os.environ.get("TWIN_RUN_LIVE_AI") != "1", reason="set TWIN_RUN_LIVE_AI=1 to call the real AI"),
    pytest.mark.skipif(not SETTINGS.ai.configured, reason="AI not configured (TWIN_AI_API_KEY / TWIN_AI_MODEL)"),
]

KNOWN_CLEAR = {"living": (6.25, 4.25), "kitchen": (3.45, 4.25), "bedroom": (3.85, 3.45), "bath": (2.30, 3.45),
               "study": (3.45, 3.45)}


def test_live_interpretation_of_sample_plan(tmp_path):
    from twin.fixtures import load_sample_apartment
    plan = tmp_path / "plan.png"
    render(load_sample_apartment(), plan)
    out = interpret_plan(AIClient(SETTINGS.ai), SAMPLE_BRIEF, "Live test", plan, {"kind": "png"})
    scene = out.scene
    report = validate_scene(scene, SAMPLE_BRIEF)
    print(f"\nmodel {out.result.model}  usage {out.result.usage}  latency {out.result.latency_s}s")
    print(f"rooms {len(scene.rooms)} walls {len(scene.walls)} openings {len(scene.openings)} furniture {len(scene.furniture)}")
    for r in report.rooms:
        key = next((k for k in KNOWN_CLEAR if k in r.name.lower()), None)
        exp = KNOWN_CLEAR.get(key)
        print(f"  {r.name:20s} clear {r.width_clear:.2f} x {r.depth_clear:.2f}" + (f"   expected {exp[0]:.2f} x {exp[1]:.2f}" if exp else ""))
    for i in report.issues:
        if i.severity in ("error", "review"):
            print(f"  [{i.severity}] {i.message}")
    assert 4 <= len(scene.rooms) <= 7
    assert len(scene.walls) >= 4
    assert scene.provenance.source == "ai"
