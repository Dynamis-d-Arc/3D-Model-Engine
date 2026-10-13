# Testing

Run everything that can run on this machine:

```bash
.venv/Scripts/python.exe -m pytest
```

Real-application tests skip themselves (with the reason) when Blender, Unreal, Edge/Playwright or the
AI key are not available. Fast subset without external applications:

```bash
.venv/Scripts/python.exe -m pytest -m "not blender and not ui and not unreal"
```

| File | Kind | What it proves |
|---|---|---|
| `test_scene_model.py` | automated | schema rejects duplicates, dangling references, bad ranges, NaN, extra fields |
| `test_validation.py` | automated | known plan → expected room sizes (±1 mm); overlaps, misplaced openings, connectivity, furniture outside rooms, door swings, certainty gate, brief mismatches |
| `test_frontend_syntax.py` | automated | every frontend ES module parses (`node --check`) |
| `test_compile.py` | automated | room spawn points on free floor, walls at centrelines, wall volume = hand calculation, no overlapping solids, openings are real voids with lintels/sills, glass/door leaves placed correctly, triangulation, determinism, a room edit changes only that object's hash |
| `test_transforms.py` | automated | scene↔Unreal location/yaw (incl. mirrored-axis consistency), plan calibration, sRGB→linear |
| `test_derive_diff.py` | automated | walls derived from rooms reproduce the fixture (ids, directions, opening offsets); fit-to-size; room updates cannot touch other rooms/walls |
| `test_ai.py` | **mocked** (httpx MockTransport) | key loading/precedence/redaction, request shape, conversion of a correct answer to the known dimensions, 16 kinds of invalid output rejected, retries/timeouts/4xx mapping, chat-completions style, image downscaling, room redesign scope |
| `test_api.py` | automated + mocked AI (+1 real Blender) | uploads (PNG/JPG/PDF, sniffing, size limit), immutable versions, scene tools, AI jobs, failed jobs with error codes, daily limit, refusal to build unready scenes, build without Blender fails cleanly, full build + zip + path traversal guard (real Blender) |
| `test_blender_integration.py` | **real** Blender | saved project + exports exist, re-import within 1 mm, `.blend` reopens with every object, room edit reuses 59/60 exports byte-for-byte |
| `test_ui_e2e.py` | **real** browser (headless Edge) + real Blender | create project, sample layout, draw + undo a room, edit, save version, generate, 3D preview, pick |
| `test_unreal_integration.py` | **real** Unreal (+ real Blender) | import within 1 cm, re-import reuses meshes, tour: nav path + every room reachable, click-to-walk arrival, wall collision, selection, screenshots; the same through the API job route |
| `test_ai_live.py` | **real** AI (opt-in `TWIN_RUN_LIVE_AI=1`) | the configured model reads the sample plan; prints dimension accuracy |

The repeatable fixture is `twin/fixtures/sample_apartment.json` (+ `sample_apartment_plan.png`, drawn
from it by `python -m twin.fixtures.render_plan`). Test artefacts go to `tests/_out/` (git-ignored).
