"""Geometric validation of a scene. Produces a report; never mutates the scene.

Severities:
    error    the scene cannot be generated correctly (blocks generation)
    review   a human must confirm or accept something first (blocks generation)
    warning  probably wrong, but geometry can still be generated
    info     worth knowing
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Literal

from pydantic import BaseModel, Field
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.model import NEEDS_REVIEW_STATUSES, Brief, Scene

Severity = Literal["error", "review", "warning", "info"]
BLOCKING: frozenset[str] = frozenset({"error", "review"})

OPENING_JUNCTION_CLEARANCE = 0.05   # min gap between an opening and a joining wall face
OPENING_HEAD_CLEARANCE = 0.05       # min wall above an opening
DIM_TOLERANCE = 0.05                # metres, for brief cross-checks


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    targets: list[str] = Field(default_factory=list)


class RoomSummary(BaseModel):
    id: str
    name: str
    type: str
    level_id: str
    area_centreline: float
    area_net: float
    width_centreline: float
    depth_centreline: float
    width_clear: float
    depth_clear: float
    ceiling_height: float


class ValidationReport(BaseModel):
    ok: bool                     # no errors
    can_generate: bool           # no errors and nothing awaiting review
    issues: list[Issue]
    rooms: list[RoomSummary]
    counts: dict[str, int]

    def by_code(self, code: str) -> list[Issue]:
        return [i for i in self.issues if i.code == code]


def _r(x: float) -> float:
    return round(x, 3)


def validate_scene(scene: Scene, brief: Brief | None = None) -> ValidationReport:
    issues: list[Issue] = []

    def add(code: str, severity: Severity, message: str, *targets: str) -> None:
        issues.append(Issue(code=code, severity=severity, message=message, targets=list(targets)))

    catalog = load_catalog()
    summaries: list[RoomSummary] = []

    for level in scene.levels:
        rooms = g.rooms_on_level(scene, level.id)
        walls = g.walls_on_level(scene, level.id)
        footprints = g.wall_footprints(walls) if walls else {}
        room_polys: dict[str, Polygon] = {}

        # ---- rooms ---------------------------------------------------------------
        for r in rooms:
            poly = g.room_polygon(r)
            if not poly.is_valid:
                add("E_ROOM_POLYGON_INVALID", "error",
                    f"Room '{r.name}' boundary crosses itself or repeats a point.", f"room:{r.id}")
                continue
            if poly.area < 0.5:
                add("E_ROOM_TOO_SMALL", "error", f"Room '{r.name}' has an area of {poly.area:.2f} m² (< 0.5 m²).",
                    f"room:{r.id}")
            room_polys[r.id] = poly
        ids = sorted(room_polys)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ov = room_polys[a].intersection(room_polys[b]).area
                if ov > 0.01:
                    add("E_ROOM_OVERLAP", "error",
                        f"Rooms '{scene.room(a).name}' and '{scene.room(b).name}' overlap by {ov:.2f} m².",
                        f"room:{a}", f"room:{b}")

        # ---- walls ---------------------------------------------------------------
        lines = {w.id: g.wall_line(w) for w in walls}
        frames = {w.id: g.wall_frame(w) for w in walls}
        for i, wa in enumerate(walls):
            for wb in walls[i + 1:]:
                fa, fb = frames[wa.id], frames[wb.id]
                cross = abs(fa.ux * fb.uy - fa.uy * fb.ux)
                if cross < math.sin(math.radians(0.5)):
                    # parallel: overlapping if on the same infinite line and their spans overlap
                    s0, d0 = fa.to_local(*wb.start)
                    s1, d1 = fa.to_local(*wb.end)
                    if abs(d0) < g.JOIN_TOL and abs(d1) < g.JOIN_TOL:
                        lo, hi = max(0.0, min(s0, s1)), min(fa.length, max(s0, s1))
                        if hi - lo > 0.02:
                            add("E_WALL_OVERLAP", "error",
                                f"Walls {wa.id} and {wb.id} overlap along {hi - lo:.2f} m.",
                                f"wall:{wa.id}", f"wall:{wb.id}")
                    continue
                inter = lines[wa.id].intersection(lines[wb.id])
                if inter.is_empty or not isinstance(inter, Point):
                    continue
                ends = [Point(p) for p in (wa.start, wa.end, wb.start, wb.end)]
                if all(inter.distance(e) > g.JOIN_TOL for e in ends):
                    sa, _ = fa.to_local(inter.x, inter.y)
                    sb, _ = fb.to_local(inter.x, inter.y)
                    arms = (sa, fa.length - sa, sb, fb.length - sb)
                    if min(arms) < 0.3:
                        add("W_WALL_OVERSHOOT", "warning",
                            f"Walls {wa.id} and {wb.id} cross at ({inter.x:.2f}, {inter.y:.2f}) with a stub of "
                            f"{min(arms):.2f} m; trim the overshooting wall.", f"wall:{wa.id}", f"wall:{wb.id}")
                    else:
                        add("I_WALL_CROSSING", "info",
                            f"Walls {wa.id} and {wb.id} cross at ({inter.x:.2f}, {inter.y:.2f}) (4-way junction).",
                            f"wall:{wa.id}", f"wall:{wb.id}")
        for w in walls:
            for label, pt in (("start", w.start), ("end", w.end)):
                if not any(o.id != w.id and lines[o.id].distance(Point(pt)) <= g.JOIN_TOL for o in walls):
                    add("I_WALL_FREE_END", "info", f"Wall {w.id} {label} is not connected to another wall.",
                        f"wall:{w.id}")
            if g.wall_frame(w).length < 0.3:
                add("W_WALL_SHORT", "warning", f"Wall {w.id} is only {g.wall_frame(w).length:.2f} m long.",
                    f"wall:{w.id}")

        # room boundaries should be backed by walls (unless deliberately open-plan)
        wall_cover = unary_union([ln.buffer(0.02) for ln in lines.values()]) if lines else None
        for r in rooms:
            poly = room_polys.get(r.id)
            if poly is None:
                continue
            boundary = poly.exterior
            uncovered = boundary.length if wall_cover is None else boundary.difference(wall_cover).length
            if uncovered > 0.3 and r.type != "balcony":
                add("W_ROOM_EDGE_NO_WALL", "warning",
                    f"{uncovered:.2f} m of the '{r.name}' boundary has no wall (open-plan or missing wall?).",
                    f"room:{r.id}")

        # ---- openings ------------------------------------------------------------
        by_wall: dict[str, list] = defaultdict(list)
        for o in scene.openings:
            if o.wall_id in frames:
                by_wall[o.wall_id].append(o)
        for w in walls:
            f = frames[w.id]
            h = scene.wall_height(w)
            blocked = junction_zones(w, walls)
            ops = sorted(by_wall.get(w.id, []), key=lambda o: o.offset)
            for o in ops:
                a, b = o.offset - o.width / 2, o.offset + o.width / 2
                if a < -1e-6 or b > f.length + 1e-6:
                    add("E_OPENING_OUTSIDE_WALL", "error",
                        f"{o.kind.title()} {o.id} spans {a:.2f}–{b:.2f} m but wall {w.id} is {f.length:.2f} m long.",
                        f"opening:{o.id}", f"wall:{w.id}")
                    continue
                for lo, hi, other in blocked:
                    if a < hi and b > lo:
                        add("E_OPENING_AT_JUNCTION", "error",
                            f"{o.kind.title()} {o.id} cuts into the junction with wall {other}; move it at least "
                            f"{OPENING_JUNCTION_CLEARANCE * 100:.0f} cm clear of the joining wall.",
                            f"opening:{o.id}", f"wall:{w.id}")
                        break
                if o.sill_height + o.height > h - OPENING_HEAD_CLEARANCE + 1e-6:
                    add("E_OPENING_TOO_TALL", "error",
                        f"{o.kind.title()} {o.id} reaches {o.sill_height + o.height:.2f} m but wall {w.id} is {h:.2f} m high.",
                        f"opening:{o.id}")
                if o.kind == "window" and o.sill_height < 0.1:
                    add("I_WINDOW_FLOOR_LEVEL", "info", f"Window {o.id} starts at floor level (full-height glazing).",
                        f"opening:{o.id}")
                if o.kind == "door" and o.opens_into:
                    sides = _side_rooms(scene, level.id, f, o.offset, w.thickness)
                    if o.opens_into not in [rm.id for rm in sides if rm is not None]:
                        add("W_DOOR_OPENS_INTO", "warning",
                            f"Door {o.id} is set to open into '{scene.room(o.opens_into).name}', "
                            f"which is not on either side of wall {w.id}.", f"opening:{o.id}")
            for o1, o2 in zip(ops, ops[1:]):
                if o1.offset + o1.width / 2 + 0.05 > o2.offset - o2.width / 2:
                    add("E_OPENING_OVERLAP", "error",
                        f"Openings {o1.id} and {o2.id} on wall {w.id} overlap or are less than 5 cm apart.",
                        f"opening:{o1.id}", f"opening:{o2.id}")

        # ---- connectivity --------------------------------------------------------
        if rooms:
            graph: dict[str, set[str]] = defaultdict(set)
            entrances = 0
            for o in scene.openings:
                if o.kind == "window" or o.wall_id not in frames:
                    continue
                w = scene.wall(o.wall_id)
                a_room, b_room = _side_rooms(scene, level.id, frames[w.id], o.offset, w.thickness)
                na = a_room.id if a_room else "__exterior__"
                nb = b_room.id if b_room else "__exterior__"
                if na == nb:
                    continue
                graph[na].add(nb)
                graph[nb].add(na)
                if "__exterior__" in (na, nb):
                    entrances += 1
            for i, a in enumerate(ids):  # open-plan connections: shared boundary without a wall
                for b in ids[i + 1:]:
                    shared = room_polys[a].boundary.intersection(room_polys[b].boundary)
                    if shared.length < 0.5:
                        continue
                    open_len = shared.length if wall_cover is None else shared.difference(wall_cover).length
                    if open_len > 0.5:
                        graph[a].add(b)
                        graph[b].add(a)
            if entrances == 0:
                add("W_NO_ENTRANCE", "warning", f"No door connects level '{level.name}' to the outside.")
                start = ids[0] if ids else None
            else:
                start = "__exterior__"
            if start is not None:
                seen = {start}
                q = deque([start])
                while q:
                    n = q.popleft()
                    for m in graph[n]:
                        if m not in seen:
                            seen.add(m)
                            q.append(m)
                for rid in ids:
                    if rid not in seen:
                        add("W_ROOM_UNREACHABLE", "warning",
                            f"Room '{scene.room(rid).name}' cannot be reached through any door or opening.",
                            f"room:{rid}")

        # ---- furniture -----------------------------------------------------------
        nets = {r.id: g.room_net_polygon(r, footprints) for r in rooms if r.id in room_polys}
        level_furniture = [fi for fi in scene.furniture if fi.room_id in room_polys]
        fps: dict[str, Polygon] = {}
        for fi in level_furniture:
            asset = catalog.get(fi.asset_id)
            if asset is None:
                add("E_UNKNOWN_ASSET", "error", f"Furniture {fi.id} uses unknown asset '{fi.asset_id}'.",
                    f"furniture:{fi.id}")
                continue
            fp = g.furniture_footprint(fi, asset)
            fps[fi.id] = fp
            room = scene.room(fi.room_id)
            label = fi.label or asset.name
            if not room_polys[room.id].contains(Point(fi.position)):
                add("E_FURNITURE_OUTSIDE_ROOM", "error",
                    f"{label} ({fi.id}) is placed outside its room '{room.name}'.", f"furniture:{fi.id}",
                    f"room:{room.id}")
            else:
                outside = fp.difference(nets[room.id].buffer(0.01)).area
                if outside > 0.002:
                    add("W_FURNITURE_CROSSES_BOUNDARY", "warning",
                        f"{label} ({fi.id}) extends {outside:.2f} m² into a wall or outside '{room.name}'.",
                        f"furniture:{fi.id}")
            w, d, hgt = g.furniture_size(fi, asset)
            lo, hi = asset.scale_range()  # stretch assets (modular boxes) tolerate 0.25..4x
            for nominal, actual in zip(asset.size, (w, d, hgt)):
                ratio = actual / nominal
                if not (lo <= ratio <= hi):
                    add("W_FURNITURE_SCALE", "warning",
                        f"{label} ({fi.id}) is scaled {ratio:.2f}x from its catalog size; it may look distorted.",
                        f"furniture:{fi.id}")
                    break
            ceiling = room.ceiling_height or level.wall_height
            if fi.elevation + hgt > ceiling - 0.02:
                add("W_FURNITURE_TOO_TALL", "warning",
                    f"{label} ({fi.id}) is {fi.elevation + hgt:.2f} m tall; the ceiling is at {ceiling:.2f} m.",
                    f"furniture:{fi.id}")
        tucks = {("seating", "table"), ("table", "seating")}
        for i, fa in enumerate(level_furniture):
            for fb in level_furniture[i + 1:]:
                if fa.room_id != fb.room_id or fa.id not in fps or fb.id not in fps:
                    continue
                ca, cb = catalog.get(fa.asset_id), catalog.get(fb.asset_id)
                if "floor_layer" in (ca.placement, cb.placement) or (ca.category, cb.category) in tucks:
                    continue
                # stacked items (hood over a range, wall cabinet over a table, appliance on a counter)
                za1 = fa.elevation + g.furniture_size(fa, ca)[2]
                zb1 = fb.elevation + g.furniture_size(fb, cb)[2]
                if za1 <= fb.elevation + 0.01 or zb1 <= fa.elevation + 0.01:
                    continue
                ov = fps[fa.id].intersection(fps[fb.id]).area
                if ov > 0.02:
                    add("W_FURNITURE_OVERLAP", "warning",
                        f"{fa.label or ca.name} ({fa.id}) and {fb.label or cb.name} ({fb.id}) overlap by {ov:.2f} m².",
                        f"furniture:{fa.id}", f"furniture:{fb.id}")
        for o in scene.openings:
            if o.kind != "door" or o.wall_id not in frames or not o.opens_into:
                continue
            swing = door_swing_polygon(scene, o)
            if swing is None:
                continue
            for fi in level_furniture:
                if fi.id in fps and catalog.get(fi.asset_id).placement != "floor_layer":
                    if fps[fi.id].intersection(swing).area > 0.01:
                        add("W_DOOR_SWING_BLOCKED", "warning",
                            f"{fi.label or catalog.get(fi.asset_id).name} ({fi.id}) blocks the swing of door {o.id}.",
                            f"furniture:{fi.id}", f"opening:{o.id}")

        # ---- summaries -----------------------------------------------------------
        for r in rooms:
            if r.id not in room_polys:
                continue
            poly, net = room_polys[r.id], nets[r.id]
            wc, dc = g.bbox_dims(poly)
            wn, dn = g.bbox_dims(net) if not net.is_empty else (0.0, 0.0)
            summaries.append(RoomSummary(
                id=r.id, name=r.name, type=r.type, level_id=level.id,
                area_centreline=_r(poly.area), area_net=_r(net.area),
                width_centreline=_r(wc), depth_centreline=_r(dc), width_clear=_r(wn), depth_clear=_r(dn),
                ceiling_height=_r(r.ceiling_height or level.wall_height)))

    # ---- certainty / review gate ---------------------------------------------------
    def certainty_issue(kind: str, ident: str, label: str, c) -> None:
        if c.status == "unresolved":
            add("E_UNRESOLVED_MEASUREMENT", "error",
                f"{label}: measurement is unresolved{(' – ' + c.note) if c.note else ''}.", f"{kind}:{ident}")
        elif c.status in NEEDS_REVIEW_STATUSES:
            verb = "read from the plan" if c.status == "extracted" else "estimated"
            conf = f" (confidence {c.confidence:.0%})" if c.confidence is not None else ""
            add("R_CONFIRM_MEASUREMENT", "review",
                f"{label}: dimensions were {verb}{conf}; confirm or accept as estimated.", f"{kind}:{ident}")

    for lv in scene.levels:
        certainty_issue("level", lv.id, f"Level '{lv.name}' wall height", lv.certainty)
    for r in scene.rooms:
        certainty_issue("room", r.id, f"Room '{r.name}'", r.certainty)
    for w in scene.walls:
        certainty_issue("wall", w.id, f"Wall {w.id}", w.certainty)
    for o in scene.openings:
        certainty_issue("opening", o.id, f"{o.kind.title()} {o.id}", o.certainty)
    for q in scene.open_questions:
        if not q.resolved:
            sev: Severity = "error" if q.severity == "blocking" else ("review" if q.severity == "warning" else "info")
            add("Q_OPEN_QUESTION", sev, q.question, *( [q.target] if q.target else []), f"question:{q.id}")

    _check_hvac(scene, add)

    # ---- brief cross-checks ----------------------------------------------------------
    if brief is not None:
        _check_brief(scene, brief, summaries, add)

    counts: dict[str, int] = defaultdict(int)
    for i in issues:
        counts[i.severity] += 1
    ok = counts.get("error", 0) == 0
    return ValidationReport(ok=ok, can_generate=ok and counts.get("review", 0) == 0, issues=issues,
                            rooms=summaries, counts=dict(counts))


HVAC_BALANCE_TOLERANCE = 0.15


def _check_hvac(scene: Scene, add) -> None:
    """HVAC layer: one aggregated review item per kind (an imported drawing has hundreds of elements),
    placement inside the building, links to units, and the airflow balance of every unit."""
    hv = scene.hvac
    if not (hv.units or hv.terminals or hv.ducts):
        return
    for kind, items, label in (("hvac_unit", hv.units, "HVAC units"), ("hvac_terminal", hv.terminals, "diffusers/grilles"),
                               ("duct", hv.ducts, "duct runs")):
        unresolved = [it for it in items if it.certainty.status == "unresolved"]
        if unresolved:
            add("E_UNRESOLVED_MEASUREMENT", "error", f"{len(unresolved)} {label} have unresolved placement.",
                *[f"{kind}:{it.id}" for it in unresolved[:50]])
        pending = [it for it in items if it.certainty.status in NEEDS_REVIEW_STATUSES]
        if pending:
            est = sum(1 for it in pending if it.certainty.status == "estimated")
            add("R_CONFIRM_HVAC", "review",
                f"{len(pending)} {label} were read from the HVAC drawing ({est} with estimated values); "
                "check them and confirm or accept the estimates.", *[f"{kind}:{it.id}" for it in pending[:50]])
    rooms = [(r, g.room_polygon(r)) for r in scene.rooms]
    outside = [f"hvac_unit:{u.id}" for u in hv.units if not any(p.buffer(0.3).contains(Point(*u.position)) for _, p in rooms)]
    outside += [f"hvac_terminal:{t.id}" for t in hv.terminals if not any(p.contains(Point(*t.position)) for _, p in rooms)]
    if outside:
        add("W_HVAC_OUTSIDE_ROOMS", "warning", f"{len(outside)} HVAC element(s) are not inside any room.", *outside[:50])
    unlinked = [f"hvac_terminal:{t.id}" for t in hv.terminals if t.unit_id is None]
    if unlinked:
        add("W_HVAC_TERMINAL_UNLINKED", "warning",
            f"{len(unlinked)} diffuser(s)/grille(s) are not linked to a unit (no airflow source in the simulation).",
            *unlinked[:50])
    for u in hv.units:
        sup = [t for t in hv.terminals if t.unit_id == u.id and t.kind in ("supply_diffuser", "supply_grille", "linear_slot")]
        if not u.airflow_lps or not sup or any(t.airflow_lps is None for t in sup):
            continue
        total = sum(t.airflow_lps for t in sup)
        if abs(total - u.airflow_lps) > HVAC_BALANCE_TOLERANCE * u.airflow_lps:
            add("W_HVAC_AIRFLOW_BALANCE", "warning",
                f"Unit {u.tag}: its {len(sup)} supply terminal(s) add up to {total:.0f} L/s but the unit supplies "
                f"{u.airflow_lps:.0f} L/s ({100 * total / u.airflow_lps:.0f}%).", f"hvac_unit:{u.id}",
                *[f"hvac_terminal:{t.id}" for t in sup])


def junction_zones(wall, walls) -> list[tuple[float, float, str]]:
    """Intervals along ``wall`` (in s) occupied by joining or crossing walls, plus clearance."""
    f = g.wall_frame(wall)
    line = g.wall_line(wall)
    zones: list[tuple[float, float, str]] = []
    for o in walls:
        if o.id == wall.id:
            continue
        positions: list[float] = []
        for pt in (o.start, o.end):
            if line.distance(Point(pt)) <= g.JOIN_TOL:
                positions.append(f.to_local(*pt)[0])
        inter = line.intersection(g.wall_line(o))
        if isinstance(inter, Point) and not inter.is_empty:
            positions.append(f.to_local(inter.x, inter.y)[0])
        half = o.thickness / 2 + OPENING_JUNCTION_CLEARANCE
        for s in positions:
            zones.append((s - half, s + half, o.id))
    return zones


def _side_rooms(scene: Scene, level_id: str, f: g.WallFrame, offset: float, thickness: float):
    probe = thickness / 2 + 0.05
    left = g.room_at(scene, level_id, *f.to_world(offset, probe))
    right = g.room_at(scene, level_id, *f.to_world(offset, -probe))
    return left, right


def door_swing_polygon(scene: Scene, opening) -> Polygon | None:
    """Quarter-circle swept by an open door leaf on the opens_into side (for clearance checks)."""
    wall = scene.wall(opening.wall_id)
    f = g.wall_frame(wall)
    level_id = wall.level_id
    left, right = _side_rooms(scene, level_id, f, opening.offset, wall.thickness)
    if left is not None and left.id == opening.opens_into:
        side = 1.0
    elif right is not None and right.id == opening.opens_into:
        side = -1.0
    else:
        return None
    hinge_s = opening.offset - opening.width / 2 if opening.hinge_side == "start" else opening.offset + opening.width / 2
    sweep_dir = 1.0 if opening.hinge_side == "start" else -1.0
    radius = opening.width
    d0 = side * wall.thickness / 2
    pts = [f.to_world(hinge_s, d0)]
    for k in range(0, 13):
        a = math.radians(90.0 * k / 12)
        pts.append(f.to_world(hinge_s + sweep_dir * radius * math.cos(a), d0 + side * radius * math.sin(a)))
    poly = Polygon(pts)
    return poly if poly.is_valid else poly.buffer(0)


def _check_brief(scene: Scene, brief: Brief, summaries: list[RoomSummary], add) -> None:
    if brief.overall_width and brief.overall_depth and scene.walls:
        outer = unary_union([fp for lv in scene.levels
                             for fp in g.wall_footprints(g.walls_on_level(scene, lv.id)).values()])
        w, d = g.bbox_dims(outer)
        tol_w = max(DIM_TOLERANCE, 0.02 * brief.overall_width)
        tol_d = max(DIM_TOLERANCE, 0.02 * brief.overall_depth)
        if abs(w - brief.overall_width) > tol_w or abs(d - brief.overall_depth) > tol_d:
            add("W_OVERALL_MISMATCH", "warning",
                f"Outer footprint is {w:.2f} × {d:.2f} m but the brief says "
                f"{brief.overall_width:.2f} × {brief.overall_depth:.2f} m.")
    for known in brief.known_rooms:
        match = next((s for s in summaries if s.name.strip().lower() == known.name.strip().lower()), None)
        if match is None:
            add("W_KNOWN_ROOM_MISSING", "warning", f"The brief lists a room '{known.name}' that is not in the layout.")
            continue
        aw, ad = ((match.width_clear, match.depth_clear) if known.basis == "clear"
                  else (match.width_centreline, match.depth_centreline))
        fits = (abs(aw - known.width) <= DIM_TOLERANCE and abs(ad - known.depth) <= DIM_TOLERANCE) or \
               (abs(aw - known.depth) <= DIM_TOLERANCE and abs(ad - known.width) <= DIM_TOLERANCE)
        if not fits:
            add("W_ROOM_DIM_MISMATCH", "warning",
                f"'{match.name}' measures {aw:.2f} × {ad:.2f} m ({known.basis}) but the brief says "
                f"{known.width:.2f} × {known.depth:.2f} m.", f"room:{match.id}")
