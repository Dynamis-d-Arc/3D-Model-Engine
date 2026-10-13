"""Generate the 3D scene from a compiled build plan.  Runs INSIDE Blender:

    blender --background --factory-startup --python build_scene.py -- \
        --plan plan.json --library furniture_library.blend --out-dir BUILD_DIR [--reuse reuse.json] [--render]

Outputs (relative to BUILD_DIR):
    scene.blend                         Blender project (textures packed)
    export/unreal/arch/<ARCH_*>.fbx     one FBX per architectural object, pivot at world origin
    export/unreal/furniture/SM_*.fbx    one FBX per furniture variant, pivot at footprint centre/floor
    export/fbx/scene_full.fbx           whole scene in one FBX (manual import / other DCCs)
    export/web/scene.glb                whole scene for the browser preview
    preview/*.png                       EEVEE renders (with --render)
    blender_report.json                 what was actually built: bounds, counts, files

Geometry comes only from the plan (deterministic); this script adds no measurements.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

from _twinlib import (MeshBuilder, box_uv, export_fbx, hex_to_linear, load_json, local_bounds,  # noqa: E402
                      mesh_world_bounds, progress, reset_scene, save_json, script_args, select_only,
                      set_smooth_by_angle)

ARCH_GROUPS = {"wall": "Walls", "floor": "Floors", "ceiling": "Ceilings", "slab": "Slab",
               "door_frame": "Openings", "door_leaf": "Openings", "window_frame": "Openings", "glass": "Openings",
               "hvac_unit": "HVAC", "hvac_duct": "HVAC", "hvac_diffuser": "HVAC", "hvac_grille": "HVAC"}


def make_material(mid: str, spec: dict) -> bpy.types.Material:
    mat = bpy.data.materials.new(f"MAT_{mid}")
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None) or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    color = hex_to_linear(spec["base_color"])
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = spec["roughness"]
    bsdf.inputs["Metallic"].default_value = spec["metallic"]
    if spec.get("texture"):
        img = bpy.data.images.load(spec["texture"], check_existing=True)
        img.colorspace_settings.name = "sRGB"
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, 200)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if spec["opacity"] < 1.0:
        bsdf.inputs["Alpha"].default_value = spec["opacity"]
        for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, value)
                except (TypeError, ValueError):
                    pass
    if spec.get("emission", 0) > 0:
        bsdf.inputs["Emission Color"].default_value = (*color, 1.0)
        bsdf.inputs["Emission Strength"].default_value = spec["emission"]
    mat.diffuse_color = (*color, spec["opacity"])
    mat["twin_material_id"] = mid
    return mat


def collection(name: str, parent: bpy.types.Collection) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        parent.children.link(col)
    return col


def build_arch(obj_spec: dict, mats: dict, tiles: dict, col: bpy.types.Collection) -> bpy.types.Object:
    used: list[str] = []
    for pr in obj_spec["prisms"]:
        for key in ("mat", "mat_pos", "mat_neg"):
            if key in pr and pr[key] not in used:
                used.append(pr[key])
    slot = {m: i for i, m in enumerate(used)}
    axis = obj_spec.get("axis")
    b = MeshBuilder()
    for pr in obj_spec["prisms"]:
        def mat_for(normal, pr=pr):
            if axis and "mat_pos" in pr:
                dot = normal.x * axis[0] + normal.y * axis[1]
                if dot > 0.7:
                    return slot[pr["mat_pos"]]
                if dot < -0.7:
                    return slot[pr["mat_neg"]]
            return slot[pr["mat"]]
        b.prism(pr["rings"], pr["tris"], pr["z0"], pr["z1"], mat_for)
    mesh = b.to_mesh(obj_spec["name"])
    for m in used:
        mesh.materials.append(mats[m])
    box_uv(mesh, scale_for_material=lambda i: tiles[used[i]])
    obj = bpy.data.objects.new(obj_spec["name"], mesh)
    col.objects.link(obj)
    for key in ("kind", "level_id", "room_id", "room_name", "wall_id", "opening_id", "hash", "collision",
                "hvac_id", "label"):
        if key in obj_spec and obj_spec[key] is not None:
            obj[f"twin_{key}"] = obj_spec[key]
    if obj_spec["kind"] == "glass" and hasattr(obj, "visible_shadow"):
        obj.visible_shadow = False
    return obj


def build_variants(plan: dict, library: str, mats: dict, tiles: dict) -> dict[str, bpy.types.Mesh]:
    needed = sorted({f["asset_id"] for f in plan["furniture"]})
    if not needed:
        return {}
    with bpy.data.libraries.load(library, link=False) as (data_from, data_to):
        missing = [a for a in needed if f"ASSET_{a}" not in data_from.objects]
        if missing:
            raise RuntimeError(f"furniture library lacks assets {missing}; rebuild the library")
        data_to.objects = [f"ASSET_{a}" for a in needed]
    assets = {o["twin_asset_id"]: o for o in data_to.objects}
    variants: dict[str, bpy.types.Mesh] = {}
    for f in plan["furniture"]:
        if f["variant"] in variants:
            continue
        src = assets[f["asset_id"]]
        mesh = src.data.copy()
        mesh.name = f"SM_{f['variant']}"
        slot_mats = []
        for i, m in enumerate(src.data.materials):
            role = m.name[len("ROLE_"):]
            mid = f["materials"][role]
            mesh.materials[i] = mats[mid]
            slot_mats.append(mid)
        box_uv(mesh, scale_for_material=lambda i, sm=slot_mats: tiles[sm[i]])
        set_smooth_by_angle(mesh, 35.0)
        mesh["twin_asset_id"] = f["asset_id"]
        mesh["twin_variant"] = f["variant"]
        variants[f["variant"]] = mesh
    for o in list(data_to.objects):
        old_mesh = o.data
        bpy.data.objects.remove(o)
        if old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
    for m in [m for m in bpy.data.materials if m.name.startswith("ROLE_") and m.users == 0]:
        bpy.data.materials.remove(m)
    return variants


def aim(obj: bpy.types.Object, direction) -> None:
    obj.rotation_euler = Vector(direction).normalized().to_track_quat("-Z", "Y").to_euler()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--library", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--reuse", help="json {export relative path: existing file} to copy instead of exporting")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args(script_args())
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    plan = load_json(args.plan)
    reuse = load_json(args.reuse) if args.reuse else {}
    report: dict = {"blender": bpy.app.version_string, "scene_hash": plan["scene_hash"], "objects": {},
                    "furniture": {}, "files": {}, "reused": [], "warnings": [], "renders": []}

    progress(0.02, "setup")
    reset_scene()
    scene = bpy.context.scene
    scene.name = "Twin"

    mats = {mid: make_material(mid, spec) for mid, spec in plan["materials"].items()}
    tiles = {mid: (spec["tile_size"] if spec.get("texture") else 1.0) for mid, spec in plan["materials"].items()}
    progress(0.08, "materials", count=len(mats))

    root = collection("TWIN", scene.collection)
    arch_col = collection("ARCH", root)
    furn_col = collection("FURN", root)
    light_col = collection("LIGHTS", root)
    cam_col = collection("CAMERAS", root)

    arch_objs = []
    n = max(1, len(plan["objects"]))
    for i, spec in enumerate(plan["objects"]):
        arch_objs.append(build_arch(spec, mats, tiles, collection(ARCH_GROUPS[spec["kind"]], arch_col)))
        if i % 8 == 0:
            progress(0.08 + 0.27 * (i + 1) / n, "architecture", object=spec["name"])

    variants = build_variants(plan, args.library, mats, tiles)
    furn_objs = []
    for f in plan["furniture"]:
        o = bpy.data.objects.new(f["name"], variants[f["variant"]])
        o.location = f["location"]
        o.rotation_euler = (0.0, 0.0, math.radians(f["rotation_deg"]))
        o.scale = f["scale"]
        collection(f"FURN_{f['room_id']}", furn_col).objects.link(o)
        for key in ("furniture_id", "room_id", "asset_id", "asset_name", "variant", "hash", "collision"):
            o[f"twin_{key}"] = f[key]
        o["twin_kind"] = "furniture"
        o["twin_size"] = f["size"]
        furn_objs.append(o)
    progress(0.45, "furniture", count=len(furn_objs), variants=len(variants))

    for lt in plan["lights"]:
        if lt["type"] == "sun":
            data = bpy.data.lights.new(lt["name"], "SUN")
            data.energy = lt["strength"]
            data.angle = math.radians(1.5)
        else:
            data = bpy.data.lights.new(lt["name"], "AREA")
            data.energy = lt["power_w"]
            data.shape = "SQUARE"
            data.size = lt["size"]
        data.color = hex_to_linear(lt["color"])
        o = bpy.data.objects.new(lt["name"], data)
        if "location" in lt:
            o.location = lt["location"]
        aim(o, lt["direction"])
        if "room_id" in lt:
            o["twin_room_id"] = lt["room_id"]
        light_col.objects.link(o)
    world = bpy.data.worlds.new("TwinWorld")
    scene.world = world
    if hasattr(world, "use_nodes") and not world.use_nodes:
        world.use_nodes = True
    bg = next((nd for nd in world.node_tree.nodes if nd.type == "BACKGROUND"), None) if world.node_tree else None
    if bg is not None:
        bg.inputs["Color"].default_value = (0.78, 0.78, 0.80, 1.0)
        bg.inputs["Strength"].default_value = 0.7
    world.color = (0.78, 0.78, 0.80)

    cams = {}
    for c in plan["cameras"]:
        data = bpy.data.cameras.new(c["name"])
        data.lens = c["lens_mm"]
        data.clip_start, data.clip_end = 0.05, 500.0
        o = bpy.data.objects.new(c["name"], data)
        o.location = c["location"]
        aim(o, [t - l for t, l in zip(c["target"], c["location"])])
        if "room_id" in c:
            o["twin_room_id"] = c["room_id"]
        cam_col.objects.link(o)
        cams[c["name"]] = o
    scene.camera = cams.get("CAM_Overview")
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = 1600, 900
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 32
        for attr in ("use_raytracing", "use_fast_gi"):
            if hasattr(scene.eevee, attr):
                setattr(scene.eevee, attr, True)
    progress(0.5, "lights_cameras")

    # ---- save the Blender project before any exporter runs ---------------------------------
    blend = out / "scene.blend"
    try:
        bpy.ops.file.pack_all()
    except RuntimeError as exc:
        report["warnings"].append(f"could not pack images: {exc}")
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=True)
    progress(0.55, "saved_blend")

    # ---- exports ---------------------------------------------------------------------------
    def emit(rel: str, objs) -> None:
        dst = out / rel
        if rel in reuse and Path(reuse[rel]).is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(reuse[rel], dst)
            report["reused"].append(rel)
        else:
            export_fbx(dst, objs)

    for i, o in enumerate(arch_objs):
        emit(f"export/unreal/arch/{o.name}.fbx", [o])
        if i % 8 == 0:
            progress(0.55 + 0.2 * (i + 1) / max(1, len(arch_objs)), "export_arch")
    tmp_col = collection("_EXPORT_TMP", scene.collection)
    for vname, mesh in variants.items():
        tmp = bpy.data.objects.new(mesh.name, mesh)
        tmp_col.objects.link(tmp)
        emit(f"export/unreal/furniture/{mesh.name}.fbx", [tmp])
        bpy.data.objects.remove(tmp)
    bpy.data.collections.remove(tmp_col)
    progress(0.8, "export_furniture")
    export_fbx(out / "export/fbx/scene_full.fbx", arch_objs + furn_objs)
    glb = out / "export/web/scene.glb"
    glb.parent.mkdir(parents=True, exist_ok=True)
    select_only(arch_objs + furn_objs)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True, export_apply=True,
                              export_texcoords=True, export_normals=True, export_materials="EXPORT",
                              export_cameras=False, export_lights=False, export_extras=True, export_yup=True)
    progress(0.88, "export_web")

    # ---- report what exists -----------------------------------------------------------------
    for o in arch_objs:
        me = o.data
        report["objects"][o.name] = {
            "kind": o.get("twin_kind"), "bounds": mesh_world_bounds(o), "vertices": len(me.vertices),
            "triangles": sum(len(p.vertices) - 2 for p in me.polygons),
            "materials": [m.name for m in me.materials], "hash": o.get("twin_hash"),
        }
    for o in furn_objs:
        lb = local_bounds(o)
        report["furniture"][o.name] = {
            "location": list(o.location), "rotation_deg": math.degrees(o.rotation_euler.z) % 360.0,
            "scale": list(o.scale), "local_bounds": lb,
            "dims": [(lb[1][k] - lb[0][k]) * o.scale[k] for k in range(3)],
            "bounds": mesh_world_bounds(o), "variant": o.get("twin_variant"), "mesh": o.data.name,
        }

    if args.render:
        render_previews(scene, cams, out, report)
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.suffix in {".blend", ".fbx", ".glb", ".png"}:
            report["files"][p.relative_to(out).as_posix()] = p.stat().st_size
    save_json(out / "blender_report.json", report)
    progress(1.0, "done")


def render_previews(scene, cams, out: Path, report: dict) -> None:
    ceilings = [o for o in scene.objects if o.get("twin_kind") == "ceiling"]
    try:
        for o in ceilings:
            o.hide_render = True
        scene.camera = cams["CAM_Overview"]
        scene.render.filepath = str(out / "preview" / "overview.png")
        bpy.ops.render.render(write_still=True)
        report["renders"].append("preview/overview.png")
        progress(0.93, "render_overview")
        for o in ceilings:
            o.hide_render = False
        scene.render.resolution_x, scene.render.resolution_y = 960, 540
        room_cams = [c for name, c in cams.items() if name != "CAM_Overview"]
        for i, cam in enumerate(room_cams):
            scene.camera = cam
            rel = f"preview/room_{cam.get('twin_room_id')}.png"
            scene.render.filepath = str(out / rel)
            bpy.ops.render.render(write_still=True)
            report["renders"].append(rel)
            progress(0.93 + 0.06 * (i + 1) / len(room_cams), "render_rooms")
    except Exception as exc:  # previews are optional; never fail the build for them
        report["warnings"].append(f"preview render failed: {exc}")
        traceback.print_exc()
    finally:
        for o in ceilings:
            o.hide_render = False
        scene.camera = cams.get("CAM_Overview")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(2)
