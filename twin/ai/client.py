"""Minimal client for OpenAI-compatible structured output (Responses or Chat Completions API).

Design rules:
  * endpoint, model and API style come from configuration (twin.config.AISettings);
  * the key is only placed in the Authorization header and never logged or returned;
  * timeouts on every request; retries with exponential backoff (+Retry-After) for
    timeouts, connection errors, 408/409/429 and 5xx; no retry for other 4xx;
  * refusals, truncated output and non-JSON text become typed AIError codes.
Schema validation of the parsed JSON happens in the caller (twin.ai.interpret).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from twin.ai.budget import BudgetGuard, estimate_input_tokens
from twin.config import AISettings

log = logging.getLogger("twin.ai")

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class AIError(RuntimeError):
    """code: AI_NOT_CONFIGURED | AI_AUTH | AI_MODEL_NOT_FOUND | AI_BAD_REQUEST | AI_IMAGE_UNSUPPORTED |
    AI_RATE_LIMITED | AI_TIMEOUT | AI_UNAVAILABLE | AI_REFUSED | AI_INCOMPLETE | AI_INVALID_JSON |
    AI_SCHEMA_VIOLATION | AI_LIMIT_REACHED | AI_PROTOCOL | AI_BUDGET_EXCEEDED | AI_BUDGET_PRICES_MISSING |
    AI_BUDGET_UNTRACKED"""

    def __init__(self, code: str, message: str, *, status: int | None = None, retryable: bool = False,
                 detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.detail = detail


@dataclass
class ContentPart:
    kind: str           # "text" | "image"
    text: str = ""
    image_url: str = ""  # data: URL
    detail: str = "high"


@dataclass
class AIResult:
    data: dict
    raw_text: str
    model: str
    request_id: str | None
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    attempts: int = 1


def _provider_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:500]
        if isinstance(err, str):
            return err[:500]
    except (ValueError, json.JSONDecodeError):
        pass
    return resp.text[:300]


class AIClient:
    def __init__(self, settings: AISettings, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, budget: BudgetGuard | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self.budget = budget          # reserves and records the cost of every HTTP attempt (twin.ai.budget)
        self.last_cost_usd: float | None = None

    # ---- request building ----------------------------------------------------------------
    def _body(self, system: str, parts: list[ContentPart], schema_name: str, schema: dict,
              max_output_tokens: int | None = None) -> tuple[str, dict]:
        s = self.settings
        mot = max_output_tokens or s.max_output_tokens
        if s.api_style == "responses":
            content = []
            for p in parts:
                if p.kind == "text":
                    content.append({"type": "input_text", "text": p.text})
                else:
                    content.append({"type": "input_image", "image_url": p.image_url, "detail": p.detail})
            body: dict[str, Any] = {
                "model": s.model, "instructions": system,
                "input": [{"role": "user", "content": content}],
                "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
                "max_output_tokens": mot, "store": False,
            }
            if s.service_tier:
                body["service_tier"] = s.service_tier
            if s.reasoning_effort:
                body["reasoning"] = {"effort": s.reasoning_effort}
            return "/responses", body
        content = []
        for p in parts:
            if p.kind == "text":
                content.append({"type": "text", "text": p.text})
            else:
                content.append({"type": "image_url", "image_url": {"url": p.image_url, "detail": p.detail}})
        body = {
            "model": s.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
            "max_completion_tokens": mot,
        }
        if s.service_tier:
            body["service_tier"] = s.service_tier
        if s.reasoning_effort:
            body["reasoning_effort"] = s.reasoning_effort
        return "/chat/completions", body

    # ---- response parsing ----------------------------------------------------------------
    def _extract(self, payload: dict) -> tuple[str, dict, str | None]:
        if self.settings.api_style == "responses":
            status = payload.get("status")
            if status == "failed":
                err = payload.get("error") or {}
                raise AIError("AI_UNAVAILABLE", f"the model run failed: {err.get('message', 'unknown error')}",
                              retryable=True)
            texts, refusal = [], None
            for item in payload.get("output", []) or []:
                if item.get("type") != "message":
                    continue
                for c in item.get("content", []) or []:
                    if c.get("type") == "output_text":
                        texts.append(c.get("text", ""))
                    elif c.get("type") == "refusal":
                        refusal = c.get("refusal") or "refused"
            if refusal:
                raise AIError("AI_REFUSED", f"the model declined: {refusal[:300]}")
            if status == "incomplete":
                reason = (payload.get("incomplete_details") or {}).get("reason", "unknown")
                raise AIError("AI_INCOMPLETE", f"the model stopped early ({reason}); output was truncated",
                              detail={"reason": reason})
            if not texts:
                raise AIError("AI_PROTOCOL", "the response contained no output text")
            return "".join(texts), payload.get("usage") or {}, payload.get("id")
        choices = payload.get("choices") or []
        if not choices:
            raise AIError("AI_PROTOCOL", "the response contained no choices")
        msg = choices[0].get("message") or {}
        if msg.get("refusal"):
            raise AIError("AI_REFUSED", f"the model declined: {str(msg['refusal'])[:300]}")
        if choices[0].get("finish_reason") == "length":
            raise AIError("AI_INCOMPLETE", "the model stopped early (length); output was truncated")
        text = msg.get("content")
        if not isinstance(text, str) or not text:
            raise AIError("AI_PROTOCOL", "the response contained no message content")
        return text, payload.get("usage") or {}, payload.get("id")

    # ---- public --------------------------------------------------------------------------
    def structured(self, *, system: str, parts: list[ContentPart], schema_name: str, schema: dict,
                   max_output_tokens: int | None = None) -> AIResult:
        s = self.settings
        if not s.configured:
            raise AIError("AI_NOT_CONFIGURED",
                          "AI is not configured: set TWIN_AI_API_KEY (or OPENAI_API_KEY) and TWIN_AI_MODEL.")
        if s.budget_usd is not None and self.budget is None:
            raise AIError("AI_BUDGET_UNTRACKED", "A spending cap is configured but this call has no budget tracker; "
                                                 "nothing was sent.")
        path, body = self._body(system, parts, schema_name, schema, max_output_tokens)
        headers = {"Authorization": f"Bearer {s.api_key.get_secret_value()}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(s.timeout_s, connect=min(15.0, s.timeout_s))
        est_input = estimate_input_tokens(system, parts, schema)
        attempts = 0
        start = time.time()
        last_error: AIError | None = None
        with httpx.Client(base_url=s.base_url, timeout=timeout, transport=self._transport) as http:
            while attempts <= s.max_retries:
                attempts += 1
                reservation = (self.budget.reserve(est_input, max_output_tokens) if self.budget
                               else None)  # raises when the worst case would exceed the cap
                t_attempt = time.time()
                outcome: dict = {"status": "error", "charge": False}
                resp = None
                try:
                    try:
                        resp = http.post(path, json=body, headers=headers)
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached the provider: free
                        outcome = {"status": "not_sent", "charge": False}
                        last_error = AIError("AI_UNAVAILABLE", f"could not reach the AI endpoint ({type(exc).__name__})",
                                             retryable=True)
                    except httpx.TimeoutException:  # may have been processed and billed: charge the worst case
                        outcome = {"status": "timeout", "charge": True}
                        last_error = AIError("AI_TIMEOUT", f"no response within {s.timeout_s:.0f}s", retryable=True)
                    except httpx.TransportError as exc:
                        outcome = {"status": "transport_error", "charge": True}
                        last_error = AIError("AI_UNAVAILABLE", f"connection to the AI endpoint failed ({type(exc).__name__})",
                                             retryable=True)
                    if resp is not None and resp.status_code >= 300:
                        last_error = self._http_error(resp)
                        outcome = {"status": f"http_{resp.status_code}", "charge": False, "error": last_error.code}
                        if not last_error.retryable:
                            raise last_error
                    elif resp is not None:
                        try:
                            payload = resp.json()
                        except (ValueError, json.JSONDecodeError):
                            outcome = {"status": "bad_payload", "charge": True, "error": "AI_PROTOCOL"}
                            raise AIError("AI_PROTOCOL", "the endpoint returned non-JSON", status=resp.status_code)
                        usage = payload.get("usage") if isinstance(payload, dict) else None
                        # billed whatever happens next; without reported usage, charge the worst case
                        outcome = {"status": "ok", "usage": usage, "charge": not usage}
                        try:
                            text, usage, rid = self._extract(payload)
                        except AIError as exc:
                            outcome.update(status="model_error", error=exc.code)
                            if exc.retryable and attempts <= s.max_retries:
                                last_error = exc
                                self._backoff(attempts, None)
                                continue
                            raise
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            outcome.update(status="invalid_json", error="AI_INVALID_JSON")
                            raise AIError("AI_INVALID_JSON", f"the model output is not valid JSON ({exc.msg})",
                                          detail=text[:500]) from exc
                        if not isinstance(data, dict):
                            outcome.update(status="invalid_json", error="AI_INVALID_JSON")
                            raise AIError("AI_INVALID_JSON", "the model output is not a JSON object")
                        log.info("ai call ok model=%s attempts=%d latency=%.1fs usage=%s", s.model, attempts,
                                 time.time() - start, usage)
                        return AIResult(data, text, s.model or "", rid, usage, round(time.time() - start, 2), attempts)
                finally:
                    if reservation is not None:
                        cost = self.budget.settle(reservation[0], status=outcome["status"], usage=outcome.get("usage"),
                                                  error_code=outcome.get("error"),
                                                  latency_s=round(time.time() - t_attempt, 2),
                                                  charge_reservation=outcome["charge"])
                        self.last_cost_usd = (self.last_cost_usd or 0.0) + (cost or 0.0)
                assert last_error is not None
                log.warning("ai call attempt %d failed: %s %s", attempts, last_error.code, last_error)
                if attempts > s.max_retries:
                    break
                self._backoff(attempts, resp)
        assert last_error is not None
        if last_error.code == "AI_RATE_LIMITED":
            last_error = AIError("AI_RATE_LIMITED", f"rate limited after {attempts} attempts; try again later",
                                 status=429)
        raise last_error

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> None:
        delay = min(30.0, 1.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if resp is not None and "retry-after" in resp.headers:
            try:
                delay = min(30.0, max(delay, float(resp.headers["retry-after"])))
            except ValueError:
                pass
        self._sleep(delay)

    def _http_error(self, resp: httpx.Response) -> AIError:
        code = resp.status_code
        msg = _provider_message(resp)
        if code in (401, 403):
            return AIError("AI_AUTH", f"the AI provider rejected the credentials ({code}): {msg}", status=code)
        if code == 404 or "model_not_found" in msg or "does not exist" in msg:
            return AIError("AI_MODEL_NOT_FOUND", f"model '{self.settings.model}' is not available to this key: {msg}",
                           status=code)
        if code == 429:
            return AIError("AI_RATE_LIMITED", f"rate limited: {msg}", status=code, retryable=True)
        if code in RETRY_STATUS:
            return AIError("AI_UNAVAILABLE", f"provider error {code}: {msg}", status=code, retryable=True)
        if code == 400 and "image" in msg.lower():
            return AIError("AI_IMAGE_UNSUPPORTED",
                           f"the model '{self.settings.model}' did not accept image input: {msg}. "
                           "Choose a vision-capable model (see `python -m twin.ai.probe`).", status=code)
        return AIError("AI_BAD_REQUEST", f"the AI provider rejected the request ({code}): {msg}", status=code)

    def list_models(self) -> list[str]:
        """GET /models (used by the probe)."""
        s = self.settings
        if s.api_key is None:
            raise AIError("AI_NOT_CONFIGURED", "no API key configured")
        with httpx.Client(base_url=s.base_url, timeout=httpx.Timeout(30.0), transport=self._transport) as http:
            resp = http.get("/models", headers={"Authorization": f"Bearer {s.api_key.get_secret_value()}"})
        if resp.status_code >= 300:
            raise self._http_error(resp)
        return sorted(m.get("id", "") for m in resp.json().get("data", []))
