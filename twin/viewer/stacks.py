"""Multi-floor buildings: floor projects stacked into one walkable building for the Unreal viewer.

Every floor stays its own project (its own versions, builds, HVAC layer, sensors, AI runs). A *stack* lists
them bottom to top and is stored as a small JSON file:

    data/stacks/<sid>/stack.json   {"id", "name", "floors": [{"project_id", "build_id" | null, "label" | null,
                                    "elevation_m" | null, "offset_m": [dx, dy] | null}], "created_at", "updated_at"}

A *composite* is the stack resolved against concrete builds (a pinned build, else the floor's newest completed
build) and written once:

    data/stacks/<sid>/composites/<cid>/{plan.json, model.glb, viewer_manifest.json}

* plan alignment: floor i's plan frame -> floor 1's. The given ``offset_m``, else the structural grid lines of
  the two floor-plan PDFs (the matcher that aligns HVAC sheets, twin.hvac.register), else none (a warning);
* elevation: the given ``elevation_m``, else the floor below's elevation + the top of anything on it (its wall
  height, or higher ceiling-void ducts and units) + 2 cm clearance + this floor's slab, rounded up to 5 cm;
* model.glb: the floors' Blender glTF exports in one file, each floor's nodes under a parent node
  ``Floor_<key>`` that carries its offset; every node name gets the prefix ``<key>_`` (F1_, F2_, ...);
* viewer_manifest.json: the floors' viewer manifests merged the same way (names and room / unit / furniture /
  sensor ids prefixed, positions shifted, room names led by the floor label) plus ``floors``; its preflight
  re-checks every node of the merged file against the merged expected bounds.

The composite id hashes all of the above, so a rebuilt floor or a changed setting gives a new composite while
older viewer links keep working as long as their files exist. The Unreal viewer opens a stack exactly like a
build (project id = stack id, build id = composite id) and lets the visitor jump between floors.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import struct
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from twin.api.storage import NotFound as NotFoundError
from twin.viewer.bundle import (MODEL_REL, TOL_CM, VIEWER_MANIFEST_VERSION, BundleError, compare_bounds, node_bounds_ue,
                                read_glb, viewer_manifest)

STACK_PREFIX = "s_"
CLEARANCE_M = 0.02          # between the top of a floor's contents and the slab above
ELEVATION_STEP_M = 0.05     # automatic elevations are rounded up to this
PT_100 = 0.0254 / 72 * 100  # metres per drawing point at 1:100: the unit of the grid matcher's tolerances
MIN_GRID_LINES = 3          # matched lines per axis for a grid alignment
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class StackError(BundleError):
    """Error with a code safe to show to the client (same shape as BundleError)."""


class FloorSpec(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    build_id: str | None = Field(default=None, max_length=64, description="pin a build (default: the newest completed one)")
    label: str | None = Field(default=None, max_length=40, description="shown in the viewer (default: Level <n>)")
    elevation_m: float | None = Field(default=None, ge=-50.0, le=500.0, description="floor finish height (default: stacked)")
    offset_m: list[float] | None = Field(default=None, description="[dx, dy] of this floor's plan frame in floor 1's "
                                                                     "(default: from the structural grid)")

    @field_validator("offset_m")
    @classmethod
    def _two(cls, v):
        if v is not None and (len(v) != 2 or any(abs(x) > 10000 for x in v)):
            raise ValueError("offset_m must be [dx, dy] in metres")
        return v


class StackSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    floors: list[FloorSpec] = Field(min_length=1, max_length=20, description="bottom floor first")


def is_stack_id(value: str) -> bool:
    return value.startswith(STACK_PREFIX)


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


class StackStore:
    """Stack definitions under <data>/stacks (JSON files; a stack is small and edited rarely)."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "stacks"

    def folder(self, sid: str) -> Path:
        if not is_stack_id(sid) or not sid[2:].isalnum():
            raise StackError("NOT_FOUND", "No such multi-floor building.")
        return self.root / sid

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for f in sorted(self.root.glob(f"{STACK_PREFIX}*/stack.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return sorted(out, key=lambda s: s.get("updated_at", 0), reverse=True)

    def get(self, sid: str) -> dict:
        path = self.folder(sid) / "stack.json"
        if not path.is_file():
            raise StackError("NOT_FOUND", "No such multi-floor building.")
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, spec: StackSpec) -> dict:
        now = time.time()
        sid = f"{STACK_PREFIX}{secrets.token_hex(6)}"
        doc = {"id": sid, **spec.model_dump(mode="json"), "created_at": now, "updated_at": now}
        _write_json(self.folder(sid) / "stack.json", doc)
        return doc

    def update(self, sid: str, spec: StackSpec) -> dict:
        doc = {**self.get(sid), **spec.model_dump(mode="json"), "updated_at": time.time()}
        _write_json(self.folder(sid) / "stack.json", doc)
        return doc

    def delete(self, sid: str) -> None:
        import shutil

        self.get(sid)
        shutil.rmtree(self.folder(sid))

    def composite_dir(self, sid: str, cid: str) -> Path:
        if not cid.startswith("c_") or not cid[2:].isalnum():
            raise StackError("NOT_FOUND", "No such composite build of this multi-floor building.")
        return self.folder(sid) / "composites" / cid


# ---- alignment and heights ------------------------------------------------------------------------

def plan_grid_m(storage, pid: str) -> tuple[list[float], list[float]] | None:
    """The structural grid lines of a project's floor-plan PDF in its plan frame (metres), or None."""
    from twin.hvac.importer import HvacImportError, PlanSheetInfo, plan_grid
    from twin.hvac.routes import plan_sheet_for

    try:
        spec, _ = plan_sheet_for(storage, pid, None)
    except (HvacImportError, NotFoundError):
        return None
    info = PlanSheetInfo(pdf=None, page_h_pt=spec["page_h_pt"], m_per_pt=spec["m_per_pt"], cache_dir=Path(spec["cache_dir"]),
                         scale=spec.get("scale"))
    px, py, _ = plan_grid(info)
    if not px and not py:
        info.pdf = Path(spec["pdf"]).read_bytes()
        px, py, _ = plan_grid(info)
    k, h = spec["m_per_pt"], spec["page_h_pt"]
    return sorted(x * k for x in px), sorted((h - y) * k for y in py)


def align_grids(ref: tuple[list[float], list[float]] | None, floor: tuple[list[float], list[float]] | None) -> dict | None:
    """Offset (dx, dy) that moves `floor`'s plan frame onto `ref`'s, from their shared structural grid."""
    from twin.hvac.register import match_axis

    if not ref or not floor:
        return None
    ox, nx, rx = match_axis([v / PT_100 for v in floor[0]], [v / PT_100 for v in ref[0]])
    oy, ny, ry = match_axis([v / PT_100 for v in floor[1]], [v / PT_100 for v in ref[1]])
    if nx < MIN_GRID_LINES or ny < MIN_GRID_LINES:
        return None
    return {"offset_m": [round(ox * PT_100, 4), round(oy * PT_100, 4)], "method": "structural grid",
            "matched": [nx, ny], "rms_m": round(max(rx, ry) * PT_100, 4)}


def _ceil_step(value: float) -> float:
    return round(math.ceil(value / ELEVATION_STEP_M - 1e-6) * ELEVATION_STEP_M, 4)


def floor_to_floor(lower: dict, upper: dict) -> float:
    """Height from the lower floor's finish to the upper one's: its contents (walls, or ducts and units in the
    ceiling void when they reach higher), a clearance and the upper floor's slab."""
    return _ceil_step(max(lower["wall_height_m"], lower["top_m"]) + CLEARANCE_M + upper["slab_m"])


# ---- resolving a stack against concrete builds ----------------------------------------------------

def resolve(storage, stack: dict) -> dict:
    """The composite plan: which build each floor uses, where it goes, and the composite id."""
    floors, warnings = [], []
    for i, spec in enumerate(stack["floors"]):
        pid = spec["project_id"]
        try:
            project = storage.get_project(pid)
        except NotFoundError:
            raise StackError("FLOOR_NOT_FOUND", f"Floor {i + 1}: project {pid} does not exist.")
        if spec.get("build_id"):
            try:
                build = storage.get_build(pid, spec["build_id"])
            except NotFoundError:
                raise StackError("FLOOR_NOT_FOUND", f"Floor {i + 1}: build {spec['build_id']} does not exist.")
        else:
            build = storage.latest_completed_build(pid)
        if build is None or build["status"] != "completed":
            raise StackError("FLOOR_NOT_BUILT", f"Floor {i + 1} ({project['name']}) has no completed build; generate it first.")
        folder = storage.build_dir(pid, build["id"])
        try:
            vm = viewer_manifest(folder, pid, project["name"], build["version"])
        except BundleError as exc:
            raise StackError(exc.code, f"Floor {i + 1} ({project['name']}): {exc}", exc.detail)
        scene = storage.load_scene(pid, build["version"])
        level = scene.levels[0]
        top_cm = vm["bounds_cm"][1][2]
        floors.append({
            "key": f"F{i + 1}", "index": i, "label": spec.get("label") or f"Level {i + 1}",
            "project_id": pid, "project_name": project["name"], "build_id": build["id"], "version": build["version"],
            "model_sha256": vm["model"]["sha256"], "level_elevation_m": level.elevation,
            "wall_height_m": level.wall_height, "slab_m": level.slab_thickness,
            "top_m": round(top_cm / 100.0 - level.elevation, 4),
        })
    # plan alignment against floor 1
    ref_grid = plan_grid_m(storage, floors[0]["project_id"]) if len(floors) > 1 else None
    for f, spec in zip(floors, stack["floors"]):
        if f["index"] == 0:
            f["alignment"] = {"offset_m": list(spec.get("offset_m") or [0.0, 0.0]), "method": "reference floor"}
        elif spec.get("offset_m") is not None:
            f["alignment"] = {"offset_m": list(spec["offset_m"]), "method": "given"}
        elif f["project_id"] == floors[0]["project_id"]:
            f["alignment"] = {"offset_m": list(floors[0]["alignment"]["offset_m"]), "method": "same drawing as floor 1"}
        else:
            a = align_grids(ref_grid, plan_grid_m(storage, f["project_id"]))
            if a is None:
                a = {"offset_m": [0.0, 0.0], "method": "none (scene origins)"}
                warnings.append(f"{f['label']}: no structural grid shared with {floors[0]['label']} was found in the floor-plan "
                                f"PDFs; the floors are placed by their drawing origins. Set offset_m if they do not line up.")
            else:
                ox, oy = floors[0]["alignment"]["offset_m"]
                a["offset_m"] = [round(a["offset_m"][0] + ox, 4), round(a["offset_m"][1] + oy, 4)]
            f["alignment"] = a
    # heights
    for f, spec in zip(floors, stack["floors"]):
        if spec.get("elevation_m") is not None:
            f["elevation_m"], f["elevation_source"] = float(spec["elevation_m"]), "given"
        elif f["index"] == 0:
            f["elevation_m"], f["elevation_source"] = 0.0, "ground"
        else:
            below = floors[f["index"] - 1]
            f["elevation_m"] = round(below["elevation_m"] + floor_to_floor(below, f), 4)
            f["elevation_source"] = "stacked"
        f["z_shift_m"] = round(f["elevation_m"] - f["level_elevation_m"], 4)
    for lower, upper in zip(floors, floors[1:]):
        gap = upper["elevation_m"] - upper["slab_m"] - (lower["elevation_m"] + max(lower["wall_height_m"], lower["top_m"]))
        lower["floor_to_floor_m"] = round(upper["elevation_m"] - lower["elevation_m"], 4)
        if gap < -1e-6:
            warnings.append(f"{lower['label']} reaches {-gap:.2f} m into {upper['label']}'s slab "
                            f"(floor-to-floor {lower['floor_to_floor_m']:.2f} m).")
    key = json.dumps({"floors": [{k: f[k] for k in ("key", "label", "project_id", "build_id", "model_sha256", "elevation_m",
                                                    "z_shift_m")} | {"offset_m": f["alignment"]["offset_m"]} for f in floors],
                      "name": stack["name"], "v": VIEWER_MANIFEST_VERSION}, sort_keys=True)
    return {"stack_id": stack["id"], "name": stack["name"], "composite_id": "c_" + hashlib.sha256(key.encode()).hexdigest()[:12],
            "floors": floors, "warnings": warnings}


# ---- merging the floors' glTF files ---------------------------------------------------------------

def _shift_texture_refs(obj, base_tex: int) -> None:
    """Every ``{...Texture: {"index": i}}`` reference in a material (core and KHR_materials_* alike)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("Texture") and isinstance(v, dict) and "index" in v:
                v["index"] += base_tex
            _shift_texture_refs(v, base_tex)
    elif isinstance(obj, list):
        for v in obj:
            _shift_texture_refs(v, base_tex)


def merge_glbs(parts: list[tuple[str, Path, tuple[float, float, float]]]) -> tuple[bytes, dict]:
    """[(key, glb, translation in glTF metres)] -> one GLB: each part's scene roots under a node ``Floor_<key>``
    with that translation, node names prefixed ``<key>_``. Returns the file and its JSON."""
    out = {"asset": {"version": "2.0", "generator": "twin multi-floor merge"}, "scene": 0, "scenes": [{"nodes": []}],
           "nodes": [], "meshes": [], "accessors": [], "bufferViews": [], "materials": [], "textures": [], "images": [],
           "samplers": []}
    blob = bytearray()
    used, required = set(), set()
    for key, path, translation in parts:
        g, binary = read_glb(path)
        for unsupported in ("skins", "animations", "cameras"):
            if g.get(unsupported):
                raise StackError("MODEL_UNSUPPORTED", f"{path.name} has {unsupported}; building models never should.")
        if len(g.get("buffers", [])) > 1 or any("uri" in b for b in g.get("buffers", [])):
            raise StackError("MODEL_UNSUPPORTED", f"{path.name} uses external buffers; only self-contained GLB files can be merged.")
        if any("uri" in im for im in g.get("images", [])):
            raise StackError("MODEL_UNSUPPORTED", f"{path.name} references external images.")
        base = {k: len(out[k]) for k in ("nodes", "meshes", "accessors", "bufferViews", "materials", "textures", "images", "samplers")}
        while len(blob) % 8:
            blob.append(0)
        bin_off = len(blob)
        blob += binary
        for bv in g.get("bufferViews", []):
            out["bufferViews"].append({**bv, "buffer": 0, "byteOffset": bv.get("byteOffset", 0) + bin_off})
        for acc in g.get("accessors", []):
            a = json.loads(json.dumps(acc))
            if "bufferView" in a:
                a["bufferView"] += base["bufferViews"]
            for part in ("indices", "values"):
                if part in a.get("sparse", {}):
                    a["sparse"][part]["bufferView"] += base["bufferViews"]
            out["accessors"].append(a)
        for mesh in g.get("meshes", []):
            m = json.loads(json.dumps(mesh))
            for p in m.get("primitives", []):
                p["attributes"] = {k: v + base["accessors"] for k, v in p["attributes"].items()}
                if "indices" in p:
                    p["indices"] += base["accessors"]
                if "material" in p:
                    p["material"] += base["materials"]
                if "targets" in p:
                    p["targets"] = [{k: v + base["accessors"] for k, v in t.items()} for t in p["targets"]]
            out["meshes"].append(m)
        for mat in g.get("materials", []):
            m = json.loads(json.dumps(mat))
            _shift_texture_refs(m, base["textures"])
            out["materials"].append(m)
        for tex in g.get("textures", []):
            t = dict(tex)
            if "source" in t:
                t["source"] += base["images"]
            if "sampler" in t:
                t["sampler"] += base["samplers"]
            out["textures"].append(t)
        for im in g.get("images", []):
            out["images"].append({**im, "bufferView": im["bufferView"] + base["bufferViews"]})
        out["samplers"].extend(g.get("samplers", []))
        for node in g.get("nodes", []):
            n = json.loads(json.dumps(node))
            if "name" in n:
                n["name"] = f"{key}_{n['name']}"
            if "mesh" in n:
                n["mesh"] += base["meshes"]
            if "children" in n:
                n["children"] = [c + base["nodes"] for c in n["children"]]
            out["nodes"].append(n)
        roots = g["scenes"][g.get("scene", 0)]["nodes"]
        out["nodes"].append({"name": f"Floor_{key}", "translation": [round(v, 6) for v in translation],
                             "children": [r + base["nodes"] for r in roots]})
        out["scenes"][0]["nodes"].append(len(out["nodes"]) - 1)
        used.update(g.get("extensionsUsed", []))
        required.update(g.get("extensionsRequired", []))
    while len(blob) % 4:
        blob.append(0)
    out["buffers"] = [{"byteLength": len(blob)}]
    for k in ("textures", "images", "samplers", "materials"):
        if not out[k]:
            del out[k]
    if used:
        out["extensionsUsed"] = sorted(used)
    if required:
        out["extensionsRequired"] = sorted(required)
    js = json.dumps(out, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    data = (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(blob)) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    return data, out


# ---- merging the floors' viewer manifests ---------------------------------------------------------

def ue_offset(floor: dict) -> list[float]:
    """Scene-frame shift of a floor (offset_m, z_shift_m) in Unreal cm."""
    dx, dy = floor["alignment"]["offset_m"]
    return [round(dx * 100, 3), round(-dy * 100, 3), round(floor["z_shift_m"] * 100, 3)]


def _p(prefix: str, value):
    return None if value is None else f"{prefix}{value}"


def _pt(p, o) -> list[float]:
    return [round(p[0] + o[0], 2), round(p[1] + o[1], 2), round((p[2] if len(p) > 2 else 0.0) + o[2], 2)]


def _box(b, o) -> list[list[float]]:
    return [_pt(b[0], o), _pt(b[1], o)]


def merge_manifests(plan: dict, floor_manifests: list[dict]) -> dict:
    """The floors' viewer manifests as one (everything except model + preflight, which need the merged file)."""
    rooms, objects, furniture, lights, units = [], [], [], [], []
    terminals = ducts = sensors = 0
    floors, lo, hi = [], [math.inf] * 3, [-math.inf] * 3
    sun_done = False
    for f, vm in zip(plan["floors"], floor_manifests):
        key, pre, o = f["key"], f"{f['key']}_", ue_offset(f)
        named = lambda n: None if n is None else f"{f['label']} · {n}"  # noqa: E731
        for r in vm["rooms"]:
            rooms.append({**r, "id": pre + r["id"], "name": named(r["name"]), "floor": key,
                          "floor_z_cm": round(r["floor_z_cm"] + o[2], 2), "ceiling_z_cm": round(r["ceiling_z_cm"] + o[2], 2),
                          "spawn_cm": _pt(r["spawn_cm"], o), "label_cm": _pt(r["label_cm"], o),
                          "polygon_cm": [[round(x + o[0], 2), round(y + o[1], 2)] for x, y in r["polygon_cm"]]})
        for ob in vm["objects"]:
            e = {**ob, "name": pre + ob["name"], "floor": key, "bounds_cm": _box(ob["bounds_cm"], o)}
            for k in ("room_id", "hvac_id", "unit_id", "wall_id", "opening_id", "sensor_id"):
                if ob.get(k) is not None:
                    e[k] = pre + ob[k]
            e["room_name"] = named(ob.get("room_name"))
            if ob["kind"] == "floor":
                e["label"] = named(ob.get("label"))
            if "location_cm" in ob:
                e["location_cm"] = _pt(ob["location_cm"], o)
            objects.append(e)
        for fu in vm["furniture"]:
            furniture.append({**fu, "name": pre + fu["name"], "furniture_id": pre + fu["furniture_id"],
                              "room_id": _p(pre, fu.get("room_id")), "room_name": named(fu.get("room_name")), "floor": key,
                              "location_cm": _pt(fu["location_cm"], o), "bounds_cm": _box(fu["bounds_cm"], o)})
        for li in vm["lights"]:
            if li.get("type") == "directional":
                if not sun_done:
                    lights.append(li)
                    sun_done = True
                continue
            lights.append({**li, "floor": key, **({"location_cm": _pt(li["location_cm"], o)} if "location_cm" in li else {})})
        for u in vm["hvac"]["units"]:
            units.append({**u, "id": pre + u["id"], "room_id": _p(pre, u.get("room_id")), "object": pre + u["object"],
                          "terminals": [pre + t for t in u.get("terminals", [])], "floor": key})
        terminals += vm["hvac"].get("terminals", 0)
        ducts += vm["hvac"].get("ducts", 0)
        sensors += vm.get("sensors", 0)
        b = _box(vm["bounds_cm"], o)
        lo = [min(a, c) for a, c in zip(lo, b[0])]
        hi = [max(a, c) for a, c in zip(hi, b[1])]
        ps = vm["player_start"]
        floors.append({
            "key": key, "index": f["index"], "label": f["label"], "project_id": f["project_id"],
            "project_name": f["project_name"], "build_id": f["build_id"], "version": f["version"],
            "elevation_m": f["elevation_m"], "elevation_source": f["elevation_source"],
            "floor_z_cm": round(f["elevation_m"] * 100, 2), "offset_cm": o, "alignment": f["alignment"],
            "floor_to_floor_m": f.get("floor_to_floor_m"), "bounds_cm": b, "rooms": len(vm["rooms"]),
            "hvac_units": len(vm["hvac"]["units"]), "unit_prefix": pre,
            "player_start": {"location_cm": _pt(ps["location_cm"], o), "yaw_deg": ps["yaw_deg"],
                             "room_id": _p(pre, ps.get("room_id"))},
        })
    first = floor_manifests[0]
    return {
        "format": "twin-viewer-manifest", "manifest_version": VIEWER_MANIFEST_VERSION,
        "project": {"id": plan["stack_id"], "name": plan["name"], "style": first["project"].get("style"), "kind": "stack"},
        "build": {"id": plan["composite_id"], "version": None, "kind": "stack", "floors": len(floors),
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "units": first["units"], "conversion": first["conversion"],
        "bounds_cm": [lo, hi],
        "rooms": rooms, "objects": objects, "furniture": furniture, "lights": lights,
        "player_start": floors[0]["player_start"],
        "hvac": {"units": units, "terminals": terminals, "ducts": ducts},
        "sensors": sensors,
        "floors": floors,
        "stack": {"id": plan["stack_id"], "composite": plan["composite_id"], "warnings": plan["warnings"]},
        "endpoints": first["endpoints"], "poll_seconds": first["poll_seconds"], "validation": first["validation"],
    }


def composite(storage, store: StackStore, stack: dict, cid: str | None = None) -> tuple[dict, Path]:
    """The viewer manifest + folder of a composite: the current one (resolved now) or an older one by id."""
    if cid is not None:
        folder = store.composite_dir(stack["id"], cid)
        if (folder / "viewer_manifest.json").is_file():
            return json.loads((folder / "viewer_manifest.json").read_text(encoding="utf-8")), folder
    plan = resolve(storage, stack)
    if cid is not None and cid != plan["composite_id"]:
        raise StackError("STACK_CHANGED", "This multi-floor building has changed since the link was made; open it again "
                                          "from the web app.")
    folder = store.composite_dir(stack["id"], plan["composite_id"])
    with _lock(folder.as_posix()):
        done = folder / "viewer_manifest.json"
        if done.is_file():
            return json.loads(done.read_text(encoding="utf-8")), folder
        manifests = [viewer_manifest(storage.build_dir(f["project_id"], f["build_id"]), f["project_id"], f["project_name"],
                                     f["version"]) for f in plan["floors"]]
        parts = []
        for f in plan["floors"]:
            dx, dy = f["alignment"]["offset_m"]
            parts.append((f["key"], storage.build_dir(f["project_id"], f["build_id"]) / MODEL_REL, (dx, f["z_shift_m"], -dy)))
        data, gltf = merge_glbs(parts)
        _, binary = _split_glb(data)
        man = merge_manifests(plan, manifests)
        expected = {o["name"]: tuple(o["bounds_cm"]) for o in man["objects"]}
        expected.update({fu["name"]: tuple(fu["bounds_cm"]) for fu in man["furniture"]})
        man["preflight"] = compare_bounds(expected, node_bounds_ue(gltf, binary), TOL_CM)
        man["model"] = {"path": "model.glb", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                        "nodes": len(gltf["nodes"]), "meshes": len(gltf["meshes"]), "materials": len(gltf.get("materials", [])),
                        "images": len(gltf.get("images", []))}
        if not man["preflight"]["ok"]:
            raise StackError("STACK_PREFLIGHT_FAILED", "The merged multi-floor model does not match its floors' scene models.",
                             {"preflight": man["preflight"]})
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "model.glb.tmp"
        tmp.write_bytes(data)
        tmp.replace(folder / "model.glb")
        _write_json(folder / "plan.json", plan)
        _write_json(done, man)
        return man, folder


def _split_glb(data: bytes) -> tuple[dict, bytes]:
    off, gltf, binary = 12, None, b""
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == 0x4E4F534A:
            gltf = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            binary = chunk
        off += 8 + clen
    return gltf, binary
