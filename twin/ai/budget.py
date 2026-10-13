"""Hard spending cap for AI calls (TWIN_AI_BUDGET_USD).

Rules (fail closed):
  * every HTTP attempt first *reserves* its worst-case cost: estimated input tokens (upper
    bound) x input price + max_output_tokens x output price. If spent + open reservations +
    this reservation would exceed the cap, the request is not sent (AI_BUDGET_EXCEEDED);
  * with a cap configured but no prices, nothing is sent (AI_BUDGET_PRICES_MISSING);
  * after the attempt the real cost from the provider's reported usage is recorded; an attempt
    whose outcome is unknown (timeout / dropped connection) is charged its full reservation;
  * reservations are process-wide and locked, so concurrent jobs cannot overrun the cap together.

Spend is the sum of ``ai_calls.cost_usd`` in the app database (all projects, all time, including
the probe). Raise the cap by changing TWIN_AI_BUDGET_USD; the history is kept.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass

TEXT_CHARS_PER_TOKEN = 3.0          # conservative (real English/JSON is ~4 chars per token)
IMAGE_TOKENS = {"high": 6000, "low": 2000, "auto": 6000, "original": 12000}  # upper bounds per image
REQUEST_OVERHEAD_TOKENS = 500

_lock = threading.Lock()
_reservations: dict[str, float] = {}


@dataclass(frozen=True)
class Prices:
    input_per_1m: float
    output_per_1m: float

    def cost(self, input_tokens: float, output_tokens: float) -> float:
        return input_tokens / 1e6 * self.input_per_1m + output_tokens / 1e6 * self.output_per_1m


def estimate_input_tokens(system: str, parts, schema: dict) -> int:
    chars = len(system) + len(json.dumps(schema))
    images = 0
    for p in parts:
        if p.kind == "text":
            chars += len(p.text)
        else:
            images += IMAGE_TOKENS.get(p.detail, IMAGE_TOKENS["original"])
    return int(chars / TEXT_CHARS_PER_TOKEN) + images + REQUEST_OVERHEAD_TOKENS


def usage_tokens(usage: dict | None) -> tuple[int, int] | None:
    if not usage:
        return None
    inp = usage.get("input_tokens", usage.get("prompt_tokens"))
    out = usage.get("output_tokens", usage.get("completion_tokens"))
    if inp is None or out is None:
        return None
    # prompt-cache writes are billed at 1.25x the input price (e.g. gpt-6-astra): count the surcharge so the
    # recorded spend never falls below the bill
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cache_write = details.get("cache_write_tokens") or 0
    return int(inp) + int(round(0.25 * cache_write)), int(out)


class BudgetGuard:
    """Reserve -> settle around every AI HTTP attempt. ``storage`` provides spend + call records."""

    def __init__(self, storage, ai_settings, *, project_id: str | None = None, job_id: str | None = None,
                 purpose: str = "") -> None:
        self.storage = storage
        self.s = ai_settings
        self.project_id = project_id
        self.job_id = job_id
        self.purpose = purpose

    @property
    def prices(self) -> Prices | None:
        if self.s.price_input_per_1m is None or self.s.price_output_per_1m is None:
            return None
        return Prices(self.s.price_input_per_1m, self.s.price_output_per_1m)

    def spent(self) -> float:
        return self.storage.ai_spent_usd()

    def status(self) -> dict:
        spent = self.spent()
        with _lock:
            reserved = sum(_reservations.values())
        cap = self.s.budget_usd
        return {"cap_usd": cap, "spent_usd": round(spent, 4), "reserved_usd": round(reserved, 4),
                "remaining_usd": None if cap is None else round(max(0.0, cap - spent - reserved), 4),
                "prices_configured": self.prices is not None,
                "price_input_per_1m": self.s.price_input_per_1m, "price_output_per_1m": self.s.price_output_per_1m}

    def reserve(self, est_input_tokens: int, max_output_tokens: int | None = None) -> tuple[str, float]:
        from twin.ai.client import AIError
        prices = self.prices
        cap = self.s.budget_usd
        if cap is not None and prices is None:
            raise AIError("AI_BUDGET_PRICES_MISSING",
                          f"A ${cap:.2f} cap is set but the model's prices are not: set TWIN_AI_PRICE_INPUT_PER_1M and "
                          "TWIN_AI_PRICE_OUTPUT_PER_1M (USD per million tokens) so spend can be enforced. Nothing was sent.")
        worst = prices.cost(est_input_tokens, max_output_tokens or self.s.max_output_tokens) if prices else 0.0
        rid = uuid.uuid4().hex
        with _lock:
            if cap is not None:
                committed = self.spent() + sum(_reservations.values())
                if committed + worst > cap + 1e-9:
                    raise AIError("AI_BUDGET_EXCEEDED",
                                  f"Spending cap reached: ${committed:.4f} of ${cap:.2f} used or reserved; this request could "
                                  f"cost up to ${worst:.4f}. Nothing was sent. Raise TWIN_AI_BUDGET_USD to continue.",
                                  detail={"cap_usd": cap, "committed_usd": round(committed, 6), "worst_case_usd": round(worst, 6)})
            _reservations[rid] = worst
        return rid, worst

    def settle(self, rid: str, *, status: str, usage: dict | None = None, error_code: str | None = None,
               latency_s: float | None = None, charge_reservation: bool = False) -> float | None:
        """Release the reservation and record the attempt with its actual (or worst-case) cost."""
        with _lock:
            worst = _reservations.get(rid, 0.0)
            cost: float | None
            toks = usage_tokens(usage)
            prices = self.prices
            if toks and prices:
                cost = prices.cost(*toks)
            elif charge_reservation:
                cost = worst if prices else None
            else:
                cost = 0.0 if prices else None
            # write the record before dropping the reservation so the cap never has a gap
            self.storage.record_ai_call(project_id=self.project_id, job_id=self.job_id, model=self.s.model,
                                        api_style=self.s.api_style, status=status, error_code=error_code, usage=usage,
                                        latency_s=latency_s, cost_usd=cost, purpose=self.purpose)
            _reservations.pop(rid, None)
        return cost
