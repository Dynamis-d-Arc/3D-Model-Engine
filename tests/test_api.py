"""Backend API tests (FastAPI TestClient, temporary data dir).

AI calls are MOCKED (httpx.MockTransport). The one build that succeeds launches REAL Blender
(marker `blender`); the failing-build tests run without Blender.
"""

import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.ai_mock import Recorder, responses_payload, sample_interpretation
from twin.api.app import create_app
from twin.config import load_settings

FAKE_KEY = "sk-test-THIS-IS-NOT-A-REAL-KEY-1234567890"
REAL = load_settings()


def make_client(tmp_path, *, ai=False, recorder=None, blender=True, extra=None) -> TestClient:
    env = {"TWIN_DATA_DIR": str(tmp_path / "data"), "TWIN_MAX_UPLOAD_MB": "2", "TWIN_RENDER_PREVIEWS": "0"}
    if ai:
        env.update(TWIN_AI_API_KEY=FAKE_KEY, TWIN_AI_MODEL="test-model", TWIN_AI_BASE_URL="https://ai.example.test/v1",
                   TWIN_AI_MAX_RETRIES="0")
    env["TWIN_BLENDER_PATH"] = (REAL.blender_path or "") if blender else str(tmp_path / "no-blender.exe")
    env.update(extra or {})
    settings = load_settings(env=env, env_file=None)
    transport = recorder.transport() if recorder else None
    return TestClient(create_app(settings, ai_transport=transport))


def wait_job(c: TestClient, jid: str, timeout: float = 300) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "needs_review", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {jid} did not finish")


def png_bytes(w=800, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def new_project(c: TestClient, **brief) -> str:
    r = c.post("/api/projects", json={"name": "Test flat", "brief": {"overall_width": 10.2, "overall_depth": 8.2,
                                                                     "wall_height": 2.7, **brief}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_never_exposes_key(tmp_path):
    c = make_client(tmp_path, ai=True)
    body = c.get("/api/health").json()
    assert body["ai"]["configured"] is True and body["ai"]["model"] == "test-model"
    assert FAKE_KEY not in json.dumps(body)


def test_uploads_png_jpg_pdf_and_rejections(tmp_path):
    import fitz
    c = make_client(tmp_path)
    pid = new_project(c)
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"}, files={"file": ("plan.png", png_bytes(), "image/png")})
    assert r.status_code == 201 and r.json()["sniffed"] == "png" and r.json()["width"] == 800
    jpg = io.BytesIO()
    Image.new("RGB", (640, 480), "grey").save(jpg, "JPEG")
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "reference"}, files={"file": ("ref.jpg", jpg.getvalue(), "image/jpeg")})
    assert r.status_code == 201 and r.json()["sniffed"] == "jpg"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(50, 50, 500, 700), width=3)
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"}, files={"file": ("plan.pdf", doc.tobytes(), "application/pdf")})
    assert r.status_code == 201, r.text
    assert r.json()["sniffed"] == "pdf" and r.json()["pages"] == 1 and r.json()["width"] > 1000
    fid = r.json()["id"]
    assert c.get(f"/api/projects/{pid}/files/{fid}/thumb").headers["content-type"] == "image/png"
    # content sniffing ignores the claimed name/type
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"}, files={"file": ("evil.png", b"MZ\x90\x00 not an image", "image/png")})
    assert r.status_code == 415 and r.json()["error"]["code"] == "UPLOAD_REJECTED"
    r = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"}, files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024), "image/png")})
    assert r.status_code == 413


def test_versions_are_immutable_and_validated(tmp_path, scene_dict):
    c = make_client(tmp_path)
    pid = new_project(c)
    v1 = c.post(f"/api/projects/{pid}/versions/fixture").json()
    assert v1["number"] == 1 and v1["validation"]["can_generate"] is True
    v1_file = Path(tmp_path / "data" / "projects" / pid / "versions" / "v0001.json")
    before = v1_file.read_bytes()
    scene = v1["scene"]
    scene["furniture"][0]["position"] = [1.6, 4.2]
    v2 = c.post(f"/api/projects/{pid}/versions", json={"scene": scene, "parent": 1, "note": "nudge sofa"})
    assert v2.status_code == 201 and v2.json()["number"] == 2 and v2.json()["parent"] == 1
    assert v1_file.read_bytes() == before
    bad = dict(scene, walls=[dict(scene["walls"][0], thickness=-1)])
    r = c.post(f"/api/projects/{pid}/versions", json={"scene": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "SCENE_INVALID"
    assert any("thickness" in m for m in r.json()["error"]["detail"])
    assert [v["number"] for v in c.get(f"/api/projects/{pid}/versions").json()] == [2, 1]


def test_scene_tools(tmp_path, scene_dict):
    c = make_client(tmp_path)
    r = c.post("/api/scene/validate", json={"scene": scene_dict})
    assert r.status_code == 200 and r.json()["can_generate"]
    no_walls = dict(scene_dict, walls=[], openings=[])
    r = c.post("/api/scene/derive-walls", json={"scene": no_walls})
    assert len(r.json()["scene"]["walls"]) == 7
    est = json.loads(json.dumps(scene_dict))
    est["rooms"][0]["certainty"]["status"] = "estimated"
    r = c.post("/api/scene/accept-estimates", json={"scene": est})
    assert r.json()["changed"] == 1 and r.json()["scene"]["rooms"][0]["certainty"]["status"] == "accepted_estimate"


def test_interpret_requires_configured_ai(tmp_path):
    c = make_client(tmp_path, ai=False)
    pid = new_project(c)
    r = c.post(f"/api/projects/{pid}/interpret", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_NOT_CONFIGURED"


def test_interpret_job_produces_reviewable_version(tmp_path):
    rec = Recorder(httpx.Response(200, json=responses_payload(sample_interpretation())))
    c = make_client(tmp_path, ai=True, recorder=rec)
    pid = new_project(c)
    fid = c.post(f"/api/projects/{pid}/files", data={"kind": "plan"},
                 files={"file": ("plan.png", png_bytes(), "image/png")}).json()["id"]
    job = c.post(f"/api/projects/{pid}/interpret", json={"plan_file_id": fid}).json()
    done = wait_job(c, job["id"])
    assert done["status"] == "needs_review", done
    v = c.get(f"/api/projects/{pid}/versions/{done['result']['version']}").json()
    assert v["source"] == "ai" and len(v["scene"]["rooms"]) == 5
    assert v["validation"]["can_generate"] is False   # extracted dimensions await confirmation
    assert c.get("/api/usage").json()["today"]["calls"] == 1


def test_failed_ai_job_has_meaningful_error(tmp_path):
    rec = Recorder(httpx.Response(200, json=responses_payload("not json at all")))
    c = make_client(tmp_path, ai=True, recorder=rec)
    pid = new_project(c)
    done = wait_job(c, c.post(f"/api/projects/{pid}/interpret", json={}).json()["id"])
    assert done["status"] == "failed"
    assert done["error"]["code"] == "AI_INVALID_JSON" and "not valid JSON" in done["error"]["message"]
    assert c.get(f"/api/projects/{pid}/versions").json() == []   # nothing was stored


def test_daily_ai_limit(tmp_path):
    rec = Recorder(*[httpx.Response(200, json=responses_payload("x")) for _ in range(3)])
    c = make_client(tmp_path, ai=True, recorder=rec, extra={"TWIN_AI_DAILY_REQUEST_LIMIT": "1"})
    pid = new_project(c)
    wait_job(c, c.post(f"/api/projects/{pid}/interpret", json={}).json()["id"])
    r = c.post(f"/api/projects/{pid}/interpret", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "AI_LIMIT_REACHED"


def test_build_refuses_unready_scene(tmp_path):
    c = make_client(tmp_path)
    pid = new_project(c)
    scene = c.post(f"/api/projects/{pid}/versions/fixture").json()["scene"]
    scene["rooms"][1]["certainty"]["status"] = "estimated"
    c.post(f"/api/projects/{pid}/versions", json={"scene": scene})
    r = c.post(f"/api/projects/{pid}/versions/2/build", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCENE_NOT_READY"


def test_build_without_blender_fails_cleanly(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions/fixture")
    job = c.post(f"/api/projects/{pid}/versions/1/build", json={}).json()
    done = wait_job(c, job["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "BLENDER_NOT_CONFIGURED"
    b = c.get(f"/api/projects/{pid}/builds/{job['build_id']}").json()
    assert b["status"] == "failed" and b["manifest"] is None
    r = c.get(f"/api/projects/{pid}/builds/{job['build_id']}/bundle/unreal")
    assert r.status_code == 409   # never offer downloads for a build that did not complete


def test_redesign_room_via_api(tmp_path):
    design = {"furniture": [{"key": "a", "asset_id": "bed_single", "room_key": "R3", "center": {"x": 0.7, "y": 1.75},
                             "rotation_deg": 90, "width_m": None, "depth_m": None, "height_m": None,
                             "source": "user_requested", "confidence": 0.9}],
              "surface_materials": [], "furniture_materials": [], "uncertainties": [], "summary": "Single bed."}
    rec = Recorder(httpx.Response(200, json=responses_payload(design)))
    c = make_client(tmp_path, ai=True, recorder=rec)
    pid = new_project(c)
    v1 = c.post(f"/api/projects/{pid}/versions/fixture").json()
    job = c.post(f"/api/projects/{pid}/versions/1/rooms/R3/redesign", json={"prompt": "single bed only"}).json()
    done = wait_job(c, job["id"])
    assert done["status"] == "needs_review", done
    v2 = c.get(f"/api/projects/{pid}/versions/{done['result']['version']}").json()
    others = lambda s: [f for f in s["furniture"] if f["room_id"] != "R3"]  # noqa: E731
    assert others(v2["scene"]) == others(v1["scene"]) and v2["scene"]["walls"] == v1["scene"]["walls"]
    assert [f["asset_id"] for f in v2["scene"]["furniture"] if f["room_id"] == "R3"] == ["bed_single"]


@pytest.mark.blender
@pytest.mark.skipif(not REAL.blender_path or not Path(REAL.blender_path).is_file(), reason="Blender not configured")
def test_full_build_through_api_real_blender(tmp_path):
    c = make_client(tmp_path)
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions/fixture")
    job = c.post(f"/api/projects/{pid}/versions/1/build", json={"render_previews": False}).json()
    done = wait_job(c, job["id"], timeout=600)
    assert done["status"] == "completed", done
    b = c.get(f"/api/projects/{pid}/builds/{job['build_id']}").json()
    assert b["manifest"]["verification"]["max_reimport_delta_m"] <= 0.001
    paths = {f["path"] for f in b["files"]}
    assert {"scene.blend", "export/web/scene.glb", "manifest.json"} <= paths
    r = c.get(f"/api/projects/{pid}/builds/{job['build_id']}/bundle/unreal")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "manifest.json" in z.namelist() and any(n.startswith("export/unreal/furniture/") for n in z.namelist())
    assert c.get(f"/api/projects/{pid}/builds/{job['build_id']}/files/../../versions/v0001.json").status_code == 404


def test_raw_ai_answer_is_kept_and_reprocessable(tmp_path):
    """A paid answer that fails validation is saved; after a fix it can be reprocessed for free."""
    from tests.ai_mock import mutated
    bad = mutated(lambda d: d["rooms"][1].update(key="living"))           # duplicate key -> rejected
    rec = Recorder(httpx.Response(200, json=responses_payload(bad)))     # exactly one AI response available
    c = make_client(tmp_path, ai=True, recorder=rec)
    pid = new_project(c)
    done = wait_job(c, c.post(f"/api/projects/{pid}/interpret", json={}).json()["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "AI_SCHEMA_VIOLATION"
    raw_file = tmp_path / "data" / "projects" / pid / "ai_responses" / f"{done['id']}.json"
    assert raw_file.is_file()
    # "fix" the saved answer and reprocess: no AI request is made
    raw = json.loads(raw_file.read_text())
    raw["data"]["rooms"][1]["key"] = "kitchen"
    raw_file.write_text(json.dumps(raw))
    r = c.post(f"/api/projects/{pid}/ai-responses/{done['id']}/reprocess")
    assert r.status_code == 201, r.text
    assert len(rec.requests) == 1
    v = c.get(f"/api/projects/{pid}/versions/{r.json()['version']}").json()
    assert len(v["scene"]["rooms"]) == 5
