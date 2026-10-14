"""'Fix problems': rules + the AI problem solver (twin.ai.solve) and their API. AI calls are MOCKED."""

import json

import httpx
import pytest

from tests.ai_mock import Recorder, responses_payload
from tests.conftest import mutate
from twin.ai.client import AIResult
from twin.ai.solve import SYSTEM_SOLVE, ai_problems, solve_problems
from twin.config import load_settings
from twin.scene.problems import AI_CODES, PROBLEMS, describe
from twin.scene.validate import validate_scene


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def structured(self, **kw):
        self.calls.append(kw)
        return AIResult(self.answers.pop(0), "", "fake-codex", "req1", {})


def without_w7(scene_dict):
    def fn(d):
        d["walls"] = [w for w in d["walls"] if w["id"] != "W7"]
        d["openings"] = [o for o in d["openings"] if o["wall_id"] != "W7"]
    return mutate(scene_dict, fn)


def op(kind, coll, ident, data, reason="#1"):
    return {"op": kind, "collection": coll, "id": ident, "data_json": json.dumps(data) if data is not None else "",
            "reason": reason}


def test_every_validation_code_has_a_plain_description():
    import inspect
    import re
    import twin.scene.sensors as sensors
    import twin.scene.validate as v
    src = inspect.getsource(v) + inspect.getsource(sensors)
    used = set(re.findall(r'"([EWIRQ]_[A-Z_]+)"', src))
    assert used <= set(PROBLEMS), used - set(PROBLEMS)
    assert describe("W_FURNITURE_SCALE")["fix"] == "rule" and "W_ROOM_UNREACHABLE" in AI_CODES
    assert "W_ROOM_DIM_MISMATCH" not in AI_CODES   # never "fixed" by changing the user's own figures


def test_solver_sees_problems_entities_and_scene(scene_dict):
    s = without_w7(scene_dict)
    todo = ai_problems(validate_scene(s))
    assert {i.code for i in todo} == {"W_ROOM_EDGE_NO_WALL"}
    answer = {"summary": "The living room and kitchen are one open space.", "questions": [], "operations": [
        op("update", "rooms", "R1", {"open_plan": True}), op("update", "rooms", "R2", {"open_plan": True}),
        op("update", "rooms", "R4", {"open_plan": True}), op("update", "rooms", "R5", {"open_plan": True})]}
    fc = FakeClient([answer])
    out = solve_problems(fc, s, todo)
    assert fc.calls[0]["system"] == SYSTEM_SOLVE and fc.calls[0]["schema_name"] == "scene_problem_fix"
    texts = [p.text for p in fc.calls[0]["parts"]]
    assert texts[0].startswith("PROBLEMS TO SOLVE:\n#1 W_ROOM_EDGE_NO_WALL (Room edges without a wall)")
    assert any(t.startswith("ENTITIES INVOLVED:") for t in texts) and any(t.startswith("SCENE MODEL") for t in texts)
    assert {p["targets"][0] for p in out.solved} == {f"room:{r}" for r in ("R1", "R2", "R4", "R5")} & {i.targets[0] for i in todo}
    assert out.rejected is None and len(out.changes) == 4
    assert "W_ROOM_EDGE_NO_WALL" not in {i.code for i in validate_scene(out.scene).issues}


def test_edits_that_add_errors_are_sent_back_then_dropped(scene_dict):
    s = without_w7(scene_dict)
    todo = ai_problems(validate_scene(s))
    overlap = op("add", "walls", "W9", {"level_id": "L0", "start": [0, 0], "end": [4, 0], "thickness": 0.1})
    fc = FakeClient([{"summary": "x", "questions": [], "operations": [overlap]}] * 2)
    out = solve_problems(fc, s, todo)
    assert len(fc.calls) == 2 and "BECAUSE:\nE_WALL_OVERLAP" in fc.calls[1]["parts"][-1].text
    assert out.scene == s and out.changes == [] and "new error" in out.rejected
    # a corrected second answer is used
    good = op("update", "rooms", "R2", {"open_plan": True})
    out = solve_problems(FakeClient([{"summary": "x", "questions": [], "operations": [overlap]},
                                     {"summary": "ok", "questions": [], "operations": [good]}]), s, todo)
    assert out.rejected is None and out.scene.room("R2").open_plan


def test_solver_settings_default_to_codex_with_its_prices():
    ai = load_settings(env={"TWIN_AI_API_KEY": "sk-x", "TWIN_AI_MODEL": "gpt-6-astra"}, env_file=None).ai
    sv = ai.solver()
    assert (sv.model, sv.price_input_per_1m, sv.price_output_per_1m, sv.reasoning_effort) == ("gpt-5.3-codex", 1.75, 14.0, "medium")
    other = load_settings(env={"TWIN_AI_API_KEY": "sk-x", "TWIN_AI_MODEL": "m", "TWIN_AI_SOLVER_MODEL": "other-model"},
                          env_file=None).ai.solver()
    assert other.model == "other-model" and other.price_input_per_1m is None   # unknown price: the cap fails closed
    assert ai.public()["solver_model"] == "gpt-5.3-codex" and "sk-x" not in json.dumps(ai.public())


# ---- API ------------------------------------------------------------------------------------------------

def test_autofix_and_missing_walls_tools(tmp_path, scene_dict):
    from tests.test_api import make_client
    c = make_client(tmp_path, blender=False)
    d = json.loads(json.dumps(scene_dict))
    next(f for f in d["furniture"] if f["id"] == "F25")["position"] = [8.2, 0.3]
    r = c.post("/api/scene/autofix", json={"scene": d}).json()
    assert [a["code"] for a in r["actions"]] == ["W_FURNITURE_CROSSES_BOUNDARY"] and r["report"]["ok"]
    s = without_w7(scene_dict).model_dump(mode="json")
    r = c.post("/api/scene/add-missing-walls", json={"scene": s, "room_id": "R2"}).json()
    assert len(r["scene"]["walls"]) > len(s["walls"]) and r["notes"]
    assert c.post("/api/scene/add-missing-walls", json={"scene": s, "room_id": "R99"}).status_code == 404
    cat = c.get("/api/catalog").json()
    assert cat["problems"]["W_FURNITURE_SCALE"]["title"] == "Furniture stretched or squashed"
    assert "W_FURNITURE_SCALE" in cat["rule_codes"]


def test_fix_problems_job_rules_then_ai(tmp_path, scene_dict):
    from tests.test_api import make_client, new_project, wait_job
    # the study armchair stands in the bathroom (rule) and W7 is missing (AI decides: open plan)
    def fn(d):
        d["walls"] = [w for w in d["walls"] if w["id"] != "W7"]
        d["openings"] = [o for o in d["openings"] if o["wall_id"] != "W7"]
        next(f for f in d["furniture"] if f["id"] == "F25")["position"] = [8.2, 0.3]
    s = mutate(scene_dict, fn)
    answer = {"summary": "The four rooms open onto each other.", "questions": ["Is there a door to the study?"],
              "operations": [op("update", "rooms", r, {"open_plan": True}) for r in ("R1", "R2", "R4", "R5")]}
    rec = Recorder(httpx.Response(200, json=responses_payload(answer)))
    c = make_client(tmp_path, ai=True, recorder=rec, blender=False)
    pid = new_project(c)
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": s.model_dump(mode="json")}).json()["number"]
    est = c.get(f"/api/projects/{pid}/versions/{v}/solve/estimate").json()
    assert est["rule_fixable"] == 1 and est["ai_problems"] >= 2 and est["model"] == "gpt-5.3-codex" and est["est_usd"] > 0
    # rules only: one version, no AI call
    job = wait_job(c, c.post(f"/api/projects/{pid}/versions/{v}/solve", json={"use_ai": False}).json()["id"])
    assert job["status"] == "needs_review" and job["result"]["ai"] is None and not rec.requests
    assert [f["code"] for f in job["result"]["rule_fixes"]] == ["W_FURNITURE_CROSSES_BOUNDARY"]
    # rules + AI
    job = wait_job(c, c.post(f"/api/projects/{pid}/versions/{v}/solve", json={}).json()["id"])
    r = job["result"]
    assert job["status"] == "needs_review", job
    assert json.loads(rec.requests[0].content)["model"] == "gpt-5.3-codex"
    assert r["ai"]["changes"] and r["ai"]["questions"] == ["Is there a door to the study?"]
    assert r["before"]["warning"] > r["after"].get("warning", 0)
    meta = c.get(f"/api/projects/{pid}/versions/{r['version']}").json()
    assert meta["source"] == "auto_fix" and meta["parent"] == v and "AI edit" in meta["note"]
    assert not any(i["code"] == "W_ROOM_EDGE_NO_WALL" for i in meta["validation"]["issues"])


def test_fix_problems_keeps_rule_fixes_when_the_ai_fails(tmp_path, scene_dict):
    from tests.test_api import make_client, new_project, wait_job
    def fn(d):
        d["walls"] = [w for w in d["walls"] if w["id"] != "W7"]
        d["openings"] = [o for o in d["openings"] if o["wall_id"] != "W7"]
        next(f for f in d["furniture"] if f["id"] == "F25")["position"] = [8.2, 0.3]
    s = mutate(scene_dict, fn)
    rec = Recorder(httpx.Response(401, json={"error": {"message": "bad key"}}))
    c = make_client(tmp_path, ai=True, recorder=rec, blender=False)
    pid = new_project(c)
    v = c.post(f"/api/projects/{pid}/versions", json={"scene": s.model_dump(mode="json")}).json()["number"]
    job = wait_job(c, c.post(f"/api/projects/{pid}/versions/{v}/solve", json={}).json()["id"])
    assert job["status"] == "needs_review" and job["result"]["ai"]["error"]["code"] == "AI_AUTH"
    assert job["result"]["rule_fixes"] and job["result"]["version"] == v + 1


def test_ai_can_add_a_door(scene_dict):
    """Openings hang off a wall (no level_id): an AI 'add' must not get one injected (it was always rejected)."""
    from twin.ai.fix import apply_operations
    from twin.scene.model import Scene
    s = mutate(scene_dict, lambda d: None)
    door = {"kind": "door", "wall_id": "W6", "offset": 1.8, "width": 0.9, "height": 2.1, "opens_into": "R3"}
    data, changes = apply_operations(s, [op("add", "openings", "O20", door)], "test")
    out = Scene.model_validate(data)
    assert next(o for o in out.openings if o.id == "O20").wall_id == "W6"
    data, _ = apply_operations(s, [op("add", "walls", "W20", {"start": [1, 1], "end": [2, 1], "thickness": 0.1})], "test")
    assert next(w for w in data["walls"] if w["id"] == "W20")["level_id"] == "L0"
