# Multi-floor buildings

Each floor of a building goes through the engine as its own project: floor plan (+ HVAC drawing, photos) → AI
interpretation → review → Blender build. A **multi-floor building** stacks those floor projects so the Unreal
viewer opens them as one building. Nothing about a floor changes: its versions, builds, HVAC layer, sensors and
live data stay with its project, and rebuilding one floor never touches the others.

Code: `twin/viewer/stacks.py` (definitions, alignment, heights, merged model + manifest), the routes in
`twin/viewer/routes.py`, the viewer's floor handling in `TwinViewerDirector` / `TwinPlayerController`.
Tests: `tests/test_stacks.py` (automated), the stack tour in `tests/test_unreal_viewer.py` (packaged viewer).

## Making one

**From a drawing set (automatic).** Drop the whole set on the start page: a multi-page PDF and / or one file per
floor, with the HVAC drawings, furniture lists, sensor sheets and photos. When the floor plans name two or more floors
(their titles, "LEVEL 2 FLOOR PLAN", "2ND STOREY", "GROUND FLOOR", "BASEMENT 1", or file names such as `L2_plan.pdf`),
every floor becomes its own project with its plan page, the HVAC drawings and CSV sheets that name the same floor and
every photo, and the floors are stacked bottom to top as one building named after the set. **Make the whole building**
(on any floor's Brief & files) then runs the autopilot for every floor that is not built yet, two at a time, and the
building opens in the viewer as soon as every floor is built. A floor that needs the user stops only itself; the rest
carry on. Sheets that name no floor (or several, like a section) are listed, never guessed. Code:
`twin/imaging/drawing_set.py`, `POST /api/drawing-sets`, the building run in `twin/api/autopilot.py`; tests:
`tests/test_buildings.py`. See [AUTOPILOT.md](AUTOPILOT.md).

**By hand.** **Generate & preview → Unreal Engine 5 → Multi-floor building → + Multi-floor building**: a name and the
floors, lowest first. Every floor needs a completed build. API:

| | |
|---|---|
| `POST /api/stacks` | `{"name", "floors": [{"project_id", "build_id"?, "label"?, "elevation_m"?, "offset_m"?: [dx, dy]}]}` |
| `GET /api/stacks[?project_id=]`, `GET/PUT/DELETE /api/stacks/{sid}` | the definition + the resolved plan (or `plan_error`) |
| `POST /api/drawing-sets` (multipart `files`, optional `name`) | a dropped set: `{"kind": "single", "project"}` or `{"kind": "building", "stack", "floors", "unassigned"}` |
| `POST /api/stacks/{sid}/autopilot` `{review_first, use_ai, furniture, furniture_max_usd, rebuild}` | job `building`: the autopilot for every floor not built yet; `GET` the same path = the newest run |
| `POST /api/stacks/{sid}/viewer/launch` | a `.twinview` link for the whole building (token scoped to the stack and its floor projects) |

A floor without `build_id` follows its project's newest completed build. Definitions are small JSON files in
`<data>/stacks/<sid>/stack.json`.

## How the floors are placed

**Plan position.** The floor plans of one building are usually separate sheets whose drawing origins differ (here
also different scales, 1:100 and 1:75). They share the structural grid, so the engine reads the dash-dot grid lines
of each floor-plan PDF (the matcher that aligns HVAC sheets, `twin.hvac.register`) and moves each floor onto floor 1
until the grid lines coincide (on a real two-floor test building: a (+10.71, +7.95) m shift, 7 + 8 grid lines
matched, 2 mm residual). Without a shared grid (raster plans, no PDF) the floors keep their drawing origins and the plan
carries a warning; `offset_m` sets the shift by hand.

**Height.** A floor's `elevation_m` is its floor finish. Default: the floor below's elevation + the top of
anything on that floor (its walls, or the ducts and ducted units in the ceiling void when they reach higher) +
2 cm + this floor's slab, rounded up to 5 cm. Example: 3.0 m ceilings, ducts up to 3.43 m, 0.2 m slab → floors
3.65 m apart. Give `elevation_m` (or a floor-to-floor height in the web dialog) to override; when a floor
would reach into the slab above, the plan says so.

## What the viewer gets

The stack is served on the normal viewer routes with project id = the stack id (`s_…`) and build id = a
*composite* id (`c_…`, a hash of the floors' builds, positions and names). The composite is written once to
`<data>/stacks/<sid>/composites/<cid>/`:

* `model.glb` — the floors' Blender glTF exports in one file, each floor's nodes under a parent node `Floor_F<n>`
  carrying its offset, every node name prefixed `F<n>_`;
* `viewer_manifest.json` — the floors' viewer manifests merged the same way (room / unit / furniture / sensor ids
  prefixed, room names led by the floor label, positions shifted) plus `floors[]` (label, elevation, offset,
  bounds, player start). Its preflight checks every node of the merged file against the merged expected bounds
  (the two-floor test building: 2333 objects, max 0.005 cm).

`hvac/live` merges the floors' feeds (unit ids prefixed like the manifest; `source` is `mixed` when one floor has
live BMS data and another the simulated BMS). `hvac/airflow` returns one illustration per floor with its offset;
the viewer places one airflow actor per floor there, so the mist and tracers stay in floor-local coordinates and each
floor shows its own MATLAB zones.

A rebuilt floor or a changed setting gives a new composite; older links keep working while their files exist
(a link to a composite that was never written says `STACK_CHANGED`).

## In the viewer

* **PgUp / PgDn** — next floor up / down. Walking: the same plan position on the new floor when it is inside a room
  there (and free of furniture), else that room's spawn point, else the nearest stair or lift, else the floor's
  entrance. Flying / ghost: the same view one storey up / down.
* The info panel shows the storey (`Level 2 (2 of 2)`); room names carry the floor label; the room list (M) lists
  every floor's rooms.
* **C** (ceilings off) — and the airflow view — also hides the floors above the current one: a cutaway into the
  floor you are on.
* Floors are not connected for walking yet (no stair geometry): click-to-walk and the tour's navigation checks
  stay on one floor.
* Room lights cast shadows, so a floor's lights stay on that floor (slabs block them); their brightness is the
  build's lumens x 0.25 (`-TwinRoomLightScale=`), and `-TwinNoRoomLights` shows sun and sky only.

The scripted tour (`-TwinTour`) adds, for a multi-floor building: a jump up through every floor and back down
(each landing checked: right floor, feet on its floor finish; `floor_F<n>.png`), and one cutaway overview per floor
(`floor_overview_F<n>.png`). Report: `floors` (`count`, `jumps`, `all_jumps_ok`).
