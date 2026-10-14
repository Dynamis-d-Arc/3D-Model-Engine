# Architecture

| Layer | Code | Responsibility |
|---|---|---|
| Frontend | `frontend/` (plain ES modules, three.js from `node_modules`) | brief, uploads, 2D plan editor, validation UI, progress, GLB preview, downloads |
| Backend API | `twin/api/app.py` (FastAPI) | projects, uploads, versions, scene tools, jobs, builds, downloads |
| Storage | `twin/api/storage.py` | SQLite metadata + files under `TWIN_DATA_DIR` |
| Jobs | `twin/api/jobs.py` | thread pools (ai ×2, blender ×1, unreal ×1), persisted status/progress/errors |
| AI service | `twin/ai/` | provider client, prompts, strict schema, conversion to a scene draft, probe |
| Imaging | `twin/imaging/preprocess.py` | content-sniffed PNG/JPG/PDF → normalised PNG + thumbnail |
| Scene model | `twin/scene/model.py` | versioned pydantic schema (source of truth) |
| Geometry | `twin/scene/{geometry,validate,compile,derive,diff,transforms,styles,textures}.py` | validation, deterministic mesh plan, editing helpers, conversions |
| Assets | `twin/assets/catalog.json` | furniture contract shared by AI, compiler and Blender |
| Blender worker | `twin/blender/` + `twin/pipeline.py` | library build, scene build, export, re-import verification, manifest |
| Unreal | `twin/unreal/` + `unreal/TwinWalkthrough/` | C++ walkthrough + **packaged desktop viewer** (runtime glTF loading, no editor), optional editor import commandlet, tour, packaging (`twin/unreal/package.py`) |
| Viewer API | `twin/viewer/` | read-only `/api/viewer/v1` for the packaged viewer: signed short-lived viewer tokens, viewer manifest + glTF pre-flight, HVAC live data / airflow illustration in Unreal space — see [UNREAL_VIEWER.md](UNREAL_VIEWER.md) |
| Multi-floor buildings | `twin/viewer/stacks.py` | floor projects stacked into one building: grid alignment, floor heights, one merged glTF + viewer manifest per composite, `/api/stacks` — see [MULTI_FLOOR.md](MULTI_FLOOR.md) |

## Data flow

1. **Brief + uploads** are stored per project. Uploads are type-checked by content and normalised.
2. **Interpretation** (AI job, optional) returns JSON that must pass the strict schema and pydantic
   validation; it is converted into a scene draft with certainty flags and open questions and stored as
   a new version with status `needs_review`. Alternatives: the fixture, or a blank layout to trace.
3. **Review**: every edit is validated live (`/api/scene/validate`); saving creates a new immutable
   version (`versions/vNNNN.json`), never overwriting an old one.
4. **Generate** (Blender job): `compile_scene` turns the version into a build plan (prisms with cap
   triangulations, furniture placements, lights, cameras, expected bounds, content hashes). Blender
   builds, saves, exports; a second Blender process re-imports every export; the backend compares
   Blender's report and the re-import report with the plan (1 mm tolerance). Blender's glTF is kept as
   `export/web/geometry.glb` and `export/web/scene.glb` = geometry + the sensor layer (`twin/layers.py`).
   Only then is the build `completed` and `manifest.json` written.
4b. **Update in place** (layer build, no Blender): a version whose building is identical to a saved build's
   (only sensors changed) reuses that build's files through hard links and rewrites only the sensor layer —
   seconds instead of minutes. See [SENSORS.md](SENSORS.md).
5. **Desktop viewer** (no job, no editor): the web app mints a read-only viewer token; the packaged viewer
   downloads the build's verified glTF + viewer manifest over `/api/viewer/v1`, builds the walkthrough at
   runtime and re-checks every actor against the scene model ([UNREAL_VIEWER.md](UNREAL_VIEWER.md)).
6. **Unreal editor import** (optional developer job): the headless editor imports per-object FBX (skipping meshes whose content hash is
   already imported), builds `L_Walkthrough`, verifies every actor's bounds against the scene model,
   then the scripted tour checks navigation, collision and selection and captures screenshots.

## Job statuses

`queued → processing → needs_review | completed | failed`. Interpretation and redesign end in
`needs_review` (a human must check the result); builds and imports end in `completed`. Failures
store `{code, message, detail}` (e.g. `AI_SCHEMA_VIOLATION`, `BLENDER_NOT_CONFIGURED`,
`GEOMETRY_MISMATCH`, `EXPORT_VERIFY_FAILED`, `UNREAL_MODULE_NOT_BUILT`). Jobs that were running when the
server stopped are marked failed with `INTERRUPTED` at the next start.

## Storage layout (`TWIN_DATA_DIR`, default `./data`)

```
twin.sqlite                         projects, files, versions, jobs, builds, ai_calls
projects/<p_id>/uploads/<f_id>/     original.<ext>, normalized.png, thumb.png
projects/<p_id>/versions/vNNNN.json immutable scene versions
projects/<p_id>/builds/<b_id>/      plan.json, scene.json, scene.blend, export/…, preview/…,
                                    export/web/geometry.glb (Blender) + scene.glb (+ layers), sensors.json,
                                    blender_report.json, verify_report.json, layer_report.json, manifest.json, unreal/…
```

Builds belong to one project version and live in their own directory, so a later build never touches
an earlier one. `builds.kind` is `full` (Blender) or `layer` (a saved build reused with new layers;
`previous_build` = the build it updates, `manifest.geometry_build` = the Blender build the geometry comes
from). Layer builds hard-link the files they reuse; nothing in a build directory is modified after it
completes, so the links are safe. Versions store `changes` against their parent. Reuse across builds is explicit: `reuse.json` lists the exports copied from the
previous build because their content hash is unchanged.

## Regenerating a room

Room edits (manual, or `redesign_room` via AI) go through `twin.scene.diff.apply_room_update`, which
replaces only that room's furniture/finishes and then calls `assert_only_rooms_changed` — any change
to walls, openings, levels, other rooms or their furniture raises `ScopeViolation`. Because every
object in the build plan carries a content hash, the next build re-exports only changed objects and the
Unreal importer re-imports only changed meshes.
