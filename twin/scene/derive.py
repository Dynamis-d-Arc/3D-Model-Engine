"""Deterministic layout helpers used during review/editing.

* ``derive_walls_from_rooms``: rebuild wall centrelines from room boundaries (shared edges
  become interior walls, outer edges exterior walls) and re-attach openings by position.
* ``fit_furniture_into_room``: nudge a furniture item so its footprint lies inside its room.
* ``fit_to_overall``: scale a whole layout to user-confirmed outer dimensions.
"""

from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import Point
from shapely.affinity import translate

from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.model import Certainty, Scene, Wall

VERTEX_TOL = 1e-3


def _key(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 4), round(p[1], 4))


def derive_walls_from_rooms(scene: Scene, exterior_thickness: float = 0.2,
                            interior_thickness: float = 0.1) -> tuple[Scene, list[str]]:
    """Return a copy of the scene with walls rebuilt from room polygons, plus notes for the user."""
    notes: list[str] = []
    new_walls: list[Wall] = []
    old_walls = {w.id: w for w in scene.walls}
    used_ids: set[str] = set()
    counter = max([int(w.id[1:]) for w in scene.walls if w.id[1:].isdigit() and w.id.startswith("W")] + [0])

    for level in scene.levels:
        rooms = g.rooms_on_level(scene, level.id)
        vertices = {_key(p) for r in rooms for p in r.polygon}
        segs: dict[tuple, list[str]] = defaultdict(list)
        for r in rooms:
            pts = [tuple(p) for p in r.polygon]
            for i, a in enumerate(pts):
                b = pts[(i + 1) % len(pts)]
                dx, dy = b[0] - a[0], b[1] - a[1]
                length = math.hypot(dx, dy)
                if length < VERTEX_TOL:
                    continue
                cuts = [0.0, 1.0]
                for v in vertices:
                    t = ((v[0] - a[0]) * dx + (v[1] - a[1]) * dy) / (length * length)
                    if 1e-6 < t < 1 - 1e-6:
                        px, py = a[0] + t * dx, a[1] + t * dy
                        if math.hypot(px - v[0], py - v[1]) < VERTEX_TOL:
                            cuts.append(t)
                cuts.sort()
                for t0, t1 in zip(cuts, cuts[1:]):
                    p0 = _key((a[0] + t0 * dx, a[1] + t0 * dy))
                    p1 = _key((a[0] + t1 * dx, a[1] + t1 * dy))
                    if p0 != p1:
                        segs[tuple(sorted((p0, p1)))].append(r.id)

        # group segments by supporting line, then merge contiguous runs of the same kind
        lines: dict[tuple, list[tuple[float, float, str]]] = defaultdict(list)
        for (p0, p1), owners in segs.items():
            ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0]) % math.pi
            if abs(ang - math.pi) < 1e-9:
                ang = 0.0
            ux, uy = math.cos(ang), math.sin(ang)
            offset = round(-p0[0] * uy + p0[1] * ux, 4)
            s0, s1 = sorted((p0[0] * ux + p0[1] * uy, p1[0] * ux + p1[1] * uy))
            if len(owners) > 2:
                notes.append(f"edge {p0}-{p1} is shared by {len(owners)} rooms; check for overlapping rooms")
            kind = "interior" if len(owners) >= 2 else "exterior"
            lines[(round(ang, 6), offset)].append((s0, s1, kind))
        for (ang, offset), parts in sorted(lines.items()):
            ux, uy = math.cos(ang), math.sin(ang)
            parts.sort()
            runs: list[list] = []
            for s0, s1, kind in parts:
                if runs and runs[-1][2] == kind and s0 <= runs[-1][1] + 1e-4:
                    runs[-1][1] = max(runs[-1][1], s1)
                else:
                    runs.append([s0, s1, kind])
            for s0, s1, kind in runs:
                start = (round(s0 * ux - offset * uy, 4), round(s0 * uy + offset * ux, 4))
                end = (round(s1 * ux - offset * uy, 4), round(s1 * uy + offset * ux, 4))
                thickness = exterior_thickness if kind == "exterior" else interior_thickness
                match = _best_old_match(start, end, [w for w in old_walls.values() if w.level_id == level.id])
                if match is not None and match.id not in used_ids:
                    wid, thickness, material, height = match.id, match.thickness, match.material, match.height
                    certainty = match.certainty
                    if math.dist(start, match.end) + math.dist(end, match.start) <                             math.dist(start, match.start) + math.dist(end, match.end):
                        start, end = end, start  # keep the original direction (offsets/hinges depend on it)
                else:
                    counter += 1
                    while f"W{counter}" in old_walls or f"W{counter}" in used_ids:
                        counter += 1
                    wid, material, height = f"W{counter}", None, None
                    certainty = Certainty(status="confirmed", source="derived",
                                          note="derived from room boundaries")
                used_ids.add(wid)
                new_walls.append(Wall(id=wid, level_id=level.id, start=start, end=end, thickness=thickness,
                                      height=height, kind=kind, material=material, certainty=certainty))

    # re-attach openings by world position
    new_openings = []
    for o in scene.openings:
        ow = old_walls.get(o.wall_id)
        if ow is None:
            continue
        of = g.wall_frame(ow)
        cx, cy = of.to_world(o.offset)
        best = None
        for w in new_walls:
            if w.level_id != ow.level_id:
                continue
            f = g.wall_frame(w)
            if abs(f.ux * of.uy - f.uy * of.ux) > math.sin(math.radians(1.0)):
                continue
            s, d = f.to_local(cx, cy)
            if abs(d) <= max(w.thickness, ow.thickness) / 2 + 0.02 and 0 <= s <= f.length:
                if best is None or abs(d) < best[2]:
                    best = (w, s, abs(d))
        if best is None:
            notes.append(f"{o.kind} {o.id} no longer lies on any wall and was removed")
            continue
        nf = g.wall_frame(best[0])
        update = {"wall_id": best[0].id, "offset": round(best[1], 4)}
        if nf.ux * of.ux + nf.uy * of.uy < 0:  # wall direction flipped: hinge side flips with it
            update["hinge_side"] = "end" if o.hinge_side == "start" else "start"
        new_openings.append(o.model_copy(update=update))

    out = scene.model_copy(update={"walls": new_walls, "openings": new_openings})
    return Scene.model_validate(out.model_dump()), notes


def _best_old_match(start, end, olds: list[Wall]) -> Wall | None:
    best, best_d = None, 1e9
    for w in olds:
        d = min(math.dist(start, w.start) + math.dist(end, w.end), math.dist(start, w.end) + math.dist(end, w.start))
        if d < best_d:
            best, best_d = w, d
    return best if best_d < 0.02 else None


def fit_furniture_into_room(scene: Scene, furniture_id: str, max_shift: float = 2.0) -> tuple[Scene, float]:
    """Translate one item the shortest found distance so its footprint fits its room's floor.

    Returns (new scene, shift distance). Raises ValueError if no fit within ``max_shift``.
    """
    item = next(f for f in scene.furniture if f.id == furniture_id)
    asset = load_catalog().get(item.asset_id)
    if asset is None:
        raise ValueError(f"unknown asset {item.asset_id}")
    room = scene.room(item.room_id)
    fps = g.wall_footprints(g.walls_on_level(scene, room.level_id))
    net = g.room_net_polygon(room, fps).buffer(0.005)
    fp = g.furniture_footprint(item, asset)
    if net.contains(fp):
        return scene, 0.0
    best = None
    for radius in [k * 0.01 for k in range(1, int(max_shift / 0.01) + 1)]:
        for step in range(0, 72):
            a = math.radians(step * 5)
            dx, dy = radius * math.cos(a), radius * math.sin(a)
            if net.contains(translate(fp, dx, dy)):
                best = (dx, dy)
                break
        if best:
            break
    if best is None:
        raise ValueError(f"{asset.name} ({item.id}) does not fit inside '{room.name}' within {max_shift} m")
    moved = item.model_copy(update={"position": (round(item.position[0] + best[0], 4),
                                                 round(item.position[1] + best[1], 4))})
    furniture = [moved if f.id == furniture_id else f for f in scene.furniture]
    return scene.model_copy(update={"furniture": furniture}), math.hypot(*best)


def fit_to_overall(scene: Scene, outer_width: float, outer_depth: float) -> Scene:
    """Scale all plan coordinates so the outer wall footprint matches the given dimensions.

    Wall thicknesses and object sizes are kept; positions scale about the footprint's
    south-west corner. Used when the AI layout is proportionally right but off in scale.
    """
    from shapely.ops import unary_union
    outer = unary_union([fp for lv in scene.levels for fp in g.wall_footprints(g.walls_on_level(scene, lv.id)).values()])
    if outer.is_empty:
        raise ValueError("scene has no walls to measure")
    minx, miny, maxx, maxy = outer.bounds
    # centreline extent = outer extent minus the exterior wall thickness on both sides
    ext_t = max((w.thickness for w in scene.walls if w.kind == "exterior"), default=0.0)
    cw, cd = (maxx - minx) - ext_t, (maxy - miny) - ext_t
    tw, td = outer_width - ext_t, outer_depth - ext_t
    if cw <= 0 or cd <= 0 or tw <= 0 or td <= 0:
        raise ValueError("dimensions too small")
    ox, oy = minx + ext_t / 2, miny + ext_t / 2
    sx, sy = tw / cw, td / cd

    def tf(p):
        return (round(ox + (p[0] - ox) * sx, 4), round(oy + (p[1] - oy) * sy, 4))

    walls = [w.model_copy(update={"start": tf(w.start), "end": tf(w.end)}) for w in scene.walls]
    rooms = [r.model_copy(update={"polygon": [tf(p) for p in r.polygon]}) for r in scene.rooms]
    old = {w.id: g.wall_frame(w) for w in scene.walls}
    new = {w.id: g.wall_frame(w) for w in walls}
    openings = [o.model_copy(update={"offset": round(o.offset * new[o.wall_id].length / old[o.wall_id].length, 4)})
                for o in scene.openings]
    furniture = [f.model_copy(update={"position": tf(f.position)}) for f in scene.furniture]
    out = scene.model_copy(update={"walls": walls, "rooms": rooms, "openings": openings, "furniture": furniture})
    return Scene.model_validate(out.model_dump())


def point_in_room(scene: Scene, room_id: str, x: float, y: float) -> bool:
    return g.room_polygon(scene.room(room_id)).contains(Point(x, y))
