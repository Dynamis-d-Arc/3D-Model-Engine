"""Compile a validated scene into a deterministic build plan for the Blender worker.

The plan lists every solid as extruded polygons ("prisms") with pre-computed cap
triangulations, so Blender only has to create vertices and faces. Wall openings are real
gaps: walls are split into pieces around doors and windows. Nothing here depends on AI
output beyond the numbers stored in the scene model.

Vertical layout per level (z relative to level.elevation):
    slab          [-slab_thickness, -FLOOR_FINISH]
    floor finish  [-FLOOR_FINISH, floor_offset]          one per room
    walls         [-FLOOR_FINISH, wall_height]           split around openings
    ceiling       [ceiling_height, ceiling_height + CEILING_THICKNESS]
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import shapely
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene import styles
from twin.scene.model import Opening, Room, Scene
from twin.scene.transforms import scene_to_unreal_location, scene_to_unreal_scale, scene_to_unreal_yaw

PLAN_VERSION = 1
LIBRARY_VERSION = "1.1.0"  # bump when the Blender furniture library geometry changes

FLOOR_FINISH = 0.02
CEILING_THICKNESS = 0.02
FRAME_WIDTH = 0.05
DOOR_LEAF_THICKNESS = 0.04
WINDOW_FRAME_DEPTH = 0.07
GLASS_THICKNESS = 0.012
EYE_HEIGHT = 1.6
BIG = 1000.0

LIGHT_COLORS = {"warm": "#FFC98F", "neutral": "#FFE4C7", "cool": "#E8EEFF", "daylight": "#FFFFFF"}


class CompileError(ValueError):
    pass


def _round(v: float) -> float:
    r = round(v, 6)
    return 0.0 if r == 0 else r


def canonical_hash(obj: Any) -> str:
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def scene_hash(scene: Scene) -> str:
    return canonical_hash(scene.model_dump(mode="json"))


# ---------------------------------------------------------------------------------------------
# prisms
# ---------------------------------------------------------------------------------------------

def make_prism(poly: Polygon, z0: float, z1: float, mat: str, mat_pos: str | None = None,
               mat_neg: str | None = None) -> dict:
    """Extruded polygon with cap triangulation (indices into the flattened ring vertices)."""
    if z1 - z0 <= 1e-6:
        raise CompileError(f"prism with non-positive height {z0}..{z1}")
    poly = orient(shapely.set_precision(poly, g.PRECISION_GRID), sign=1.0)
    rings = [list(poly.exterior.coords)[:-1]] + [list(r.coords)[:-1] for r in poly.interiors]
    rings = [[(_round(x), _round(y)) for x, y in ring] for ring in rings]
    index: dict[tuple[float, float], int] = {}
    flat: list[tuple[float, float]] = []
    for ring in rings:
        for p in ring:
            index.setdefault(p, len(flat))
            flat.append(p)
    tris: list[list[int]] = []
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        pts = [(_round(x), _round(y)) for x, y in list(tri.exterior.coords)[:-1]]
        try:
            ids = [index[p] for p in pts]
        except KeyError as exc:  # CDT must not invent vertices; refuse rather than guess
            raise CompileError(f"triangulation produced an unexpected vertex {exc}") from exc
        if g.signed_area(pts) < 0:
            ids.reverse()
        tris.append(ids)
    out = {"rings": [[list(p) for p in ring] for ring in rings], "z0": _round(z0), "z1": _round(z1),
           "tris": tris, "mat": mat}
    if mat_pos:
        out["mat_pos"] = mat_pos
    if mat_neg:
        out["mat_neg"] = mat_neg
    return out


def prism_bounds(prisms: list[dict]) -> list[list[float]]:
    xs = [p[0] for pr in prisms for ring in pr["rings"] for p in ring]
    ys = [p[1] for pr in prisms for ring in pr["rings"] for p in ring]
    zs = [z for pr in prisms for z in (pr["z0"], pr["z1"])]
    return [[_round(min(xs)), _round(min(ys)), _round(min(zs))], [_round(max(xs)), _round(max(ys)), _round(max(zs))]]


def prism_volume(prism: dict) -> float:
    poly = Polygon(prism["rings"][0], prism["rings"][1:])
    return poly.area * (prism["z1"] - prism["z0"])


def _arch_object(name: str, kind: str, prisms: list[dict], *, level_id: str, collision: str = "complex",
                 axis: tuple[float, float] | None = None, **meta: Any) -> dict:
    obj = {"name": name, "kind": kind, "level_id": level_id, "collision": collision, "prisms": prisms,
           "expected_bounds": prism_bounds(prisms)}
    if axis is not None:
        obj["axis"] = [_round(axis[0]), _round(axis[1])]
    obj.update({k: v for k, v in meta.items() if v is not None})
    return obj


# ---------------------------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------------------------

def compile_scene(scene: Scene) -> dict:
    catalog = load_catalog()
    mats = styles.material_table(scene)
    used_mats: set[str] = set()
    objects: list[dict] = []
    furniture: list[dict] = []
    lights: list[dict] = []
    cameras: list[dict] = []
    rooms_out: list[dict] = []
    openings_out: list[dict] = []
    open_mats = styles.opening_materials(scene)

    def use(mat_id: str) -> str:
        if mat_id not in mats:
            raise CompileError(f"unknown material '{mat_id}'")
        used_mats.add(mat_id)
        return mat_id

    for level in scene.levels:
        elev = level.elevation
        walls = g.walls_on_level(scene, level.id)
        rooms = g.rooms_on_level(scene, level.id)
        fps = g.wall_footprints(walls) if walls else {}
        room_polys = {r.id: g.room_polygon(r) for r in rooms}
        for rid, poly in room_polys.items():
            if not poly.is_valid:
                raise CompileError(f"room {rid} polygon is invalid; fix validation errors first")

        def wall_mat_for(room: Room | None, height: float, wall_kind: str) -> str:
            if room is not None:
                return styles.room_wall_material_at(scene, room, height)
            if wall_kind == "exterior":
                return styles.exterior_wall_material(scene)
            return styles.STYLES[scene.project.style]["walls"]["default"]

        # ---- walls with real openings -----------------------------------------------------
        for w in walls:
            f = g.wall_frame(w)
            polys = g.polygons_of(fps[w.id])
            if not polys:
                continue
            H = scene.wall_height(w)
            zb, zt = elev - FLOOR_FINISH, elev + H
            svals = [f.to_local(x, y)[0] for p in polys for x, y in p.exterior.coords]
            smin, smax = min(svals), max(svals)
            ops = [o for o in scene.openings if o.wall_id == w.id]
            bps = {smin, smax}
            for o in ops:
                bps.update({min(max(o.offset - o.width / 2, smin), smax), min(max(o.offset + o.width / 2, smin), smax)})
            probe = w.thickness / 2 + 0.03
            for r in rooms:  # split where rooms change along either face, so each piece has one finish per side
                for x, y in r.polygon:
                    s, d = f.to_local(x, y)
                    if abs(d) <= probe and smin + 1e-4 < s < smax - 1e-4:
                        bps.add(s)
            cuts = sorted(bps)
            merged = [cuts[0]]
            for s in cuts[1:]:
                if s - merged[-1] > 1e-4:
                    merged.append(s)
            prisms: list[dict] = []
            for s0, s1 in zip(merged, merged[1:]):
                mid = (s0 + s1) / 2
                strip = Polygon(f.rect(s0, s1, -BIG, BIG))
                pieces = [pp for poly in polys for pp in g.polygons_of(poly.intersection(strip))]
                if not pieces:
                    continue
                cover = next((o for o in ops if o.offset - o.width / 2 - 1e-6 <= mid <= o.offset + o.width / 2 + 1e-6), None)
                if cover is None:
                    zr = [(zb, zt)]
                else:
                    zr = []
                    if cover.sill_height > 1e-4:
                        zr.append((zb, elev + cover.sill_height))
                    if elev + cover.sill_height + cover.height < zt - 1e-4:
                        zr.append((elev + cover.sill_height + cover.height, zt))
                room_pos = g.room_at(scene, level.id, *f.to_world(mid, probe))
                room_neg = g.room_at(scene, level.id, *f.to_world(mid, -probe))
                bands = set() if w.material else {elev + b for r in (room_pos, room_neg) if r is not None
                                                  for b in styles.room_wall_bands(r)}
                for z0, z1 in zr:
                    # split at dado/frieze heights of either side so each piece has one finish per side
                    zs = [z0, *sorted(b for b in bands if z0 + 1e-4 < b < z1 - 1e-4), z1]
                    for za, zb_ in zip(zs, zs[1:]):
                        if w.material:
                            m_pos = m_neg = cap = use(w.material)
                        else:
                            hm = (za + zb_) / 2 - elev
                            m_pos = use(wall_mat_for(room_pos, hm, w.kind))
                            m_neg = use(wall_mat_for(room_neg, hm, w.kind))
                            cap = m_pos if room_pos is not None else m_neg
                        for pp in pieces:
                            prisms.append(make_prism(pp, za, zb_, cap, m_pos, m_neg))
            if prisms:
                objects.append(_arch_object(f"ARCH_Wall_{w.id}", "wall", prisms, level_id=level.id,
                                            axis=(f.nx, f.ny), wall_id=w.id, wall_kind=w.kind,
                                            length=_round(f.length), thickness=w.thickness, height=H))

        # ---- openings: frames, leaves, glass --------------------------------------------
        for o in [o for o in scene.openings if o.wall_id in {w.id for w in walls}]:
            w = scene.wall(o.wall_id)
            f = g.wall_frame(w)
            a, b = o.offset - o.width / 2, o.offset + o.width / 2
            z_sill, z_head = elev + o.sill_height, elev + o.sill_height + o.height
            t = w.thickness
            rect = lambda s0, s1, d0, d1: Polygon(f.rect(s0, s1, d0, d1))  # noqa: E731
            fw = min(FRAME_WIDTH, o.width / 4)
            center = f.to_world(o.offset, 0)
            info = {"id": o.id, "kind": o.kind, "wall_id": w.id, "level_id": level.id,
                    "center": [_round(center[0]), _round(center[1])], "width": o.width, "height": o.height,
                    "sill_height": o.sill_height, "wall_angle_deg": _round(f.angle_deg)}
            if o.kind == "door":
                fm, lm = use(open_mats["door_frame"]), use(open_mats["door"])
                cd = t / 2 + 0.01
                frame = [make_prism(rect(a, a + fw, -cd, cd), elev, z_head, fm),
                         make_prism(rect(b - fw, b, -cd, cd), elev, z_head, fm),
                         make_prism(rect(a + fw, b - fw, -cd, cd), z_head - fw, z_head, fm)]
                objects.append(_arch_object(f"ARCH_Door_{o.id}_Frame", "door_frame", frame, level_id=level.id,
                                            opening_id=o.id, wall_id=w.id))
                side = _door_side(scene, level.id, f, o, t)
                hinge = a + fw if o.hinge_side == "start" else b - fw
                inward = 1.0 if o.hinge_side == "start" else -1.0
                leaf_w = (b - a) - 2 * fw - 0.01
                s0, s1 = sorted((hinge, hinge + inward * DOOR_LEAF_THICKNESS))
                d0, d1 = sorted((side * t / 2, side * (t / 2 + leaf_w)))
                leaf = [make_prism(rect(s0, s1, d0, d1), elev + 0.01, z_head - fw - 0.005, lm)]
                objects.append(_arch_object(f"ARCH_Door_{o.id}_Leaf", "door_leaf", leaf, level_id=level.id,
                                            opening_id=o.id, wall_id=w.id, collision="complex"))
                info["opens_into"] = o.opens_into
                info["hinge_side"] = o.hinge_side
            elif o.kind == "window":
                fm, gm = use(open_mats["window_frame"]), use(open_mats["glass"])
                fd = min(WINDOW_FRAME_DEPTH, t) / 2
                frame = [make_prism(rect(a, a + fw, -fd, fd), z_sill, z_head, fm),
                         make_prism(rect(b - fw, b, -fd, fd), z_sill, z_head, fm),
                         make_prism(rect(a + fw, b - fw, -fd, fd), z_sill, z_sill + fw, fm),
                         make_prism(rect(a + fw, b - fw, -fd, fd), z_head - fw, z_head, fm)]
                objects.append(_arch_object(f"ARCH_Window_{o.id}_Frame", "window_frame", frame, level_id=level.id,
                                            opening_id=o.id, wall_id=w.id))
                glass = [make_prism(rect(a + fw, b - fw, -GLASS_THICKNESS / 2, GLASS_THICKNESS / 2),
                                    z_sill + fw, z_head - fw, gm)]
                objects.append(_arch_object(f"ARCH_Window_{o.id}_Glass", "glass", glass, level_id=level.id,
                                            opening_id=o.id, wall_id=w.id, collision="complex"))
            openings_out.append(info)

        # ---- slab, floors, ceilings ---------------------------------------------------------
        outline = unary_union(list(room_polys.values()) + list(fps.values()))
        slab_prisms = [make_prism(p, elev - level.slab_thickness, elev - FLOOR_FINISH, use("slab_concrete"))
                       for p in g.polygons_of(outline)]
        if slab_prisms:
            objects.append(_arch_object(f"ARCH_Slab_{level.id}", "slab", slab_prisms, level_id=level.id))
        net_by_room = {r.id: g.room_net_polygon(r, fps) for r in rooms}
        for r in rooms:
            poly = room_polys[r.id]
            fm = use(styles.room_floor_material(scene, r))
            top = elev + r.floor_offset
            floor = [make_prism(poly, min(elev, top) - FLOOR_FINISH, top, fm)]
            objects.append(_arch_object(f"ARCH_Floor_{r.id}", "floor", floor, level_id=level.id, room_id=r.id,
                                        room_name=r.name))
            ceil_h = elev + (r.ceiling_height or level.wall_height)
            if level.ceilings:
                cm = use(styles.room_ceiling_material(scene, r))
                objects.append(_arch_object(f"ARCH_Ceiling_{r.id}", "ceiling",
                                            [make_prism(poly, ceil_h, ceil_h + CEILING_THICKNESS, cm)],
                                            level_id=level.id, room_id=r.id, room_name=r.name))
            net = net_by_room[r.id]
            rep = (net if not net.is_empty else poly).representative_point()
            wc, dc = g.bbox_dims(poly)
            wn, dn = g.bbox_dims(net) if not net.is_empty else (0.0, 0.0)
            rooms_out.append({
                "id": r.id, "name": r.name, "type": r.type, "level_id": level.id,
                "polygon": [[_round(x), _round(y)] for x, y in r.polygon],
                "label_point": [_round(rep.x), _round(rep.y)],
                "area_centreline": _round(poly.area), "area_net": _round(net.area),
                "dims_centreline": [_round(wc), _round(dc)], "dims_clear": [_round(wn), _round(dn)],
                "floor_z": _round(top), "ceiling_z": _round(ceil_h), "floor_material": fm,
                "wall_material": styles.room_wall_material(scene, r), "status": r.certainty.status,
            })
            if scene.lighting.ceiling_lights and level.ceilings:
                area = poly.area
                lights.append({"name": f"LIGHT_{r.id}", "type": "area", "room_id": r.id,
                               "location": [_round(rep.x), _round(rep.y), _round(ceil_h - 0.03)],
                               "direction": [0.0, 0.0, -1.0], "size": _round(min(1.2, 0.3 * math.sqrt(area))),
                               "power_w": _round(35.0 * area), "lumens": _round(120.0 * area),
                               "color": LIGHT_COLORS[scene.lighting.mood]})
            cam = _room_camera(r.id, net if not net.is_empty else poly, top)
            if cam:
                cameras.append(cam)

    # ---- furniture -------------------------------------------------------------------------
    for item in scene.furniture:
        asset = catalog.get(item.asset_id)
        if asset is None:
            raise CompileError(f"furniture {item.id}: unknown asset '{item.asset_id}'")
        room = scene.room(item.room_id)
        level = scene.level(room.level_id)
        w, d, h = g.furniture_size(item, asset)
        scale = [_round(w / asset.size[0]), _round(d / asset.size[1]), _round(h / asset.size[2])]
        role_mats = {role: use(styles.role_material(scene, role, item.materials)) for role in asset.roles}
        variant = f"{asset.id}__{canonical_hash(role_mats)[:8]}"
        z = level.elevation + room.floor_offset + item.elevation
        fp = g.furniture_footprint(item, asset)
        minx, miny, maxx, maxy = fp.bounds
        loc = [_round(item.position[0]), _round(item.position[1]), _round(z)]
        furniture.append({
            "name": f"FURN_{room.id}_{item.id}_{asset.id}", "furniture_id": item.id, "room_id": room.id,
            "level_id": level.id, "asset_id": asset.id, "asset_name": item.label or asset.name,
            "category": asset.category, "location": loc, "rotation_deg": _round(item.rotation_deg % 360.0),
            "scale": scale, "size": [_round(w), _round(d), _round(h)], "nominal_size": list(asset.size),
            "materials": role_mats, "variant": variant, "collision": "box",
            "footprint_bounds": [[_round(minx), _round(miny)], [_round(maxx), _round(maxy)]],
            "status": item.certainty.status,
            "unreal": {"location_cm": [_round(v) for v in scene_to_unreal_location(*loc)],
                       "yaw_deg": _round(scene_to_unreal_yaw(item.rotation_deg)),
                       "scale": list(scene_to_unreal_scale(*scale))},
        })

    # ---- HVAC: units and ducts in the ceiling void, terminals in the ceiling plane -----------------
    objects.extend(compile_hvac(scene, use))

    # ---- free-floor spawn point per room (teleport targets, navigation checks) -------------------
    for room_out in rooms_out:
        room = scene.room(room_out["id"])
        level = scene.level(room.level_id)
        fps = g.wall_footprints(g.walls_on_level(scene, level.id))
        net = g.room_net_polygon(room, fps)
        obstacles = [g.furniture_footprint(f, catalog.get(f.asset_id)) for f in scene.furniture
                     if f.room_id == room.id and catalog.get(f.asset_id).placement != "floor_layer"]
        sp = free_floor_point(net, obstacles, room_out["label_point"])
        room_out["spawn_point"] = [_round(sp[0]), _round(sp[1])]

    # ---- sun + overview camera -------------------------------------------------------------
    lt = scene.lighting
    az, el = math.radians(lt.sun_azimuth_deg), math.radians(lt.sun_elevation_deg)
    to_sun = (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
    lights.append({"name": "LIGHT_Sun", "type": "sun", "direction": [_round(-c) for c in to_sun],
                   "strength": lt.sun_strength, "color": "#FFF4E5"})
    all_bounds = [o["expected_bounds"] for o in objects] or [[[0, 0, 0], [1, 1, 1]]]
    lo = [min(b[0][i] for b in all_bounds) for i in range(3)]
    hi = [max(b[1][i] for b in all_bounds) for i in range(3)]
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    span = max(hi[0] - lo[0], hi[1] - lo[1], 1.0)
    cameras.insert(0, {"name": "CAM_Overview", "kind": "overview",
                       "location": [_round(cx - 0.35 * span), _round(cy - 0.85 * span), _round(1.05 * span)],
                       "target": [_round(cx), _round(cy), 0.0], "lens_mm": 32.0})

    # ---- hashes & materials ----------------------------------------------------------------
    mat_defs = {m: mats[m].model_dump(mode="json") for m in sorted(used_mats)}
    for obj in objects:
        deps = {k: mat_defs[k] for k in sorted({p[key] for p in obj["prisms"] for key in ("mat", "mat_pos", "mat_neg") if key in p})}
        obj["hash"] = canonical_hash({"obj": {k: v for k, v in obj.items() if k != "hash"}, "mats": deps})
    for fu in furniture:
        deps = {k: mat_defs[k] for k in sorted(set(fu["materials"].values()))}
        fu["variant_hash"] = canonical_hash({"variant": fu["variant"], "mats": deps, "library": LIBRARY_VERSION})
        fu["hash"] = canonical_hash({"fu": {k: v for k, v in fu.items() if k != "hash"}})

    reviewed = [f"{kind}:{el.id}" for kind, coll in (("room", scene.rooms), ("wall", scene.walls),
                                                      ("opening", scene.openings), ("furniture", scene.furniture))
                for el in coll if el.certainty.status == "accepted_estimate"]
    # the sensor layer is kept apart from the building: Blender never builds it, twin.layers writes it on top
    from twin.scene.sensors import compile_sensors
    sensors, sensor_mats = compile_sensors(scene)
    reviewed += [f"sensor:{s.id}" for s in scene.sensors if s.certainty.status == "accepted_estimate"]
    return {
        "plan_version": PLAN_VERSION,
        "library_version": LIBRARY_VERSION,
        "catalog_version": catalog.catalog_version,
        "scene_hash": scene_hash(scene),
        "project": {"name": scene.project.name, "style": scene.project.style},
        "units": "m",
        "coordinate_system": scene.coordinate_system,
        "unreal_conversion": "UE(cm) = (100x, -100y, 100z); yaw_ue = -rotation_deg",
        "bounds": [[_round(v) for v in lo], [_round(v) for v in hi]],
        "materials": mat_defs,
        "objects": objects,
        "furniture": furniture,
        "lights": lights,
        "cameras": cameras,
        "rooms": rooms_out,
        "openings": openings_out,
        "sensors": sensors,
        "sensor_materials": sensor_mats,
        "accepted_estimates": reviewed,
    }


HVAC_FACE_DROP = 0.025      # diffuser/grille faces sit this far below the ceiling so they read in 3D
HVAC_FACE_THICKNESS = 0.03


def hvac_ceiling(scene: Scene, level, x: float, y: float) -> float:
    """Absolute ceiling height at a plan point (the room's ceiling, else the level's wall height)."""
    room = g.room_at(scene, level.id, x, y)
    return level.elevation + ((room.ceiling_height if room and room.ceiling_height else None) or level.wall_height)


def hvac_placement(scene: Scene) -> dict[str, dict]:
    """Vertical placement of every HVAC element (absolute z), shared by the compiler and the airflow model."""
    hv = scene.hvac
    out: dict[str, dict] = {}
    gap = hv.plenum_clearance
    for u in hv.units:
        level = scene.level(u.level_id)
        ceil = hvac_ceiling(scene, level, *u.position)
        h = u.size.height
        if u.elevation is not None:
            z0 = level.elevation + u.elevation
        elif u.kind == "fcu_ducted":
            z0 = ceil + gap
        elif u.kind == "split_wall":
            z0 = level.elevation + min(2.2, ceil - level.elevation - h - 0.05)
        else:  # exposed, cassette, ahu, others: hung just below the ceiling
            z0 = ceil - h
        out[u.id] = {"z0": z0, "z1": z0 + h, "ceiling": ceil}
    for t in hv.terminals:
        level = scene.level(t.level_id)
        ceil = hvac_ceiling(scene, level, *t.position)
        face = level.elevation + t.elevation if t.elevation is not None else ceil - HVAC_FACE_DROP
        out[t.id] = {"z0": face, "z1": face + HVAC_FACE_THICKNESS, "ceiling": ceil}
    for d in hv.ducts:
        level = scene.level(d.level_id)
        mid = d.path[len(d.path) // 2] if len(d.path) > 2 else ((d.path[0][0] + d.path[-1][0]) / 2, (d.path[0][1] + d.path[-1][1]) / 2)
        ceil = hvac_ceiling(scene, level, *mid)
        z0 = level.elevation + d.elevation if d.elevation is not None else ceil + gap
        out[d.id] = {"z0": z0, "z1": z0 + d.height, "ceiling": ceil}
    return out


def _oriented_rect(cx: float, cy: float, w: float, d: float, rot_deg: float) -> Polygon:
    a = math.radians(rot_deg)
    c, s_ = math.cos(a), math.sin(a)
    return Polygon([(cx + lx * c - ly * s_, cy + lx * s_ + ly * c)
                    for lx, ly in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))])


def duct_footprint(d) -> Polygon:
    from shapely.geometry import LineString
    if d.width_end is not None:
        (ax, ay), (bx, by) = d.path
        L = math.hypot(bx - ax, by - ay) or 1e-9
        nx, ny = -(by - ay) / L, (bx - ax) / L
        return Polygon([(ax + nx * d.width / 2, ay + ny * d.width / 2), (bx + nx * d.width_end / 2, by + ny * d.width_end / 2),
                        (bx - nx * d.width_end / 2, by - ny * d.width_end / 2), (ax - nx * d.width / 2, ay - ny * d.width / 2)])
    return LineString(d.path).buffer(d.width / 2, cap_style="flat", join_style="mitre", mitre_limit=3.0)


def compile_hvac(scene: Scene, use) -> list[dict]:
    """Prism objects for HVAC units, ducts and terminals (named HVAC_*)."""
    hv = scene.hvac
    if not (hv.units or hv.terminals or hv.ducts):
        return []
    z = hvac_placement(scene)
    units = {u.id: u for u in hv.units}
    out: list[dict] = []
    for u in hv.units:
        zz = z[u.id]
        body = _oriented_rect(u.position[0], u.position[1], u.size.width, u.size.depth, u.rotation_deg)
        prisms = [make_prism(body, zz["z0"], zz["z1"], use("hvac_unit"))]
        # access panel / control box on the underside so the unit's orientation reads in 3D
        a = math.radians(u.rotation_deg)
        off = u.size.width * 0.3
        box = _oriented_rect(u.position[0] - off * math.cos(a), u.position[1] - off * math.sin(a),
                             min(0.25, u.size.width * 0.25), min(0.2, u.size.depth * 0.4), u.rotation_deg)
        prisms.append(make_prism(box, zz["z0"] - 0.02, zz["z0"], use("hvac_unit_dark")))
        label = f"{u.tag}" + (f" · {u.airflow_lps:.0f} L/s" if u.airflow_lps else "")
        out.append(_arch_object(f"HVAC_Unit_{u.id}", "hvac_unit", prisms, level_id=u.level_id, collision="none",
                                room_id=u.room_id, hvac_id=u.id, label=label, tag=u.tag, unit_kind=u.kind,
                                airflow_lps=u.airflow_lps))
    for d in hv.ducts:
        zz = z[d.id]
        fp = duct_footprint(d)
        prisms = [make_prism(p, zz["z0"], zz["z1"], use("hvac_duct_return" if d.system == "return" else "hvac_duct_supply"))
                  for p in g.polygons_of(fp) if p.area > 1e-4]
        if not prisms:
            continue
        tag = units[d.unit_id].tag if d.unit_id in units else None
        label = f"{d.system.title()} duct {d.width * 1000:.0f}x{d.height * 1000:.0f}" + (f" · {tag}" if tag else "")
        out.append(_arch_object(f"HVAC_Duct_{d.id}", "hvac_duct", prisms, level_id=d.level_id, collision="none",
                                hvac_id=d.id, label=label, system=d.system, unit_tag=tag))
    for t in hv.terminals:
        zz = z[t.id]
        face = _oriented_rect(t.position[0], t.position[1], t.width, t.depth, t.rotation_deg)
        core = _oriented_rect(t.position[0], t.position[1], t.width * 0.55, t.depth * 0.55, t.rotation_deg)
        prisms = [make_prism(face, zz["z0"] + 0.006, zz["z1"], use("hvac_terminal")),
                  make_prism(core, zz["z0"], zz["z0"] + 0.006, use("hvac_terminal_core"))]
        # neck up into the void so the terminal visibly connects to the duct level
        neck = _oriented_rect(t.position[0], t.position[1], max(0.08, t.width * 0.5), max(0.06, t.depth * 0.5), t.rotation_deg)
        top = zz["ceiling"] + hv.plenum_clearance + 0.1
        if top > zz["z1"] + 0.02:
            prisms.append(make_prism(neck, zz["z1"], top, use("hvac_terminal_core")))
        kind = "hvac_diffuser" if t.kind.startswith("supply") or t.kind == "linear_slot" else "hvac_grille"
        tag = units[t.unit_id].tag if t.unit_id in units else None
        label = {"supply_diffuser": "Supply diffuser", "supply_grille": "Supply grille", "return_grille": "Return grille",
                 "exhaust_grille": "Exhaust grille", "linear_slot": "Linear slot diffuser"}[t.kind]
        label += (f" · {t.airflow_lps:.0f} L/s" if t.airflow_lps else "") + (f" · {tag}" if tag else "")
        out.append(_arch_object(f"HVAC_{'Diffuser' if kind == 'hvac_diffuser' else 'Grille'}_{t.id}", kind, prisms,
                                level_id=t.level_id, collision="none", room_id=t.room_id, hvac_id=t.id, label=label,
                                airflow_lps=t.airflow_lps, unit_tag=tag))
    return out


SPAWN_CLEARANCE = 0.35  # metres from walls and furniture (visitor capsule radius is 0.25)


def free_floor_point(net: Any, obstacles: list[Polygon], near: list[float]) -> tuple[float, float]:
    """A standing spot on open floor: in the largest furniture-free area, as close as possible to `near`."""
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    free = net.buffer(-SPAWN_CLEARANCE)
    if obstacles:
        free = free.difference(unary_union([o.buffer(SPAWN_CLEARANCE) for o in obstacles]))
    pieces = g.polygons_of(free)
    if not pieces:
        return (near[0], near[1])
    best = max(pieces, key=lambda p: p.area)
    target = Point(near)
    if best.contains(target):
        return (target.x, target.y)
    inner = best.buffer(-0.05)
    region = inner if not inner.is_empty else best
    p = nearest_points(region, target)[0]
    return (p.x, p.y)


def _door_side(scene: Scene, level_id: str, f: g.WallFrame, o: Opening, t: float) -> float:
    """+1 if the leaf opens to the wall's left (+normal), -1 for the right."""
    probe = t / 2 + 0.05
    left = g.room_at(scene, level_id, *f.to_world(o.offset, probe))
    right = g.room_at(scene, level_id, *f.to_world(o.offset, -probe))
    if o.opens_into:
        if left is not None and left.id == o.opens_into:
            return 1.0
        if right is not None and right.id == o.opens_into:
            return -1.0
    if left is None and right is not None:
        return -1.0
    return 1.0


def _room_camera(room_id: str, area: Any, floor_z: float) -> dict | None:
    """Eye-level camera in the south-west of the room looking toward the north-east."""
    minx, miny, maxx, maxy = area.bounds
    inset = min(0.45, (maxx - minx) / 4, (maxy - miny) / 4)
    from shapely.geometry import Point
    for fx, fy, tx, ty in ((minx + inset, miny + inset, maxx, maxy), (maxx - inset, miny + inset, minx, maxy),
                           (maxx - inset, maxy - inset, minx, miny), (minx + inset, maxy - inset, maxx, miny)):
        if area.contains(Point(fx, fy)):
            return {"name": f"CAM_{room_id}", "kind": "room", "room_id": room_id,
                    "location": [_round(fx), _round(fy), _round(floor_z + EYE_HEIGHT)],
                    "target": [_round(tx), _round(ty), _round(floor_z + 1.0)], "lens_mm": 16.0}
    rep = area.representative_point()
    return {"name": f"CAM_{room_id}", "kind": "room", "room_id": room_id,
            "location": [_round(rep.x), _round(rep.y), _round(floor_z + EYE_HEIGHT)],
            "target": [_round(rep.x + 1.0), _round(rep.y + 1.0), _round(floor_z + 1.0)], "lens_mm": 16.0}
