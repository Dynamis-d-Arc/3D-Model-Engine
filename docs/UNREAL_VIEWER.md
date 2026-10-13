# Unreal desktop viewer (packaged, runtime loading)

The desktop viewer is the Unreal walkthrough packaged as a normal Windows application. It downloads a
generated building from the backend **at runtime**, so neither the person viewing nor the server needs
the Unreal Editor (or Blender). The editor import described in [UNREAL.md](UNREAL.md) remains as an
optional developer / QA tool.

## Architecture

```
 browser (web app)                         backend (FastAPI)                               files
 ─────────────────                         ─────────────────                               ─────
 brief, uploads, review, Generate  ──►  AI interpretation, scene versions,        data/projects/<p>/
 "Open in desktop viewer"          ──►  Blender worker (build + verify),     ──►    builds/<b>/manifest.json
        │  POST /api/projects/{p}/viewer/launch   HVAC import, telemetry, MATLAB            export/web/scene.glb
        │  -> <building>.twinview (read-only token)                                 viewer/viewer_manifest.json
        ▼
 packaged viewer (TwinWalkthrough.exe)  ── HTTPS/HTTP, Authorization: Bearer <viewer token> ──►  /api/viewer/v1/*
   ATwinViewerDirector: manifest -> model.glb (cached) -> glTF parse (worker thread) -> procedural meshes
   + collision -> lights / nav bounds -> in-engine check against the scene model -> walkthrough
   ATwinPlayerController / ATwinHUD: walk, rooms, selection, HVAC readings, airflow illustration, building switch
```

* **AI, Blender generation, project storage and authentication stay on the backend.** The viewer is a
  read-only client of one documented API. It never sees an API key or a backend secret.
* **One file format end to end:** the build's Blender glTF 2.0 export (`export/web/scene.glb`, already
  verified by re-import during the build) is what the viewer loads. glTF carries metallic/roughness
  (the editor FBX import loses them), node names and the `twin_*` custom properties.
* **Each build is separate:** builds are immutable folders addressed by `(project id, build id)`; the
  viewer caches models under `Saved/TwinCache/<project>/<build>/<sha256>.glb`. Nothing is written into
  shared Unreal content, so one user's model can never overwrite another's.
* **No Unreal Editor on the server.** The editor is only needed on the machine that *packages* the
  viewer (once per viewer release).

## API (`/api/viewer/v1`, version 1)

All routes except `/info` need `Authorization: Bearer <viewer token>`. Errors are
`{"error": {"code", "message", "detail"}}`.

| Route | Returns |
|---|---|
| `GET /info` | `{"api": "twin-viewer", "api_version": 1, ...}` (no auth; for compatibility checks) |
| `GET /buildings` | projects + completed builds the token may open (`project_id`, `name`, `builds[]`: `build_id`, `version`, `rooms`, `furniture`, `model_bytes`) |
| `GET /buildings/{p}/builds/{b}/manifest` | viewer manifest (below) |
| `GET /buildings/{p}/builds/{b}/model.glb` | the glTF binary (`model/gltf-binary`) |
| `GET /buildings/{p}/builds/{b}/hvac/live` | latest reading per HVAC unit of the build, `source` = `live` / `simulated` / `unavailable`, `measured` (true only for real BMS data), `label`, `matching` (`missing` / `unknown` unit ids) |
| `GET /buildings/{p}/builds/{b}/hvac/airflow` | airflow **illustration** geometry per unit (outlet, throw directions, supply/return terminals, zone box; Unreal cm), `kind: "illustration"`, `disclaimer`, and the saved MATLAB thermal model state (`thermal`, labelled simulation) |

Error codes the viewer handles: `TOKEN_MISSING`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `TOKEN_MALFORMED`
(401), `OUT_OF_SCOPE` (403), `NOT_FOUND` (404), `BUILD_NOT_COMPLETED`, `MODEL_MISSING`, `HVAC_MISSING`
(409), plus client-side `SERVER_UNREACHABLE`, `MODEL_TRUNCATED`, `MODEL_INVALID`, `MODEL_TOO_LARGE`,
`MANIFEST_UNSUPPORTED`, `LAUNCH_INVALID`.

**Minting a link** (web-app side; today the web app has no accounts, so this route has the same trust as
the rest of the web API — put it behind the login when accounts arrive):

```
POST /api/projects/{p}/viewer/launch   {"build_id": "b_..." (default newest completed), "ttl_hours": 12, "all_projects": false}
-> {"launch": {"format": "twin-viewer-launch", "version": 1, "server", "project_id", "build_id", "project_name",
               "token", "expires_at"}, "file_name": "<project>.twinview", "expires_at": ...}
```

### Viewer tokens

`tvw1.<payload>.<HMAC-SHA256>` (twin/viewer/tokens.py): read-only, scoped to one project (or `*` with
`all_projects`), optional single build, expiry (default `TWIN_VIEWER_TOKEN_TTL_H=12`, max 30 days).
Signed with `TWIN_VIEWER_SECRET` (≥ 32 characters) or, when unset, a random secret generated once per
data directory (`data/viewer_secret.key`). Tokens are redacted from logs like API keys. They travel only
in the `Authorization` header, never in URLs.

### Viewer manifest (`format: twin-viewer-manifest`, `manifest_version: 1`)

Everything already in Unreal centimetres: `bounds_cm`, `rooms[]` (`id`, `name`, `dims_clear_m`,
`area_m2`, `floor_z_cm`, `spawn_cm`, `polygon_cm`, ...), `objects[]` / `furniture[]` keyed by the glTF
node name (`kind`, `room_id`, `label`, `collision`, expected `bounds_cm`, HVAC `hvac_id` / `tag` /
`unit_id` / `airflow_lps`, furniture `asset_id` / `size_text`), `lights[]`, `player_start`,
`hvac.units[]`, `model` (`bytes`, `sha256`, counts) and **`preflight`**: the server's own check that every
glTF node, converted to Unreal space, matches the scene model within 1 cm (cached per build in
`builds/<b>/viewer/viewer_manifest.json`). Launch links are only handed out after this check runs.

## Coordinates and validation

| Frame | Units / axes |
|---|---|
| scene model, Blender | metres, right-handed, +X east, +Y north, +Z up |
| glTF (Blender exporter, Y up) | `(x_g, y_g, z_g) = (x_b, z_b, −y_b)` |
| Unreal | cm, left-handed, Z up: `100·(x_g, z_g, y_g) = (100 x_b, −100 y_b, 100 z_b)` |

The swap of two axes is a mirror, applied to vertices, normals and node transforms (`T_ue = C·T_g·C⁻¹`);
it also turns glTF's counter-clockwise front faces into Unreal's front-face winding, so index order is
kept. The same numbers are checked twice:

1. **server pre-flight** (Python, `twin/viewer/bundle.py`): glTF node boxes vs the scene model;
2. **in Unreal** after spawning (`ATwinViewerDirector::ValidateBuild`): every actor's world bounds vs the
   manifest's scene-model bounds, every manifest object present, every room has a floor, the building's
   overall bounds — written to `Saved/TwinViewer/validation_<build>.json` and shown in the HUD
   ("Model check: N objects match the floor plan").

## What the walkthrough does

* First-person walk / fly / ghost, capsule collision against walls, doors frames, glass and furniture
  boxes; HVAC elements are selectable but never block; click-to-walk on a runtime nav mesh (the viewer
  level's NavMeshBoundsVolume is moved and scaled over each loaded building).
* Room name, clear size, area and floor finish of the room you stand in; **M** room list (click to go),
  **Tab** / **1–9** jump.
* Click any object: furniture (asset, size, room) or HVAC equipment: tag, unit, design airflow and the
  latest reading — labelled **LIVE BMS (measured)** or **SIMULATED BMS – demonstration, not measured**,
  with its age; a broken feed keeps the last reading and says so.
* **V** airflow illustration: tracer dots from each unit's supply diffusers (or casing outlet) along the
  ceiling, into the room and back to its return grille; colour = the unit's supply → return air
  temperature from the telemetry feed; units that are off show nothing. While it is visible a banner says
  **"AIRFLOW ILLUSTRATION – schematic, not measured, not a CFD result"**, names the temperature source
  and shows the MATLAB thermal model's west/east zone temperatures as *simulation*.
* **B** buildings list (switch model without restarting), **Ctrl+V** paste a launch code, **R** retry,
  **P** screenshot.

Loading shows the stage, a progress bar and MB downloaded; errors show the code, the message and what to
do. Models are limited to 1 GB by default (`-TwinMaxModelMB=`); parsing runs on a worker thread and
meshes are created in 12 ms slices so the window stays responsive.

## Packaging (developer machine)

Needs UE 5.8, Visual Studio Build Tools (C++), Windows SDK and the .NET Framework 4.8.1 Developer Pack
(see [UNREAL.md](UNREAL.md)).

```bash
.venv/Scripts/python.exe -m twin.unreal.package --config Development
```

1. compiles the editor module, 2. creates `/Game/Viewer` (materials `M_TwinSurface`, `M_TwinGlass`,
`M_TwinFlow`, level `L_Viewer`) with `twin/unreal/scripts/create_viewer_content.py`, 3. runs
`RunUAT BuildCookRun` (only `/Game/Viewer` and engine content are cooked; `/Game/Twins` — editor-imported,
per-user buildings — is excluded), 4. adds `Open Twin Viewer.cmd`, `README.txt`, `viewer_build.json`,
5. scans every packaged file for the AI key, the viewer secret, `.env` contents and key-shaped strings and
fails if any is found. Output: `dist/TwinViewer/Windows/` (git-ignored). `--config Shipping` builds the
release flavour (no console, smaller); `--zip` also writes a zip.

## Launching (another person)

1. Copy the `Windows` folder (or the zip) to their PC. If Windows reports missing runtime DLLs, run
   `Engine\Extras\Redist\en-us\UEPrereqSetup_x64.exe` once.
2. In the web app: **Generate & preview → Unreal Engine 5 → Open in desktop viewer** downloads
   `<project>.twinview`.
3. `"Open Twin Viewer.cmd" "<project>.twinview"` (or drag the file onto it). Or start
   `TwinWalkthrough.exe`, click **Copy launch code** in the web app and press **Ctrl+V** in the viewer.

Command line: `TwinWalkthrough.exe -TwinLaunch=<file.twinview>` or
`-TwinServer=<url> -TwinProject=<p> -TwinBuild=<b> -TwinTokenFile=<file>` (`-TwinToken=` works but is
visible in the process list), `-TwinNoCache`, `-TwinMaxModelMB=`, `-windowed -ResX= -ResY=`.

The backend must be reachable from the viewing PC: by default it binds to `127.0.0.1` (same PC). For other
PCs, put it behind a reverse proxy with TLS and set `TWIN_PUBLIC_URL` so launch files carry that address.

### Scripted check (automation)

`TwinWalkthrough.exe -TwinLaunch=<file> -TwinTour -TwinTourOut=<dir> [-TwinSwitchTo=<project>/<build>]
[-TwinTourRooms=N]` loads the building, waits for the navigation mesh, then records into
`<dir>/tour_report.json`: the load timings and in-engine validation, inventory, nav path + reachability
from the player-start room, click-to-walk arrival, wall collision, furniture selection, HVAC unit selection +
telemetry identity matching, the airflow layer (paths, dots, disclaimer, MATLAB state), the optional model
switch, screenshots, average fps — and exits (code 3 when the building cannot be loaded; the report then
holds `load_error`). `tests/viewer_harness.py` drives it.

## Pixel Streaming (prepared, not implemented)

Nothing here assumes a local user: all inputs come from the command line / launch file, all data from the
API, and the HUD is drawn in-engine. To stream to a browser later:

1. enable the `PixelStreaming2` plugin in `TwinWalkthrough.uproject` and package again;
2. run instances on a GPU host with `-PixelStreamingURL=ws://<signalling>:8888 -RenderOffscreen
   -TwinServer=<internal backend url> -TwinProject=<p> -TwinBuild=<b> -TwinTokenFile=<per-session file>`;
3. a small session broker (backend side) mints one short-lived, build-scoped token per browser session
   (`mint(projects=[p], build=b, ttl_hours=1)`), starts / reuses an instance and hands the browser the
   signalling URL — the token never reaches the browser;
4. the web app embeds the Pixel Streaming player instead of downloading a `.twinview` file.

No GPU streaming infrastructure is deployed by this repository.

## Tests

| Test | Kind | Proves |
|---|---|---|
| `tests/test_viewer.py` | automated (synthetic GLB written by the test) | token signing / scope / expiry / tamper, secret per data dir, log redaction, glTF→Unreal conversion incl. a rotated node, pre-flight catches a 2.5 cm misplacement, MATLAB state selection, API auth + scope + 401/403/404/409, HVAC live labelled simulated → live (measured) with unknown/missing units, airflow labelled illustration, launch link |
| `tests/test_viewer.py::test_blender_glb_matches_scene_model` | **real Blender** | the real glTF export matches the scene model in Unreal space |
| `tests/test_unreal_viewer.py` | **real packaged Unreal viewer** + real Blender | build → backend → packaged exe loads it at runtime: in-engine bounds check, nav path, click-to-walk, wall collision, selection, HVAC selection + telemetry identity, airflow layer, building switch, bad-token and unreachable-server errors exit cleanly, no secrets in the package |

### Verified (2026-10-14, UE 5.8.3, Windows 11, laptop GPU)

Both the **Development** and the **Shipping** packages passed `tests/test_unreal_viewer.py` (11/11 including a
private 38-room commercial floor with 509 furniture items and 30 HVAC units, ~1000 objects / 324k
triangles): runtime load in 1.5–2.1 s, every actor within 0.0002 cm of the scene model in Unreal, nav mesh
ready in ~1.5 s, click-to-walk arrival, wall collision, selection, 30/30 HVAC unit identities matched to
their readings, the airflow layer, a switch to a second 65-room building (1300 objects, 1024² textures) in
~2 s, cache reuse, and clean exits (code 3) for a tampered token, an expired link and an unreachable server.
Shipping package: 382 MB; average 78–84 fps during the scripted tours.

## Known limitations

* Procedural mesh components are drawn without Nanite and without mesh distance fields, so Lumen GI is
  screen-space only; room point lights do not cast shadows (performance on laptops).
* No multi-level buildings yet (the scene model has one level per build).
* The airflow layer is an illustration by design; there is no CFD in the viewer.
* Live BMS data arrives only when something pushes it to `POST /api/projects/{p}/hvac/telemetry`;
  otherwise the simulated BMS is shown and labelled as such.
* The launch route has no user accounts behind it yet (see OPERATIONS.md roadmap).
