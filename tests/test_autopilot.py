"""One drop zone, furniture lists and the autopilot (one click from drawings to a build).

AI calls are MOCKED (httpx.MockTransport); Blender is switched off, so a run stops with the layout ready.
"""

import io
import json
from pathlib import Path

import httpx
from PIL import Image

from tests.ai_mock import Recorder, responses_payload, sample_interpretation
from tests.test_api import make_client, new_project, png_bytes, wait_job
from twin.scene.furniture_sheet import import_furniture_sheet
from twin.scene.model import Scene
from twin.scene.review import accept_estimates, accept_open_questions
from twin.scene.validate import validate_scene

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "angled_apartment"


def photo_bytes() -> bytes:
    img = Image.new("RGB", (300, 200))
    img.putdata([((x * 7) % 256, (y * 5) % 256, 120) for y in range(200) for x in range(300)])
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def pdf_bytes(pages: list[str]) -> bytes:
    import fitz
    doc = fitz.open()
    for title in pages:
        page = doc.new_page(width=842, height=595)
        page.insert_text((40, 40), title)
        if "FLOOR PLAN" in title:
            for i in range(60):
                page.draw_rect(fitz.Rect(100 + i * 5, 100, 140 + i * 5, 300))
    data = doc.tobytes()
    doc.close()
    return data


def up(c, pid, name, data, kind="auto", page="0"):
    return c.post(f"/api/projects/{pid}/files", data={"kind": kind, "page": page}, files={"file": (name, data)})


# ---- furniture list --------------------------------------------------------------------------

def test_furniture_list_reproduces_the_reference_layout():
    truth = Scene.model_validate_json((SAMPLE / "scene_truth.json").read_text(encoding="utf-8"))
    empty = truth.model_copy(update={"furniture": []})
    scene, info = import_furniture_sheet(empty, (SAMPLE / "furniture_list.csv").read_text(encoding="utf-8"))
    assert info == {"added": 45, "removed": 0, "problems": []}
    for f in scene.furniture:
        t = min(truth.furniture, key=lambda q: (q.position[0] - f.position[0]) ** 2 + (q.position[1] - f.position[1]) ** 2)
        assert (t.asset_id, t.room_id) == (f.asset_id, f.room_id)
        assert abs(t.position[0] - f.position[0]) < 0.002 and abs(t.position[1] - f.position[1]) < 0.002
        assert f.certainty.status == "confirmed" and f.certainty.source == "user"
    assert validate_scene(scene).ok


def test_furniture_list_replaces_ai_items_keeps_hand_placed_and_reports_bad_rows(scene_dict):
    d = json.loads(json.dumps(scene_dict))
    d["furniture"][0]["certainty"] = {"status": "confirmed", "source": "user", "confidence": None, "note": ""}
    for f in d["furniture"][1:]:
        f["certainty"] = {"status": "estimated", "source": "ai", "confidence": 0.5, "note": ""}
    scene = Scene.model_validate(d)
    text = ("id,room,item,catalog_id,x_m,y_m,rotation_deg\n"
            f"F1,{scene.rooms[0].name},Sofa,sofa,2.0,2.0,90\n"   # an alias, and an id already taken
            "F9,,Lamp,floor_lamp,1.0,1.0,0\n"                  # room from the position
            "F10,,Spaceship,spaceship,1.0,1.0,0\n"
            "F11,,Desk,desk,99,99,0\n")
    out, info = import_furniture_sheet(scene, text)
    assert info["added"] == 2 and info["removed"] == len(scene.furniture) - 1
    assert len(info["problems"]) == 2 and "spaceship" in info["problems"][0] and "outside every room" in info["problems"][1]
    ids = [f.id for f in out.furniture]
    assert ids[0] == scene.furniture[0].id and len(set(ids)) == 3
    sofa = next(f for f in out.furniture if f.asset_id == "sofa_3seat" and f.certainty.note == "furniture list")
    assert sofa.rotation_deg == 90 and sofa.id != "F1"


def test_accept_helpers_leave_blocking_questions(scene_dict):
    d = Scene.model_validate(scene_dict).model_dump(mode="json")
    d["walls"][0]["certainty"]["status"] = "estimated"
    d["open_questions"] = [{"id": "Q1", "target": "", "question": "Is it a door?", "severity": "warning",
                            "resolved": False, "answer": ""},
                           {"id": "Q2", "target": "", "question": "Which floor?", "severity": "blocking",
                            "resolved": False, "answer": ""}]
    assert accept_estimates(d) == [f"wall:{d['walls'][0]['id']}"]
    assert [q["id"] for q in accept_open_questions(d)] == ["Q1"]
    assert d["open_questions"][1]["resolved"] is False


# ---- one drop zone ---------------------------------------------------------------------------

def test_auto_upload_sorts_every_kind(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    r = up(c, pid, "floor_plan.pdf", (SAMPLE / "floor_plan.pdf").read_bytes()).json()
    assert r["kind"] == "plan" and r["classified"] and r["calibration"]          # vector plan: calibrated by itself
    assert up(c, pid, "hvac_plan.pdf", (SAMPLE / "hvac_plan.pdf").read_bytes()).json()["kind"] == "hvac"
    assert up(c, pid, "sheet-2.pdf", (SAMPLE / "hvac_plan.pdf").read_bytes()).json()["kind"] == "hvac"   # by its text
    assert up(c, pid, "scan.png", png_bytes()).json()["kind"] == "plan"           # white paper
    assert up(c, pid, "living.jpg", photo_bytes()).json()["kind"] == "reference"
    r = up(c, pid, "furniture_list.csv", (SAMPLE / "furniture_list.csv").read_bytes()).json()
    assert r["kind"] == "furniture_list" and r["sniffed"] == "csv"
    r = up(c, pid, "sensors.csv", c.get("/api/sensors/template.csv").content).json()
    assert r["kind"] == "sensor_sheet"
    r = up(c, pid, "other.csv", b"a,b,c\n1,2,3\n")
    assert r.status_code == 415 and "catalog_id" in r.json()["error"]["message"]
    assert up(c, pid, "x.csv", b"tag,kind\nT1,co2\n", kind="furniture_list").status_code == 415
    # the user corrects the sorting on the thumbnail
    photo = [f for f in c.get(f"/api/projects/{pid}").json()["files"] if f["kind"] == "reference"][0]
    assert c.patch(f"/api/projects/{pid}/files/{photo['id']}", json={"kind": "plan"}).json()["kind"] == "plan"
    assert c.patch(f"/api/projects/{pid}/files/{photo['id']}", json={"kind": "hvac"}).status_code == 415


def test_pdf_page_is_picked_automatically(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    data = pdf_bytes(["COVER SHEET - DRAWING LIST", "LEVEL 2 FLOOR PLAN 1:100", "SECTION A-A"])
    r = up(c, pid, "set.pdf", data).json()
    assert r["page"] == 1 and r["pages"] == 3 and "page 2 of 3" in r["page_reason"]
    assert up(c, pid, "set.pdf", data, page="3").json()["page"] == 2              # an explicit page still wins


def test_import_furniture_endpoint(tmp_path, scene_dict):
    c = make_client(tmp_path, blender=False)
    text = "catalog_id,x,y\nfloor_lamp,1.0,1.0\n"
    r = c.post("/api/scene/import-furniture", json={"scene": scene_dict, "text": text, "replace": False}).json()
    assert r["added"] == 1 and len(r["scene"]["furniture"]) == len(scene_dict["furniture"]) + 1


# ---- autopilot ---------------------------------------------------------------------------------

def interpreting_client(tmp_path, *answers):
    rec = Recorder(*(answers or (httpx.Response(200, json=responses_payload(sample_interpretation())),)))
    return make_client(tmp_path, ai=True, recorder=rec, blender=False), rec


def test_autopilot_plan_says_what_will_run(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    plan = c.get(f"/api/projects/{pid}/autopilot/plan").json()
    assert plan["blocked"] and "floor plan" in plan["blocked"]
    up(c, pid, "plan.png", png_bytes())
    plan = c.get(f"/api/projects/{pid}/autopilot/plan").json()
    assert "AI" in plan["blocked"]                                          # reading a plan needs the AI
    r = c.post(f"/api/projects/{pid}/autopilot", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    c, _ = interpreting_client(tmp_path / "ai")
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    up(c, pid, "hvac.pdf", (SAMPLE / "hvac_plan.pdf").read_bytes())
    steps = {s["key"]: s for s in c.get(f"/api/projects/{pid}/autopilot/plan").json()["steps"]}
    assert list(steps) == ["layout", "hvac", "furniture", "fix", "accept", "build"]
    assert not steps["hvac"]["run"] and "vector PDF" in steps["hvac"]["note"]  # a PNG plan cannot carry HVAC
    assert not steps["build"]["run"] and "Blender" in steps["build"]["note"]
    review = c.get(f"/api/projects/{pid}/autopilot/plan?review_first=true").json()["steps"]
    assert [s["key"] for s in review][-2:] == ["fix", "review"]


def test_autopilot_reads_fixes_accepts_and_stops_without_blender(tmp_path):
    c, rec = interpreting_client(tmp_path)
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    job = c.post(f"/api/projects/{pid}/autopilot", json={"use_ai": False}).json()
    assert c.post(f"/api/projects/{pid}/autopilot", json={}).status_code in (202, 409)
    done = wait_job(c, job["id"])
    r = done["result"]
    assert done["status"] == "needs_review", done
    st = {s["key"]: s["status"] for s in r["steps"]}
    assert st["layout"] == "done" and st["fix"] == "done" and st["accept"] == "done" and st["build"] == "skipped"
    assert r["stopped"]["code"] == "BLENDER_MISSING" and r["assumptions"]
    v = c.get(f"/api/projects/{pid}/versions/{r['version']}").json()
    assert v["source"] == "autopilot" and v["validation"]["can_generate"] is True
    assert len(rec.requests) == 1                                            # one AI call: the reading
    sources = [x["source"] for x in c.get(f"/api/projects/{pid}/versions").json()]
    assert sources[-1] == "ai" and sources[0] == "autopilot"                 # every step kept as its own version


def test_autopilot_review_first_stops_before_accepting(tmp_path):
    c, _ = interpreting_client(tmp_path)
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={"use_ai": False, "review_first": True}).json()["id"])
    assert done["status"] == "needs_review" and done["result"]["stopped"]["code"] == "REVIEW_FIRST"
    assert {s["key"] for s in done["result"]["steps"]} == {"layout", "furniture", "fix"}
    # "Finish automatically" from that version: no new AI reading, accept, then stop at the build
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={"use_ai": False}).json()["id"])
    assert [s["key"] for s in done["result"]["steps"]] == ["fix", "accept", "build"]
    assert done["result"]["stopped"]["code"] == "BLENDER_MISSING"


def test_autopilot_places_the_furniture_list_once(tmp_path, scene_dict):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    d = Scene.model_validate(scene_dict).model_dump(mode="json")
    for f in d["furniture"]:   # as the AI suggested them
        f["certainty"] = {"status": "estimated", "source": "ai", "confidence": 0.5, "note": ""}
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": d}).json()["number"]
    up(c, pid, "list.csv", b"catalog_id,x,y\nfloor_lamp,1.0,1.0\n")
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={"from_version": v}).json()["id"])
    steps = {s["key"]: s for s in done["result"]["steps"]}
    assert steps["furniture_list"]["status"] == "done" and "1 item from" in steps["furniture_list"]["note"]
    assert "layout" not in steps
    scene = c.get(f"/api/projects/{pid}/versions/{done['result']['version']}").json()["scene"]
    assert [f["asset_id"] for f in scene["furniture"]] == ["floor_lamp"]
    again = c.get(f"/api/projects/{pid}/autopilot/plan").json()["steps"]
    assert "furniture_list" not in [s["key"] for s in again]                 # not applied a second time


def test_autopilot_stops_when_a_blocking_question_remains(tmp_path):
    ans = sample_interpretation()
    ans["uncertainties"] = [{"target": "room:kitchen", "question": "Is the kitchen part of this unit?", "severity": "blocking"}]
    c, _ = interpreting_client(tmp_path, httpx.Response(200, json=responses_payload(ans)))
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={"use_ai": False}).json()["id"])
    r = done["result"]
    assert done["status"] == "needs_review" and r["stopped"]["code"] == "NEEDS_YOU"
    assert "question" in r["stopped"]["message"] and any(i["code"] == "Q_OPEN_QUESTION" for i in r["remaining"])
    assert {s["key"]: s["status"] for s in r["steps"]}["build"] == "skipped"


def test_autopilot_reports_a_failed_reading(tmp_path):
    c, _ = interpreting_client(tmp_path, httpx.Response(200, json=responses_payload("not json")))
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={}).json()["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "AI_INVALID_JSON"
    assert done["error"]["detail"]["steps"][0]["status"] == "failed"


def test_only_autopilot_runs_can_be_stopped(tmp_path):
    c, _ = interpreting_client(tmp_path)
    pid = new_project(c)
    up(c, pid, "plan.png", png_bytes())
    job = c.post(f"/api/projects/{pid}/interpret", json={}).json()
    assert c.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    wait_job(c, job["id"])


def test_autopilot_keeps_the_ai_furniture_when_the_list_does_not_fit(tmp_path, scene_dict):
    c = make_client(tmp_path, blender=False)
    pid = new_project(c)
    d = Scene.model_validate(scene_dict).model_dump(mode="json")
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": d}).json()["number"]
    up(c, pid, "list.csv", b"catalog_id,x,y\nfloor_lamp,1.0,1.0\nsofa,90,90\ndesk,91,91\n")   # 2 of 3 rows elsewhere
    done = wait_job(c, c.post(f"/api/projects/{pid}/autopilot", json={"from_version": v}).json()["id"])
    step = {s["key"]: s for s in done["result"]["steps"]}["furniture_list"]
    assert step["status"] == "skipped" and "did not fit" in step["note"]
    scene = c.get(f"/api/projects/{pid}/versions/{done['result']['version']}").json()["scene"]
    assert len(scene["furniture"]) == len(d["furniture"])
