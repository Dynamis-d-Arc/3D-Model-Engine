# Scene model (schema 1.0.0)

The scene model is the source of truth for the whole pipeline. JSON Schema: `GET /api/schema/scene`
(generated from `twin/scene/model.py`).

## Units and coordinate system

* Metres everywhere; angles in degrees.
* Right-handed, **+X east** (right on the plan image), **+Y north** (up on the plan image), **+Z up**.
* Plan coordinates are **wall centrelines**. Room polygons follow centrelines, so neighbouring rooms
  share edges exactly. *Centreline* room size = polygon bounding box; *clear* size = wall face to wall
  face (polygon minus wall footprints). The UI shows clear sizes; the brief says which one you entered.
* Angles are counter-clockwise about +Z seen from above, 0 = +X.
* Furniture `position` = centre of the footprint at floor level; `rotation_deg = 0` means the item's
  front faces −Y (south); 90 faces +X.
* Elevations are relative to the level; `level.elevation` is absolute (floor finish).

| Target | Conversion |
|---|---|
| Blender | identity (scene unit scale 1.0, metres) |
| Unreal | `(x, y, z) m → (100x, −100y, 100z) cm`, `yaw = −rotation_deg`, scale unchanged |
| Plan image | `x = (px − origin_px)·m_per_px`, `y = (origin_py − py)·m_per_px` (calibration per upload) |
| three.js preview (glTF, Y up) | `(x, y, z) → (x, z, −y)` |

These are covered by `tests/test_transforms.py` and verified inside Unreal by the importer
(actor bounds vs. scene model).

## Entities

| Entity | Key fields |
|---|---|
| `project` | name, description, style (`modern`, `minimalist`, `industrial`, `luxury`, `scandinavian`), prompt |
| `levels[]` | id, elevation, wall_height, slab_thickness, ceilings |
| `rooms[]` | id, name, type, level_id, polygon (centreline, CCW), floor_offset, ceiling_height, finishes {floor, walls, ceiling, dado + dado_height, frieze + frieze_from}, open_plan (edges without a wall are intentional: no `W_ROOM_EDGE_NO_WALL`) |
| `walls[]` | id, level_id, start, end, thickness, height (null = level), kind exterior/interior, material |
| `openings[]` | id, kind door/window/opening, wall_id, offset (centre distance from wall start), width, height, sill_height, hinge_side start/end, opens_into |
| `furniture[]` | id, asset_id (catalog), room_id, position, elevation (omitted = the asset's `default_elevation`, e.g. 2.0 m for an exhaust hood), rotation_deg, size (null = catalog), materials {role: material}, label |
| `materials[]` | id, base_color (#RRGGBB sRGB), roughness, metallic, pattern, tile_size (m), opacity, emission, palette (extra colours), joint_color (grout/joints/holes) |
| `lighting` | mood, ceiling_lights, sun azimuth/elevation/strength |
| `sensors[]` | id, tag, kind, level_id, room_id, position, elevation, mounting wall/ceiling/free, unit_id, bms_point, label — a layer: changing it never changes the building, see [SENSORS.md](SENSORS.md) |
| `open_questions[]` | questions from the AI with severity and the user's answer |
| `provenance` | source (`user`/`ai`/`fixture`/`derived`/`manual`), AI model, request id |

Every room, wall, opening, furniture item and level carries a **certainty**:

| status | meaning | blocks generation? |
|---|---|---|
| `confirmed` | given or checked by the user (or a fixture) | no |
| `extracted` | read from the plan by the AI | yes, until confirmed or accepted |
| `estimated` | scaled or guessed | yes, until confirmed or accepted |
| `accepted_estimate` | the user knowingly accepted an estimate (listed in the build manifest) | no |
| `unresolved` | no usable value | yes (error) |

## Validation (`twin/scene/validate.py`)

Severity `error` and `review` block generation; `warning`/`info` do not.

| Code | Severity | Check |
|---|---|---|
| E_ROOM_POLYGON_INVALID / E_ROOM_TOO_SMALL | error | self-intersection, < 0.5 m² |
| E_ROOM_OVERLAP | error | rooms overlap > 0.01 m² |
| E_WALL_OVERLAP | error | collinear walls overlapping > 2 cm |
| W_WALL_OVERSHOOT / I_WALL_CROSSING | warning / info | crossing walls (stub < 0.3 m vs. real 4-way junction) |
| W_ROOM_EDGE_NO_WALL | warning | room boundary without a wall (open plan?) |
| E_OPENING_OUTSIDE_WALL / E_OPENING_AT_JUNCTION / E_OPENING_TOO_TALL / E_OPENING_OVERLAP | error | misplaced openings |
| W_DOOR_OPENS_INTO | warning | door swings into a room not adjacent to its wall |
| W_NO_ENTRANCE / W_ROOM_UNREACHABLE | warning | connectivity through doors/openings/open boundaries |
| E_FURNITURE_OUTSIDE_ROOM | error | centre outside its room |
| W_FURNITURE_CROSSES_BOUNDARY / W_FURNITURE_OVERLAP / W_DOOR_SWING_BLOCKED / W_FURNITURE_SCALE / W_FURNITURE_TOO_TALL | warning | placement quality (overlap ignores vertically separated items such as a hood over a range; scale allows 0.6-1.6x, or 0.25-4x for `stretch` assets) |
| E_UNKNOWN_ASSET | error | asset not in the catalog |
| R_CONFIRM_MEASUREMENT / E_UNRESOLVED_MEASUREMENT / Q_OPEN_QUESTION | review / error | certainty gate |
| W_OVERALL_MISMATCH / W_ROOM_DIM_MISMATCH / W_KNOWN_ROOM_MISSING | warning | cross-check against the brief (±5 cm or 2 %) |

## Geometry compilation (`twin/scene/compile.py`)

Vertical layout per level (relative to `elevation`): slab `[−slab, −0.02]`, floor finish `[−0.02, floor_offset]`,
walls `[−0.02, wall_height]`, ceiling `[ceiling_height, +0.02]`.

* **Junctions**: wall footprints are rectangles around the centreline, extended at joined ends by half
  the joining wall's thickness, then made disjoint by priority (exterior first, then longer walls), so
  corners and T/X-junctions close without overlapping solids.
* **Openings are real gaps**: each wall is cut into strips at opening edges (and where the room on
  either side changes); a strip covered by an opening keeps only the part below the sill and above the
  head. Door frames, open door leaves (90°, into `opens_into`), window frames and glass are separate objects.
* **Per-side finishes**: wall faces get the finish of the room they face; exterior faces the style's facade.
* **Wall bands**: a room's `dado` covers its wall faces from the floor to `dado_height` and its `frieze`
  from `frieze_from` to the top (metres above the level floor; `dado_height` < `frieze_from`). Wall pieces
  are split at the band heights of the rooms on both sides, so each piece still has one finish per side;
  the solid volume is unchanged.
* Every prism carries a constrained-Delaunay cap triangulation, expected bounds and a content hash.

### Sample apartment fixture (`twin/fixtures/sample_apartment.json`)

10.0 × 8.0 m on centrelines (10.2 × 8.2 m outer), 2.7 m walls, exterior 0.20 m, interior 0.10 m.
Clear sizes: Living/Dining 6.25 × 4.25, Kitchen 3.45 × 4.25, Bedroom 3.85 × 3.45, Bathroom 2.30 × 3.45,
Study 3.45 × 3.45. Wall solid volume = 9.295 m² × 2.72 m − 3.5816 m³ of openings = **21.7008 m³**
(asserted in `tests/test_compile.py`).
