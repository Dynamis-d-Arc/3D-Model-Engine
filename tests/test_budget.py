"""Spending cap (twin/ai/budget.py). ALL MOCKED: no network, no real key."""

import sqlite3
from dataclasses import replace

import httpx
import pytest

from tests.ai_mock import Recorder, responses_payload, sample_interpretation
from tests.test_api import make_client, new_project
from twin.ai.budget import BudgetGuard
from twin.ai.client import AIClient, AIError, ContentPart
from twin.api.storage import Storage
from twin.config import load_settings

FAKE_KEY = "sk-test-THIS-IS-NOT-A-REAL-KEY-1234567890"
PARTS = [ContentPart("text", "hello")]
SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def ai_settings(**over):
    base = load_settings(env={"TWIN_AI_API_KEY": FAKE_KEY, "TWIN_AI_MODEL": "test-model",
                              "TWIN_AI_BASE_URL": "https://ai.example.test/v1", "TWIN_AI_MAX_RETRIES": "0",
                              "TWIN_AI_PRICE_INPUT_PER_1M": "1.0", "TWIN_AI_PRICE_OUTPUT_PER_1M": "2.0",
                              "TWIN_AI_MAX_OUTPUT_TOKENS": "20000", "TWIN_AI_BUDGET_USD": "10"}, env_file=None).ai
    return replace(base, **over)


def client(tmp_path, rec, **over):
    s = ai_settings(**over)
    storage = Storage(tmp_path / "data")
    return AIClient(s, transport=rec.transport(), sleep=lambda _: None,
                    budget=BudgetGuard(storage, s, purpose="test")), storage


def call(c):
    return c.structured(system="sys", parts=PARTS, schema_name="t", schema=SCHEMA)


def ok_response():
    return httpx.Response(200, json=responses_payload({}))  # usage: 1200 input, 3400 output tokens


def test_settings_parse_budget_and_prices():
    s = ai_settings()
    assert s.budget_usd == 10.0 and s.price_input_per_1m == 1.0 and s.price_output_per_1m == 2.0
    with pytest.raises(ValueError):
        load_settings(env={"TWIN_AI_BUDGET_USD": "ten"}, env_file=None)


def test_cap_without_prices_sends_nothing(tmp_path):
    rec = Recorder(ok_response())
    c, _ = client(tmp_path, rec, price_output_per_1m=None)
    with pytest.raises(AIError) as exc:
        call(c)
    assert exc.value.code == "AI_BUDGET_PRICES_MISSING" and rec.requests == []


def test_cap_without_tracker_sends_nothing():
    rec = Recorder(ok_response())
    c = AIClient(ai_settings(), transport=rec.transport())
    with pytest.raises(AIError) as exc:
        call(c)
    assert exc.value.code == "AI_BUDGET_UNTRACKED" and rec.requests == []


def test_actual_cost_is_recorded(tmp_path):
    rec = Recorder(ok_response())
    c, storage = client(tmp_path, rec)
    call(c)
    expected = 1200 / 1e6 * 1.0 + 3400 / 1e6 * 2.0      # = $0.008
    assert c.last_cost_usd == pytest.approx(expected)
    assert storage.ai_spent_usd() == pytest.approx(expected)


def test_worst_case_over_cap_is_refused_before_sending(tmp_path):
    rec = Recorder(ok_response())
    c, storage = client(tmp_path, rec, budget_usd=0.03)   # worst case > 20000 * $2/1M = $0.04
    with pytest.raises(AIError) as exc:
        call(c)
    assert exc.value.code == "AI_BUDGET_EXCEEDED" and rec.requests == [] and storage.ai_spent_usd() == 0


def test_spend_never_exceeds_cap(tmp_path):
    cap = 0.08
    rec = Recorder(*[ok_response() for _ in range(20)])
    c, storage = client(tmp_path, rec, budget_usd=cap)
    sent = 0
    with pytest.raises(AIError) as exc:
        for _ in range(20):
            call(c)
            sent += 1
            assert storage.ai_spent_usd() <= cap
    assert exc.value.code == "AI_BUDGET_EXCEEDED"
    assert len(rec.requests) == sent and storage.ai_spent_usd() <= cap
    assert storage.ai_spent_usd() + exc.value.detail["worst_case_usd"] > cap


def test_timeout_is_charged_worst_case(tmp_path):
    rec = Recorder(httpx.ReadTimeout("slow"))
    c, storage = client(tmp_path, rec)
    with pytest.raises(AIError) as exc:
        call(c)
    assert exc.value.code == "AI_TIMEOUT"
    assert storage.ai_spent_usd() >= 20000 / 1e6 * 2.0   # at least the full output allowance


def test_connection_failure_is_free(tmp_path):
    rec = Recorder(httpx.ConnectError("refused"))
    c, storage = client(tmp_path, rec)
    with pytest.raises(AIError):
        call(c)
    assert storage.ai_spent_usd() == 0


def test_rejected_attempt_costs_nothing_and_retry_is_charged_once(tmp_path):
    rec = Recorder(httpx.Response(429, json={"error": {"message": "slow down"}}), ok_response())
    c, storage = client(tmp_path, rec, max_retries=1)
    call(c)
    assert storage.ai_spent_usd() == pytest.approx(0.008)
    with sqlite3.connect(storage.db_path) as db:
        rows = db.execute("SELECT status, cost_usd FROM ai_calls ORDER BY id").fetchall()
    assert rows == [("http_429", 0.0), ("ok", pytest.approx(0.008))]


def test_concurrent_reservations_cannot_overrun(tmp_path):
    s = ai_settings(budget_usd=0.07)
    storage = Storage(tmp_path / "data")
    g1, g2 = BudgetGuard(storage, s), BudgetGuard(storage, s)
    rid, worst = g1.reserve(1000)            # ~$0.041 reserved
    assert worst == pytest.approx(1000 / 1e6 + 0.04)
    with pytest.raises(AIError) as exc:
        g2.reserve(1000)                      # would bring the total over $0.07
    assert exc.value.code == "AI_BUDGET_EXCEEDED"
    g1.settle(rid, status="ok", usage={"input_tokens": 1000, "output_tokens": 500})
    rid2, _ = g2.reserve(1000)                # fits again once the first one settled at its real cost
    g2.settle(rid2, status="not_sent")


def test_api_refuses_when_cap_reached(tmp_path):
    c = make_client(tmp_path, ai=True, extra={"TWIN_AI_BUDGET_USD": "0.001", "TWIN_AI_PRICE_INPUT_PER_1M": "1",
                                               "TWIN_AI_PRICE_OUTPUT_PER_1M": "2"})
    storage = c.app.state.storage
    storage.record_ai_call(project_id=None, job_id=None, model="m", api_style="responses", status="ok", cost_usd=0.002)
    health = c.get("/api/health").json()["ai_budget"]
    assert health["cap_usd"] == 0.001 and health["spent_usd"] == 0.002 and health["remaining_usd"] == 0
    pid = new_project(c)
    r = c.post(f"/api/projects/{pid}/interpret", json={})
    assert r.status_code == 402 and r.json()["error"]["code"] == "AI_BUDGET_EXCEEDED"


def test_api_refuses_without_prices(tmp_path):
    c = make_client(tmp_path, ai=True, extra={"TWIN_AI_BUDGET_USD": "10"})
    pid = new_project(c)
    r = c.post(f"/api/projects/{pid}/interpret", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_BUDGET_PRICES_MISSING"


def test_interpret_job_reports_cost(tmp_path):
    from tests.test_api import wait_job
    rec = Recorder(httpx.Response(200, json=responses_payload(sample_interpretation())))
    c = make_client(tmp_path, ai=True, recorder=rec, extra={"TWIN_AI_BUDGET_USD": "10", "TWIN_AI_PRICE_INPUT_PER_1M": "1",
                                                              "TWIN_AI_PRICE_OUTPUT_PER_1M": "2"})
    pid = new_project(c)
    done = wait_job(c, c.post(f"/api/projects/{pid}/interpret", json={}).json()["id"])
    assert done["status"] == "needs_review" and done["result"]["cost_usd"] == pytest.approx(0.008)
    assert c.get("/api/usage").json()["budget"]["spent_usd"] == pytest.approx(0.008)


def test_old_database_is_migrated(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "twin.sqlite") as db:
        db.execute("CREATE TABLE ai_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, job_id TEXT, model TEXT, "
                   "api_style TEXT, status TEXT NOT NULL, error_code TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                   "total_tokens INTEGER, latency_s REAL, created_at REAL NOT NULL)")
    storage = Storage(data)
    storage.record_ai_call(project_id=None, job_id=None, model="m", api_style="responses", status="ok", cost_usd=0.5)
    assert storage.ai_spent_usd() == 0.5


def test_cache_writes_are_charged_with_their_surcharge():
    from twin.ai.budget import usage_tokens
    assert usage_tokens({"input_tokens": 8000, "output_tokens": 100}) == (8000, 100)
    u = {"input_tokens": 8000, "input_tokens_details": {"cache_write_tokens": 8000, "cached_tokens": 0}, "output_tokens": 100}
    assert usage_tokens(u) == (10000, 100)        # 1.25x input price on the cache-written part
