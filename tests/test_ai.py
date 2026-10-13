"""AI service tests. ALL MOCKED: httpx.MockTransport replays canned provider responses.

No network access and no API key are needed. Real provider checks live in
test_ai_live.py (marker `live_ai`, opt-in).
"""

import json
import logging
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from tests.ai_mock import Recorder, chat_payload, mutated, responses_payload, sample_interpretation
from twin.ai.client import AIClient, AIError
from twin.ai.interpret import interpret_plan, redesign_room
from twin.config import RedactSecrets, load_settings
from twin.fixtures import SAMPLE_BRIEF, load_sample_apartment
from twin.scene.validate import validate_scene

FAKE_KEY = "sk-test-THIS-IS-NOT-A-REAL-KEY-1234567890"


def settings(**overrides):
    base = load_settings(env={"TWIN_AI_API_KEY": FAKE_KEY, "TWIN_AI_MODEL": "test-model",
                              "TWIN_AI_BASE_URL": "https://ai.example.test/v1", "TWIN_AI_MAX_RETRIES": "3"},
                         env_file=None).ai
    return replace(base, **overrides)


def client(rec: Recorder, **overrides) -> AIClient:
    return AIClient(settings(**overrides), transport=rec.transport(), sleep=lambda s: None)


# ---- credentials ---------------------------------------------------------------------------

def test_key_from_environment_and_fallback(tmp_path):
    s = load_settings(env={"OPENAI_API_KEY": FAKE_KEY, "TWIN_AI_MODEL": "m"}, env_file=None).ai
    assert s.configured and s.key_source == "OPENAI_API_KEY"
    assert s.api_key.get_secret_value() == FAKE_KEY


def test_env_file_and_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nTWIN_AI_API_KEY="sk-from-file-123456789"\nTWIN_AI_MODEL=file-model\n')
    s = load_settings(env={}, env_file=env_file).ai
    assert s.api_key.get_secret_value() == "sk-from-file-123456789" and s.model == "file-model"
    s2 = load_settings(env={"TWIN_AI_MODEL": "env-model"}, env_file=env_file).ai
    assert s2.model == "env-model"  # real environment wins


def test_missing_key_is_reported_not_guessed():
    s = load_settings(env={}, env_file=None).ai
    assert not s.configured and s.api_key is None
    with pytest.raises(AIError) as exc:
        AIClient(s).structured(system="x", parts=[], schema_name="x", schema={})
    assert exc.value.code == "AI_NOT_CONFIGURED"


def test_key_never_exposed():
    s = settings()
    assert FAKE_KEY not in json.dumps(s.public())
    assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s.api_key)
    assert isinstance(s.api_key, SecretStr)


def test_log_redaction():
    f = RedactSecrets([FAKE_KEY])
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "auth header Bearer %s and %s", (FAKE_KEY, "sk-otherkey12345678"), None)
    f.filter(rec)
    assert FAKE_KEY not in rec.getMessage() and "sk-otherkey" not in rec.getMessage()


def test_request_shape_and_auth_header():
    rec = Recorder(httpx.Response(200, json=responses_payload(sample_interpretation())))
    out = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    req = rec.requests[0]
    assert req.url.path == "/v1/responses"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert FAKE_KEY not in req.content.decode()
    assert body["model"] == "test-model" and body["store"] is False
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert out.result.usage["total_tokens"] == 4600


# ---- conversion of a correct interpretation ---------------------------------------------------

def test_interpretation_reproduces_known_dimensions():
    rec = Recorder(httpx.Response(200, json=responses_payload(sample_interpretation())))
    out = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    scene = out.scene
    report = validate_scene(scene, SAMPLE_BRIEF)
    assert report.ok, [i.message for i in report.issues if i.severity == "error"]
    got = {r.name: (r.width_clear, r.depth_clear) for r in report.rooms}
    assert got["Bedroom"] == pytest.approx((3.85, 3.45), abs=1e-3)
    assert got["Kitchen"] == pytest.approx((3.45, 4.25), abs=1e-3)
    # plan-label readings need confirmation; generation is gated
    assert {r.certainty.status for r in scene.rooms} == {"extracted"}
    assert not report.can_generate
    assert scene.provenance.source == "ai" and scene.provenance.ai_model == "test-model"
    fixture = load_sample_apartment()
    assert sorted((o.wall_id, o.offset) for o in scene.openings) == sorted((o.wall_id, o.offset) for o in fixture.openings)
    assert scene.room("R4").finishes.floor and scene.room("R1").finishes.floor != scene.room("R4").finishes.floor


def test_offwall_opening_becomes_question_not_silent_fix():
    data = mutated(lambda d: d["openings"][0].update(center={"x": 3.0, "y": 6.5}))   # 1.5 m inside the living room
    rec = Recorder(httpx.Response(200, json=responses_payload(data)))
    scene = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None).scene
    assert len(scene.openings) == len(data["openings"]) - 1
    assert any("not added" in q.question for q in scene.open_questions)


# ---- invalid AI output is rejected ------------------------------------------------------------

@pytest.mark.parametrize("payload, code", [
    (responses_payload("Sure! Here is the plan: rooms..."), "AI_INVALID_JSON"),
    (responses_payload("[1, 2, 3]"), "AI_INVALID_JSON"),
    (responses_payload(mutated(lambda d: d.pop("rooms"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d.update(colour="red"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["furniture"][0].update(asset_id="hot_tub"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["walls"][0].update(thickness_m=3.0))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["walls"][0].update(thickness_m="thick"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["openings"][0].update(wall_key="nope"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["furniture"][0].update(room_key="garage"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["rooms"][0].update(polygon=[{"x": 0, "y": 0}, {"x": 1, "y": 1}]))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["surface_materials"][0].update(base_color_hex="oak"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["rooms"][1].update(key="living"))), "AI_SCHEMA_VIOLATION"),
    (responses_payload(mutated(lambda d: d["rooms"][0]["polygon"][0].update(x=float("1e9")))), "AI_SCHEMA_VIOLATION"),
    (responses_payload({}, refusal="I can't help with that."), "AI_REFUSED"),
    (responses_payload("{\"plan_readable\": tr", status="incomplete", incomplete_reason="max_output_tokens"), "AI_INCOMPLETE"),
    (responses_payload(mutated(lambda d: d.update(plan_readable=False, unreadable_reason="photo of a cat", rooms=[],
                                                  walls=[], openings=[], furniture=[], surface_materials=[]))), "AI_PLAN_UNREADABLE"),
])
def test_invalid_output_rejected(payload, code):
    rec = Recorder(httpx.Response(200, json=payload))
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == code
    assert len(rec.requests) == 1  # invalid content is not blindly retried


def test_self_intersecting_ai_room_fails_validation_not_conversion():
    data = mutated(lambda d: d["rooms"][2].update(polygon=[{"x": 0, "y": 0}, {"x": 4, "y": 3.6}, {"x": 4, "y": 0}, {"x": 0, "y": 3.6}]))
    rec = Recorder(httpx.Response(200, json=responses_payload(data)))
    scene = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None).scene
    assert "E_ROOM_POLYGON_INVALID" in {i.code for i in validate_scene(scene).issues}


# ---- transport errors, retries, timeouts --------------------------------------------------------

def test_retry_on_429_then_success():
    rec = Recorder(httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}}),
                   httpx.Response(200, json=responses_payload(sample_interpretation())))
    out = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert out.result.attempts == 2 and len(rec.requests) == 2


def test_5xx_exhausts_retries():
    rec = Recorder(*[httpx.Response(503, json={"error": {"message": "overloaded"}}) for _ in range(4)])
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == "AI_UNAVAILABLE" and len(rec.requests) == 4   # 1 + 3 retries


def test_timeout_is_retried_then_reported():
    rec = Recorder(*[httpx.ReadTimeout("slow") for _ in range(4)])
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == "AI_TIMEOUT" and len(rec.requests) == 4


@pytest.mark.parametrize("status, message, code", [
    (401, "Incorrect API key provided", "AI_AUTH"),
    (403, "project lacks access", "AI_AUTH"),
    (404, "The model `test-model` does not exist", "AI_MODEL_NOT_FOUND"),
    (400, "Invalid image_url: image input is not supported for this model", "AI_IMAGE_UNSUPPORTED"),
    (400, "Invalid schema for response_format", "AI_BAD_REQUEST"),
])
def test_client_errors_are_not_retried(status, message, code):
    rec = Recorder(httpx.Response(status, json={"error": {"message": message}}))
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == code and len(rec.requests) == 1
    assert FAKE_KEY not in str(exc.value)


def test_chat_completions_style():
    rec = Recorder(httpx.Response(200, json=chat_payload(sample_interpretation())))
    out = interpret_plan(client(rec, api_style="chat_completions"), SAMPLE_BRIEF, "Sample", plan_image=None)
    body = json.loads(rec.requests[0].content)
    assert rec.requests[0].url.path == "/v1/chat/completions"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert len(out.scene.rooms) == 5


def test_chat_truncation():
    rec = Recorder(httpx.Response(200, json=chat_payload("{\"rooms\": [", finish_reason="length")))
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec, api_style="chat_completions"), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == "AI_INCOMPLETE"


def test_plan_image_is_sent(tmp_path):
    from PIL import Image
    p = tmp_path / "plan.png"
    Image.new("RGB", (3000, 2000), "white").save(p)
    rec = Recorder(httpx.Response(200, json=responses_payload(sample_interpretation())))
    interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=p, plan_info={"kind": "png"})
    content = json.loads(rec.requests[0].content)["input"][0]["content"]
    images = [c for c in content if c["type"] == "input_image"]
    assert len(images) == 1 and images[0]["image_url"].startswith("data:image/png;base64,")
    assert any("2048x1365" in c.get("text", "") for c in content)  # downscaled to the model limit


# ---- room redesign -------------------------------------------------------------------------------

def _room_design(room_key="R3"):
    return {"furniture": [
        {"key": "a", "asset_id": "bed_single", "room_key": room_key, "center": {"x": 0.7, "y": 1.75}, "rotation_deg": 90,
         "width_m": None, "depth_m": None, "height_m": None, "source": "user_requested", "confidence": 0.9},
        {"key": "b", "asset_id": "desk", "room_key": room_key, "center": {"x": 2.8, "y": 0.46}, "rotation_deg": 180,
         "width_m": None, "depth_m": None, "height_m": None, "source": "ai_suggested", "confidence": 0.8}],
        "surface_materials": [{"target": "floor", "room_key": None, "description": "walnut", "base_color_hex": "#5C3E2A",
                               "roughness": 0.5, "metallic": 0.0, "pattern": "wood"}],
        "furniture_materials": [], "uncertainties": [], "summary": "Guest room with a desk."}


def test_redesign_changes_only_one_room():
    scene = load_sample_apartment()
    rec = Recorder(httpx.Response(200, json=responses_payload(_room_design())))
    updated, _, info = redesign_room(client(rec), scene, "R3", "turn this into a guest room with a desk")
    assert [f for f in updated.furniture if f.room_id != "R3"] == [f for f in scene.furniture if f.room_id != "R3"]
    assert sorted(f.asset_id for f in updated.furniture if f.room_id == "R3") == ["bed_single", "desk"]
    assert updated.walls == scene.walls and updated.openings == scene.openings
    assert updated.room("R3").finishes.floor is not None and updated.room("R1") == scene.room("R1")
    assert info["summary"].startswith("Guest room")


def test_redesign_rejects_items_for_other_rooms():
    scene = load_sample_apartment()
    rec = Recorder(httpx.Response(200, json=responses_payload(_room_design(room_key="R1"))))
    with pytest.raises(AIError) as exc:
        redesign_room(client(rec), scene, "R3", "x")
    assert exc.value.code == "AI_SCHEMA_VIOLATION"


# ---- prose length: shortened with a note, never a reason to discard a valid answer --------------

def test_overlong_prose_is_trimmed_not_rejected():
    long = "x" * 900
    data = mutated(lambda d: (d.update(scale_basis=long), d["surface_materials"][0].update(description=long)))
    rec = Recorder(httpx.Response(200, json=responses_payload(data)))
    out = interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert len(out.scene.rooms) == 5
    assert any("scale_basis" in n and "shortened" in n for n in out.notes)
    assert any("surface_materials.0.description" in n for n in out.notes)


def test_trimming_never_touches_geometry():
    from twin.ai.schema import trim_free_text
    data = mutated(lambda d: d["walls"][0].update(thickness_m=3.0))
    assert trim_free_text(data) == []
    rec = Recorder(httpx.Response(200, json=responses_payload(data)))
    with pytest.raises(AIError) as exc:
        interpret_plan(client(rec), SAMPLE_BRIEF, "Sample", plan_image=None)
    assert exc.value.code == "AI_SCHEMA_VIOLATION"
