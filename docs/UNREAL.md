# Unreal Engine 5 walkthrough

> **For people viewing buildings use the packaged desktop viewer** ([UNREAL_VIEWER.md](UNREAL_VIEWER.md)): it
> loads builds from the backend at runtime and needs no Unreal Editor anywhere except on the machine that
> packages it. The editor import below is a developer / QA tool (and the way to make a cooked, hand-tuned level).

## What exists

* **Engine**: UE 5.8.3 at `C:\Program Files\Epic Games\UE_5.8` (auto-detected; `TWIN_UNREAL_ENGINE_DIR`).
* **Template project**: `unreal/TwinWalkthrough/TwinWalkthrough.uproject` (`TWIN_UNREAL_PROJECT`),
  C++ module `TwinWalkthrough` + PythonScriptPlugin + EditorScriptingUtilities. Lumen, virtual shadow
  maps, dynamic nav mesh (agent 30 cm radius / 180 cm), DX12.
  The existing `MCPTest` project was left untouched (it is an MCP connectivity test, not an
  architectural template).

## Prerequisites to compile the template

* Visual Studio 2022/2026 or Build Tools with the C++ workload, MSVC 14.4x/14.50, a Windows 10/11 SDK
  — present on this PC (VS 2026 Build Tools, MSVC 14.50, SDK 10.0.26100).
* **.NET Framework 4.6+ SDK.** Without it UnrealBuildTool stops with
  `Could not find NetFxSDK install dir; ... Install a version of .NET Framework SDK at 4.6.0 or higher`
  (required by the engine's SwarmInterface module for every editor target). On this PC the
  **.NET Framework 4.8.1 Developer Pack was installed on 2026-10-13** (winget, with the user's approval).
  Elsewhere install one of:
  * Visual Studio Installer → Build Tools 2026 → Modify → Individual components → **.NET Framework 4.8.1 SDK**
    (+ targeting pack), or
  * the **.NET Framework 4.8.1 Developer Pack** (`winget install Microsoft.DotNet.Framework.DeveloperPack_4`).

Then:

```bash
.venv/Scripts/python.exe -m twin.unreal.setup
```

which runs `Build.bat TwinWalkthroughEditor Win64 Development -Project=...` and checks that
`Binaries/Win64/UnrealEditor-TwinWalkthrough.dll` exists.

## Import (automated)

`POST /api/projects/{p}/builds/{b}/unreal/import` (button **Import into walkthrough**) runs
`UnrealEditor-Cmd.exe <project> -run=pythonscript -script="twin/unreal/scripts/import_twin.py <args.json>" -unattended -nullrhi`:

1. imports `export/unreal/arch/*.fbx` into `/Game/Twins/<project>/Arch/<name>_<hash8>` and furniture variants
   into `/Game/Twins/Shared/Furniture/SM_<variant>_<hash8>`; an asset whose `TwinHash` metadata matches is
   reused, so a regenerated room re-imports only what changed;
2. sets collision (architecture complex-as-simple, furniture box), creates `M_TwinGlass` (translucent);
3. creates `/Game/Twins/<project>/<build>/L_Walkthrough`: static mesh actors at manifest transforms with
   tags (`TwinKind`, `TwinRoom`, `TwinRoomName`, `TwinDims`, `TwinArea`, `TwinLabel`, `TwinAsset`, `TwinSize`,
   …), outliner folders by kind/room, directional light + sky light + sky atmosphere + height fog,
   per-room point lights (lumens), unbound post-process volume, ground plane, PlayerStart inside the
   entrance, NavMeshBoundsVolume, a project-info actor and the `TwinGameMode` override;
4. gives the NavMeshBoundsVolume a real box brush via `unreal.TwinEditorUtils.build_box_brush` (a volume
   spawned from a script has no brush and would bound nothing, so no nav mesh would be generated);
5. compares **every actor's world bounds** with the scene model (tolerance 1 cm) and writes
   `unreal/ue_import_report.json`.

After a successful import the job plays the map with `-TwinTour` (below) and stores the report and
screenshots under the build's `unreal/tour/`.

Headless notes (learned on UE 5.8): `LevelEditorSubsystem.new_level/save_current_level` and
`spawn_actor_from_object` do not work in commandlets; the importer uses
`EditorLoadingAndSavingUtils.new_blank_map` + `spawn_actor_from_class(StaticMeshActor)` +
`save_map`, and `StaticMeshEditorSubsystem` may be absent (falls back to `EditorStaticMeshLibrary`).

**Verified on 2026-10-13** (UE 5.8.3, sample apartment, `tests/test_unreal_integration.py`): 60 meshes
imported, 66 actors, max bounds deviation **0.0001 cm**; re-import reuses all 60 meshes; tour: nav path
Living/Dining → Study valid (7.8 m through the archway, kitchen and study door), every room reachable,
click-to-walk arrives within 30 cm, pushing into a wall stops at 35 cm (10 cm wall face + 25 cm capsule),
selection shows the double bed's info, ~130 fps at 1600×900, screenshots per room + overview + nav mesh.

Navigation settings that matter (`Config/DefaultEngine.ini`): the nav data is spawned from
`NavigationSystemV1.SupportedAgents` — without an entry it uses a 35 cm fallback agent that erodes 70 cm
doorways shut; cell size lives in `RecastNavMesh.NavMeshResolutionParams` (5 cm here), runtime generation
is Dynamic, agent radius 25 cm / height 180 cm (matches the visitor capsule).

## Coordinates

Manifest transforms are already in Unreal space: `(100x, −100y, 100z)` cm, `yaw = −rotation_deg`.
Architecture actors sit at the origin (vertices are in world space); furniture actors use the variant
mesh with location/yaw/scale.

## Walkthrough controls (`TwinPlayerController`, `TwinHUD`)

| Input | Action |
|---|---|
| WASD, mouse or arrow keys | move / look (eye height 1.6 m, capsule 1.8 m) |
| Shift / Space | run / jump |
| F | fly mode (E/Q or Space/Ctrl up/down) |
| G | ghost mode (fly through walls) |
| Left click | select the object under the crosshair → info panel + bounding box |
| Right click | walk there along the nav mesh |
| Tab, 1–9 | jump to the next / n-th room |
| C | ceilings on/off |
| X, H, P, Esc | clear selection, help, screenshot (`Saved/Screenshots`), quit |

The HUD shows the project, current room (name, clear size, area, floor material) and the mode.

## Scripted tour (automated check)

`UnrealEditor.exe <project> <map> -game -TwinTour -TwinTourOut=<dir>` records the actor inventory,
finds a nav path between the first and last room, path-follows it (click-to-walk), pushes into a wall
for 3 s (must stay inside the room), selects the largest furniture item, takes a screenshot per room,
one of the selection and an overview, writes `tour_report.json` and exits.
`tests/test_unreal_integration.py` (marker `unreal`) runs build → import → re-import → tour.

## Manual import (if automation is unavailable)

1. Download the **Unreal bundle** of a build (`export/unreal/**`, `manifest.json`).
2. Import the FBX files (Interchange defaults; scale 1.0; do not force front X axis).
3. Place architecture meshes at the origin and furniture at `manifest.furniture[].unreal`
   (`location_cm`, `yaw_deg`, `scale`), or run `import_twin.py` from the editor's Python console with an
   args file.
