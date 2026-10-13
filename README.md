# Floor Plan Twin

Turn a floor plan, known dimensions, reference images and a text brief into a furnished 3D
apartment: a saved **Blender project**, **Unreal Engine 5–ready exports**, a browser preview,
and an Unreal walkthrough template.

```
upload + brief ─► AI interpretation ─► scene model (versioned, validated) ─► geometry compiler
   (PNG/JPG/PDF)    (optional, strict JSON)   ▲  human review & editing        (deterministic)
                                              │                                      │
                                              └─────── room-scoped changes ◄─────────┤
                                                                                     ▼
            Unreal walkthrough ◄── per-object FBX + manifest ◄── Blender worker (headless)
            (C++ template)          (verified by re-import)        .blend · FBX · GLB · renders
```

The **scene model** (`twin/scene/model.py`) is the single source of truth: metres, right-handed,
Z up, wall centrelines. AI output is only ever a *draft* of that model; all geometry is computed
deterministically from it, and nothing is reported as generated until the files exist and pass
verification.

## Status

| Stage | What works | How it is tested |
|---|---|---|
| 1 MVP | Upload PNG/JPG/PDF, brief with dimensions, sample apartment, blank layout, Blender build: walls with real door/window openings, floors, ceilings, slab, frames, open door leaves, glass, 26 reusable furniture assets, materials + generated textures, lights, cameras, `.blend`, per-object FBX, full FBX, GLB, preview renders | unit tests, real Blender integration tests, real-browser e2e test |
| 2 Review & edit | 2D plan editor over the calibrated plan image, draw/edit rooms, walls, openings, furniture, derive walls, fit to brief, live validation, measurement confirmation, AI questions, immutable versions, incremental rebuild (unchanged exports reused byte-for-byte) | unit + API + e2e tests |
| 3 Unreal | C++ walkthrough template (walk/fly/ghost, collision, nav mesh, click-to-walk, room teleport, selection + info HUD, ceilings toggle, scripted verification tour), headless import with hash-based mesh reuse and in-engine bounds verification | real UE 5.8.3 tests: 66 actors within 0.0001 cm, mesh reuse on re-import, every room reachable on the nav mesh, click-to-walk, wall collision, selection, screenshots |
| AI | OpenAI-compatible client (Responses or Chat Completions), strict JSON schema, retries/timeouts, typed errors, usage log + daily limit, plan interpretation, single-room redesign, probe CLI | mocked HTTP tests; live test is opt-in once the key is set |
| HVAC | HVAC drawing (vector PDF) → aligned on the structural grid → OCR + symbol detection → fan-coil units, diffusers, grilles and ducts placed, linked and built in 3D; **Fix with AI** (plain-language corrections → validated edit operations → new version); **Live HVAC** tab: an airflow illustration (one zone per AC unit) driven by MATLAB thermal results or live / simulated BMS data | synthetic detection/registration tests, airflow illustration tests in Node, mocked AI fix, API tests, optional private-drawing regression (`realdata`, tests/local) — see [docs/HVAC.md](docs/HVAC.md) |
| 4–5 | Not started (multi-floor, stairs, accounts, deployment). See docs/OPERATIONS.md | – |

## Quick start (Windows)

```bash
uv venv --python 3.12 .venv
```
```bash
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"   # includes rapidocr_onnxruntime (offline OCR for HVAC drawings)
```
```bash
cd frontend && npm install
```
```bash
.venv/Scripts/python.exe run_server.py
```
Open http://127.0.0.1:8765. Blender is auto-detected (`C:\Program Files\Blender Foundation\Blender *`);
set `TWIN_BLENDER_PATH` otherwise. Without an AI key everything except "Interpret with AI" and
"Redesign with AI" works.

### Turning on the AI (OpenAI-compatible key)

1. Copy `.env.example` to `.env` and set `TWIN_AI_API_KEY` (never commit `.env`; it is git-ignored).
2. List the models your key can use and test image + strict-JSON support:
   ```bash
   .venv/Scripts/python.exe -m twin.ai.probe
   ```
3. Put a model that passes into `TWIN_AI_MODEL`, restart the server, run the probe again.
4. Optional real test (costs tokens): `TWIN_RUN_LIVE_AI=1 .venv/Scripts/python.exe -m pytest -m live_ai -s`

### Unreal walkthrough

```bash
.venv/Scripts/python.exe -m twin.unreal.setup
```
compiles `unreal/TwinWalkthrough` (UE 5.8). Then use **Import into walkthrough** / **Launch walkthrough**
on a completed build, or see [docs/UNREAL.md](docs/UNREAL.md) for manual steps.

## Using it

1. **Brief & files** – name, style, prompt, outer dimensions, wall height, room sizes you know;
   upload the plan (and style references).
2. Create a layout: **Interpret with AI**, **Start from overall dimensions** (trace manually), or
   **Use sample apartment** (known measurements).
3. **Review layout** – calibrate the plan image (click a known dimension, then the origin), draw or
   adjust rooms (shared corners move together), derive walls, add doors/windows/furniture, confirm
   or accept estimated measurements, answer AI questions, **Save version**.
4. **Generate & preview** – Blender builds and verifies; preview the GLB, view renders, download
   the `.blend`, the Unreal bundle, FBX/GLB, manifest and reports.
5. **HVAC (optional)** – upload the HVAC drawing in step 1, *Import HVAC drawing* in Review, accept the
   estimates, generate; **Live HVAC** shows the airflow simulation driven by live or simulated unit data.
6. Anything wrong? **Fix with AI** in Review (or on a picked 3D object) describes the problem in words and
   gets a corrected version to review.
5. Change one room (manually or **Redesign with AI**) → new version → regenerate: unchanged exports
   are reused, and the Unreal importer re-imports only meshes whose content hash changed.

## Commands

| Command | Purpose |
|---|---|
| `python run_server.py` | backend + UI on `TWIN_HOST:TWIN_PORT` (default 127.0.0.1:8765) |
| `python -m pytest` | all tests that can run here (real Blender/UE/browser tests auto-skip when unavailable) |
| `python -m pytest -m "not blender and not ui and not unreal"` | pure automated + mocked tests only (fast) |
| `python -m twin.ai.probe [--model ID]` | check key, list models, test image + JSON schema |
| `python -m twin.unreal.setup` | compile the Unreal walkthrough template |
| `python -m twin.fixtures.render_plan out.png` | draw the sample apartment as a plan image |
| `python tests/ui_shots.py` | screenshot every tab of the running app (headless Edge) |

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – modules, data flow, storage, jobs, versioning
- [docs/SCENE_MODEL.md](docs/SCENE_MODEL.md) – coordinate system, schema, validation codes, geometry rules
- [docs/AI.md](docs/AI.md) – provider configuration, schema, honesty rules, errors, costs
- [docs/BLENDER.md](docs/BLENDER.md) – worker, asset library, export settings, verification
- [docs/UNREAL.md](docs/UNREAL.md) – template, prerequisites, import, controls, tour, manual steps
- [docs/TESTING.md](docs/TESTING.md) – automated vs mocked vs real integration tests
- [docs/OPERATIONS.md](docs/OPERATIONS.md) – running, security, data, roadmap to a service
- [docs/HVAC.md](docs/HVAC.md) – HVAC drawing import, Fix with AI, live data API, airflow simulation
