"""Re-import the exported files in a clean Blender and compare them with the build plan.

    blender --background --factory-startup --python verify_exports.py -- --plan plan.json --out-dir BUILD_DIR

Checks every per-object Unreal FBX (architecture: world bounds; furniture variants: the
nominal catalog box at the origin) and the web GLB (object names). Writes
verify_report.json; any mismatch above tolerance is listed under "failures".
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy  # noqa: E402

from _twinlib import load_json, mesh_world_bounds, progress, save_json, script_args  # noqa: E402

TOL = 0.001  # metres


def clear() -> None:
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)


def import_fbx(path: Path) -> list:
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.import_scene, "fbx"):
        bpy.ops.import_scene.fbx(filepath=str(path))
    else:
        bpy.ops.wm.fbx_import(filepath=str(path))
    return [o for o in bpy.data.objects if o not in before]


def union_bounds(objs) -> list[list[float]]:
    bs = [mesh_world_bounds(o) for o in objs if o.type == "MESH"]
    return [[min(b[0][i] for b in bs) for i in range(3)], [max(b[1][i] for b in bs) for i in range(3)]]


def delta(a, b) -> float:
    return max(abs(a[i][k] - b[i][k]) for i in range(2) for k in range(3))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(script_args())
    out = Path(args.out_dir).resolve()
    plan = load_json(args.plan)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    report = {"blender": bpy.app.version_string, "tolerance_m": TOL, "checked": 0, "failures": [], "max_delta_m": 0.0,
              "files": {}}

    checks = [(f"export/unreal/arch/{o['name']}.fbx", o["expected_bounds"], o["name"]) for o in plan["objects"]]
    seen = set()
    for f in plan["furniture"]:
        if f["variant"] in seen:
            continue
        seen.add(f["variant"])
        w, d, h = f["nominal_size"]
        checks.append((f"export/unreal/furniture/SM_{f['variant']}.fbx", [[-w / 2, -d / 2, 0.0], [w / 2, d / 2, h]],
                       f"SM_{f['variant']}"))
    for i, (rel, expected, name) in enumerate(checks):
        path = out / rel
        if not path.is_file() or path.stat().st_size == 0:
            report["failures"].append({"file": rel, "error": "missing or empty"})
            continue
        clear()
        objs = import_fbx(path)
        meshes = [o for o in objs if o.type == "MESH"]
        if not meshes:
            report["failures"].append({"file": rel, "error": "no mesh imported"})
            continue
        got = union_bounds(meshes)
        dlt = delta(got, expected)
        report["files"][rel] = {"delta_m": round(dlt, 6), "objects": [o.name for o in meshes],
                                "materials": sorted({s.material.name for o in meshes for s in o.material_slots if s.material})}
        report["max_delta_m"] = max(report["max_delta_m"], dlt)
        report["checked"] += 1
        if dlt > TOL:
            report["failures"].append({"file": rel, "error": f"bounds differ by {dlt:.4f} m",
                                       "expected": expected, "got": got})
        if i % 10 == 0:
            progress(0.9 * (i + 1) / len(checks), "verify_fbx")

    clear()
    glb = out / "export/web/scene.glb"
    if glb.is_file():
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(glb))
        names = {o.name for o in bpy.data.objects if o not in before and o.type == "MESH"}
        expected_names = {o["name"] for o in plan["objects"]} | {f["name"] for f in plan["furniture"]}
        missing = sorted(expected_names - names)
        report["glb"] = {"objects": len(names), "missing": missing[:20]}
        if missing:
            report["failures"].append({"file": "export/web/scene.glb", "error": f"{len(missing)} objects missing"})
        report["checked"] += 1
    else:
        report["failures"].append({"file": "export/web/scene.glb", "error": "missing"})
    # the saved Blender project must open and contain every planned object
    blend = out / "scene.blend"
    if blend.is_file():
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        names = {o.name for o in bpy.data.objects}
        expected_names = {o["name"] for o in plan["objects"]} | {f["name"] for f in plan["furniture"]}
        missing = sorted(expected_names - names)
        report["blend"] = {"objects": len(bpy.data.objects), "meshes": len(bpy.data.meshes),
                           "materials": len(bpy.data.materials), "missing": missing[:20],
                           "unpacked_images": [i.name for i in bpy.data.images if i.source == "FILE" and not i.packed_file]}
        if missing:
            report["failures"].append({"file": "scene.blend", "error": f"{len(missing)} objects missing"})
        report["checked"] += 1
    else:
        report["failures"].append({"file": "scene.blend", "error": "missing"})
    save_json(out / "verify_report.json", report)
    progress(1.0, "verified", failures=len(report["failures"]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(2)
