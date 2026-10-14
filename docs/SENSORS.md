# Sensors and in-place model updates

The first time a building goes through the engine, the full pipeline runs once:

1. upload the floor plan, HVAC drawing and site photos;
2. the AI interprets them;
3. the user validates and reviews the layout;
4. Blender generates the 3D model (the browser preview, the Live tab and the Unreal viewer display it);
5. the build is saved: its scene version, geometry, HVAC inventory, equipment ids, source files and
   exports. Versions and builds are immutable, so this saved twin never changes afterwards.

Later changes that do not touch the building — today: **sensors** — do not go through steps 1–4 again:

6. open the saved twin (Review loads the latest version as it is);
7. add sensors: click on the plan, or import a placement sheet (CSV);
8. validate the change: sensor checks, plus "what changed since vN" (sensors added / moved / edited /
   removed, and whether anything else changed);
9. **update the model in place**: the saved build's files are reused as they are and only the sensor layer is
   written on top (no AI, no Blender — about a second, even on a large building);
10. save: the edit is a new version (vN+1) and the update a new build; the older version and build stay as they
   were, so any version can be compared with another or restored.

Change the building itself (rooms, walls, doors, furniture, HVAC, materials, …) and step 9 is refused: that
version is generated again with Blender (unchanged exports are still reused, see ARCHITECTURE.md).

## The sensor model (`scene.sensors[]`, `twin/scene/model.py`)

| field | meaning |
|---|---|
| `id`, `tag` | internal id (`S12`) and the tag used on site / in the BMS (`TS-03`, unique) |
| `kind` | `temperature`, `humidity`, `temp_humidity`, `co2`, `pressure`, `occupancy`, `airflow`, `energy`, `other` |
| `level_id`, `room_id` | room null = the room it stands in |
| `position` | plan location (metres); wall sensors placed in the editor snap to the nearest wall face |
| `elevation` | centre height above the floor; null = 1.5 m (wall / free-standing) or just under the ceiling (`mounting: ceiling`) |
| `mounting` | `wall`, `ceiling`, `free` |
| `unit_id` | the HVAC unit it monitors (follows the unit by tag when the HVAC drawing is re-imported) |
| `bms_point` | BMS / IoT point name of its live reading |
| `label`, `certainty` | as for other entities; sensors placed at a room centre from a sheet are `estimated` |

Validation (`twin/scene/sensors.py: check_sensors`): `E_SENSOR_OUTSIDE_ROOM`, `E_SENSOR_DUPLICATE_TAG`,
`E_SENSOR_ABOVE_CEILING`, `E_SENSOR_BELOW_FLOOR`, `E_SENSOR_LEVEL`, `W_SENSOR_OUTSIDE_ROOMS`,
`W_SENSOR_UNIT_LEVEL`, `W_SENSOR_DUPLICATE_POINT`, `R_CONFIRM_SENSORS` (estimated positions block the update
until confirmed or accepted), `I_SENSOR_NO_POINT`.

In 3D a sensor is a 12 cm octagonal puck with a cap in the kind's colour (`SENSOR_<id>`, collision none,
selectable). The browser preview adds a pin over every sensor so they read in the overview.

## How the in-place update works (`twin/layers.py`)

* **Full builds** keep Blender's glTF as `export/web/geometry.glb` and compose `export/web/scene.glb` =
  geometry + sensor layer. Every viewer loads `scene.glb`. The `.blend`, FBX files and preview renders contain
  the building only.
* **Layer builds** (`builds.kind = 'layer'`) start from a completed build whose building is identical:
  `geometry_key(scene)` = the scene without `sensors`, `provenance` and `open_questions`. Every file of that build
  is hard-linked (copied where the filesystem has no hard links); `plan.json`, `scene.json`, `manifest.json`,
  `sensors.json`, `layer_report.json` and `scene.glb` are written new.
* The composed glTF only **appends**: the building's JSON arrays and binary chunk are extended, never rewritten.
  `layer_report.json` proves it — the geometry binary is a byte-identical prefix, existing nodes, meshes,
  accessors, buffer views, materials, images and textures are unchanged, and every sensor node's bounds match the
  scene model within 1 mm.
* The manifest says `build_kind`, `base_build`, `geometry_build` (the Blender build the geometry comes from) and
  lists `sensors[]` (scene metres + Unreal cm) and `layers.sensors.changes`.
* The packaged Unreal viewer gets the sensors as viewer-manifest objects (`kind: sensor`), so its pre-flight and
  in-engine bounds checks cover them; no viewer rebuild is needed. The editor import (FBX per object) does not
  include sensors.

Measured on a 65-room, 653-furniture, 40-unit floor: Blender build 439 s, sensor layer update 2.4 s
(validation, 762 files linked, glTF composed and re-checked, 1 318 objects passing the viewer pre-flight).

## API

| | |
|---|---|
| `GET /api/projects/{p}/versions/{n}/build-options` | `layer.possible`, `base_build`, sensor `changes`, `up_to_date`; or the `reason` (what changed in the building) |
| `POST /api/projects/{p}/versions/{n}/build` | `mode`: `full` (default, Blender), `layer` (409 `LAYER_UPDATE_NOT_POSSIBLE` if the building changed), `auto`; optional `base_build` |
| `GET /api/projects/{p}/versions/{n}/changes?against=m` | counts per collection, `geometry_changed`, sensor changes, full diff (default: against the parent) |
| `POST /api/scene/import-sensors` | `{scene, text}` → the scene with the sheet merged (not saved), `added`, `updated`, `problems` |
| `GET /api/sensors/template.csv` | placement sheet template |
| `POST /api/projects/{p}/hvac/telemetry` | also `sensors: [{sensor: tag / id / BMS point, value, humidity?, ts?}]` |
| `GET /api/projects/{p}/hvac/live?version=n` | also `sensors: {id: {value, unit, humidity?, source: live / simulated, room_id}}` |

Saved versions record `changes` against their parent (shown in the version list, e.g. `+3 sensors`).

## Placement sheet (CSV)

```
tag,kind,x,y,elevation,mounting,room,unit,bms_point,label
TS-01,temperature,4.20,2.10,1.5,wall,,,AHU1.ZONE1.TEMP,
CO2-01,co2,,,,ceiling,Living,,,placed at the room centre
```

`tag` is required. `x`/`y` (plan metres) place a sensor exactly; with only `room` (name or id) it goes to the room
centre as an estimate to move later. `kind` accepts common abbreviations (`temp`, `rh`, `t/rh`, `dp`, `pir`, `kwh`,
…), `unit` a unit tag or id. A row whose tag already exists updates that sensor; empty cells keep its values.
Rows with problems are skipped and listed, never guessed. The merged scene appears as unsaved edits in Review.

## Live readings

The Live tab lists every sensor of the version with its reading and shows them in 3D. Real readings pushed to
`/hvac/telemetry` (by tag, id or BMS point) are used per sensor while fresh (5 minutes); otherwise the reading
is simulated consistently with the simulated or live unit data (zone air temperature of the linked unit, CO2 and
occupancy from the occupancy curve). Simulated values are labelled as such.
