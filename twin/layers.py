"""Layer updates: change a saved, verified build without regenerating it.

A full build (``twin.pipeline.run_build``) runs Blender once and keeps its glTF as
``export/web/geometry.glb``. Layers (today: the sensor layer) are written on top of that file in pure
Python: the composed ``export/web/scene.glb`` is the geometry file with its JSON arrays and binary chunk
*extended*, never rewritten, plus one node per sensor. Every viewer (browser preview, Live tab, packaged
Unreal viewer) loads ``scene.glb``, so they all show the layer without knowing about it.

A *layer build* starts from a completed build whose building geometry is identical
(``twin.scene.sensors.geometry_key``). It links every file of that build (hard links, so nothing is copied
or touched), rewrites only the plan, scene, manifest and the composed glTF, and proves that the geometry
part of the new glTF is byte-identical to the saved one. No Blender run, seconds instead of minutes.

glTF is +Y up: scene (x, y, z) -> glTF (x, z, -y), a rotation, so triangle winding is kept.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import time
from pathlib import Path
from typing import Callable

import numpy as np

from twin.scene.compile import scene_hash
from twin.scene.model import Scene
from twin.scene.sensors import compile_sensors, geometry_key, layer_changes
from twin.scene.validate import validate_scene
from twin.viewer.bundle import BundleError, node_bounds_ue, read_glb, scene_bounds_to_ue

GEOMETRY_REL = "export/web/geometry.glb"
MODEL_REL = "export/web/scene.glb"
SENSORS_REL = "sensors.json"
LAYER_REPORT_REL = "layer_report.json"
TOL_CM = 0.1   # 1 mm, the same tolerance as the Blender re-import check

# files a layer build writes itself; everything else is linked from the base build
OWN_FILES = {"manifest.json", "plan.json", "scene.json", "reuse.json", "timings.json", MODEL_REL, GEOMETRY_REL,
             SENSORS_REL, LAYER_REPORT_REL}
OWN_DIRS = ("viewer/", "unreal/")   # derived per build (viewer cache, editor import results)

ProgressFn = Callable[[float, str], None]


class LayerError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


# ---- glTF writing ------------------------------------------------------------------------------

def _srgb_to_linear(hex_color: str) -> list[float]:
    h = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def _gltf_material(mid: str, spec: dict) -> dict:
    lin = _srgb_to_linear(spec["base_color"])
    mat = {"name": f"MAT_{mid}",
           "pbrMetallicRoughness": {"baseColorFactor": [*lin, spec.get("opacity", 1.0)],
                                    "metallicFactor": spec.get("metallic", 0.0),
                                    "roughnessFactor": spec.get("roughness", 0.6)}}
    if spec.get("emission", 0) > 0:
        mat["emissiveFactor"] = [round(c * min(1.0, spec["emission"]), 6) for c in lin]
    if spec.get("opacity", 1.0) < 1.0:
        mat["alphaMode"] = "BLEND"
    return mat


def prism_triangles(prisms: list[dict], origin: list[float]) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Flat-shaded triangles of plan prisms, grouped by material, in glTF axes relative to ``origin``
    (scene metres): {material: (positions Nx3, normals Nx3, indices M)}."""
    groups: dict[str, tuple[list, list, list]] = {}
    ox, oy, oz = origin

    def g(x, y, z):  # scene -> glTF, relative to the node origin
        return (x - ox, z - oz, -(y - oy))

    for pr in prisms:
        pos, nrm, idx = groups.setdefault(pr["mat"], ([], [], []))
        flat = [p for ring in pr["rings"] for p in ring]
        z0, z1 = pr["z0"], pr["z1"]
        for zc, up in ((z1, True), (z0, False)):     # caps: top faces +z, bottom faces -z
            base = len(pos)
            pos.extend(g(x, y, zc) for x, y in flat)
            nrm.extend([(0.0, 1.0, 0.0) if up else (0.0, -1.0, 0.0)] * len(flat))
            for a, b, c in pr["tris"]:
                idx.extend((base + a, base + b, base + c) if up else (base + a, base + c, base + b))
        for ring in pr["rings"]:                     # sides: outward normals (exterior CCW, holes CW)
            n = len(ring)
            for i in range(n):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                if L < 1e-9:
                    continue
                nx, ny = (by - ay) / L, -(bx - ax) / L
                base = len(pos)
                pos.extend((g(ax, ay, z0), g(bx, by, z0), g(bx, by, z1), g(ax, ay, z1)))
                nrm.extend([(nx, 0.0, -ny)] * 4)
                idx.extend((base, base + 1, base + 2, base, base + 2, base + 3))
    return {m: (np.asarray(p, dtype=np.float32), np.asarray(n, dtype=np.float32),
                np.asarray(i, dtype=np.uint32)) for m, (p, n, i) in groups.items()}


def append_layer(gltf: dict, binary: bytes, objects: list[dict], materials: dict[str, dict],
                 layer: str) -> tuple[dict, bytes]:
    """New glTF JSON + binary: the given file with one node per object appended (nothing reordered)."""
    out = json.loads(json.dumps(gltf))
    buf = bytearray(binary)
    for key in ("nodes", "meshes", "accessors", "bufferViews", "materials", "buffers"):
        out.setdefault(key, [])
    if not out["buffers"]:
        out["buffers"].append({"byteLength": 0})
    if "scenes" not in out or not out["scenes"]:
        out["scenes"], out["scene"] = [{"nodes": []}], 0
    scene_nodes = out["scenes"][out.get("scene", 0)].setdefault("nodes", [])
    mat_index = {m.get("name"): i for i, m in enumerate(out["materials"])}

    def view(data: bytes, target: int) -> int:
        while len(buf) % 4:
            buf.append(0)
        out["bufferViews"].append({"buffer": 0, "byteOffset": len(buf), "byteLength": len(data), "target": target})
        buf.extend(data)
        return len(out["bufferViews"]) - 1

    def acc(view_i: int, ctype: int, count: int, typ: str, **extra) -> int:
        out["accessors"].append({"bufferView": view_i, "componentType": ctype, "count": count, "type": typ, **extra})
        return len(out["accessors"]) - 1

    for obj in objects:
        origin = obj.get("location") or [0.0, 0.0, 0.0]
        prims = []
        for mid, (p, n, i) in prism_triangles(obj["prisms"], origin).items():
            name = f"MAT_{mid}"
            if name not in mat_index:
                out["materials"].append(_gltf_material(mid, materials[mid]))
                mat_index[name] = len(out["materials"]) - 1
            small = int(i.max()) < 65535
            idx = i.astype(np.uint16 if small else np.uint32)
            pa = acc(view(p.tobytes(), 34962), 5126, len(p), "VEC3", min=p.min(axis=0).tolist(), max=p.max(axis=0).tolist())
            na = acc(view(n.tobytes(), 34962), 5126, len(n), "VEC3")
            ia = acc(view(idx.tobytes(), 34963), 5123 if small else 5125, len(idx), "SCALAR")
            prims.append({"attributes": {"POSITION": pa, "NORMAL": na}, "indices": ia, "material": mat_index[name]})
        out["meshes"].append({"name": obj["name"], "primitives": prims})
        extras = {"twin_layer": layer, "twin_kind": obj["kind"], "twin_hash": obj["hash"]}
        extras.update({f"twin_{k}": obj[k] for k in ("sensor_id", "tag", "room_id", "label", "bms_point")
                       if obj.get(k) is not None})
        out["nodes"].append({"name": obj["name"], "mesh": len(out["meshes"]) - 1,
                             "translation": [origin[0], origin[2], -origin[1]], "extras": extras})
        scene_nodes.append(len(out["nodes"]) - 1)
    while len(buf) % 4:
        buf.append(0)
    out["buffers"][0]["byteLength"] = len(buf)
    return out, bytes(buf)


def write_glb(path: Path, gltf: dict, binary: bytes) -> None:
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    bn = binary + b"\x00" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(js), 0x4E4F534A) + js
    if bn:
        chunks += struct.pack("<II", len(bn), 0x004E4942) + bn
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)))
        fh.write(chunks)
    tmp.replace(path)


def compose(geometry: Path, out: Path, objects: list[dict], materials: dict[str, dict]) -> dict:
    """scene.glb = geometry.glb + the layer objects, then re-read and checked. Returns the layer report."""
    gltf, binary = read_glb(geometry)
    new_gltf, new_bin = append_layer(gltf, binary, objects, materials, "sensors")
    write_glb(out, new_gltf, new_bin)
    return verify(geometry, out, objects)


def verify(geometry: Path, model: Path, objects: list[dict], tol_cm: float = TOL_CM) -> dict:
    """The composed file must keep the geometry file intact and hold every layer object where the plan
    says it is. Failures are listed; ``ok`` is False if there is any."""
    g_json, g_bin = read_glb(geometry)
    m_json, m_bin = read_glb(model)
    failures: list[dict] = []
    if m_bin[:len(g_bin)] != g_bin:
        failures.append({"check": "geometry binary", "error": "the building's binary data was changed"})
    for key in ("nodes", "meshes", "accessors", "bufferViews", "materials", "images", "textures", "samplers"):
        a, b = g_json.get(key, []), m_json.get(key, [])
        if b[:len(a)] != a:
            failures.append({"check": f"geometry {key}", "error": f"existing {key} were changed or reordered"})
    roots_a = g_json["scenes"][g_json.get("scene", 0)]["nodes"] if g_json.get("scenes") else []
    roots_b = m_json["scenes"][m_json.get("scene", 0)]["nodes"]
    if roots_b[:len(roots_a)] != roots_a:
        failures.append({"check": "geometry scene", "error": "the building's scene nodes were changed"})
    got = node_bounds_ue(m_json, m_bin)
    worst = 0.0
    for o in objects:
        if o["name"] not in got:
            failures.append({"object": o["name"], "error": "missing from the composed model"})
            continue
        emin, emax = scene_bounds_to_ue(*o["expected_bounds"])
        gmin, gmax = got[o["name"]]
        dev = max(abs(a - b) for a, b in zip(gmin + gmax, emin + emax))
        worst = max(worst, dev)
        if dev > tol_cm:
            failures.append({"object": o["name"], "error": f"bounds differ by {dev:.3f} cm",
                             "expected": [emin, emax], "got": [gmin, gmax]})
    return {"layer": "sensors", "objects": len(objects), "geometry_nodes": len(g_json.get("nodes", [])),
            "model_nodes": len(m_json.get("nodes", [])), "geometry_bytes": geometry.stat().st_size,
            "model_bytes": model.stat().st_size, "max_deviation_cm": round(worst, 4), "tolerance_cm": tol_cm,
            "failures": failures[:50], "ok": not failures}


def sensor_entries(plan: dict) -> list[dict]:
    """Manifest entries of the sensor layer (scene metres + Unreal centimetres)."""
    out = []
    for o in plan.get("sensors", []):
        x, y, z = o["location"]
        out.append({"name": o["name"], "sensor_id": o["sensor_id"], "tag": o["tag"], "kind": o["sensor_kind"],
                    "label": o["label"], "mounting": o["mounting"], "room_id": o["room_id"], "room_name": o["room_name"],
                    "unit_id": o["unit_id"], "unit_tag": o["unit_tag"], "bms_point": o["bms_point"],
                    "location_m": o["location"], "height_m": o["height_m"], "bounds_m": o["expected_bounds"],
                    "hash": o["hash"], "status": o["status"],
                    "unreal": {"location_cm": [round(x * 100, 4), round(-y * 100, 4), round(z * 100, 4)]}})
    return out


def apply_layers(build_dir: Path, plan: dict) -> dict:
    """After Blender: keep its glTF as geometry.glb and compose scene.glb with the layers on top."""
    model, geometry = build_dir / MODEL_REL, build_dir / GEOMETRY_REL
    os.replace(model, geometry)
    report = compose(geometry, model, plan.get("sensors", []), plan.get("sensor_materials", {}))
    (build_dir / SENSORS_REL).write_text(json.dumps({"sensors": sensor_entries(plan)}, indent=1), encoding="utf-8")
    (build_dir / LAYER_REPORT_REL).write_text(json.dumps(report, indent=1), encoding="utf-8")
    if not report["ok"]:
        raise LayerError("LAYER_VERIFY_FAILED", "The composed model does not match the scene model.",
                         {"failures": report["failures"]})
    return report


# ---- layer builds ------------------------------------------------------------------------------

def link_or_copy(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def layer_compatible(base_scene: Scene, scene: Scene) -> bool:
    return geometry_key(base_scene) == geometry_key(scene)


def run_layer_build(scene: Scene, build_dir: Path, *, build_id: str, base_dir: Path, base_id: str,
                    base_version: int | None = None, on_progress: ProgressFn | None = None) -> dict:
    """Update a saved build with this version's layers. Returns the new manifest."""
    from twin.pipeline import write_layer_manifest   # local: pipeline imports this module

    t0 = time.time()
    progress = on_progress or (lambda p, s: None)
    report_v = validate_scene(scene)
    if not report_v.can_generate:
        raise LayerError("SCENE_NOT_READY", "The scene has errors or unconfirmed values.",
                         {"issues": [i.message for i in report_v.issues if i.severity in ("error", "review")][:50]})
    for rel in ("manifest.json", "plan.json", "scene.json"):
        if not (base_dir / rel).is_file():
            raise LayerError("BASE_BUILD_INCOMPLETE", f"The saved build {base_id} has no {rel}.")
    base_scene = Scene.model_validate_json((base_dir / "scene.json").read_text(encoding="utf-8"))
    if not layer_compatible(base_scene, scene):
        from twin.scene.diff import diff_scenes
        d = diff_scenes(base_scene, scene)
        d.pop("sensors", None)
        raise LayerError("GEOMETRY_CHANGED", "The building itself changed since the saved build; regenerate it "
                         "with Blender instead of updating the layers.", {"changed": d})
    geometry_src = base_dir / GEOMETRY_REL
    if not geometry_src.is_file():
        geometry_src = base_dir / MODEL_REL   # builds made before layers existed: scene.glb is pure geometry
    if not geometry_src.is_file():
        raise LayerError("BASE_BUILD_INCOMPLETE", f"The saved build {base_id} has no glTF model.")

    progress(0.1, "linking the saved model")
    build_dir.mkdir(parents=True, exist_ok=True)
    linked = {"link": 0, "copy": 0}
    for p in sorted(base_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base_dir).as_posix()
        if rel in OWN_FILES or rel.startswith(OWN_DIRS) or rel.endswith((".tmp", ".log")):
            continue
        linked[link_or_copy(p, build_dir / rel)] += 1
    linked[link_or_copy(geometry_src, build_dir / GEOMETRY_REL)] += 1

    progress(0.4, "placing the sensors")
    plan = json.loads((base_dir / "plan.json").read_text(encoding="utf-8"))
    sensors, mats = compile_sensors(scene)
    plan["sensors"], plan["sensor_materials"] = sensors, mats
    plan["scene_hash"] = scene_hash(scene)
    plan["accepted_estimates"] = [a for a in plan.get("accepted_estimates", []) if not a.startswith("sensor:")] + \
        [f"sensor:{s.id}" for s in scene.sensors if s.certainty.status == "accepted_estimate"]
    (build_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (build_dir / "scene.json").write_text(scene.model_dump_json(indent=1), encoding="utf-8")

    progress(0.6, "writing the sensor layer")
    try:
        report = compose(build_dir / GEOMETRY_REL, build_dir / MODEL_REL, sensors, mats)
    except BundleError as exc:
        raise LayerError(exc.code, str(exc), exc.detail) from exc
    report["changes"] = layer_changes(base_scene, scene)
    report["linked_files"] = linked
    (build_dir / SENSORS_REL).write_text(json.dumps({"sensors": sensor_entries(plan)}, indent=1), encoding="utf-8")
    (build_dir / LAYER_REPORT_REL).write_text(json.dumps(report, indent=1), encoding="utf-8")
    if not report["ok"]:
        raise LayerError("LAYER_VERIFY_FAILED", "The updated model does not match the scene model.",
                         {"failures": report["failures"]})

    progress(0.9, "writing the manifest")
    base_manifest = json.loads((base_dir / "manifest.json").read_text(encoding="utf-8"))
    timings = {"total_s": round(time.time() - t0, 2)}
    manifest = write_layer_manifest(build_dir, base_manifest, plan, build_id=build_id, base_id=base_id,
                                    base_version=base_version, report=report, timings=timings)
    (build_dir / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    progress(1.0, "completed")
    return manifest
