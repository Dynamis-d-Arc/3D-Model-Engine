# AI interpretation

The AI turns a plan image + brief (+ optional style references) into a **draft** scene model. It never
produces geometry: the deterministic compiler does, after a human has reviewed the draft.

## Configuration (`.env` or environment; see `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `TWIN_AI_API_KEY` (or `OPENAI_API_KEY`) | – | server-side only; held as `SecretStr`; never logged, returned or written to outputs |
| `TWIN_AI_MODEL` | – | required; no model is hardcoded |
| `TWIN_AI_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint |
| `TWIN_AI_API_STYLE` | `responses` | `responses` (OpenAI Responses API) or `chat_completions` |
| `TWIN_AI_REASONING_EFFORT` | – | sent only when set |
| `TWIN_AI_FURNITURE_EFFORT` | `low` | reasoning effort of the tiled furniture detection (its main cost driver) |
| `TWIN_AI_TIMEOUT_S` / `TWIN_AI_MAX_RETRIES` | 180 / 3 | per request |
| `TWIN_AI_MAX_OUTPUT_TOKENS` | 20000 | truncated output is an error, not a partial result |
| `TWIN_AI_DAILY_REQUEST_LIMIT` | 200 | cost guard across all projects (UTC day) |

`python -m twin.ai.probe` prints the key's source (not the key), lists visible models and sends one tiny
image + strict-JSON request to confirm the configured model supports both.

## Spending cap (`twin/ai/budget.py`)

| Variable | Notes |
|---|---|
| `TWIN_AI_BUDGET_USD` | hard cap on total AI spend (all projects, all time, probe included); this install: **$10** |
| `TWIN_AI_PRICE_INPUT_PER_1M` / `TWIN_AI_PRICE_OUTPUT_PER_1M` | model prices (USD per 1M tokens, Standard tier); required when a cap is set, unless the model is in `MODEL_PRICES` (`twin/config.py`, published prices with the date they were checked); a set value always wins |
| `TWIN_AI_SERVICE_TIER` | `default`: pins requests to Standard pricing (OpenAI's "Fast" tier costs 2x) |

* Before every HTTP attempt the worst case is **reserved**: conservative input estimate (3 chars/token,
  6000 tokens per high-detail image) × input price + the full output allowance × output price. If spend +
  open reservations + this reservation exceed the cap, nothing is sent (`AI_BUDGET_EXCEEDED`).
  Reservations are locked process-wide, so concurrent jobs cannot overrun the cap together.
* After the attempt the actual cost from the provider's reported `usage` is recorded in `ai_calls.cost_usd`.
  Rejected requests (4xx/5xx) and connections that never reached the provider cost nothing; a timeout,
  dropped connection or a response without usage is charged its full reservation (it may have been billed).
* Cap set but prices missing → nothing is sent (`AI_BUDGET_PRICES_MISSING`). The probe's test call goes
  through the same cap. `/api/health` and `/api/usage` report `cap_usd`, `spent_usd`, `remaining_usd`.
* Cached-input discounts are ignored (charged at the full input price), so recorded spend errs high.
  Prompt-cache writes (`input_tokens_details.cache_write_tokens`, billed at 1.25× input on gpt-6-astra)
  are charged with that surcharge.
  Set a project budget on the OpenAI dashboard as a second, provider-side safety net.

## Vector PDF drawing sheets (`twin/imaging/pdf_text.py`)

For PDF uploads the text layer is extracted at upload time (`pdf_text.json`). If a drawing scale is printed
(`1:100`), every text block (room names, areas such as "48 SQM", dimension strings) gets an exact position in
the **plan frame** (full-scale metres, origin at the sheet's bottom-left) and the plan underlay is calibrated
automatically. Interpretation then makes one small call to locate the plan on the sheet (excluding title block
and revision table), sends a sharp crop of that area plus the labels inside it, and asks for all coordinates in
the plan frame. Without a detectable scale the plan is sent as a normal image.

## Request

* Responses API: `POST {base}/responses` with `instructions`, one user message containing text parts,
  the plan as `input_image` (PNG, ≤ 2048 px, `detail: high`) and up to four references (JPEG, ≤ 1024 px,
  `detail: low`), `text.format = {type: json_schema, strict: true}`, `store: false`.
* Chat Completions: the same content as `messages`, `response_format.json_schema.strict = true`.
* PDFs are rasterised (200 dpi) before sending; image analysis is never faked — if the model rejects
  images the job fails with `AI_IMAGE_UNSUPPORTED`.

## Output contract

`twin/ai/schema.py` (`AIInterpretation`): rooms (polygon + `dimension_source` + confidence), walls,
openings (centre point + wall key), furniture (catalog asset ids only), surface and furniture-role
materials, lighting mood, `uncertainties`, summary, `plan_readable`.

The strict schema sent to the provider is generated from the pydantic models (all fields required,
`additionalProperties: false`). Range, length and pattern constraints are enforced afterwards by
pydantic, plus cross-references (unknown wall/room keys, duplicate keys). Any violation →
`AI_SCHEMA_VIOLATION` and nothing is stored.

## Honesty rules (prompt + conversion)

* Source priority: user brief > plan labels > scaling > estimation; the source is recorded per element.
* `plan_label` → status `extracted`, scaled/estimated → `estimated`: both need confirmation (or explicit
  acceptance) before generation. A "user_provided" claim that does not match the brief is downgraded.
* An opening that is not on its wall (> thickness/2 + 0.25 m away) is **not** moved; it becomes an open
  question. Uncertainties become open questions; "blocking" ones block generation until resolved.
* Unreadable input → `plan_readable=false` → job fails with `AI_PLAN_UNREADABLE`.

## Errors

`AI_NOT_CONFIGURED`, `AI_AUTH`, `AI_MODEL_NOT_FOUND`, `AI_BAD_REQUEST`, `AI_IMAGE_UNSUPPORTED`,
`AI_RATE_LIMITED`, `AI_TIMEOUT`, `AI_UNAVAILABLE`, `AI_REFUSED`, `AI_INCOMPLETE`, `AI_INVALID_JSON`,
`AI_SCHEMA_VIOLATION`, `AI_PLAN_UNREADABLE`, `AI_LIMIT_REACHED`. Retries (exponential backoff, honours
`Retry-After`) only for timeouts, connection errors, 408/409/429/5xx.

## Furniture & equipment detection (`twin/ai/furniture.py`)

Equipment layouts carry hundreds of items that the whole-sheet interpretation cannot read (at ~2.5 cm per
pixel a hand sink is a dozen pixels and its label is illegible). **Review layout -> Furniture & equipment ->
Detect furniture (AI)** (`POST /api/projects/{pid}/versions/{n}/detect-furniture`, cost estimate at
`GET .../detect-furniture/estimate`) renders the plan area of the vector PDF in tiles (12 m core + 1.5 m
margin, ~85 px/m, core outlined in red), sends each tile with its exact PDF text labels, the rooms in it and
the catalog with its **drawing words (aliases)**, and asks for every drawn item: asset, centre, measured
width/depth, rotation (front direction), label + tag, confidence. Compact keys and `TWIN_AI_FURNITURE_EFFORT`
(default `low` reasoning) keep the output small; 3 tiles run in parallel. Each tile reports only items centred
in its core; the merge drops near-duplicates, implausible sizes and unknown assets, assigns rooms (centre in a
wall -> nearest room within 0.3 m) and stores a new version: detected items are `extracted` (to confirm),
items placed by hand are kept, mounted items get the catalog's mounting height. Every paid tile answer is saved
to `ai_responses/{job}_tiles.json`; `{"reuse_job_id": "j_..."}` re-runs the merge without calling the AI.

Measured on an A1 kitchen equipment layout with `gpt-6-astra` (one dense tile): 78 items, 7.9k input / 4.5k
output tokens, 87 s, $0.33; 12 tiles ≈ $4 (the estimate endpoint uses 8k/5k tokens per tile, input × 1.25).

The catalog (`twin/assets/catalog.json`, 55 assets) has commercial kitchen, F&B and back-of-house items
(work tables, base/wall cabinets, chillers, upright fridges, sinks, hand sinks, ranges, griddles, fryers, combi
ovens, grills, dishwashers, hoods, shelving, trolleys, appliances, service counters, display cases, round and
communal tables, benches, lockers, server racks, urinals, a generic `equipment_box`), each with aliases,
`default_elevation` and `stretch` (modular designs that may take any drawn size). The interpretation prompt
no longer caps furniture at "about three per room".

## Room redesign

`POST /api/projects/{p}/versions/{n}/rooms/{room}/redesign` sends only that room (polygon, usable bounds,
openings, current furniture) and accepts only furniture/finishes for it; the result goes through the
scope check in `twin/scene/diff.py`, so other rooms cannot change.

## Usage tracking

Every call is recorded in `ai_calls` (model, status, error code, tokens, latency); `/api/usage` and the
Activity tab show today's totals against the daily limit.
