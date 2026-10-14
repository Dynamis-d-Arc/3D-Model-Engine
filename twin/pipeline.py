"""Generation pipeline: scene model -> build plan -> Blender -> verified exports -> UE manifest.

A build is only reported as successful when every expected file exists, Blender's own
report matches the plan within tolerance, and the re-imported exports match too.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from shapely.geometry import Point

from twin.assets.catalog import CATALOG_PATH
from twin.blender.runner import (SCRIPTS_DIR, BlenderError, BlenderNotConfigured, blender_version, require_blender,
                                 run_script)
from twin.config import PROJECT_ROOT, Settings
from twin.layers import (GEOMETRY_REL, LAYER_REPORT_REL, MODEL_REL, SENSORS_REL, LayerError, apply_layers,
                         sensor_entries)
from twin.scene import geometry as g
from twin.scene.compile import LIBRARY_VERSION, compile_scene
from twin.scene.model import Material, Scene
from twin.scene.textures import ensure_texture
from twin.scene.transforms import scene_to_unreal_location, scene_to_unreal_yaw
from twin.scene.validate import validate_scene
from twin.viewer.bundle import BundleError

LIBRARY_DIR = PROJECT_ROOT / "assets_build"
TEXTURE_CACHE = PROJECT_ROOT / "assets_build" / "textures"
TOL_M = 0.001
TOL_DEG = 0.01
MANIFEST_VERSION = 1

ProgressFn = Callable[[float, str], None]


class BuildFailed(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class BuildResult:
    build_dir: Path
    manifest: dict
    timings: dict = field(default_factory=dict)
    reused: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def library_key() -> str:
    h = hashlib.sha256(LIBRARY_VERSION.encode())
    for p in (CATALOG_PATH, SCRIPTS_DIR / "build_library.py", SCRIPTS_DIR / "_twinlib.py"):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def ensure_library(blender: str, log_path: Path, on_progress: ProgressFn | None = None) -> Path:
    """Build the furniture library once per catalog/script version (cached in assets_build/)."""
    key = library_key()
    path = LIBRARY_DIR / f"furniture_library_{key}.blend"
    report_path = LIBRARY_DIR / f"furniture_library_{key}.json"
    if path.is_file() and report_path.is_file():
        return path
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    run_script(blender, "build_library.py",
               ["--catalog", str(CATALOG_PATH), "--out", str(path), "--report", str(report_path)],
               log_path=log_path, timeout_s=600,
               on_progress=(lambda p, s, _x: on_progress(p, f"library:{s}")) if on_progress else None)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for asset_id, a in report["assets"].items():
        if any(abs(x - y) > TOL_M for x, y in zip(a["dims"], a["expected"])):
            raise BuildFailed("LIBRARY_INVALID", f"library asset {asset_id} has dims {a['dims']}, expected {a['expected']}")
    return path


def prepare_plan(scene: Scene) -> dict:
    plan = compile_scene(scene)
    for mid, spec in plan["materials"].items():
        tex = ensure_texture(Material.model_validate(spec), TEXTURE_CACHE)
        if tex is not None:
            spec["texture"] = str(tex)
    return plan


def export_paths(plan: dict) -> dict[str, str]:
    """Per-object Unreal exports -> content hash (used for reuse across builds)."""
    paths = {f"export/unreal/arch/{o['name']}.fbx": o["hash"] for o in plan["objects"]}
    for f in plan["furniture"]:
        paths[f"export/unreal/furniture/SM_{f['variant']}.fbx"] = f["variant_hash"]
    return paths


def compute_reuse(plan: dict, previous_build: Path | None) -> dict[str, str]:
    if previous_build is None:
        return {}
    man_path = previous_build / "manifest.json"
    if not man_path.is_file():
        return {}
    prev = json.loads(man_path.read_text(encoding="utf-8"))
    prev_hash = {e["file"]: e["hash"] for e in prev.get("exports", [])}
    reuse = {}
    for rel, h in export_paths(plan).items():
        src = previous_build / rel
        if prev_hash.get(rel) == h and src.is_file():
            reuse[rel] = str(src)
    return reuse


def check_against_plan(plan: dict, report: dict) -> list[str]:
    """Compare what Blender built with what the plan asked for."""
    problems = []
    for o in plan["objects"]:
        got = report["objects"].get(o["name"])
        if got is None:
            problems.append(f"{o['name']}: not built")
            continue
        d = max(abs(got["bounds"][i][k] - o["expected_bounds"][i][k]) for i in range(2) for k in range(3))
        if d > TOL_M:
            problems.append(f"{o['name']}: bounds differ by {d:.4f} m")
    for f in plan["furniture"]:
        got = report["furniture"].get(f["name"])
        if got is None:
            problems.append(f"{f['name']}: not built")
            continue
        if max(abs(a - b) for a, b in zip(got["dims"], f["size"])) > TOL_M:
            problems.append(f"{f['name']}: size {got['dims']} != {f['size']}")
        if max(abs(a - b) for a, b in zip(got["location"], f["location"])) > TOL_M:
            problems.append(f"{f['name']}: location {got['location']} != {f['location']}")
        dr = abs((got["rotation_deg"] - f["rotation_deg"] + 180) % 360 - 180)
        if dr > TOL_DEG:
            problems.append(f"{f['name']}: rotation off by {dr:.3f} deg")
    return problems


def player_start(scene: Scene, plan: dict) -> dict:
    """Stand 1.2 m inside the entrance door (or in the largest room), facing into the room."""
    for o in scene.openings:
        if o.kind != "door" or not o.opens_into:
            continue
        w = scene.wall(o.wall_id)
        f = g.wall_frame(w)
        for side in (1.0, -1.0):
            outside = f.to_world(o.offset, -side * (w.thickness / 2 + 0.3))
            inside = f.to_world(o.offset, side * (w.thickness / 2 + 1.2))
            if g.room_at(scene, w.level_id, *outside) is None and \
                    g.room_polygon(scene.room(o.opens_into)).contains(Point(*inside)):
                yaw = math.degrees(math.atan2(side * f.ny, side * f.nx))
                z = scene.level(w.level_id).elevation
                return {"location": [inside[0], inside[1], z], "rotation_deg": yaw, "room_id": o.opens_into}
    room = max(plan["rooms"], key=lambda r: r["area_net"]) if plan["rooms"] else None
    if room is None:
        return {"location": [0.0, 0.0, 0.0], "rotation_deg": 0.0, "room_id": None}
    return {"location": [*room["label_point"], room["floor_z"]], "rotation_deg": 0.0, "room_id": room["id"]}


def write_manifest(scene: Scene, plan: dict, build_dir: Path, build_id: str, report: dict, verify: dict,
                   blender_ver: str, reused: list[str], layer: dict | None = None) -> dict:
    """Everything the Unreal importer and the web viewer need, in one file."""
    def ue_loc(p):
        return [round(v, 4) for v in scene_to_unreal_location(*p)]

    rooms = {r["id"]: r for r in plan["rooms"]}
    exports = []
    for rel, h in export_paths(plan).items():
        p = build_dir / rel
        exports.append({"file": rel, "hash": h, "bytes": p.stat().st_size, "sha256": _sha256(p)})
    objects = []
    for o in plan["objects"]:
        room = rooms.get(o.get("room_id"))
        objects.append({
            "name": o["name"], "kind": o["kind"], "level_id": o["level_id"], "room_id": o.get("room_id"),
            "room_name": room["name"] if room else None, "wall_id": o.get("wall_id"),
            "opening_id": o.get("opening_id"), "fbx": f"export/unreal/arch/{o['name']}.fbx", "hash": o["hash"],
            "collision": o["collision"], "unreal": {"location_cm": [0.0, 0.0, 0.0], "yaw_deg": 0.0},
            "bounds_m": o["expected_bounds"],
            **{k: o[k] for k in ("label", "hvac_id", "tag", "unit_kind", "airflow_lps", "system", "unit_tag")
               if o.get(k) is not None},
        })
    furniture = []
    for f in plan["furniture"]:
        furniture.append({
            "name": f["name"], "furniture_id": f["furniture_id"], "room_id": f["room_id"],
            "room_name": rooms[f["room_id"]]["name"], "asset_id": f["asset_id"], "label": f["asset_name"],
            "category": f["category"], "size_m": f["size"], "materials": f["materials"],
            "fbx": f"export/unreal/furniture/SM_{f['variant']}.fbx", "mesh": f"SM_{f['variant']}",
            "hash": f["hash"], "variant_hash": f["variant_hash"], "collision": f["collision"],
            "location_m": f["location"], "rotation_deg": f["rotation_deg"], "unreal": f["unreal"],
            "status": f["status"],
        })
    lights = []
    for lt in plan["lights"]:
        if lt["type"] == "sun":
            dx, dy, dz = lt["direction"]
            ux, uy, uz = dx, -dy, dz
            lights.append({"name": lt["name"], "type": "directional", "color": lt["color"],
                           "pitch_deg": round(math.degrees(math.asin(max(-1.0, min(1.0, uz)))), 4),
                           "yaw_deg": round(math.degrees(math.atan2(uy, ux)), 4), "intensity_lux": 6.0})
        else:
            lights.append({"name": lt["name"], "type": "point", "room_id": lt["room_id"], "color": lt["color"],
                           "location_cm": ue_loc(lt["location"]), "lumens": lt["lumens"],
                           "radius_cm": round(math.sqrt(rooms[lt["room_id"]]["area_centreline"]) * 100 * 1.6, 1)})
    start = player_start(scene, plan)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "build_id": build_id,
        # "full" = Blender built the geometry; "layer" = a saved build reused with new layers on top
        "build_kind": "full",
        "geometry_build": build_id,
        "base_build": None,
        "project": plan["project"],
        "scene_hash": plan["scene_hash"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "blender": blender_ver,
        "units": {"scene": "m", "unreal": "cm"},
        "conversion": plan["unreal_conversion"],
        "bounds_m": plan["bounds"],
        "rooms": [{**r, "label_point_cm": ue_loc([*r["label_point"], r["floor_z"]]),
                   "spawn_point_cm": ue_loc([*r["spawn_point"], r["floor_z"]])} for r in plan["rooms"]],
        "openings": plan["openings"],
        "objects": objects,
        "furniture": furniture,
        "lights": lights,
        "player_start": {**start, "unreal": {"location_cm": ue_loc(start["location"]),
                                             "yaw_deg": scene_to_unreal_yaw(start["rotation_deg"])}},
        "exports": exports,
        "files": {
            "blend": "scene.blend", "fbx_full": "export/fbx/scene_full.fbx", "glb": "export/web/scene.glb",
            "glb_geometry": GEOMETRY_REL, "sensors": SENSORS_REL,
            "plan": "plan.json", "scene": "scene.json", "previews": report.get("renders", []),
        },
        "verification": {"blender_report": "blender_report.json", "verify_report": "verify_report.json",
                         "files_checked": verify["checked"], "max_reimport_delta_m": verify["max_delta_m"],
                         "tolerance_m": TOL_M, "layer_report": LAYER_REPORT_REL,
                         "layer_max_deviation_cm": (layer or {}).get("max_deviation_cm")},
        "reused_exports": reused,
        "sensors": sensor_entries(plan),
        "layers": {"sensors": {"count": len(plan.get("sensors", [])), "file": SENSORS_REL}},
        "accepted_estimates": plan["accepted_estimates"],
    }
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def write_layer_manifest(build_dir: Path, base: dict, plan: dict, *, build_id: str, base_id: str,
                         base_version: int | None, report: dict, timings: dict) -> dict:
    """Manifest of a layer build: the saved build's manifest (its geometry, exports and Blender
    verification still describe the linked files) with the new layers and identity."""
    manifest = json.loads(json.dumps(base))
    files = manifest.setdefault("files", {})
    files.update({"glb": MODEL_REL, "glb_geometry": GEOMETRY_REL, "sensors": SENSORS_REL})
    manifest.update({
        "build_id": build_id, "build_kind": "layer", "base_build": base_id, "base_version": base_version,
        "geometry_build": base.get("geometry_build") or base_id,
        "scene_hash": plan["scene_hash"], "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sensors": sensor_entries(plan),
        "layers": {"sensors": {"count": len(plan.get("sensors", [])), "file": SENSORS_REL,
                               "changes": report.get("changes")}},
        "accepted_estimates": plan["accepted_estimates"],
        "timings": timings,
    })
    manifest.setdefault("verification", {}).update({"layer_report": LAYER_REPORT_REL,
                                                     "layer_max_deviation_cm": report["max_deviation_cm"]})
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def run_build(scene: Scene, build_dir: Path, settings: Settings, *, build_id: str,
              previous_build: Path | None = None, render: bool | None = None,
              on_progress: ProgressFn | None = None) -> BuildResult:
    t0 = time.time()
    progress = on_progress or (lambda p, s: None)
    report_v = validate_scene(scene)
    if not report_v.can_generate:
        blocking = [i.message for i in report_v.issues if i.severity in ("error", "review")]
        raise BuildFailed("SCENE_NOT_READY", "The scene has errors or unconfirmed measurements.",
                          {"issues": blocking[:50]})
    try:
        blender = require_blender(settings.blender_path)
        version = blender_version(blender)
    except BlenderNotConfigured as exc:
        raise BuildFailed("BLENDER_NOT_CONFIGURED", str(exc)) from exc
    except (BlenderError, OSError) as exc:
        raise BuildFailed("BLENDER_UNAVAILABLE", f"Blender at {settings.blender_path} could not be started: {exc}") from exc
    build_dir.mkdir(parents=True, exist_ok=True)
    log = build_dir / "blender.log"
    timings: dict[str, float] = {}

    progress(0.02, "compiling geometry")
    plan = prepare_plan(scene)
    (build_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (build_dir / "scene.json").write_text(scene.model_dump_json(indent=1), encoding="utf-8")
    timings["compile_s"] = round(time.time() - t0, 2)

    progress(0.05, "preparing furniture library")
    library = ensure_library(blender, log, lambda p, s: progress(0.05 + 0.05 * p, s))
    reuse = compute_reuse(plan, previous_build)
    (build_dir / "reuse.json").write_text(json.dumps(reuse, indent=1), encoding="utf-8")

    t1 = time.time()
    args = ["--plan", str(build_dir / "plan.json"), "--library", str(library), "--out-dir", str(build_dir),
            "--reuse", str(build_dir / "reuse.json")]
    if settings.render_previews if render is None else render:
        args.append("--render")
    try:
        run_script(blender, "build_scene.py", args, log_path=log, timeout_s=1800,
                   on_progress=lambda p, s, _x: progress(0.10 + 0.70 * p, f"blender: {s}"))
        timings["blender_build_s"] = round(time.time() - t1, 2)
        t2 = time.time()
        progress(0.82, "verifying exports")
        run_script(blender, "verify_exports.py", ["--plan", str(build_dir / "plan.json"), "--out-dir", str(build_dir)],
                   log_path=log, timeout_s=900,
                   on_progress=lambda p, s, _x: progress(0.82 + 0.13 * p, f"verify: {s}"))
        timings["verify_s"] = round(time.time() - t2, 2)
    except BlenderError as exc:
        raise BuildFailed("BLENDER_FAILED", str(exc), {"log_tail": exc.log_tail[-4000:], "log": "blender.log"}) from exc

    progress(0.96, "checking outputs")
    required = ["scene.blend", "export/fbx/scene_full.fbx", "export/web/scene.glb", "blender_report.json",
                "verify_report.json", *export_paths(plan).keys()]
    missing = [r for r in required if not (build_dir / r).is_file() or (build_dir / r).stat().st_size == 0]
    if missing:
        raise BuildFailed("OUTPUT_MISSING", f"{len(missing)} expected output files are missing or empty.",
                          {"missing": missing[:50]})
    report = json.loads((build_dir / "blender_report.json").read_text(encoding="utf-8"))
    problems = check_against_plan(plan, report)
    if problems:
        raise BuildFailed("GEOMETRY_MISMATCH", "Blender output does not match the scene model.",
                          {"problems": problems[:50]})
    verify = json.loads((build_dir / "verify_report.json").read_text(encoding="utf-8"))
    if verify["failures"]:
        raise BuildFailed("EXPORT_VERIFY_FAILED", "Re-imported exports do not match the scene model.",
                          {"failures": verify["failures"][:50]})
    progress(0.97, "adding the sensor layer")
    try:  # Blender's glTF becomes geometry.glb; scene.glb = geometry + layers (what every viewer loads)
        layer = apply_layers(build_dir, plan)
    except (LayerError, BundleError) as exc:
        raise BuildFailed(exc.code, str(exc), exc.detail) from exc
    manifest = write_manifest(scene, plan, build_dir, build_id, report, verify, version, report.get("reused", []),
                              layer)
    timings["total_s"] = round(time.time() - t0, 2)
    (build_dir / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    progress(1.0, "completed")
    return BuildResult(build_dir, manifest, timings, report.get("reused", []))


def copy_tree_files(src: Path, dst: Path) -> None:  # used by tests to snapshot builds
    shutil.copytree(src, dst, dirs_exist_ok=True)
