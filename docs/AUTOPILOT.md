# Autopilot: one click from drawings to a 3D model

The user drops the drawings in and presses **Make the 3D model**. Everything they would otherwise start one by one
runs in order as one job, and they check the result instead of every step.

```
drop files ─► sorted (plan / HVAC / furniture list / sensor sheet / reference) ─► Make the 3D model
                                                                                        │
   read the plan ─► furniture list ─► HVAC drawing ─► sensor sheet ─► drawn furniture ─► fix problems ─► accept ─► build
     (AI)            (CSV)             (vector PDF)     (CSV)          (AI, ≤ $1)        (rules + AI)              (Blender)
                                                                                        │
                       finished model + "Made automatically": what ran, what it assumed (click one → Review)
```

Code: `twin/api/autopilot.py` (steps, job, routes), `twin/imaging/classify.py` (sorting uploads, PDF page),
`twin/scene/furniture_sheet.py` (furniture lists), `twin/scene/review.py` (accepting estimates),
`frontend/js/autopilot.js`. Tests: `tests/test_autopilot.py`.

## What the user does

| Before | Now |
|---|---|
| New project dialog (name, style) | drop the files on the start page: the project is created and named after them |
| three drop zones, the PDF page typed before uploading | one drop zone; each file sorted by itself, the plan page of a multi-page PDF found automatically |
| brief form + **Save brief** | optional, folded, saved as you type |
| Interpret → Fix problems → Accept all → Accept as drawn → Save version → Import HVAC → Accept → Save → Detect furniture → Build → View | **Make the 3D model** (or **Finish automatically** in the bar for an existing layout) |
| each checklist button followed by **Save version** + a note | checklist buttons save a version by themselves (a burst of clicks = one version) |
| watching the progress bar | a desktop notification and a marked tab title when it is done or needs you |

## The steps

Each step is the same job a button would start (it shows up in Activity and saves its own version), so the history
is complete and every step can be undone by restoring an earlier version.

| Step | Runs when | Notes |
|---|---|---|
| Read the plan | the project has no layout yet (or "Read the plan again") | needs the AI; a failure ends the run with the AI's error |
| Place the furniture list | a furniture list CSV is uploaded and not used yet | replaces the AI's furniture; hand-placed items stay; **not used** when most rows do not fit the layout |
| Place the HVAC drawing | an HVAC drawing is uploaded, the plan is a vector PDF, the layout has no HVAC yet | a drawing that places nothing is reported, not hidden |
| Place the sensors | a sensor sheet CSV is uploaded and not used yet | after HVAC, so sensors link to their unit by tag |
| Read the drawn furniture | first run, vector PDF plan, no furniture list, AI on | only when the estimate is ≤ `furniture_max_usd` ($1); a run that finds nothing keeps the AI's furniture |
| Fix problems | always | free rules, then the AI problem solver (docs/PROBLEMS.md) |
| Accept the estimates | unless *Let me check the layout before it is built* | extracted / estimated values → `accepted_estimate`; non-blocking AI questions answered "as drawn" |
| Build the 3D model | the layout can be generated and Blender is installed | `mode: auto`: a sensor-only change updates the saved model in place |

**It stops and asks** only for what it cannot decide, with the reason in the result (`stopped`): a *blocking* AI
question or errors no fix could solve (`NEEDS_YOU`, the user goes to Review; **Finish automatically** continues
from there), *Let me check the layout first* (`REVIEW_FIRST`), Blender missing (`BLENDER_MISSING`), or **Stop**
(`STOPPED`, after the step it is on; that step's job finishes and is kept).

## Honesty

Review moves from before the build to after it, it is not dropped. Every accepted estimate is listed in the run's
result (`assumptions`, e.g. `wall:W3`) and, as for a manual *Accept all*, in the build manifest
(`accepted_estimates`). The Generate tab shows them grouped above the model; one click opens the item in Review,
where it shows *accepted estimate* and **Confirm**. Blocking questions are never answered for the user.

## Uploads: one drop zone (`kind=auto`)

| File | Sorted as | How |
|---|---|---|
| CSV | furniture list / sensor sheet | a catalog column (`catalog_id`, `asset_id`, …) / a `tag` column; anything else is refused with the columns found |
| PDF | HVAC drawing / floor plan | the file name (hvac, acmv, mech, aircon, duct…), else the text layer (ACMV, FCU, diffuser… outnumber "floor plan") |
| PNG / JPG | floor plan / reference | white paper with thin lines vs colour (and the file name) |

Multi-page PDFs (`page=0`): every page is scored by how much is drawn on it, a "floor plan" title (or HVAC words for
an HVAC drawing) and penalties for sections, elevations, schedules and cover sheets; the record says why
(`page_reason`). An explicit page still wins. A wrong guess is fixed on the thumbnail
(`PATCH /api/projects/{p}/files/{f}` `{kind}`), which also calibrates a vector PDF that becomes the plan. The plan
in use is the one the user picked, else the newest vector PDF (it carries the scale), else the newest plan.

## API

| Endpoint | |
|---|---|
| `GET /api/projects/{p}/autopilot/plan?review_first=&furniture=&fresh=` | the steps a run would take, with the reason for each it leaves out; `blocked` when it cannot start |
| `POST /api/projects/{p}/autopilot` `{review_first, furniture: auto\|detect\|skip, furniture_max_usd, use_ai, fresh, from_version, plan_file_id, reference_file_ids}` | job `autopilot`; `completed` with `build_id`, or `needs_review` with `stopped` |
| `POST /api/jobs/{j}/cancel` | stop an autopilot run after its current step |
| `POST /api/projects/{p}/files` `kind=auto`, `page=0` | sorted upload (also `furniture_list`, `sensor_sheet`) |
| `POST /api/scene/import-furniture` `{scene, text, replace}` | furniture list into an unsaved scene (Review → Furniture → Import furniture list) |

The job result while it runs and at the end: `{steps: [{key, label, status: pending|running|done|skipped|failed|
waiting|stopped, note, version, job_id}], version, build_id, assumptions[], questions[], remaining[], stopped,
cost_usd}`. Autopilot runs have their own thread pool, so they never hold up the AI or Blender pools they wait for.
