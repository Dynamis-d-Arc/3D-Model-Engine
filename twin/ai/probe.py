"""Check the configured AI provider before using it:  python -m twin.ai.probe [--model ID] [--list-only]

1. confirms a key is configured (never prints it),
2. lists the models the key can access (GET /models),
3. sends one tiny strict-JSON request with a generated test image to verify that the
   configured model accepts image input and structured output.

Step 3 goes through the same spending cap as the app (TWIN_AI_BUDGET_USD) and is recorded in
the app database; steps 1-2 are free. Use --list-only to skip the paid step.
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from dataclasses import replace

from PIL import Image, ImageDraw

from twin.ai.budget import BudgetGuard
from twin.ai.client import AIClient, AIError, ContentPart
from twin.api.storage import Storage
from twin.config import load_settings

TEST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["shape", "label", "rooms_count"],
    "properties": {"shape": {"type": "string", "enum": ["rectangle", "circle", "triangle", "none"]},
                   "label": {"type": "string"}, "rooms_count": {"type": "integer"}},
}


def test_image() -> str:
    img = Image.new("RGB", (480, 320), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 420, 260], outline="black", width=6)
    d.line([240, 60, 240, 260], fill="black", width=4)
    d.text((110, 150), "BED 3.0 m", fill="black")
    d.text((280, 150), "BATH", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="override TWIN_AI_MODEL for this probe")
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args(argv)
    settings = load_settings()
    ai = settings.ai if not args.model else replace(settings.ai, model=args.model)
    print(f"endpoint host : {ai.public()['endpoint_host']}  (style: {ai.api_style})")
    print(f"key           : {'present via ' + ai.key_source if ai.api_key else 'MISSING'}")
    print(f"model         : {ai.model or 'NOT SET (TWIN_AI_MODEL)'}")
    storage = Storage(settings.data_dir)
    guard = BudgetGuard(storage, ai, purpose="probe")
    b = guard.status()
    cap = f"${b['cap_usd']:.2f}" if b["cap_usd"] is not None else "NONE (set TWIN_AI_BUDGET_USD)"
    prices = (f"${ai.price_input_per_1m}/1M in, ${ai.price_output_per_1m}/1M out" if b["prices_configured"]
              else "NOT SET (TWIN_AI_PRICE_INPUT_PER_1M / TWIN_AI_PRICE_OUTPUT_PER_1M)")
    print(f"spending cap  : {cap}   spent so far ${b['spent_usd']:.4f}")
    print(f"prices        : {prices}")
    if ai.api_key is None:
        print("\nSet TWIN_AI_API_KEY (or OPENAI_API_KEY) in .env or the environment, then re-run.")
        return 2
    client = AIClient(ai)
    try:
        models = client.list_models()
        interesting = [m for m in models if any(t in m for t in ("codex", "gpt-5", "gpt-6", "gpt-4.1", "gpt-4o", "o3", "o4"))]
        print(f"\n{len(models)} models visible to this key; likely candidates:")
        for m in interesting[:60]:
            print("  ", m)
    except AIError as exc:
        print(f"\nlisting models failed: {exc.code}: {exc}")
        if exc.code == "AI_AUTH":
            return 3
    if args.list_only:
        return 0
    if not ai.model:
        print("\nSet TWIN_AI_MODEL to one of the models above and run the probe again.")
        return 2
    test_ai = replace(ai, max_output_tokens=min(ai.max_output_tokens, 4000))  # small worst-case reservation
    client = AIClient(test_ai, budget=BudgetGuard(storage, test_ai, purpose="probe"))
    print(f"\ntesting image input + strict JSON with '{ai.model}' ...")
    try:
        res = client.structured(
            system="Describe the test drawing. Answer only with the requested JSON.",
            parts=[ContentPart("text", "What outline shape is drawn, what text label mentions metres, and how many "
                                       "rooms does the outline contain?"),
                   ContentPart("image", image_url=test_image(), detail="low")],
            schema_name="probe", schema=TEST_SCHEMA)
    except AIError as exc:
        print(f"FAILED: {exc.code}: {exc}")
        return 4
    ok = res.data.get("shape") == "rectangle" and "3" in str(res.data.get("label", ""))
    print(f"response      : {res.data}")
    print(f"usage         : {res.usage}  latency {res.latency_s}s  cost ${client.last_cost_usd or 0:.6f}")
    print(f"spent so far  : ${guard.spent():.4f}" + (f" of ${ai.budget_usd:.2f}" if ai.budget_usd is not None else ""))
    print("RESULT        : " + ("OK - image input and structured output work" if ok else
                                "PARTIAL - the call worked but the image was not read as expected"))
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
