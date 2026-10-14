"""What the runtime Unreal viewer downloads for one build, and the checks that guard it.

The viewer loads the build's Blender glTF export (``export/web/scene.glb``) at runtime, so the server
never needs the Unreal Editor. Every node of that file carries the object name of the build manifest;
this module turns the manifest + scene into a *viewer manifest* (all coordinates already in Unreal
centimetres) and checks, before anything is served, that every glTF node lands where the scene model
says it should once converted to Unreal space.

Coordinate frames
    scene / Blender : metres, right-handed, +X east, +Y north, +Z up
    glTF (Y-up)     : metres, (x_g, y_g, z_g) = (x_b, z_b, -y_b)          (Blender exporter, export_yup)
    Unreal          : centimetres, left-handed, Z up: (100 x_b, -100 y_b, 100 z_b) = 100 * (x_g, z_g, y_g)
The glTF -> Unreal map swaps two axes (a reflection) and scales by 100; it is applied to vertices, normals
and node transforms alike (T_ue = C T_g C^-1). Triangle index order is kept: a reflected counter-clockwise
glTF triangle is exactly Unreal's front-face winding.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np

VIEWER_MANIFEST_VERSION = 1
TOL_CM = 1.0
MODEL_REL = "export/web/scene.glb"
_CTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class BundleError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


# ---- glTF binary ---------------------------------------------------------------------------

def read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    if len(data) < 20:
        raise BundleError("MODEL_INVALID", "the model file is too short to be a GLB")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise BundleError("MODEL_INVALID", f"not a glTF 2.0 binary (magic {magic!r}, version {version})")
    off, gltf, binary = 12, None, b""
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            binary = chunk
        off += 8 + clen  # chunk lengths include their 4-byte padding
    if gltf is None:
        raise BundleError("MODEL_INVALID", "the GLB has no JSON chunk")
    return gltf, binary


def accessor(gltf: dict, binary: bytes, index: int) -> np.ndarray:
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    dtype = np.dtype(_CTYPE[acc["componentType"]])
    n = _NCOMP[acc["type"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * n
    count = acc["count"]
    if stride == dtype.itemsize * n:
        arr = np.frombuffer(binary, dtype=dtype, count=count * n, offset=start).reshape(count, n)
    else:
        rows = np.lib.stride_tricks.as_strided(np.frombuffer(binary, dtype=np.uint8, offset=start),
                                               shape=(count, stride), strides=(stride, 1))
        arr = rows[:, : dtype.itemsize * n].copy().view(dtype).reshape(count, n)
    return arr


def node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # column-major in glTF
    t = node.get("translation", [0.0, 0.0, 0.0])
    x, y, z, w = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    s = node.get("scale", [1.0, 1.0, 1.0])
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * np.array(s)[None, :]
    m[:3, 3] = t
    return m


def gltf_to_ue(points_m: np.ndarray) -> np.ndarray:
    """glTF Y-up metres -> Unreal centimetres (x, z, y) * 100."""
    return points_m[..., [0, 2, 1]] * 100.0


def node_bounds_ue(gltf: dict, binary: bytes) -> dict[str, tuple[list[float], list[float]]]:
    """World AABB of every named mesh node in Unreal cm: the node transform applied to the mesh's local
    box (what Unreal reports as actor bounds; a rotated cabinet's box corners, not its rounded edges)."""
    positions: dict[int, np.ndarray] = {}

    def mesh_points(mi: int) -> np.ndarray:
        if mi not in positions:
            pts = [accessor(gltf, binary, p["attributes"]["POSITION"]).astype(np.float64)
                   for p in gltf["meshes"][mi]["primitives"]]
            positions[mi] = np.concatenate(pts) if pts else np.zeros((0, 3))
        return positions[mi]

    out: dict[str, tuple[list[float], list[float]]] = {}
    roots = gltf["scenes"][gltf.get("scene", 0)]["nodes"]
    stack = [(i, np.eye(4)) for i in roots]
    while stack:
        i, parent = stack.pop()
        node = gltf["nodes"][i]
        world = parent @ node_matrix(node)
        if "mesh" in node:
            p = mesh_points(node["mesh"])
            if len(p):
                lo, hi = p.min(axis=0), p.max(axis=0)
                box = np.array([[hi[0] if i & 1 else lo[0], hi[1] if i & 2 else lo[1], hi[2] if i & 4 else lo[2]]
                                for i in range(8)])
                w = (np.c_[box, np.ones(8)] @ world.T)[:, :3]
                ue = gltf_to_ue(w)
                out[node.get("name", f"node_{i}")] = (ue.min(axis=0).tolist(), ue.max(axis=0).tolist())
        stack.extend((c, world) for c in node.get("children", []))
    return out


# ---- expected placement from the scene model ------------------------------------------------

def scene_bounds_to_ue(bmin, bmax) -> tuple[list[float], list[float]]:
    """Scene AABB (m) -> Unreal AABB (cm): x*100, -y*100 (swaps min/max), z*100."""
    return ([bmin[0] * 100, -bmax[1] * 100, bmin[2] * 100], [bmax[0] * 100, -bmin[1] * 100, bmax[2] * 100])


def furniture_bounds_m(f: dict) -> tuple[list[float], list[float]]:
    w, d, h = f["size_m"]
    a = math.radians(f["rotation_deg"])
    x0, y0, z0 = f["location_m"]
    cs = [(x0 + lx * math.cos(a) - ly * math.sin(a), y0 + lx * math.sin(a) + ly * math.cos(a))
          for lx, ly in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))]
    return ([min(c[0] for c in cs), min(c[1] for c in cs), z0], [max(c[0] for c in cs), max(c[1] for c in cs), z0 + h])


def expected_bounds_ue(manifest: dict) -> dict[str, tuple[list[float], list[float]]]:
    out = {o["name"]: scene_bounds_to_ue(*o["bounds_m"]) for o in manifest["objects"]}
    out.update({f["name"]: scene_bounds_to_ue(*furniture_bounds_m(f)) for f in manifest["furniture"]})
    out.update({s["name"]: scene_bounds_to_ue(*s["bounds_m"]) for s in manifest.get("sensors", [])})
    return out


def check_model(manifest: dict, gltf: dict, binary: bytes, tol_cm: float = TOL_CM) -> dict:
    """Every manifest object must be a glTF node whose Unreal-space bounds match the scene model."""
    return compare_bounds(expected_bounds_ue(manifest), node_bounds_ue(gltf, binary), tol_cm)


def compare_bounds(expected: dict, got: dict, tol_cm: float = TOL_CM) -> dict:
    """Expected vs actual node bounds (name -> (min, max) in Unreal cm): the preflight report."""
    failures, worst = [], 0.0
    for name, (emin, emax) in expected.items():
        if name not in got:
            continue
        gmin, gmax = got[name]
        dev = max(abs(a - b) for a, b in zip(gmin + gmax, emin + emax))
        worst = max(worst, dev)
        if dev > tol_cm:
            failures.append({"object": name, "deviation_cm": round(dev, 3), "expected": [emin, emax], "got": [gmin, gmax]})
    missing = sorted(set(expected) - set(got))
    return {"checked": len(expected) - len(missing), "missing": missing[:50], "missing_count": len(missing),
            "extra_nodes": sorted(set(got) - set(expected))[:50], "max_deviation_cm": round(worst, 4),
            "tolerance_cm": tol_cm, "failures": failures[:50],
            "ok": not failures and not missing}


# ---- the viewer manifest -------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cm(p) -> list[float]:
    """Scene point (m) -> Unreal cm."""
    return [round(p[0] * 100, 2), round(-p[1] * 100, 2), round((p[2] if len(p) > 2 else 0.0) * 100, 2)]


ARCH_LABELS = {"door_leaf": "Door", "door_frame": "Door frame", "window_frame": "Window", "glass": "Window glass",
               "slab": "Floor slab", "ceiling": "Ceiling"}


def build_viewer_manifest(build_dir: Path, project_id: str, project_name: str, version: int | None) -> dict:
    """Viewer manifest for one completed build (pure function of the build directory)."""
    man_path, model = build_dir / "manifest.json", build_dir / MODEL_REL
    if not man_path.is_file():
        raise BundleError("BUILD_INCOMPLETE", "the build has no manifest.json")
    if not model.is_file():
        raise BundleError("MODEL_MISSING", f"the build has no {MODEL_REL}")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    scene = json.loads((build_dir / "scene.json").read_text(encoding="utf-8")) if (build_dir / "scene.json").is_file() else {}
    gltf, binary = read_glb(model)
    preflight = check_model(manifest, gltf, binary)
    rooms = {r["id"]: r for r in manifest["rooms"]}
    hv = scene.get("hvac") or {}
    units = {u["id"]: u for u in hv.get("units", [])}
    unit_of = {t["id"]: t.get("unit_id") for t in hv.get("terminals", [])} | {d["id"]: d.get("unit_id") for d in hv.get("ducts", [])}

    objects = []
    for o in manifest["objects"]:
        room = rooms.get(o.get("room_id")) if o.get("room_id") else None
        label = o.get("label") or ARCH_LABELS.get(o["kind"]) or (f"Wall {o['wall_id']}" if o["kind"] == "wall" else None)
        if o["kind"] == "floor" and room:
            label = room["name"]
        e = {"name": o["name"], "kind": o["kind"], "room_id": o.get("room_id"), "room_name": room["name"] if room else None,
             "label": label, "collision": o["collision"], "bounds_cm": scene_bounds_to_ue(*o["bounds_m"])}
        for k in ("hvac_id", "tag", "unit_kind", "airflow_lps", "system", "unit_tag", "wall_id", "opening_id"):
            if o.get(k) is not None:
                e[k] = o[k]
        if o["kind"] == "hvac_unit":
            e["unit_id"] = o.get("hvac_id")
        elif o["kind"].startswith("hvac_") and unit_of.get(o.get("hvac_id")):
            e["unit_id"] = unit_of[o["hvac_id"]]
        objects.append(e)
    for s in manifest.get("sensors", []):   # the sensor layer: selectable, never blocks movement
        e = {"name": s["name"], "kind": "sensor", "room_id": s.get("room_id"), "room_name": s.get("room_name"),
             "label": s["label"], "collision": "none", "bounds_cm": scene_bounds_to_ue(*s["bounds_m"]),
             "location_cm": s["unreal"]["location_cm"]}
        for k in ("sensor_id", "tag", "bms_point", "unit_id", "unit_tag", "mounting", "height_m"):
            if s.get(k) is not None:
                e[k] = s[k]
        e["sensor_kind"] = s["kind"]
        objects.append(e)
    furniture = []
    for f in manifest["furniture"]:
        w, d, h = f["size_m"]
        furniture.append({"name": f["name"], "kind": "furniture", "furniture_id": f["furniture_id"], "room_id": f["room_id"],
                          "room_name": f["room_name"], "label": f["label"], "asset_id": f["asset_id"],
                          "category": f["category"], "size_m": f["size_m"], "size_text": f"{w:.2f} x {d:.2f} x {h:.2f} m",
                          "collision": f["collision"], "location_cm": f["unreal"]["location_cm"],
                          "yaw_deg": f["unreal"]["yaw_deg"], "scale": f["unreal"]["scale"],
                          "bounds_cm": scene_bounds_to_ue(*furniture_bounds_m(f)), "status": f.get("status")})
    hvac_units = []
    for u in hv.get("units", []):
        hvac_units.append({"id": u["id"], "tag": u["tag"], "kind": u.get("kind"), "room_id": u.get("room_id"),
                           "airflow_lps": u.get("airflow_lps"), "cooling_kw": u.get("cooling_kw"),
                           "object": f"HVAC_Unit_{u['id']}",
                           "terminals": sorted(t["id"] for t in hv.get("terminals", []) if t.get("unit_id") == u["id"])})
    b = manifest["bounds_m"]
    ps = manifest["player_start"]
    return {
        "format": "twin-viewer-manifest", "manifest_version": VIEWER_MANIFEST_VERSION,
        "project": {"id": project_id, "name": project_name, "style": manifest["project"].get("style")},
        "build": {"id": manifest["build_id"], "version": version, "created_at": manifest.get("created_at"),
                  "scene_hash": manifest.get("scene_hash"), "blender": manifest.get("blender"),
                  "kind": manifest.get("build_kind", "full"), "geometry_build": manifest.get("geometry_build")},
        "units": {"model": "glTF 2.0 binary, metres, +Y up (Blender export)", "viewer": "Unreal cm, Z up"},
        "conversion": "UE(cm) = 100 * (x_gltf, z_gltf, y_gltf) = (100 x, -100 y, 100 z) of the scene model",
        "model": {"path": "model.glb", "bytes": model.stat().st_size, "sha256": _sha256(model),
                  "nodes": len(gltf.get("nodes", [])), "meshes": len(gltf.get("meshes", [])),
                  "materials": len(gltf.get("materials", [])), "images": len(gltf.get("images", []))},
        "preflight": preflight,
        "bounds_cm": scene_bounds_to_ue(*b),
        "rooms": [{"id": r["id"], "name": r["name"], "type": r.get("type"), "level_id": r.get("level_id"),
                   "dims_clear_m": r["dims_clear"], "area_m2": round(r["area_net"], 2),
                   "floor_z_cm": round(r["floor_z"] * 100, 2), "ceiling_z_cm": round(r["ceiling_z"] * 100, 2),
                   "spawn_cm": r.get("spawn_point_cm") or r["label_point_cm"], "label_cm": r["label_point_cm"],
                   "floor_material": r.get("floor_material"),
                   "polygon_cm": [[round(x * 100, 2), round(-y * 100, 2)] for x, y in r["polygon"]]}
                  for r in manifest["rooms"]],
        "objects": objects,
        "furniture": furniture,
        "lights": manifest["lights"],
        "player_start": {"location_cm": ps["unreal"]["location_cm"], "yaw_deg": ps["unreal"]["yaw_deg"],
                         "room_id": ps.get("room_id")},
        "hvac": {"units": hvac_units, "terminals": len(hv.get("terminals", [])), "ducts": len(hv.get("ducts", []))},
        "sensors": len(manifest.get("sensors", [])),
        "endpoints": {"live": "hvac/live", "airflow": "hvac/airflow"},
        "poll_seconds": 4,
        "validation": {"tolerance_cm": TOL_CM},
    }


def viewer_manifest(build_dir: Path, project_id: str, project_name: str, version: int | None) -> dict:
    """Cached per build (builds are immutable): build_dir/viewer/viewer_manifest.json."""
    cache = build_dir / "viewer" / "viewer_manifest.json"
    model = build_dir / MODEL_REL
    if cache.is_file() and model.is_file():
        try:
            m = json.loads(cache.read_text(encoding="utf-8"))
            if (m.get("manifest_version") == VIEWER_MANIFEST_VERSION and m["model"]["bytes"] == model.stat().st_size
                    and m["project"]["name"] == project_name):
                return m
        except (ValueError, KeyError):
            pass
    m = build_viewer_manifest(build_dir, project_id, project_name, version)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(m), encoding="utf-8")
    tmp.replace(cache)
    return m


# ---- airflow illustration geometry in Unreal space -------------------------------------------

def airflow_ue(scene) -> dict:
    """Per-unit airflow illustration zones (twin.hvac.illustration) + supply/return terminals, Unreal cm.

    Illustration only: positions and directions say where air enters and leaves; the paths the viewer
    draws between them are schematic, not a flow solution."""
    from twin.hvac.illustration import airflow_zones
    from twin.scene.compile import hvac_placement

    z = airflow_zones(scene)
    place = hvac_placement(scene)
    terms: dict[str, list] = {}
    for t in scene.hvac.terminals:
        if not t.unit_id:
            continue
        zz = place[t.id]
        terms.setdefault(t.unit_id, []).append({
            "id": t.id, "kind": t.kind, "throw": t.throw, "airflow_lps": t.airflow_lps,
            "rotation_yaw_deg": round(-t.rotation_deg, 3), "size_cm": [round(t.width * 100, 1), round(t.depth * 100, 1)],
            "location_cm": _cm((t.position[0], t.position[1], zz["z0"]))})
    units = {u.id: u for u in scene.hvac.units}
    zones = []
    for zn in z["zones"]:
        u = units[zn["unit_id"]]
        zz = place[u.id]
        o = zn["outlet"]["o"]  # viewer frame (X, Y up, Z = -y)
        d = zn["outlet"]["dir"]
        bx0, bz0, bx1, bz1 = zn["b"]
        zones.append({
            "unit_id": u.id, "tag": u.tag, "room_id": u.room_id, "kind": u.kind, "airflow_lps": zn["airflowLs"],
            "unit_cm": _cm((u.position[0], u.position[1], (zz["z0"] + zz["z1"]) / 2)),
            "outlet_cm": [round(o[0] * 100, 2), round(o[2] * 100, 2), round(o[1] * 100, 2)],
            "outlet_dir": None if d is None else [d[0], d[2], 0.0],
            "box_cm": [[round(bx0 * 100, 1), round(bz0 * 100, 1)], [round(bx1 * 100, 1), round(bz1 * 100, 1)]],
            "ceiling_cm": round(zn["outlet"]["ceiling"] * 100, 1),
            "supply": [t for t in terms.get(u.id, []) if t["kind"].startswith("supply") or t["kind"] == "linear_slot"],
            "return": [t for t in terms.get(u.id, []) if t["kind"] in ("return_grille", "exhaust_grille")],
        })
    return {"kind": "illustration",
            "disclaimer": "Schematic airflow illustration. Paths are drawn from outlet positions and design "
                          "airflow; they are not measured and not a CFD result.",
            "level_id": z["level_id"],
            "outline_cm": [[round(x * 100, 1), round(zz_ * 100, 1)] for x, zz_ in z["outline"]],
            "zones": zones}
