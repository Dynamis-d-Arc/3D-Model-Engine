"""Create the packaged viewer's own content.  Runs INSIDE the UE editor (Python), on the PACKAGING machine:

    UnrealEditor-Cmd.exe TwinWalkthrough.uproject -run=pythonscript -script="create_viewer_content.py <report.json>"

Creates (replacing older versions):
    /Game/Viewer/Materials/M_TwinSurface   opaque, two-sided PBR: BaseColor, BaseColorTex x UseTex, Metallic, Roughness, Emissive
    /Game/Viewer/Materials/M_TwinGlass     translucent, two-sided: BaseColor, Opacity, Roughness
    /Game/Viewer/Materials/M_TwinFlow      unlit, instanced: colour from PerInstanceCustomData[0..2] x Glow (airflow dots)
    /Game/Viewer/L_Viewer                  empty level: sun, sky light, atmosphere, fog, post process, a movable
                                           1 m NavMeshBoundsVolume (moved over each building at runtime), TwinGameMode

Buildings are NOT content: the packaged viewer downloads them from the backend at runtime. Materials can only be
compiled by the editor, which is why this step exists; it never runs on the server.
"""

import json
import sys
import time
import traceback

import unreal

EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
ROOT = "/Game/Viewer"
MATS = f"{ROOT}/Materials"


def log(msg):
    unreal.log(f"[twin] {msg}")


def fresh_material(name):
    path = f"{MATS}/{name}"
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    return unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, MATS, unreal.Material, unreal.MaterialFactoryNew())


def scalar(mat, name, default, x, y):
    e = MEL.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, x, y)
    e.set_editor_property("parameter_name", name)
    e.set_editor_property("default_value", default)
    return e


def vector(mat, name, default, x, y):
    e = MEL.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, x, y)
    e.set_editor_property("parameter_name", name)
    e.set_editor_property("default_value", unreal.LinearColor(*default))
    return e


def finish(mat, report):
    MEL.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    report["materials"].append(mat.get_path_name())


def surface(report):
    mat = fresh_material("M_TwinSurface")
    mat.set_editor_property("two_sided", True)
    base = vector(mat, "BaseColor", (0.8, 0.8, 0.8, 1.0), -900, -250)
    tex = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -900, 0)
    tex.set_editor_property("parameter_name", "BaseColorTex")
    tex.set_editor_property("texture", unreal.load_asset("/Engine/EngineResources/DefaultTexture"))
    tex.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    use = scalar(mat, "UseTex", 0.0, -900, 250)
    lerp = MEL.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -550, 0)
    lerp.set_editor_property("const_a", 1.0)  # no texture: multiply by white
    MEL.connect_material_expressions(tex, "RGB", lerp, "B")
    MEL.connect_material_expressions(use, "", lerp, "Alpha")
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -300, -100)
    MEL.connect_material_expressions(base, "", mul, "A")
    MEL.connect_material_expressions(lerp, "", mul, "B")
    MEL.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)
    MEL.connect_material_property(scalar(mat, "Metallic", 0.0, -300, 150), "", unreal.MaterialProperty.MP_METALLIC)
    MEL.connect_material_property(scalar(mat, "Roughness", 0.6, -300, 250), "", unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.connect_material_property(vector(mat, "Emissive", (0.0, 0.0, 0.0, 1.0), -300, 350), "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    finish(mat, report)


def glass(report):
    mat = fresh_material("M_TwinGlass")
    mat.set_editor_property("two_sided", True)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    try:
        mat.set_editor_property("translucency_lighting_mode", unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
    except Exception as exc:  # noqa: BLE001 - optional nicety
        report["warnings"].append(f"glass lighting mode: {exc}")
    MEL.connect_material_property(vector(mat, "BaseColor", (0.6, 0.72, 0.76, 1.0), -400, -100), "", unreal.MaterialProperty.MP_BASE_COLOR)
    MEL.connect_material_property(scalar(mat, "Opacity", 0.2, -400, 50), "", unreal.MaterialProperty.MP_OPACITY)
    MEL.connect_material_property(scalar(mat, "Roughness", 0.05, -400, 150), "", unreal.MaterialProperty.MP_ROUGHNESS)
    spec = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -400, 250)
    spec.set_editor_property("r", 0.9)
    MEL.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)
    finish(mat, report)


def flow(report):
    mat = fresh_material("M_TwinFlow")
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("used_with_instanced_static_meshes", True)
    chans = []
    for i in range(3):
        c = MEL.create_material_expression(mat, unreal.MaterialExpressionPerInstanceCustomData, -900, -200 + i * 150)
        c.set_editor_property("data_index", i)
        c.set_editor_property("const_default_value", 0.75)
        chans.append(c)
    rg = MEL.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -650, -150)
    MEL.connect_material_expressions(chans[0], "", rg, "A")
    MEL.connect_material_expressions(chans[1], "", rg, "B")
    rgb = MEL.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -450, -100)
    MEL.connect_material_expressions(rg, "", rgb, "A")
    MEL.connect_material_expressions(chans[2], "", rgb, "B")
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -250, -100)
    MEL.connect_material_expressions(rgb, "", mul, "A")
    MEL.connect_material_expressions(scalar(mat, "Glow", 2.5, -450, 100), "", mul, "B")
    MEL.connect_material_property(mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    finish(mat, report)


def spawn(actor_sub, cls, loc, rot=(0.0, 0.0, 0.0), label=None):
    a = actor_sub.spawn_actor_from_class(cls, unreal.Vector(*loc), unreal.Rotator(roll=rot[0], pitch=rot[1], yaw=rot[2]))
    if a is None:
        raise RuntimeError(f"could not spawn {cls}")
    if label:
        a.set_actor_label(label)
    return a


def level(report):
    path = f"{ROOT}/L_Viewer"
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    world = unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
    if world is None:
        raise RuntimeError("could not create a blank map")
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    sun = spawn(actor_sub, unreal.DirectionalLight, (0.0, 0.0, 2000.0), (0.0, -50.0, 30.0), "Sun")
    comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
    comp.set_mobility(unreal.ComponentMobility.MOVABLE)
    comp.set_intensity(6.0)
    comp.set_editor_property("atmosphere_sun_light", True)
    sky = spawn(actor_sub, unreal.SkyLight, (0.0, 0.0, 1500.0), label="SkyLight")
    sc = sky.get_component_by_class(unreal.SkyLightComponent)
    sc.set_mobility(unreal.ComponentMobility.MOVABLE)
    sc.set_editor_property("real_time_capture", True)
    sc.set_intensity(1.5)
    spawn(actor_sub, unreal.SkyAtmosphere, (0.0, 0.0, 0.0), label="SkyAtmosphere")
    spawn(actor_sub, unreal.ExponentialHeightFog, (0.0, 0.0, -100.0), label="HeightFog")
    ppv = spawn(actor_sub, unreal.PostProcessVolume, (0.0, 0.0, 0.0), label="PostProcess")
    ppv.set_editor_property("unbound", True)
    pps = ppv.get_editor_property("settings")
    for prop, value in (("lumen_final_gather_quality", 2.0), ("lumen_scene_lighting_quality", 2.0), ("auto_exposure_bias", 0.5)):
        try:
            pps.set_editor_property(f"override_{prop}", True)
            pps.set_editor_property(prop, value)
        except Exception as exc:  # noqa: BLE001 - quality knobs are optional
            report["warnings"].append(f"post-process {prop}: {exc}")
    ppv.set_editor_property("settings", pps)
    nav = spawn(actor_sub, unreal.NavMeshBoundsVolume, (0.0, 0.0, 0.0), label="NavBounds")
    utils = getattr(unreal, "TwinEditorUtils", None)
    if utils is None or not utils.build_box_brush(nav, unreal.Vector(50.0, 50.0, 50.0)):
        raise RuntimeError("could not build the NavMeshBoundsVolume brush (TwinWalkthrough module not compiled?)")
    nav.root_component.set_mobility(unreal.ComponentMobility.MOVABLE)  # moved + scaled over each building at runtime
    gm = unreal.load_class(None, "/Script/TwinWalkthrough.TwinGameMode")
    if gm is None:
        raise RuntimeError("TwinGameMode class not found (C++ module not compiled)")
    world.get_world_settings().set_editor_property("default_game_mode", gm)
    if not unreal.EditorLoadingAndSavingUtils.save_map(world, path):
        raise RuntimeError(f"saving {path} failed")
    report["map"] = path


def main():
    t0 = time.time()
    report_path = sys.argv[-1]
    report = {"status": "running", "materials": [], "warnings": []}
    try:
        surface(report)
        glass(report)
        flow(report)
        level(report)
        EAL.save_directory(ROOT, only_if_is_dirty=True, recursive=True)
        report["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["trace"] = traceback.format_exc()[-4000:]
        unreal.log_error(f"[twin] viewer content failed: {exc}")
    report["seconds"] = round(time.time() - t0, 1)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    log(f"viewer content report: {report_path} status={report['status']}")


main()
