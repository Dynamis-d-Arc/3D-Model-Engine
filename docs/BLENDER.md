# Blender worker

Tested with **Blender 5.2.2 LTS** (`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`, found
automatically; override with `TWIN_BLENDER_PATH`). All scripts run headless:
`blender --background --factory-startup --python-exit-code 3 --python <script> -- <args>`.
If Blender is missing, builds fail with `BLENDER_NOT_CONFIGURED` and nothing is reported as generated.

## Scripts (`twin/blender/scripts/`, run inside Blender, no `twin` imports)

| Script | Purpose |
|---|---|
| `build_library.py` | builds `assets_build/furniture_library_<hash>.blend` from `twin/assets/catalog.json` (55 assets: residential plus commercial-kitchen, F&B and back-of-house equipment; ~29k triangles total). Each asset is normalised to exactly its catalog size (origin at footprint centre on the floor, front −Y) and has material slots `ROLE_<role>`. Rebuilt automatically when the catalog or the script changes. |
| `build_scene.py` | builds the scene from `plan.json`, saves `scene.blend` (textures packed) **before** exporting, writes per-object FBX, full FBX, GLB, optional EEVEE renders and `blender_report.json` |
| `verify_exports.py` | fresh Blender: re-imports every FBX and the GLB, compares bounds with the plan (1 mm), reopens `scene.blend` and checks every object exists → `verify_report.json` |
| `_twinlib.py` | shared mesh builder (prisms from plan triangulations, boxes, cylinders), UVs, export helper |

Textures are generated on the backend (`twin/scene/textures.py`, tileable, deterministic) and cached in
`assets_build/textures`; UVs are written in metres / tile size so tiling survives FBX and glTF.

Patterns: `wood`, `tile`, `brick`, `marble`, `stone`, `concrete`, `fabric`, `plaster`, `resin` (poured floor),
`brushed` (stainless) modulate `base_color`; `panel` (insulated panel, joint every tile), `perforated`
(25 mm hole grid), `stack_tile` (75 x 300 mm stack bond), `endgrain` (end-grain block mosaic), `chevron`
(geometric upholstery) and `boards` (300 mm vertical boards) also use the material's `palette` and
`joint_color`. Fine patterns render at 1024 px per tile, the rest at 512 px.
A palette can be measured from site photos and applied the same way.

## Outputs per build

```
scene.blend                          Blender project (collections TWIN/ARCH/{Walls,Floors,Ceilings,Slab,Openings}, FURN/FURN_<room>, LIGHTS, CAMERAS)
export/unreal/arch/ARCH_*.fbx        one mesh per architectural object, pivot at world origin
export/unreal/furniture/SM_*.fbx     one mesh per furniture variant (asset + materials), pivot at footprint centre
export/fbx/scene_full.fbx            everything in one file (manual import, other DCCs)
export/web/scene.glb                 browser preview
preview/overview.png, room_*.png     EEVEE renders (optional)
manifest.json                        Unreal import manifest: transforms in cm, room info, hashes, sha256
```

Object naming: `ARCH_Wall_<wall>`, `ARCH_Floor_<room>`, `ARCH_Ceiling_<room>`, `ARCH_Slab_<level>`,
`ARCH_Door_<opening>_{Frame,Leaf}`, `ARCH_Window_<opening>_{Frame,Glass}`, `FURN_<room>_<item>_<asset>`.
Objects carry custom properties `twin_*` (kind, room, wall, opening, hash, collision).

## FBX export settings (`_twinlib.FBX_EXPORT_SETTINGS`)

| Setting | Value | Why |
|---|---|---|
| `apply_unit_scale` / `apply_scale_options` | True / `FBX_SCALE_UNITS` | 1 Blender unit = 1 m → 100 Unreal units |
| `global_scale` | 1.0 | |
| `axis_forward` / `axis_up` | `-Z` / `Y` (Blender defaults) | Unreal converts to its frame; verified: no rotation offset |
| `bake_space_transform` | False | known to mis-rotate parented meshes; objects are never parented |
| `mesh_smooth_type` | `FACE` | |
| `use_triangles` | True | deterministic triangulation of n-gons |
| `object_types` | `{MESH}` | lights/cameras are recreated in Unreal from the manifest |
| `path_mode` / `embed_textures` | `COPY` / True | textures travel inside the FBX |
| `add_leaf_bones` | False | |

Collision: architecture meshes are imported with complex-as-simple collision; furniture gets one
simple box when the editor API allows it (otherwise complex collision).

## Incremental builds

`plan.json` gives every object a content hash (geometry + materials, + library version for furniture).
The backend copies FBX files whose hash matches the previous completed build (`reuse.json`); the scene
`.blend`, full FBX and GLB are always rebuilt. Test: changing one room's floor finish re-exports exactly
`ARCH_Floor_<room>.fbx`.

## Running by hand

```bash
.venv/Scripts/python.exe -c "from pathlib import Path; from twin.config import load_settings; from twin.fixtures import load_sample_apartment; from twin.pipeline import run_build; print(run_build(load_sample_apartment(), Path('out/build'), load_settings(), build_id='manual').timings)"
```
