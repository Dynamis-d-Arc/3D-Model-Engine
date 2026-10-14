# Fixing layout problems

After a layout is created (AI interpretation, tracing, import) the validator usually finds problems: furniture
stretched to cover a long run, a door cutting into a wall corner, a room nobody can walk into. The **Problems**
card at the top of *Review layout* turns the validation report into a short checklist, and **Fix problems
automatically** fixes what it can in one go:

1. **Free rules** (`twin/scene/autofix.py`) fix the mechanical problems instantly, the same way every time.
2. **The AI problem solver** (`twin/ai/solve.py`, default model **gpt-5.3-codex**) gets what needs judgement:
   it looks at the problems, the entities involved, the scene and the floor plan drawing, and answers with
   small edit operations.
3. Everything is validated again and saved as **one new version** with a note listing what changed. The old
   version is never touched; the user looks at the result before building.

## The checklist

Issues are grouped by code into plain-language rows (titles and explanations live in
`twin/scene/problems.py`, served with `/api/catalog` as `problems`). Each row shows how serious it is and the
fix that fits it:

| Kind | Meaning | What the row offers |
|---|---|---|
| `rule` | a free rule fixes it | **Fix** (all of the group) and **Fix** per item: applied in the editor, undoable (Ctrl+Z), then *Save version* |
| `ai` | needs judgement on the drawing | **Ask AI**: the solver works on just that group and saves a version |
| `decide` | the user picks (room edges without a wall) | per room **Open plan** (sets `room.open_plan`) or **Add wall**; **All open plan**; **Ask AI** decides from the drawing |
| `accept` | values the AI estimated | **Accept all** (marks them accepted estimates) |
| `answer` | questions from the AI | per question: an answer box, **Answer**, **Accept as drawn**; **Accept all as drawn** |
| `manual` | only the user knows (their own dimensions, a missing measurement) | click the item to select it on the plan |
| `info` | notes | folded away under "N notes (no action needed)" |

"Must fix" (errors) and "Needs your OK" (review items) block generation; "Check" rows are warnings.

## Rules

| Code | Rule |
|---|---|
| `W_FURNITURE_SCALE` | stretched past its catalog range → a row of N items of normal size along the same axis (a 4.8x bookshelf becomes 5 bookshelves); squashed / too tall → resized to the nearest size in range |
| `E_FURNITURE_OUTSIDE_ROOM` | it stands inside another room → it now belongs to that room (not moved); else moved into its room (≤ 3 m) |
| `W_FURNITURE_CROSSES_BOUNDARY` | nudged the shortest distance (≤ 1 m) that clears the wall, preferably without blocking a door or bumping other furniture |
| `W_DOOR_SWING_BLOCKED` | moved (≤ 1.5 m) clear of every door swing in the room, staying in the room |
| `W_FURNITURE_OVERLAP` | the smaller item moves (≤ 1.5 m) until it is clear |
| `W_FURNITURE_TOO_TALL` | lowered under the ceiling (if it stays in its size range) |
| `E_OPENING_AT_JUNCTION`, `E_OPENING_OVERLAP`, `E_OPENING_OUTSIDE_WALL` | slid along its wall (≤ 1 m) to a free spot between the same two rooms; if none, narrowed in 5 cm steps (doors to 0.7 m, windows 0.4 m) |
| `E_OPENING_TOO_TALL` | lowered under the wall top |
| `W_WALL_OVERSHOOT` | the short stub is trimmed back to the crossing (only if no opening sits on the trimmed part) |

Rules run in passes until nothing more changes (a fix can uncover another). A rule that cannot fix a case leaves
it alone. A changed value keeps its review status (an estimate stays an estimate) and gets a note.

Measured on real projects (rules only): an AI-read library plan went from 4 errors + 35 warnings to 5 warnings
in about 2 s; two large CAD floors (40–60 rooms, 500–650 furniture items) from 202 → 25 and 405 → 100 warnings
(30–50 s on a laptop).

## The AI problem solver

* Model: `TWIN_AI_SOLVER_MODEL` (default `gpt-5.3-codex`), same key, endpoint and **spending cap** as the other AI
  features. Its own prices: `TWIN_AI_SOLVER_PRICE_INPUT_PER_1M` / `..._OUTPUT_PER_1M` (default for gpt-5.3-codex:
  $1.75 / $14 per 1M tokens, Standard tier, checked 2026-10-14). Another model without prices is refused (fail
  closed). Reasoning effort `TWIN_AI_SOLVER_EFFORT` (default `medium`).
* Asked: errors and warnings whose code is `ai`, `decide` or a `rule` the rules could not fix — at most 40 per
  run. Never the user's own figures (brief mismatches, missing measurements).
* Input: the numbered problems, the entities they name, the compact scene JSON, the furniture catalog when
  furniture is involved, and the floor plan image (`include_plan`, default on).
* Output: the same edit operations as *Fix with AI* (`twin/ai/fix.py`: add / update / delete per collection).
  They must apply and produce a valid scene, and **must not add a new error**; otherwise the problems are sent
  back once with the reasons, and if the second answer still fails the AI's edits are dropped (the free fixes
  are kept). Raw answers are kept in `data/projects/<id>/ai_responses/<job>_solve<n>.json`.
* Result: which problems are gone (`solved`), any new warnings (`introduced`), questions the model could not
  answer from the drawing, and the cost.

Cost: one call per run plus at most one retry. On the library plan (13 rooms, 77 walls, 190 furniture items) a
run with 5 problems cost **$0.08** and solved all 5 (four open-plan edges, one door into an unreachable terrace).
The estimate shown before running comes from `GET .../solve/estimate`.

## API

| Endpoint | |
|---|---|
| `POST /api/scene/autofix` `{scene, brief?, codes?, targets?}` | free rules on an unsaved scene → `{scene, actions[], report}` |
| `POST /api/scene/add-missing-walls` `{scene, room_id}` | walls along the room's open edges → `{scene, notes[]}` |
| `GET /api/projects/{p}/versions/{n}/solve/estimate` | `{rule_fixable, ai_problems, model, est_usd, max_usd, remaining_usd}` |
| `POST /api/projects/{p}/versions/{n}/solve` `{use_ai, include_plan, codes?, targets?}` | job `solve` → `needs_review` with `{version, rule_fixes[], ai{model, summary, changes, solved, introduced, questions, rejected, cost_usd} | null, before, after, remaining[]}`; `completed` with `version: null` when nothing changed |

An AI failure (budget, auth, rate limit) does not fail the job: the free fixes are saved and `ai.error` says why.
