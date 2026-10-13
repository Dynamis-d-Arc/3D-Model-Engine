"""Airflow simulation domain: the scene as a voxel grid the browser solver (frontend/js/airflow/solver.js)
can run in real time.

    build_domain(scene)                 whole level, ~70k cells (~0.4 m for a 1,300 m2 floor)
    build_domain(scene, room_id="R12")  one room in detail, ~110k cells (~0.1-0.2 m)

Grid: cells of `h` metres in plan, `dz` vertically, from the floor to the ceiling of the first level.
Index of cell (i, j, k) = i + nx * (j + ny * k); i grows east (+x), j north (+y), k up.

* solid     walls (rasterised along their centrelines so they are air-tight at any cell size), the outside
            of the building (or of the focus room); door and opening gaps are carved up to the door head;
            windows stay closed; doors of cold rooms stay closed.
* furniture solid obstacles (counters, cabinets, appliances, sofas ...; table tops only; chairs, shelving
            and other see-through items stay open), so the air flows around them.
* loads     internal sensible heat at full occupancy (W per plan column) by room type, plus lighting,
            distributed over the occupied zone (kitchens: mostly at cooking height).
* envelope  UA (W/K) of cells next to an exterior wall, for the outdoor temperature.
* fixed     cold rooms / freezers held at their set temperature.
* units     every HVAC unit with its supply outlets (design airflow split over its diffusers, normalised to
            the unit airflow so supply = return), return cells and, for units without diffusers, a
            discharge across the unit's width. A ceiling diffuser injects over its face cells and blows out
            along the ceiling in its throw directions (4-way, 2-way, 1-way) or straight down ("jets").
* relief    cells where air can leave the simulated region (doors of the focus room, exterior doors), so
            the pressure solve stays consistent when supply and return inside the region do not balance.
The same loads, aggregated per unit, drive the simulated BMS feed (twin.hvac.telemetry) so the demo data
and the simulation agree.
"""

from __future__ import annotations

import base64
import math
import re

import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.prepared import prep

from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.compile import hvac_placement
from twin.scene.model import Scene

DOMAIN_VERSION = 2                   # bump when the format changes: cached domains are rebuilt
TARGET_CELLS = 70_000                # keeps the browser solver real-time
TARGET_CELLS_ROOM = 80_000           # one room in detail
MIN_CELL_ROOM = 0.08
RHO_CP = 1206.0                      # J/(m3 K), air at ~25 C
LIGHTING_W_M2 = 8.0
ENVELOPE_U = 1.6                     # W/(m2 K), mixed wall + glazing
DEFAULT_SETPOINT = 24.0
# sensible internal load at full occupancy, W per m2 of floor, and the vertical profile
ROOM_LOADS = {
    "kitchen": (140.0, "cooking"), "dining": (35.0, "occupied"), "living": (25.0, "occupied"),
    "office": (20.0, "occupied"), "bedroom": (10.0, "occupied"), "utility": (30.0, "occupied"),
    "laundry": (40.0, "occupied"), "storage": (5.0, "occupied"), "hallway": (8.0, "occupied"),
    "entry": (8.0, "occupied"), "toilet": (6.0, "occupied"), "bathroom": (8.0, "occupied"),
    "balcony": (0.0, "occupied"), "other": (15.0, "occupied"),
}
COLD_ROOM_RE = re.compile(r"cold\s*room|chiller|freezer|cool\s*room", re.I)
# furniture the air passes through (open frames, chairs, plants) and items that only block at their top
POROUS_ASSETS = {"dining_chair", "office_chair", "bar_stool", "shelving_rack", "plant", "floor_lamp", "rug",
                 "mobile_cart"}
TOP_ONLY_CATEGORIES = {"table"}
FURNITURE_MIN_HEIGHT = 0.25


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def cold_room_temp(name: str, room_type: str = "storage") -> float | None:
    """Set temperature of a walk-in cold room / freezer (corridors and lobbies named after them excluded)."""
    if not COLD_ROOM_RE.search(name) or room_type in ("hallway", "entry") or \
            re.search(r"corridor|lobby|passage|aisle|service", name, re.I):
        return None
    return -18.0 if re.search(r"freez", name, re.I) else 3.0


def _supercover(x0: float, y0: float, x1: float, y1: float, gx0: float, gy0: float, h: float) -> set[tuple[int, int]]:
    """Every grid column a segment touches (4-connected), so a rasterised wall has no diagonal leaks."""
    out: set[tuple[int, int]] = set()
    L = math.hypot(x1 - x0, y1 - y0)
    n = max(2, int(L / (h * 0.25)) + 2)
    prev = None
    for s in range(n + 1):
        t = s / n
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        c = (int(math.floor((x - gx0) / h)), int(math.floor((y - gy0) / h)))
        if prev is not None and c[0] != prev[0] and c[1] != prev[1]:
            out.add((c[0], prev[1]))          # close the diagonal step
        out.add(c)
        prev = c
    return out


def build_domain(scene: Scene, cell: float | None = None, room_id: str | None = None) -> dict:
    focus = scene.room(room_id) if room_id else None
    level = scene.level(focus.level_id) if focus else scene.levels[0]
    rooms = [r for r in scene.rooms if r.level_id == level.id]
    if not rooms:
        raise ValueError("the scene has no rooms")
    walls = g.walls_on_level(scene, level.id)
    fps = g.wall_footprints(walls) if walls else {}
    building = unary_union([g.room_polygon(r) for r in rooms] + list(fps.values()))
    region = g.room_polygon(focus) if focus else building
    H = level.wall_height
    minx, miny, maxx, maxy = region.bounds
    area = (maxx - minx) * (maxy - miny)
    if focus:
        h = cell or max(MIN_CELL_ROOM, round((area * H / TARGET_CELLS_ROOM) ** (1 / 3), 2))
    else:
        h = cell or max(0.25, round((area * H / TARGET_CELLS) ** (1 / 3) * 20) / 20)
    nz = max(4, int(round(H / h)))
    dz = H / nz
    gx0, gy0 = minx - h, miny - h
    nx, ny = int(math.ceil((maxx - minx) / h)) + 2, int(math.ceil((maxy - miny) / h)) + 2
    N = nx * ny * nz

    xs = gx0 + (np.arange(nx) + 0.5) * h
    ys = gy0 + (np.arange(ny) + 0.5) * h
    inside = np.zeros((ny, nx), bool)
    room_idx = np.full((ny, nx), -1, np.int16)
    prep_b = prep(building.buffer(h * 0.3))
    prep_r = prep(region.buffer(h * 0.3)) if focus else prep_b
    room_preps = [(ri, prep(g.room_polygon(r))) for ri, r in enumerate(rooms)]
    for j in range(ny):
        for i in range(nx):
            p = Point(xs[i], ys[j])
            if prep_r.contains(p):
                inside[j, i] = True
                for ri, pr in room_preps:
                    if pr.contains(p):
                        room_idx[j, i] = ri
                        break
    # cells inside the building but in no room (wall cores, gaps between traced rooms): grow rooms into them
    for _ in range(12):
        gap = inside & (room_idx < 0)
        if not gap.any():
            break
        grown = room_idx.copy()
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(np.roll(room_idx, dj, axis=0), di, axis=1)
            take = gap & (grown < 0) & (shifted >= 0)
            grown[take] = shifted[take]
        room_idx = grown
    if focus:   # the sliver beyond an open edge belongs to the simulated room, not to its neighbour
        room_idx[inside] = next(ri for ri, r in enumerate(rooms) if r.id == focus.id)
    solid = np.zeros((nz, ny, nx), np.uint8)
    solid[:, ~inside] = 1

    cold = {ri: cold_room_temp(r.name, r.type) for ri, r in enumerate(rooms)}
    cold_ids = {rooms[ri].id for ri, t in cold.items() if t is not None}
    wall_cols: dict[str, set[tuple[int, int]]] = {}
    for w in walls:
        cols = _supercover(w.start[0], w.start[1], w.end[0], w.end[1], gx0, gy0, h)
        wall_cols[w.id] = cols
        for i, j in cols:
            if 0 <= i < nx and 0 <= j < ny:
                solid[:, j, i] = 1
    # carve doors / openings; doors on the boundary of the simulated region are relief openings
    relief: list[list[int]] = []
    carved: set[tuple[int, int]] = set()
    for o in scene.openings:
        if o.kind == "window" or o.wall_id not in wall_cols:
            continue
        w = scene.wall(o.wall_id)
        f = g.wall_frame(w)
        probe = w.thickness / 2 + h * 0.6
        sides = [g.room_at(scene, level.id, *f.to_world(o.offset, d)) for d in (probe, -probe)]
        if any(r is not None and r.id in cold_ids for r in sides):
            continue  # cold-room doors stay shut
        boundary = any(r is not None and r.id == focus.id for r in sides) if focus else w.kind == "exterior"
        margin = min(0.05, o.width * 0.1)
        a = f.to_world(o.offset - o.width / 2 + margin)
        b = f.to_world(o.offset + o.width / 2 - margin)
        head = o.sill_height + o.height
        k_top = min(nz, int(math.floor(head / dz)))
        k_bot = int(math.floor(o.sill_height / dz))
        for i, j in _supercover(a[0], a[1], b[0], b[1], gx0, gy0, h):
            if 0 <= i < nx and 0 <= j < ny and inside[j, i]:
                solid[k_bot:max(k_bot + 1, k_top), j, i] = 0
                carved.add((i, j))
                if boundary:
                    relief.extend([i, j, k] for k in range(k_bot, max(k_bot + 1, k_top)))

    # open edges of the region (an open-plan room boundary without a wall) are relief openings too
    out = ~inside
    edge = np.zeros_like(inside)
    edge[1:, :] |= out[:-1, :]
    edge[:-1, :] |= out[1:, :]
    edge[:, 1:] |= out[:, :-1]
    edge[:, :-1] |= out[:, 1:]
    for j, i in zip(*np.nonzero(edge & inside & (solid[0] == 0))):
        relief.extend([int(i), int(j), k] for k in range(nz) if not solid[k, j, i])

    open2d = solid[0] == 0
    # ---- loads ----------------------------------------------------------------------------------
    col_area = h * h
    loads2d = np.zeros((ny, nx), np.float32)
    profile2d = np.zeros((ny, nx), np.uint8)       # 0 occupied zone, 1 cooking
    fixed = np.full((ny, nx), np.nan, np.float32)
    for ri, r in enumerate(rooms):
        m = (room_idx == ri) & open2d
        dens, prof = ROOM_LOADS.get(r.type, (15.0, "occupied"))
        if cold[ri] is not None:
            fixed[m] = cold[ri]
            continue
        loads2d[m] = dens * col_area
        profile2d[m] = 1 if prof == "cooking" else 0
    # the exterior wall itself is a rasterised (solid) column; the room cell behind it gets the gain
    ext_cols = set()
    for w in walls:
        if w.kind != "exterior":
            continue
        for i, j in wall_cols[w.id]:
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny and open2d[jj, ii] and np.isnan(fixed[jj, ii]):
                    ext_cols.add((ii, jj))
    env = [[i, j, round(ENVELOPE_U * h * H, 4)] for i, j in sorted(ext_cols)]

    # ---- furniture obstacles (after the loads: the heat of a column goes to its remaining air cells) ----
    catalog = load_catalog()
    furniture_cells = 0
    for fi in scene.furniture:
        asset = catalog.get(fi.asset_id)
        if asset is None or asset.placement == "floor_layer" or asset.id in POROUS_ASSETS:
            continue
        _, _, hgt = g.furniture_size(fi, asset)
        if hgt < FURNITURE_MIN_HEIGHT and fi.elevation < 0.05:
            continue
        z0, z1 = fi.elevation, min(H - 0.05, fi.elevation + hgt)
        if asset.category in TOP_ONLY_CATEGORIES:
            z0 = max(z0, z1 - 0.06)
        ks = [k for k in range(nz) if min(z1, (k + 1) * dz) - max(z0, k * dz) >= 0.45 * dz]
        if not ks:
            ks = [min(nz - 1, int(((z0 + z1) / 2) // dz))]
        fp = g.furniture_footprint(fi, asset)
        bx0, by0, bx1, by1 = fp.bounds
        i0, i1 = max(0, int((bx0 - gx0) // h)), min(nx - 1, int((bx1 - gx0) // h))
        j0, j1 = max(0, int((by0 - gy0) // h)), min(ny - 1, int((by1 - gy0) // h))
        pf = prep(fp)
        cols = [(i, j) for j in range(j0, j1 + 1) for i in range(i0, i1 + 1) if pf.contains(Point(xs[i], ys[j]))]
        if not cols and fp.area >= 0.4 * h * h:
            ci, cj = int((fi.position[0] - gx0) // h), int((fi.position[1] - gy0) // h)
            if 0 <= ci < nx and 0 <= cj < ny:
                cols = [(ci, cj)]
        for i, j in cols:
            if not open2d[j, i] or (i, j) in carved:
                continue
            for k in ks:
                if not solid[k, j, i]:
                    solid[k, j, i] = 1
                    furniture_cells += 1

    # ---- HVAC ------------------------------------------------------------------------------------
    hv = scene.hvac
    zpl = hvac_placement(scene) if (hv.units or hv.terminals) else {}

    def in_region(x: float, y: float) -> bool:
        return prep_r.contains(Point(x, y))

    def top_k(i: int, j: int, k: int | None = None) -> int:
        k = nz - 1 if k is None else max(0, min(nz - 1, k))
        while k > 0 and solid[k, j, i]:
            k -= 1
        return k

    def cell_of(x: float, y: float, top: bool = True, z: float | None = None) -> list[int] | None:
        i, j = int(math.floor((x - gx0) / h)), int(math.floor((y - gy0) / h))
        best = None
        for r in range(0, 4):
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny and open2d[jj, ii]:
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, ii, jj)
            if best:
                break
        if best is None:
            return None
        k = top_k(best[1], best[2], None if top else int(math.floor((z or 0.0) / dz)))
        return [best[1], best[2], k]

    def face_cells(t, centre: list[int], outward: bool) -> tuple[list[list[int]], list[list[float]]]:
        """Top-layer cells under a ceiling terminal's face and, for supplies, the jet cells around the face
        with their discharge direction (along the ceiling, slightly down)."""
        a = math.radians(t.rotation_deg)
        ex, ey = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))
        hw, hd = t.width / 2, t.depth / 2
        reach = max(hw, hd) + h * 1.01
        i0, i1 = max(0, int((t.position[0] - reach - gx0) // h)), min(nx - 1, int((t.position[0] + reach - gx0) // h))
        j0, j1 = max(0, int((t.position[1] - reach - gy0) // h)), min(ny - 1, int((t.position[1] + reach - gy0) // h))
        face: list[list[int]] = []
        jets: list[list[float]] = []
        throw = t.throw if t.throw != "none" else "down"
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                if not open2d[j, i]:
                    continue
                dx, dy = xs[i] - t.position[0], ys[j] - t.position[1]
                lx, ly = dx * ex[0] + dy * ex[1], dx * ey[0] + dy * ey[1]
                k = top_k(i, j)
                if solid[k, j, i]:
                    continue
                if abs(lx) <= hw and abs(ly) <= hd:
                    face.append([i, j, k])
                elif outward and throw != "down" and abs(lx) <= hw + h and abs(ly) <= hd + h:
                    if abs(lx) - hw >= abs(ly) - hd:
                        if throw == "1way" and lx < 0:
                            continue
                        v = (math.copysign(1, lx) * ex[0], math.copysign(1, lx) * ex[1])
                    else:
                        if throw in ("2way", "1way"):
                            continue
                        v = (math.copysign(1, ly) * ey[0], math.copysign(1, ly) * ey[1])
                    jets.append([i, j, k, round(v[0], 3), round(v[1], 3), -0.12])
        if not face:
            face = [centre]
        if outward and not jets:   # "down", or nothing open around the face: blow straight down
            jets = [[c[0], c[1], c[2], 0.0, 0.0, -1.0] for c in face]
        return face, jets

    def grow(cells: list[list[int]], q_lps: float, v_face: float = 1.0) -> list[list[int]]:
        """Widen a return's face over neighbouring top-layer cells until the face velocity is <= v_face
        (a fine grid would otherwise suck a whole unit's airflow through one 10 cm cell)."""
        have = {(c[0], c[1]) for c in cells}
        ring = list(have)
        for _ in range(12):
            if q_lps / 1000 / (len(cells) * h * h) <= v_face or not ring:
                break
            nxt = []
            for i, j in ring:
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ii, jj = i + di, j + dj
                    if (ii, jj) in have or not (0 <= ii < nx and 0 <= jj < ny) or not open2d[jj, ii]:
                        continue
                    k = top_k(ii, jj)
                    if solid[k, jj, ii]:
                        continue
                    have.add((ii, jj))
                    nxt.append((ii, jj))
                    cells.append([ii, jj, k])
            ring = nxt
        return cells

    units_out = []
    for u in hv.units:
        sup = [t for t in hv.terminals if t.unit_id == u.id and t.kind in ("supply_diffuser", "supply_grille", "linear_slot")]
        ret = [t for t in hv.terminals if t.unit_id == u.id and t.kind in ("return_grille", "exhaust_grille")]
        flow = u.airflow_lps or sum(t.airflow_lps or 0 for t in sup) or 300.0
        design = [t.airflow_lps or flow / max(1, len(sup)) for t in sup]
        scale = flow / sum(design) if design and sum(design) > 0 else 1.0
        supplies = []
        for t, q in zip(sup, design):
            if not in_region(*t.position):
                continue
            c = cell_of(*t.position)
            if c:
                cells, jets = face_cells(t, c, True)
                supplies.append({"id": t.id, "cell": c, "q_lps": round(q * scale, 2), "throw": t.throw,
                                 "face_m2": round(t.width * t.depth, 4), "cells": cells, "jets": jets})
        returns = []
        for t in ret:
            if not in_region(*t.position):
                continue
            c = cell_of(*t.position)
            if c:
                cells, _ = face_cells(t, c, False)
                returns.append({"id": t.id, "cell": c, "q_lps": round(flow / len(ret), 2),
                                "cells": grow(cells, flow / len(ret))})
        ucell = None
        if in_region(*u.position):
            ucell = cell_of(*u.position, top=u.kind == "fcu_ducted",
                            z=(zpl[u.id]["z0"] - level.elevation) if u.id in zpl else None)
        if not ret and ucell:   # no return grille: the unit draws from the ceiling plenum around itself
            cells = [[ucell[0], ucell[1], top_k(ucell[0], ucell[1])]]
            returns.append({"id": f"{u.id}:return", "cell": [ucell[0], ucell[1], nz - 1], "q_lps": round(flow, 2),
                            "cells": grow(cells, flow, 0.6)})
        discharge = None
        if not sup and ucell:
            # exposed / wall unit: blows out of its long side into its own room (away from the wall)
            a = math.radians(u.rotation_deg)
            home = g.room_polygon(scene.room(u.room_id)) if u.room_id else None
            best = None
            for sgn in (1, -1):
                nxv, nyv = -math.sin(a) * sgn, math.cos(a) * sgn
                c = cell_of(u.position[0] + nxv * 1.0, u.position[1] + nyv * 1.0, top=False, z=ucell[2] * dz)
                if not c:
                    continue
                pts = [Point(u.position[0] + nxv * d_, u.position[1] + nyv * d_) for d_ in (0.6, 1.2, 1.8)]
                score = sum(1 for p_ in pts if home is not None and home.contains(p_)) * 10
                score += sum(1 for p_ in pts if prep_b.contains(p_))
                if best is None or score > best[0]:
                    best = (score, [round(nxv, 3), round(nyv, 3), -0.35], c)
            if best:
                dvec, c = best[1], best[2]
                off = u.size.depth / 2 + h * 0.6
                cells: list[list[int]] = []
                n_along = max(1, int(u.size.width / h))
                for s_ in range(n_along + 1):   # the outlet spans the unit's width
                    along = -u.size.width / 2 + u.size.width * s_ / n_along
                    cc = cell_of(u.position[0] + math.cos(a) * along + dvec[0] * off,
                                 u.position[1] + math.sin(a) * along + dvec[1] * off, top=False, z=ucell[2] * dz)
                    if cc and cc not in cells:
                        cells.append(cc)
                cells = cells or [c]
                discharge = {"cell": c, "dir": dvec, "q_lps": round(flow, 2), "cells": cells,
                             "jets": [[cc[0], cc[1], cc[2], *dvec] for cc in cells]}
        if not supplies and not discharge and not returns:
            continue   # the unit serves nothing inside the simulated region
        units_out.append({
            "id": u.id, "tag": u.tag, "kind": u.kind, "airflow_lps": round(flow, 2),
            "room_id": u.room_id, "cell": ucell, "supplies": supplies, "returns": returns, "discharge": discharge,
            "setpoint_c": 22.0 if u.room_id and scene.room(u.room_id).type == "kitchen" else DEFAULT_SETPOINT,
        })

    # every open column belongs to the outlet that reaches it best: distance scaled by the outlet's airflow
    # (a 600 L/s diffuser serves a larger area than a 40 L/s one), same room preferred
    owner = np.full((ny, nx), -1, np.int16)
    outlets = []
    for ui, u in enumerate(units_out):
        for s_ in u["supplies"]:
            outlets.append((ui, s_["cell"][0], s_["cell"][1], s_["q_lps"]))
        if u["discharge"]:
            outlets.append((ui, u["discharge"]["cell"][0], u["discharge"]["cell"][1], u["discharge"]["q_lps"]))
    if outlets:
        oi = np.array([o[1] for o in outlets], float)
        oj = np.array([o[2] for o in outlets], float)
        ou = np.array([o[0] for o in outlets])
        reach = np.sqrt(np.maximum(np.array([o[3] for o in outlets], float), 10.0) / 150.0) / h
        o_room = np.array([room_idx[int(j), int(i)] for i, j in zip(oi, oj)])
        jj, ii = np.nonzero(open2d & np.isnan(fixed))
        for i, j in zip(ii, jj):
            d = np.sqrt((oi - i) ** 2 + (oj - j) ** 2) / reach
            d = d + np.where(o_room == room_idx[j, i], 0.0, 10.0)
            owner[j, i] = ou[int(np.argmin(d))]
    light = LIGHTING_W_M2 * col_area
    for ui, u in enumerate(units_out):
        m = owner == ui
        u["internal_w"] = round(float(loads2d[m].sum() + light * m.sum()), 1)
        u["envelope_ua"] = round(sum(e[2] for e in env if owner[e[1], e[0]] == ui), 2)
        u["served_m2"] = round(float(m.sum() * col_area), 1)

    return {
        "domain_version": DOMAIN_VERSION,
        "level_id": level.id,
        "focus": {"room_id": focus.id, "name": focus.name} if focus else None,
        "grid": {"x0": gx0, "y0": gy0, "z0": level.elevation, "h": h, "dz": dz, "nx": nx, "ny": ny, "nz": nz},
        "solid": _b64(solid.reshape(-1)),
        "room_index": _b64(room_idx.reshape(-1)),
        "owner": _b64(owner.reshape(-1)),
        "rooms": [{"id": r.id, "name": r.name, "type": r.type, "fixed_c": cold[ri]} for ri, r in enumerate(rooms)],
        "loads_w": _b64(loads2d.reshape(-1)),
        "load_profile": _b64(profile2d.reshape(-1)),
        "fixed_c": _b64(fixed.reshape(-1)),
        "envelope": env,
        "relief": relief,
        "units": units_out,
        "constants": {"rho_cp": RHO_CP, "lighting_w_m2": LIGHTING_W_M2, "envelope_u": ENVELOPE_U},
        "stats": {"cells": N, "open_cells": int((solid == 0).sum()), "cell_m": h, "layer_m": round(dz, 4),
                  "units": len(units_out), "supplies": sum(len(u["supplies"]) for u in units_out),
                  "furniture_cells": furniture_cells, "relief_cells": len(relief),
                  "cold_rooms": sorted(cold_ids)},
    }


def focus_rooms(scene: Scene) -> list[dict]:
    """Rooms worth a detailed simulation: those with a supply outlet or an exposed unit in them."""
    level = scene.levels[0]
    sup = [t for t in scene.hvac.terminals if t.kind in ("supply_diffuser", "supply_grille", "linear_slot")]
    out = []
    for r in g.rooms_on_level(scene, level.id):
        poly = g.room_polygon(r)
        pp = prep(poly)
        n = sum(1 for t in sup if pp.contains(Point(*t.position)))
        n += sum(1 for u in scene.hvac.units if u.kind != "fcu_ducted" and pp.contains(Point(*u.position)))
        if n:
            out.append({"id": r.id, "name": r.name, "type": r.type, "outlets": n, "area_m2": round(poly.area, 1)})
    return sorted(out, key=lambda x: (-x["outlets"], x["name"]))
