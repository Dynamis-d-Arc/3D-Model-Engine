"""Import a verified build into the Unreal walkthrough project.  Runs INSIDE the UE editor (Python):

    UnrealEditor-Cmd.exe TwinWalkthrough.uproject -run=pythonscript -script="import_twin.py <args.json>"

args.json: {"manifest": ".../manifest.json", "build_dir": "...", "project_id": "p_x", "report": ".../ue_import_report.json"}

Content layout (re-imports only what changed, by content hash):
    /Game/Twins/<project>/Arch/<object>_<hash8>        architecture meshes (pivot at world origin)
    /Game/Twins/Shared/Furniture/SM_<variant>_<hash8>  furniture meshes shared by every project
    /Game/Twins/Shared/M_TwinGlass                     translucent glass material
    /Game/Twins/<project>/<build>/L_Walkthrough        the walkthrough level for this build

Coordinates: the manifest already holds Unreal transforms (cm, left-handed, yaw = -scene rotation).
After spawning, every actor's world bounds are compared with the scene model and the result
is written to the report; any deviation above tolerance is listed under "bounds_failures".
"""

import json
import math
import os
import sys
import time
import traceback

import unreal

TOL_CM = 1.0
EAL = unreal.EditorAssetLibrary


def log(msg):
    unreal.log(f"[twin] {msg}")


def slug(text):
    return "".join(c if c.isalnum() or c == "_" else "_" for c in text)


def tags_for(pairs):
    out = [unreal.Name("Twin")]
    for k, v in pairs.items():
        if v is None or v == "":
            continue
        out.append(unreal.Name(f"Twin{k}={v}"))
    return out


def ue_bounds_from_scene(bmin, bmax):
    """Scene AABB (m) -> Unreal AABB (cm): x*100, -y*100 (swaps min/max), z*100."""
    return ([bmin[0] * 100, -bmax[1] * 100, bmin[2] * 100], [bmax[0] * 100, -bmin[1] * 100, bmax[2] * 100])


def actor_bounds(actor):
    origin, extent = actor.get_actor_bounds(False)
    return ([origin.x - extent.x, origin.y - extent.y, origin.z - extent.z],
            [origin.x + extent.x, origin.y + extent.y, origin.z + extent.z])


def import_fbx(path, dest_folder, dest_name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", path)
    task.set_editor_property("destination_path", dest_folder)
    task.set_editor_property("destination_name", dest_name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    paths = list(task.get_editor_property("imported_object_paths") or [])
    meshes = [unreal.load_asset(p) for p in paths]
    meshes = [m for m in meshes if isinstance(m, unreal.StaticMesh)]
    if not meshes:
        candidate = f"{dest_folder}/{dest_name}"
        if EAL.does_asset_exist(candidate):
            m = unreal.load_asset(candidate)
            if isinstance(m, unreal.StaticMesh):
                meshes = [m]
    if not meshes:
        raise RuntimeError(f"FBX import produced no static mesh: {path} (imported: {paths})")
    return meshes[0]


def ensure_glass(folder):
    path = f"{folder}/M_TwinGlass"
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    mel = unreal.MaterialEditingLibrary
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset("M_TwinGlass", folder, unreal.Material,
                                                                   unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    try:
        mat.set_editor_property("translucency_lighting_mode", unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
    except Exception:  # noqa: BLE001 - optional nicety
        pass
    col = mel.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -400, -100)
    col.set_editor_property("constant", unreal.LinearColor(0.78, 0.86, 0.9, 1.0))
    mel.connect_material_property(col, "", unreal.MaterialProperty.MP_BASE_COLOR)
    for value, prop, y in ((0.16, unreal.MaterialProperty.MP_OPACITY, 50), (0.04, unreal.MaterialProperty.MP_ROUGHNESS, 150),
                           (0.9, unreal.MaterialProperty.MP_SPECULAR, 250)):
        c = mel.create_material_expression(mat, unreal.MaterialExpressionConstant, -400, y)
        c.set_editor_property("r", value)
        mel.connect_material_property(c, "", prop)
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


def _complex_as_simple(mesh):
    body = mesh.get_editor_property("body_setup")
    if body is not None:
        body.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)


def set_collision(mesh, kind, report):
    """Architecture: exact (complex-as-simple). Furniture: one simple box when the editor API allows it."""
    if kind != "furniture":
        return _complex_as_simple(mesh)
    for lib in (unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem), getattr(unreal, "EditorStaticMeshLibrary", None)):
        if lib is None:
            continue
        try:
            lib.remove_collisions(mesh)
            lib.add_simple_collisions(mesh, unreal.ScriptCollisionShapeType.BOX)
            return
        except Exception as exc:  # noqa: BLE001 - fall through to the next option
            report.setdefault("collision_notes", []).append(f"{type(lib).__name__}: {exc}")
    _complex_as_simple(mesh)
    if "furniture uses complex collision" not in report["warnings"]:
        report["warnings"].append("furniture uses complex collision (box-collision API unavailable in this context)")


def spawn_mesh(actor_sub, mesh, loc, yaw, scale, label, folder, tags, mobility=unreal.ComponentMobility.STATIC):
    # spawn_actor_from_object needs the level editor's actor factories (absent in commandlets);
    # spawning the class and assigning the mesh works everywhere.
    actor = actor_sub.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(*loc),
                                             unreal.Rotator(roll=0.0, pitch=0.0, yaw=yaw))
    if actor is None:
        raise RuntimeError(f"could not spawn {label}")
    actor.static_mesh_component.set_static_mesh(mesh)
    actor.set_actor_label(label)
    actor.set_actor_scale3d(unreal.Vector(*scale))
    actor.set_folder_path(folder)
    actor.set_editor_property("tags", tags)
    comp = actor.get_component_by_class(unreal.StaticMeshComponent)
    if comp is not None:
        comp.set_mobility(mobility)
    return actor


def spawn_class(actor_sub, cls, loc, rot=(0.0, 0.0, 0.0), label=None, folder="Twin/Environment"):
    actor = actor_sub.spawn_actor_from_class(cls, unreal.Vector(*loc), unreal.Rotator(roll=rot[0], pitch=rot[1], yaw=rot[2]))
    if label:
        actor.set_actor_label(label)
    actor.set_folder_path(folder)
    return actor


def hex_color(h):
    h = h.lstrip("#")
    return unreal.LinearColor(int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, 1.0)


def main():
    t0 = time.time()
    args = json.load(open(sys.argv[-1], encoding="utf-8")) if sys.argv[-1].endswith(".json") else \
        json.load(open(os.environ["TWIN_UE_ARGS"], encoding="utf-8"))
    report = {"status": "running", "warnings": [], "imported": [], "reused": [], "bounds_failures": []}
    report_path = args["report"]
    try:
        run(args, report)
        report["status"] = "ok" if not report["bounds_failures"] else "bounds_mismatch"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["trace"] = traceback.format_exc()[-4000:]
        unreal.log_error(f"[twin] import failed: {exc}")
    report["seconds"] = round(time.time() - t0, 2)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    log(f"report written: {report_path} status={report['status']}")


def run(args, report):
    manifest = json.load(open(args["manifest"], encoding="utf-8"))
    build_dir = args["build_dir"]
    project = slug(args["project_id"])
    build = slug(manifest["build_id"])
    root = "/Game/Twins"
    arch_folder = f"{root}/{project}/Arch"
    furn_folder = f"{root}/Shared/Furniture"
    map_path = f"{root}/{project}/{build}/L_Walkthrough"
    report["map"] = map_path
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    glass = ensure_glass(f"{root}/Shared")

    # ---- meshes (content-hash addressed; unchanged meshes are reused) --------------------------
    meshes = {}
    for o in manifest["objects"]:
        name = f"{o['name']}_{o['hash'][:8]}"
        path = f"{arch_folder}/{name}"
        if EAL.does_asset_exist(path) and EAL.get_metadata_tag(unreal.load_asset(path), "TwinHash") == o["hash"]:
            meshes[o["name"]] = unreal.load_asset(path)
            report["reused"].append(path)
            continue
        mesh = import_fbx(os.path.join(build_dir, o["fbx"]), arch_folder, name)
        set_collision(mesh, o["kind"], report)
        EAL.set_metadata_tag(mesh, "TwinHash", o["hash"])
        EAL.save_loaded_asset(mesh)
        meshes[o["name"]] = mesh
        report["imported"].append(path)
    variants = {}
    for f in manifest["furniture"]:
        if f["mesh"] in variants:
            continue
        name = f"{f['mesh']}_{f['variant_hash'][:8]}"
        path = f"{furn_folder}/{name}"
        if EAL.does_asset_exist(path) and EAL.get_metadata_tag(unreal.load_asset(path), "TwinHash") == f["variant_hash"]:
            variants[f["mesh"]] = unreal.load_asset(path)
            report["reused"].append(path)
            continue
        mesh = import_fbx(os.path.join(build_dir, f["fbx"]), furn_folder, name)
        set_collision(mesh, "furniture", report)
        EAL.set_metadata_tag(mesh, "TwinHash", f["variant_hash"])
        EAL.save_loaded_asset(mesh)
        variants[f["mesh"]] = mesh
        report["imported"].append(path)
    log(f"meshes ready: {len(report['imported'])} imported, {len(report['reused'])} reused")

    # ---- level ---------------------------------------------------------------------------------
    # LevelEditorSubsystem.new_level/save_current_level do not work in commandlets; a blank map
    # saved with EditorLoadingAndSavingUtils.save_map works headless and in the editor.
    if EAL.does_asset_exist(map_path):
        EAL.delete_asset(map_path)  # re-import of the same build replaces only its own map
    world = unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
    if world is None:
        raise RuntimeError("could not create a blank map")
    rooms = {r["id"]: r for r in manifest["rooms"]}
    spawned = {"arch": 0, "furniture": 0, "lights": 0}
    checks = []

    for o in manifest["objects"]:
        room = rooms.get(o.get("room_id")) if o.get("room_id") else None
        meta = {"Kind": o["kind"], "Name": o["name"], "Room": o.get("room_id"),
                "RoomName": room["name"] if room else None, "Wall": o.get("wall_id"), "Opening": o.get("opening_id")}
        if o["kind"] == "floor" and room:
            meta.update({"Dims": f"{room['dims_clear'][0]:.2f} x {room['dims_clear'][1]:.2f} m",
                         "Area": f"{room['area_net']:.1f} m2", "FloorMaterial": room["floor_material"],
                         "Spawn": ",".join(f"{v:.1f}" for v in room.get("spawn_point_cm", room["label_point_cm"])),
                         "Label": room["name"]})
        if o["kind"] in ("door_leaf", "door_frame", "window_frame", "glass"):
            meta["Label"] = {"door_leaf": "Door", "door_frame": "Door frame", "window_frame": "Window",
                             "glass": "Window glass"}[o["kind"]]
        if o["kind"] == "wall":
            meta["Label"] = f"Wall {o.get('wall_id')}"
        if o.get("label"):
            meta["Label"] = o["label"]
        folder = f"Twin/HVAC/{o['kind']}" if o["kind"].startswith("hvac_") else f"Twin/Architecture/{o['kind']}"
        actor = spawn_mesh(actor_sub, meshes[o["name"]], (0.0, 0.0, 0.0), 0.0, (1.0, 1.0, 1.0), o["name"],
                           folder, tags_for(meta))
        if o["kind"] == "glass":
            actor.get_component_by_class(unreal.StaticMeshComponent).set_material(0, glass)
        checks.append((actor, ue_bounds_from_scene(*o["bounds_m"]), o["name"]))
        spawned["arch"] += 1

    for f in manifest["furniture"]:
        ue = f["unreal"]
        w, d, h = f["size_m"]
        meta = {"Kind": "furniture", "Name": f["name"], "Id": f["furniture_id"], "Room": f["room_id"],
                "RoomName": f["room_name"], "Label": f["label"], "Asset": f["asset_id"],
                "Size": f"{w:.2f} x {d:.2f} x {h:.2f} m"}
        actor = spawn_mesh(actor_sub, variants[f["mesh"]], ue["location_cm"], ue["yaw_deg"], ue["scale"], f["name"],
                           f"Twin/Furniture/{f['room_name']}", tags_for(meta))
        # expected world AABB: rotated footprint in the scene, converted to Unreal
        a = math.radians(f["rotation_deg"])
        corners = [(f["location_m"][0] + lx * math.cos(a) - ly * math.sin(a), f["location_m"][1] + lx * math.sin(a) + ly * math.cos(a))
                   for lx, ly in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))]
        bmin = [min(c[0] for c in corners), min(c[1] for c in corners), f["location_m"][2]]
        bmax = [max(c[0] for c in corners), max(c[1] for c in corners), f["location_m"][2] + h]
        checks.append((actor, ue_bounds_from_scene(bmin, bmax), f["name"]))
        spawned["furniture"] += 1

    # ---- environment -----------------------------------------------------------------------------
    b = manifest["bounds_m"]
    (cx, cy, _), (sx, sy) = [(b[0][0] + b[1][0]) * 50, -(b[0][1] + b[1][1]) * 50, 0.0], \
                            ((b[1][0] - b[0][0]) * 100, (b[1][1] - b[0][1]) * 100)
    for lt in manifest["lights"]:
        if lt["type"] == "directional":
            sun = spawn_class(actor_sub, unreal.DirectionalLight, (cx, cy, 2000.0), (0.0, lt["pitch_deg"], lt["yaw_deg"]), "Sun")
            comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE)
            comp.set_intensity(lt["intensity_lux"])
            comp.set_light_color(hex_color(lt["color"]))
            comp.set_editor_property("atmosphere_sun_light", True)
        else:
            x, y, z = lt["location_cm"]  # 45 cm below the ceiling so the ceiling itself is lit
            pl = spawn_class(actor_sub, unreal.PointLight, (x, y, z - 45.0), label=lt["name"], folder="Twin/Lights")
            comp = pl.get_component_by_class(unreal.PointLightComponent)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE)
            comp.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
            comp.set_intensity(lt["lumens"])
            comp.set_attenuation_radius(lt["radius_cm"])
            comp.set_light_color(hex_color(lt["color"]))
            comp.set_editor_property("source_radius", 30.0)
            comp.set_editor_property("soft_source_radius", 60.0)
        spawned["lights"] += 1
    sky = spawn_class(actor_sub, unreal.SkyLight, (cx, cy, 1500.0), label="SkyLight")
    sky_comp = sky.get_component_by_class(unreal.SkyLightComponent)
    sky_comp.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_comp.set_editor_property("real_time_capture", True)
    sky_comp.set_intensity(1.5)
    spawn_class(actor_sub, unreal.SkyAtmosphere, (cx, cy, 0.0), label="SkyAtmosphere")
    spawn_class(actor_sub, unreal.ExponentialHeightFog, (cx, cy, -100.0), label="HeightFog")
    ppv = spawn_class(actor_sub, unreal.PostProcessVolume, (cx, cy, 0.0), label="PostProcess")
    ppv.set_editor_property("unbound", True)
    pps = ppv.get_editor_property("settings")
    for prop, value in (("lumen_final_gather_quality", 2.0), ("lumen_scene_lighting_quality", 2.0),
                        ("auto_exposure_bias", 0.5)):
        try:
            pps.set_editor_property(f"override_{prop}", True)
            pps.set_editor_property(prop, value)
        except Exception as exc:  # noqa: BLE001 - quality knobs are optional
            report["warnings"].append(f"post-process {prop}: {exc}")
    ppv.set_editor_property("settings", pps)

    ground_z = b[0][2] * 100 - 1.0
    plane = unreal.load_asset("/Engine/BasicShapes/Plane")
    if plane:
        g = spawn_mesh(actor_sub, plane, (cx, cy, ground_z), 0.0, (max(sx, sy) * 6 / 100, max(sx, sy) * 6 / 100, 1.0),
                       "Ground", "Twin/Environment", [unreal.Name("TwinGround")])
        g.get_component_by_class(unreal.StaticMeshComponent).set_material(
            0, unreal.load_asset("/Engine/EngineMaterials/DefaultMaterial") or g.get_component_by_class(unreal.StaticMeshComponent).get_material(0))

    ps = manifest["player_start"]["unreal"]
    loc = ps["location_cm"]
    spawn_class(actor_sub, unreal.PlayerStart, (loc[0], loc[1], loc[2] + 100.0), (0.0, 0.0, ps["yaw_deg"]), "PlayerStart",
                "Twin/Gameplay")
    nav = spawn_class(actor_sub, unreal.NavMeshBoundsVolume, (cx, cy, 100.0), label="NavBounds", folder="Twin/Gameplay")
    half = unreal.Vector((sx + 400) / 2.0, (sy + 400) / 2.0, 300.0)
    utils = getattr(unreal, "TwinEditorUtils", None)
    if utils is None or not utils.build_box_brush(nav, half):
        # without a brush the volume bounds nothing and no nav mesh is generated
        raise RuntimeError("could not build the NavMeshBoundsVolume brush (TwinEditorUtils missing?)")
    # the RecastNavMesh actor is created by the navigation system at runtime (RuntimeGeneration=Dynamic)

    info = spawn_class(actor_sub, unreal.TargetPoint, (cx, cy, 0.0), label="TwinProjectInfo", folder="Twin/Gameplay")
    info.set_editor_property("tags", tags_for({"Kind": "project", "Name": manifest["project"]["name"],
                                               "Build": manifest["build_id"], "Style": manifest["project"]["style"],
                                               "Rooms": len(manifest["rooms"])}))

    gm = unreal.load_class(None, "/Script/TwinWalkthrough.TwinGameMode")
    if gm is not None:
        world.get_world_settings().set_editor_property("default_game_mode", gm)
    else:
        report["warnings"].append("TwinGameMode class not found (C++ module not compiled); the map uses the default game mode")

    # ---- verify placement inside Unreal ------------------------------------------------------------
    worst = 0.0
    for actor, (emin, emax), name in checks:
        gmin, gmax = actor_bounds(actor)
        dev = max(abs(a - e) for a, e in zip(gmin + gmax, emin + emax))
        worst = max(worst, dev)
        if dev > TOL_CM:
            report["bounds_failures"].append({"actor": name, "deviation_cm": round(dev, 3),
                                              "expected": [emin, emax], "got": [gmin, gmax]})
    report["bounds_checked"] = len(checks)
    report["max_bounds_deviation_cm"] = round(worst, 4)
    report["spawned"] = spawned
    if not unreal.EditorLoadingAndSavingUtils.save_map(world, map_path):
        raise RuntimeError(f"saving the level {map_path} failed")
    EAL.save_directory(f"{root}/{project}", only_if_is_dirty=True, recursive=True)
    EAL.save_directory(f"{root}/Shared", only_if_is_dirty=True, recursive=True)
    report["static_meshes"] = len(meshes) + len(variants)
    report["actors"] = spawned["arch"] + spawned["furniture"]
    log(f"level saved: {map_path}")


main()
