"""Create the packaged viewer's own content.  Runs INSIDE the UE editor (Python), on the PACKAGING machine:

    UnrealEditor-Cmd.exe TwinWalkthrough.uproject -run=pythonscript -script="create_viewer_content.py <report.json>"

Creates (replacing older versions):
    /Game/Viewer/Materials/M_TwinSurface   opaque, two-sided PBR: BaseColor, BaseColorTex x UseTex, Metallic, Roughness, Emissive
    /Game/Viewer/Materials/M_TwinGlass     translucent, two-sided: BaseColor, Opacity, Roughness
    /Game/Viewer/Materials/M_TwinFlow      unlit, translucent, instanced: colour from PerInstanceCustomData[0..2] x Glow
                                           (airflow tracer dots)
    /Game/Viewer/Materials/M_TwinMist      unlit, translucent, two-sided, no depth test: the airflow mist raymarched
                                           per unit zone box (params Lo/Hi/O0-3/D0-3/Th/Look, Mask texture + MaskRect)
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


def exposure_free(mat, src, x, y, report):
    """src x 1/exposure: the colour on screen no longer follows the eye adaptation (dots and mist keep their
    colours in bright and dark rooms). Falls back to src when the node is unavailable."""
    try:
        eai = MEL.create_material_expression(mat, unreal.MaterialExpressionEyeAdaptationInverse, x, y)
        for pin in ("LightValue", "Light Value", "LightValueInput"):
            if MEL.connect_material_expressions(src, "", eai, pin):
                return eai
        report["warnings"].append("EyeAdaptationInverse: no LightValue pin")
    except Exception as exc:  # noqa: BLE001 - optional nicety
        report["warnings"].append(f"EyeAdaptationInverse: {exc}")
    return src


def flow(report):
    """Tracer dots: small, pale, drawn over the mist (translucent, higher sort priority set by the actor)."""
    mat = fresh_material("M_TwinFlow")
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    mat.set_editor_property("used_with_instanced_static_meshes", True)
    chans = []
    for i in range(3):
        c = MEL.create_material_expression(mat, unreal.MaterialExpressionPerInstanceCustomData, -900, -200 + i * 150)
        c.set_editor_property("data_index", i)
        c.set_editor_property("const_default_value", 0.9)
        chans.append(c)
    rg = MEL.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -650, -150)
    MEL.connect_material_expressions(chans[0], "", rg, "A")
    MEL.connect_material_expressions(chans[1], "", rg, "B")
    rgb = MEL.create_material_expression(mat, unreal.MaterialExpressionAppendVector, -450, -100)
    MEL.connect_material_expressions(rg, "", rgb, "A")
    MEL.connect_material_expressions(chans[2], "", rgb, "B")
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -250, -100)
    MEL.connect_material_expressions(rgb, "", mul, "A")
    MEL.connect_material_expressions(scalar(mat, "Glow", 1.0, -450, 100), "", mul, "B")
    MEL.connect_material_property(exposure_free(mat, mul, -100, -100, report), "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.connect_material_property(scalar(mat, "Opacity", 0.9, -250, 150), "", unreal.MaterialProperty.MP_OPACITY)
    finish(mat, report)


# The web Live tab's cloud (frontend/js/airflow/illustration.js CLOUD_FS) in Unreal axes (X, Y, Z up) and metres.
# The box mesh sits at the world origin, so local space = world space; it is two-sided and only its back faces
# (the box exit) draw; the march stops at the opaque scene (walls, furniture) and outside the floor outline.
MIST_HLSL = r"""
float3 ro = CamL / 100.0;
float3 rel = PixL / 100.0 - ro;
float pd = length(rel);
float3 rd = rel / max(pd, 1e-4) + 1e-6;
float3 lo = Lo.xyz / 100.0;
float3 hi = Hi.xyz / 100.0;
float3 ta = (lo - ro) / rd;
float3 tb = (hi - ro) / rd;
float3 tn = min(ta, tb);
float3 tf = max(ta, tb);
float t0 = max(0.0, max(tn.x, max(tn.y, tn.z)));
float t1 = min(tf.x, min(tf.y, tf.z));
if (pd < t1 - 0.02) return float4(0, 0, 0, 0);
t1 = min(t1, pd * SceneZ / max(PixZ, 0.001));
if (t1 <= t0) return float4(0, 0, 0, 0);
float ds = (t1 - t0) / 16.0;
int n = (int)(Th.w + 0.5);
float3 Os[4] = { O0.xyz / 100.0, O1.xyz / 100.0, O2.xyz / 100.0, O3.xyz / 100.0 };
float3 Ds[4] = { D0.xyz, D1.xyz, D2.xyz, D3.xyz };
float4 sum = float4(0, 0, 0, 0);
for (int j = 0; j < 16; j++)
{
    float3 p = ro + rd * (t0 + (j + 0.5) * ds);
    float2 uv = (p.xy * 100.0 - MaskRect.xy) * MaskRect.zw;
    if (Mask.SampleLevel(MaskSampler, uv, 0).r < 0.5) continue;
    float jet = 0.0;
    [unroll] for (int i = 0; i < 4; i++)
    {
        if (i < n)
        {
            float3 v = p - Os[i];
            float3 dir = Ds[i];
            float d = dot(v, dir);
            float len = min(8.5, (abs(dir.x) > 0.5 ? hi.x - lo.x : hi.y - lo.y) * 0.82);
            float h = Os[i].z - 2.1 * smoothstep(0.0, len, d);
            float lat = dot(v.xy, float2(dir.y, -dir.x));
            float w = max(0.2 + d * 0.2, 0.05);
            float b = exp(-(lat * lat + (p.z - h) * (p.z - h)) / (w * w));
            jet = max(jet, b * smoothstep(-0.1, 0.25, d) * (1.0 - smoothstep(len * 0.8, len * 1.25, d)));
        }
    }
    jet *= Look.x;
    float temp = lerp(Th.x, Th.y, saturate((p.y - lo.y) / (hi.y - lo.y))) + 0.13 * (p.z - lo.z - 1.5) - Th.z * jet;
    float u = saturate((temp - 12.0) / 24.0);
    float3 c;
    if (u < 0.2) c = lerp(float3(0.28, 0.16, 0.55), float3(0.04, 0.65, 0.95), u / 0.2);
    else if (u < 0.42) c = lerp(float3(0.04, 0.65, 0.95), float3(0.13, 0.94, 0.64), (u - 0.2) / 0.22);
    else if (u < 0.64) c = lerp(float3(0.13, 0.94, 0.64), float3(0.85, 0.93, 0.28), (u - 0.42) / 0.22);
    else if (u < 0.82) c = lerp(float3(0.85, 0.93, 0.28), float3(1.0, 0.56, 0.23), (u - 0.64) / 0.18);
    else c = lerp(float3(1.0, 0.56, 0.23), float3(0.94, 0.2, 0.19), (u - 0.82) / 0.18);
    float ripple = 0.83 + 0.17 * sin(p.x * 2.1 + p.y * 1.6 + p.z * 3.0 - Secs * 2.2);
    float2 edge = min(p.xy - lo.xy, hi.xy - p.xy);
    float feather = smoothstep(0.0, 0.7, min(edge.x, edge.y));
    float a = 1.0 - exp(-(0.035 + 0.38 * jet) * ripple * feather * Look.y * ds);
    sum.rgb += (1.0 - sum.a) * c * a;
    sum.a += (1.0 - sum.a) * a;
}
if (sum.a < 0.001) return float4(0, 0, 0, 0);
return float4(pow(sum.rgb / sum.a, 2.2) * Look.z, min(0.8, sum.a));
"""


def mist(report):
    """Airflow mist: translucent, unlit, two-sided, no depth test (the shader stops at the scene depth itself)."""
    mat = fresh_material("M_TwinMist")
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    mat.set_editor_property("two_sided", True)
    mat.set_editor_property("disable_depth_test", True)
    custom = MEL.create_material_expression(mat, unreal.MaterialExpressionCustom, -500, 0)
    custom.set_editor_property("code", MIST_HLSL.strip())
    custom.set_editor_property("description", "Airflow mist (illustration, not CFD)")
    custom.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT4)
    pins = ["PixL", "CamL", "SceneZ", "PixZ", "Secs", "Lo", "Hi", "O0", "O1", "O2", "O3", "D0", "D1", "D2", "D3",
            "Th", "Look", "Mask", "MaskRect"]
    inputs = []
    for name in pins:
        ci = unreal.CustomInput()
        ci.set_editor_property("input_name", name)
        inputs.append(ci)
    custom.set_editor_property("inputs", inputs)

    y = -1400
    def node(cls, **props):
        nonlocal y
        e = MEL.create_material_expression(mat, cls, -1000, y)
        y += 120
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e

    sources = {
        "PixL": node(unreal.MaterialExpressionLocalPosition),
        "CamL": None,
        "SceneZ": node(unreal.MaterialExpressionSceneDepth),
        "PixZ": node(unreal.MaterialExpressionPixelDepth),
        "Secs": node(unreal.MaterialExpressionTime),
    }
    cam = node(unreal.MaterialExpressionCameraPositionWS)
    to_local = node(unreal.MaterialExpressionTransformPosition,
                    transform_source_type=unreal.MaterialPositionTransformSource.TRANSFORMPOSSOURCE_WORLD,
                    transform_type=unreal.MaterialPositionTransformSource.TRANSFORMPOSSOURCE_LOCAL)
    if not MEL.connect_material_expressions(cam, "", to_local, ""):
        raise RuntimeError("mist: camera -> local transform did not connect")
    sources["CamL"] = to_local
    defaults = {"Lo": (0, 0, 0, 0), "Hi": (100, 100, 280, 0), "Th": (27, 27, 0, 0), "Look": (1, 1, 1.15, 0),
                "MaskRect": (0, 0, 0, 0)}
    for name in ("Lo", "Hi", "O0", "O1", "O2", "O3", "D0", "D1", "D2", "D3", "Th", "Look", "MaskRect"):
        p = vector(mat, name, defaults.get(name, (0, 0, 0, 0)), -1000, y)
        y += 120
        sources[name] = p
    tex = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureObjectParameter, -1000, y)
    tex.set_editor_property("parameter_name", "Mask")
    tex.set_editor_property("texture", unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture"))
    sources["Mask"] = tex
    for name in pins:
        # a vector parameter's default pin is RGB (float3); Th and MaskRect use their 4th component
        out = "RGBA" if isinstance(sources[name], unreal.MaterialExpressionVectorParameter) else ""
        if not MEL.connect_material_expressions(sources[name], out, custom, name):
            raise RuntimeError(f"mist: input {name} did not connect")
    rgb = MEL.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -250, -60)
    for ch, on in (("r", True), ("g", True), ("b", True), ("a", False)):
        rgb.set_editor_property(ch, on)
    MEL.connect_material_expressions(custom, "", rgb, "")
    alpha = MEL.create_material_expression(mat, unreal.MaterialExpressionComponentMask, -250, 80)
    for ch, on in (("r", False), ("g", False), ("b", False), ("a", True)):
        alpha.set_editor_property(ch, on)
    MEL.connect_material_expressions(custom, "", alpha, "")
    MEL.connect_material_property(exposure_free(mat, rgb, -100, -60, report), "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MEL.connect_material_property(alpha, "", unreal.MaterialProperty.MP_OPACITY)
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
        mist(report)
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
