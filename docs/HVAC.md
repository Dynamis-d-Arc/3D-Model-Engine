# HVAC: drawing import, live data, airflow simulation

```
HVAC drawing (vector PDF) ─► register on the structural grid ─► OCR labels ─► detect symbols ─► place + link
                                     (twin.hvac.register)        (ocr)          (detect)        (importer)
                                                                                                    │
     live BMS readings ──► /hvac/telemetry ─┐                                                       ▼
     simulated BMS (demo) ──────────────────┼─► /hvac/live ─► airflow illustration    ◄─ scene.hvac ─► Blender / GLB / Unreal
     MATLAB thermal results ─► /thermal-results ─┘          (one zone per unit)         (HVAC_* objects)
                                            └─ unit setpoint, supply air, airflow, on/off
```

## 1. Importing an HVAC drawing

Upload the drawing in **Brief & files → HVAC drawing**, then **Review layout → HVAC → Import HVAC drawing**.
The import runs in its own process (`python -m twin.hvac.worker`, ~50 s for a 40-unit A1 sheet; OCR is cached per file)
and stores a **new version** with status *needs review*, a report and a plan overlay image.

| Step | Code | What it does |
|---|---|---|
| Layers | `twin/hvac/sheet.py` | Splits the sheet's strokes into the architectural underlay (grey, halftone) and the services layer (black/colour). |
| Registration | `twin/hvac/register.py` | Finds the dash-dot structural grid lines on both sheets and matches their spacing → translation from HVAC-sheet points into the floor plan's plan frame. The sheets may be drawn at different scales (e.g. an HVAC sheet at 1:100 on a floor plan at 1:75): the standard scale ratios are searched and the one matching the most grid lines wins. Falls back to raster phase correlation. A manual `dx_pt / dy_pt` can be passed to the API. |
| Labels | `twin/hvac/ocr.py` | CAD text is often vector strokes (some sheets have no text layer), so the services layer is rendered and read with RapidOCR (ONNX, offline, CPU) in overlapping tiles; a second, closer pass re-reads airflows around diffusers and units that came back unlabelled. |
| Symbols | `twin/hvac/detect.py` | Supply diffusers = nested squares; return grilles = rectangle + one diagonal; fan-coil units = two concentric marker boxes on the casing's long axis (+ casing outline); wall/exposed units = a casing box next to a tag; ducts = mutually nearest parallel strokes whose spacing matches a duct width printed on the sheet (`SAD 750x250` = supply duct 750 wide × 250 high), plus duct-shaped quads and tapered transitions; leader lines are removed first. |
| Placement | `twin/hvac/importer.py` | Drops anything outside the building, assigns rooms, links every terminal to its unit by following the duct network (gaps ≤ 0.4 m bridged), then uses the designer's airflow balance (unit airflow ≈ sum of its diffusers) to settle ambiguous links. Airflows come from labels; missing ones are estimated from the unit's airflow and flagged. One unreadable tag that is the only number missing from a complete sequence is inferred. |

Every element carries a certainty: `extracted` (read from the drawing) or `estimated` (a value was inferred).
Validation shows **one review item per kind** (`R_CONFIRM_HVAC`), not hundreds; *Accept all remaining
estimates* clears them. Other checks: `W_HVAC_AIRFLOW_BALANCE` (unit vs. its diffusers, ±15 %),
`W_HVAC_TERMINAL_UNLINKED`, `W_HVAC_OUTSIDE_ROOMS`.

**Reference result** (a 40-unit A1 services sheet on a 1:100 plan): all 7 + 13 grid lines matched, residual
2.8 mm; 40 units (39 tags read, 1 inferred), 83 supply diffusers, 57 return grilles, 199 duct runs (174 m),
16 units balanced within ±2 %. The report lists the rest for review or *Fix with AI*.

Limits: vector PDFs only (scanned HVAC plans need a vision-AI pass, not built yet); duct runs are straight
pieces joined at elbows, so some fittings and flexible connections are missing; symbol rules are tuned to
common CAD blocks (other offices' blocks may need new rules); one level per import.

## 2. In the 3D model

`twin/scene/compile.py::compile_hvac` turns the HVAC layer into prism objects (`HVAC_Unit_*`, `HVAC_Duct_*`,
`HVAC_Diffuser_*`, `HVAC_Grille_*`): ducted units and ducts in the ceiling void (`plenum_clearance` above
the room's ceiling), exposed units just below it, wall splits at 2.2 m, diffuser/grille faces in the ceiling
plane with a neck up to the duct level. They go through the normal Blender build, export verification
(1 mm), GLB and the Unreal import (folder `Twin/HVAC`, label tag on every actor).

## 3. Fix with AI

**Review → Fix with AI** (or select an element first; also on a picked 3D object in *Generate*). The
request, the selected entities and a compact scene JSON (~49k tokens for a 65-room floor, ≈ $0.12 with a $2/$10 model) go to the
configured model, which answers with edit operations (`add` / `update` / `delete` on rooms, walls,
openings, furniture, materials, units, terminals, ducts). They are applied, validated by the same schema
and validator as manual edits, sent back once with the errors if invalid, and stored as a new version.
AI-changed values are marked `accepted_estimate` with source `ai`. Code: `twin/ai/fix.py`,
`POST /api/projects/{pid}/versions/{n}/fix`.

## 4. Live data and the airflow (tab *4 · Live HVAC*)

**Readings** (per unit): `on, mode, setpoint_c, supply_air_c, return_air_c, fan_speed, airflow_lps,
room_temp_c, alarm`; site: `outdoor_c, occupancy`.

* Push real data: `POST /api/projects/{pid}/hvac/telemetry` with `{"readings": [{"unit": "1FC-23",
  "supply_air_c": 13.6, "return_air_c": 24.1, "fan_speed": 0.8}], "site": {"outdoor_c": 33}}` (units by tag
  or id). Set `TWIN_HVAC_INGEST_TOKEN` to require an `X-Twin-Token` header. Readings live in memory (latest
  + short history); a deployment would add a time-series store.
* `GET /api/projects/{pid}/hvac/live?version=N` returns live data when a reading arrived in the last
  5 minutes, else the **simulated BMS** (`twin/hvac/telemetry.py`): Bangkok outdoor temperature and a
  food-school occupancy curve, each unit holding its setpoint with the supply temperature that its zone's
  heat balance needs (coil limited to 12 °C, so undersized zones run warm), and one unit "trips" for 4
  minutes every 20 so the twin has something to react to. The panel always says which source is shown.

**The airflow in the Live tab** is an animated illustration, not CFD: it only needs three things per AC unit
(where it is, whether it blows, how warm the air is). Walls and furniture do not block the flow; the clouds
are only clipped to the floor outline.

* **Zones** (`twin/hvac/illustration.py`, `GET .../versions/{n}/airflow-zones`): one per unit, a box of ±3 m
  around it clamped to the floor outline (rooms + walls, simplified to ≤ 64 vertices). Everything is in the viewer
  frame (x, height, z = -plan y); there is no shifted zone frame as in the study.
* **Outlets**: the jet leaves the casing's discharge face, 8 cm clear of it, along the casing axis
  that faces the unit's supply duct (ducted units; else its nearest supply diffuser) or its own room (exposed / wall
  units). It is kept just below the ceiling (ducted casings hang above it here). Without a face the default rule
  applies: away from the nearest box edge along ±x / ±z.
* **Paths** (`frontend/js/airflow/school-flow.js`): 6 closed Catmull-Rom loops per outlet, 192 samples each: level
  out of the louvres, down to 1.5 m and 0.65 m, back along the floor at 0.35 m and up again; reach
  min(8.5 m, 0.82 × box span); every sample kept in the box and the floor polygon.
* **Rendering** (`frontend/js/airflow/illustration.js`), per zone: a cloud (box raymarched in 16 steps; air
  temperature = mix(front, rear) + 0.13 (y − 1.5) − drop × jet, jet = Gaussian beam from the outlet falling 2.1 m over
  its reach; 12–36 °C ramp with an animated ripple), 6 tracer dots per path (moving only while `playing`), faint path
  lines. With the clouds on, walls and openings are drawn at 12 % and furniture at 30 % on a dark slab; with them off
  the floor shows the west / east zone colours (18–34 °C). Cutaway walls at 1.05 m.
* **Drivers** (`frontend/js/airflow/thermal.js`):
  * *MATLAB day* (default when the project has results; `GET/PUT/DELETE /api/projects/{pid}/hvac/thermal-results`,
    see `validate_thermal_results`): setpoint scenario (18–25 °C), timeline 0–8 h with Play, metrics and
    a west/east chart. Each unit takes its floor's west or east temperature (x < split line);
    drop = min(t − 12, cooling / (airflow m³/s × 1.2 × 1005)) with cooling = (AC kW − units × 0.06) × coolingKW/inputKW.
    The results floor is the one whose unit tags match the scene's units.
  * *Live airflow* chip (remembered; always on without results): the BMS readings (`/hvac/live`: pushed telemetry,
    else the simulated BMS). Zone means of the units' room / return-air temperatures; units that are off are not
    drawn; running units take their own temperature and drop = min(t − 12, 7 kW / (unit airflow × 1.2 × 1005)).
* **Isolate a room**: clipping planes at the room rectangle + 0.3 m, everything without a part in the
  room moved to a layer the camera does not draw, zones whose outlet is not within 0.6 m of the room muted, the
  room's clouds pulled in to its rectangle, the tracer dots clipped.

The earlier real-time voxel solver (`frontend/js/airflow/solver.js` + `worker.js`, `GET .../airflow-domain`) is not
used by the Live tab any more; the domain's per-unit loads still drive the simulated BMS (`twin/hvac/telemetry.py`).

## Tests

`tests/test_hvac.py`: model references, compiler placement, validation, grid registration, symbol
detection on synthetic vector primitives, label parsing, airflow domain, simulated-BMS heat balance,
telemetry store, AI fix operations (mocked model, retry on invalid edits), HVAC API, the domain's outlets,
furniture obstacles and relief cells, the room domain, the browser solver in Node (no NaN, solids stay still,
cooling then warming when units switch off; a fine room grid stays stable, balanced through the relief cells,
with well-formed volume bytes and streamlines), and an optional `realdata` regression on private drawings (`tests/local/`, not in the repository).
