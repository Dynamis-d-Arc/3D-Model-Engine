"""Rule-based fixes for validation problems: free, instant and repeatable (no AI).

Each rule handles one issue code with the smallest change that makes the problem go away and says in plain
words what it did. Rules never decide what the drawing leaves open (is a wall missing or is the room open
plan? where is the door into the terrace?): those problems are left for the user or the AI problem solver
(twin.ai.solve). ``autofix`` runs the rules in passes until nothing more changes, because one fix can
uncover another (a resized table may then poke into a wall and be nudged back in the next pass).

User actions for the problems that need a decision live here too: ``add_missing_walls`` (the room edges
really are walls) and the room's ``open_plan`` flag (they are not; set by the editor).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from twin.assets.catalog import Asset, load_catalog
from twin.scene import geometry as g
from twin.scene.model import Brief, Certainty, FurnitureItem, Scene, Size3, Wall
from twin.scene.validate import (OPENING_HEAD_CLEARANCE, Issue, ValidationReport, _side_rooms, door_swing_polygon,
                                 junction_zones, validate_scene)

MAX_SPLIT = 12            # a stretched item becomes at most this many items in a row
OPENING_SEARCH_M = 1.0    # how far a door / window may slide along its wall
MIN_OPENING_WIDTH = {"door": 0.7, "window": 0.4, "opening": 0.6}
# last resort on a short wall (e.g. a facet of a curved bay): shrink the opening to the wall's free stretch, not below
FIT_MIN_WIDTH = {"door": 0.6, "window": 0.25, "opening": 0.4}
SWING_SEARCH_M = 1.5      # how far furniture may move out of a door swing


class _Skip(Exception):
    """The rule cannot fix this case; leave the problem for the user / the AI."""


@dataclass
class FixAction:
    code: str
    target: str
    text: str

    def as_dict(self) -> dict:
        return {"code": self.code, "target": self.target, "text": self.text}


def _target(issue: Issue, kind: str, last: bool = False) -> str:
    ids = [t.split(":", 1)[1] for t in issue.targets if t.startswith(kind + ":")]
    if not ids:
        raise _Skip
    return ids[-1] if last else ids[0]


def _item(scene: Scene, fid: str) -> FurnitureItem:
    item = next((f for f in scene.furniture if f.id == fid), None)
    if item is None:
        raise _Skip
    return item


def _asset(item: FurnitureItem) -> Asset:
    asset = load_catalog().get(item.asset_id)
    if asset is None:
        raise _Skip
    return asset


def _name(item: FurnitureItem, asset: Asset) -> str:
    return f"{item.label or asset.name} ({item.id})"


def _replace_furniture(scene: Scene, fid: str, items: list[FurnitureItem]) -> Scene:
    out: list[FurnitureItem] = []
    for f in scene.furniture:
        out.extend(items if f.id == fid else [f])
    return scene.model_copy(update={"furniture": out})


def _auto(c: Certainty, note: str) -> Certainty:
    """A rule-made change keeps the value's review status (an estimate stays an estimate) and says why."""
    return c.model_copy(update={"note": note[:500]})


def _next_ids(scene: Scene, prefix: str, n: int) -> list[str]:
    used = {f.id for f in scene.furniture}
    nums = [int(i[len(prefix):]) for i in used if i.startswith(prefix) and i[len(prefix):].isdigit()]
    k = max(nums, default=0)
    out = []
    while len(out) < n:
        k += 1
        if f"{prefix}{k}" not in used:
            out.append(f"{prefix}{k}")
    return out


# ---- furniture ---------------------------------------------------------------------------------
def fix_furniture_scale(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    """Stretched beyond its catalog range -> the same item repeated in a row (a long shelf run reads as several
    shelves, not one distorted one); squashed or over-tall -> resized to the nearest size that still looks right."""
    item = _item(scene, _target(issue, "furniture"))
    asset = _asset(item)
    w, d, h = g.furniture_size(item, asset)
    w0, d0, h0 = asset.size
    lo, hi = asset.scale_range()

    def clamp(v: float, v0: float) -> float:
        return min(max(v, lo * v0), hi * v0)

    rw, rd = w / w0, d / d0
    if max(rw, rd) > hi:
        along_x = rw >= rd
        length, nominal = (w, w0) if along_x else (d, d0)
        pieces = min(MAX_SPLIT, max(2, round(length / nominal)))
        step = length / pieces
        piece = clamp(step, nominal)
        size = Size3(width=round(piece if along_x else clamp(w, w0), 3), depth=round(clamp(d, d0) if along_x else piece, 3),
                     height=round(clamp(h, h0), 3))
        a = math.radians(item.rotation_deg)
        ux, uy = (math.cos(a), math.sin(a)) if along_x else (-math.sin(a), math.cos(a))
        ids = [item.id] + _next_ids(scene, "F", pieces - 1)
        note = f"auto-fix: one item stretched {max(rw, rd):.1f}x replaced by {pieces} in a row"
        items = []
        for k, ident in enumerate(ids):
            off = (k - (pieces - 1) / 2) * step
            items.append(item.model_copy(update={
                "id": ident, "size": size, "certainty": _auto(item.certainty, note),
                "position": (round(item.position[0] + ux * off, 4), round(item.position[1] + uy * off, 4))}))
        return (_replace_furniture(scene, item.id, items),
                f"{_name(item, asset)} was stretched {max(rw, rd):.1f}x: replaced by a row of {pieces} "
                f"(each {piece:.2f} m) so none is distorted.")
    new = (round(clamp(w, w0), 3), round(clamp(d, d0), 3), round(clamp(h, h0), 3))
    if new == (round(w, 3), round(d, 3), round(h, 3)):
        raise _Skip
    worst = next(r for r in (w / w0, d / d0, h / h0) if not lo <= r <= hi)
    moved = item.model_copy(update={"size": Size3(width=new[0], depth=new[1], height=new[2]),
                                    "certainty": _auto(item.certainty, "auto-fix: resized into its normal size range")})
    return (_replace_furniture(scene, item.id, [moved]),
            f"{_name(item, asset)} was {'squashed' if worst < lo else 'stretched'} to {worst:.2f}x: resized to "
            f"{new[0]:.2f} × {new[1]:.2f} × {new[2]:.2f} m, the closest size that still looks right.")


def fix_furniture_into_room(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    """Outside its room: if it stands inside another room it belongs to that one; else move it in.
    Poking into a wall: nudge it the shortest distance that clears the wall."""
    item = _item(scene, _target(issue, "furniture"))
    asset = _asset(item)
    room = scene.room(item.room_id)
    if issue.code == "E_FURNITURE_OUTSIDE_ROOM":
        other = g.room_at(scene, room.level_id, *item.position)
        if other is not None and other.id != room.id:
            moved = item.model_copy(update={"room_id": other.id})
            return (_replace_furniture(scene, item.id, [moved]),
                    f"{_name(item, asset)} stands in '{other.name}', not '{room.name}': it now belongs to '{other.name}' "
                    "(it was not moved).")
    note = "auto-fix: moved into its room"
    max_shift = 3.0 if issue.code.startswith("E_") else 1.0
    try:   # inside the room, and if possible without blocking a door or bumping into other furniture
        scene, r = _nudge(scene, item, asset, note, max_shift=max_shift, clear_furniture=False)
    except _Skip:
        scene, r = _nudge(scene, item, asset, note, max_shift=max_shift, clear_furniture=None)
    return scene, f"{_name(item, asset)} {'was outside' if issue.code.startswith('E_') else 'poked into a wall of'} " \
                  f"'{room.name}': moved {r:.2f} m into the room."


class _GeoCache:
    """Room floors and door swings for the rules, rebuilt only when walls, openings or rooms change (the scene is
    immutable: an edit replaces the list it touches, so list identity tells what changed)."""

    def __init__(self) -> None:
        self.key: tuple | None = None
        self.refs: tuple = ()
        self.fps: dict = {}
        self.nets: dict = {}
        self.door_swings: list | None = None
        self.swings: dict = {}

    def _sync(self, scene: Scene) -> None:
        key = (id(scene.walls), id(scene.openings), id(scene.rooms))
        if key != self.key:
            self.key, self.refs = key, (scene.walls, scene.openings, scene.rooms)
            self.fps, self.nets, self.door_swings, self.swings = {}, {}, None, {}

    def net(self, scene: Scene, room):
        self._sync(scene)
        if room.id not in self.nets:
            if room.level_id not in self.fps:
                fps = g.wall_footprints(g.walls_on_level(scene, room.level_id))
                self.fps[room.level_id] = (fps, unary_union(list(fps.values())) if fps else None)
            fps, union = self.fps[room.level_id]
            net = g.room_net_polygon(room, fps, union).buffer(0.005)
            shapely.prepare(net)
            self.nets[room.id] = net
        return self.nets[room.id]

    def swing(self, scene: Scene, room):
        self._sync(scene)
        if self.door_swings is None:
            self.door_swings = [p for o in scene.openings if o.kind == "door" and o.opens_into
                                and (p := door_swing_polygon(scene, o)) is not None]
        if room.id not in self.swings:
            near = g.room_polygon(room).buffer(0.05)
            self.swings[room.id] = [p for p in self.door_swings if p.intersects(near)]
        return self.swings[room.id]


def _overlap(cands: np.ndarray, geoms: list) -> np.ndarray:
    """Total overlap area of each candidate footprint with the given shapes (summed pairwise via a spatial index:
    much faster than intersecting with one merged shape)."""
    areas = np.zeros(len(cands))
    if not geoms or not len(cands):
        return areas
    ci, gi = shapely.STRtree(geoms).query(cands, predicate="intersects")
    if len(ci):
        np.add.at(areas, ci, shapely.area(shapely.intersection(cands[ci], np.asarray(geoms, dtype=object)[gi])))
    return areas


_geo = _GeoCache()


def _offsets(max_shift: float) -> np.ndarray:
    """Candidate moves, nearest first: 1 cm rings up to 30 cm, then 3 cm rings, 36 directions each."""
    radii, r = [], 0.0
    while r < max_shift - 1e-9:
        r = round(r + (0.01 if r < 0.3 - 1e-9 else 0.03), 3)
        radii.append(r)
    ang = np.radians(np.arange(36) * 10.0)
    rr = np.repeat(np.array(radii), 36)
    aa = np.tile(ang, len(radii))
    return np.stack([rr * np.cos(aa), rr * np.sin(aa), rr], axis=1)


def _nudge(scene: Scene, item: FurnitureItem, asset: Asset, note: str, *, max_shift: float = SWING_SEARCH_M,
           clear_furniture: bool | None) -> tuple[Scene, float]:
    """Move the item the shortest distance that keeps it inside its room's floor. With ``clear_furniture`` True it
    must also end up out of every door swing and off the other furniture; False: out of door swings and overlapping
    the other furniture no more than now; None: only inside the room. All candidates are tested at once."""
    room = scene.room(item.room_id)
    catalog = load_catalog()
    fp = g.furniture_footprint(item, asset)
    offs = _offsets(max_shift)
    ring = np.asarray(fp.exterior.coords)                          # (5, 2)
    cands = shapely.polygons(ring[None, :, :] + offs[:, None, :2])
    ok = shapely.contains(_geo.net(scene, room), cands)
    if clear_furniture is not None and ok.any():
        live = np.flatnonzero(ok)
        ok[live] = _overlap(cands[live], _geo.swing(scene, room)) <= 0.005
        reach = fp.buffer(max_shift + 0.05)   # only the furniture the search can reach matters
        others = [fo for f in scene.furniture
                  if f.id != item.id and f.room_id == item.room_id and (a := catalog.get(f.asset_id)) is not None
                  and a.placement != "floor_layer" and (fo := g.furniture_footprint(f, a)).intersects(reach)]
        if others and ok.any():
            allowed = 0.005 if clear_furniture else float(_overlap(np.array([fp], dtype=object), others)[0]) + 0.02
            live = np.flatnonzero(ok)
            ok[live] = _overlap(cands[live], others) <= allowed
    idx = np.flatnonzero(ok)
    if not len(idx):
        raise _Skip
    dx, dy, r = offs[idx[0]]
    moved = item.model_copy(update={"position": (round(item.position[0] + float(dx), 4), round(item.position[1] + float(dy), 4)),
                                    "certainty": _auto(item.certainty, note)})
    return _replace_furniture(scene, item.id, [moved]), float(r)


def fix_door_swing(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    """Move the item clear of every door swing in its room, staying inside the room and off the other furniture."""
    item = _item(scene, _target(issue, "furniture"))
    asset = _asset(item)
    oid = _target(issue, "opening")
    scene, r = _nudge(scene, item, asset, f"auto-fix: moved clear of door {oid}", clear_furniture=False)
    return scene, f"{_name(item, asset)} blocked door {oid}: moved it {r:.2f} m so the door can open."


def fix_furniture_overlap(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    """Two items overlap: move the smaller one the shortest distance that clears it."""
    ids = [t.split(":", 1)[1] for t in issue.targets if t.startswith("furniture:")]
    if len(ids) != 2:
        raise _Skip
    pair = [(_item(scene, i), None) for i in ids]
    pair = [(it, _asset(it)) for it, _ in pair]
    pair.sort(key=lambda p: g.furniture_footprint(*p).area)
    (item, asset), (other, other_asset) = pair
    scene, r = _nudge(scene, item, asset, f"auto-fix: moved off {other.id}", clear_furniture=True)
    return scene, f"{_name(item, asset)} overlapped {_name(other, other_asset)}: moved it {r:.2f} m."


def fix_furniture_too_tall(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    item = _item(scene, _target(issue, "furniture"))
    asset = _asset(item)
    room = scene.room(item.room_id)
    level = next(lv for lv in scene.levels if lv.id == room.level_id)
    ceiling = room.ceiling_height or level.wall_height
    w, d, h = g.furniture_size(item, asset)
    target = round(ceiling - item.elevation - 0.05, 3)
    if target < asset.scale_range()[0] * asset.size[2] or target >= h:
        raise _Skip
    moved = item.model_copy(update={"size": Size3(width=w, depth=d, height=target),
                                    "certainty": _auto(item.certainty, "auto-fix: lowered under the ceiling")})
    return (_replace_furniture(scene, item.id, [moved]),
            f"{_name(item, asset)} went through the ceiling: lowered it to {target:.2f} m.")


# ---- openings ----------------------------------------------------------------------------------
def _slide_opening(scene: Scene, oid: str) -> tuple[Scene, str]:
    """Slide (then if needed narrow) an opening along its wall until it is inside the wall, clear of joining walls
    and of the other openings, and still between the same two rooms."""
    o = next((x for x in scene.openings if x.id == oid), None)
    if o is None:
        raise _Skip
    wall = scene.wall(o.wall_id)
    walls = g.walls_on_level(scene, wall.level_id)
    f = g.wall_frame(wall)
    zones = junction_zones(wall, walls)
    others = [(x.offset - x.width / 2 - 0.05, x.offset + x.width / 2 + 0.05)
              for x in scene.openings if x.wall_id == wall.id and x.id != o.id]
    sides0 = {r.id if r else None for r in _side_rooms(scene, wall.level_id, f, o.offset, wall.thickness)}

    def ok(off: float, width: float) -> bool:
        a, b = off - width / 2, off + width / 2
        if a < 0 or b > f.length:
            return False
        if any(a < hi and b > lo for lo, hi, _ in zones) or any(a < hi and b > lo for lo, hi in others):
            return False
        sides = {r.id if r else None for r in _side_rooms(scene, wall.level_id, f, off, wall.thickness)}
        return sides == sides0

    widths = [o.width]
    w = o.width
    while w - 0.05 >= MIN_OPENING_WIDTH[o.kind] - 1e-9:
        w = round(w - 0.05, 3)
        widths.append(w)
    for width in widths:
        for k in range(int(OPENING_SEARCH_M / 0.01) + 1):
            for sign in (1, -1):
                off = round(o.offset + sign * k * 0.01, 3)
                if (k or sign == 1) and ok(off, width):
                    if k == 0 and width == o.width:
                        raise _Skip   # already fine: the problem is something else
                    moved = o.model_copy(update={"offset": off, "width": width,
                                                 "certainty": _auto(o.certainty, "auto-fix: moved / narrowed to fit its wall")})
                    scene = scene.model_copy(update={"openings": [moved if x.id == o.id else x for x in scene.openings]})
                    what = []
                    if k:
                        what.append(f"moved it {k} cm along wall {wall.id}")
                    if width != o.width:
                        what.append(f"narrowed it {o.width:.2f} → {width:.2f} m")
                    return scene, f"{o.kind.title()} {o.id}: {' and '.join(what)}."
    # no room at the usual sizes (a short wall): fill the free stretch nearest to where it was
    free = _free_spans(f.length, [(lo, hi) for lo, hi, _ in zones] + others)
    best = None
    for lo, hi in free:
        width = min(o.width, math.floor((hi - lo - 0.01) * 100) / 100)
        off = round((lo + hi) / 2, 3)
        if width >= FIT_MIN_WIDTH[o.kind] - 1e-9 and ok(off, width) and (best is None or abs(off - o.offset) < abs(best[0] - o.offset)):
            best = (off, width, hi - lo)
    if best is not None:
        off, width, clear = best
        moved = o.model_copy(update={"offset": off, "width": width,
                                     "certainty": _auto(o.certainty, "auto-fix: narrowed to the free stretch of a short wall")})
        scene = scene.model_copy(update={"openings": [moved if x.id == o.id else x for x in scene.openings]})
        return scene, (f"{o.kind.title()} {o.id}: narrowed it {o.width:.2f} → {width:.2f} m to fit the {clear:.2f} m of "
                       f"wall {wall.id} that is clear of its corners.")
    if o.kind == "window":   # a door is never removed: the room behind it may have no other way in
        clear = max((hi - lo for lo, hi in free), default=0.0)
        scene = scene.model_copy(update={"openings": [x for x in scene.openings if x.id != o.id]})
        return scene, (f"Window {o.id}: removed it, because wall {wall.id} has only {clear:.2f} m clear of its corners, "
                       f"too little for a window. Add one to a longer wall if you need it.")
    raise _Skip


def _free_spans(length: float, blocked: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The stretches of a wall (0..length along it) that no blocked interval covers."""
    spans, pos = [], 0.0
    for lo, hi in sorted(blocked):
        if lo > pos:
            spans.append((pos, min(lo, length)))
        pos = max(pos, hi)
    if pos < length:
        spans.append((pos, length))
    return [(a, b) for a, b in spans if b > a]


def fix_opening_position(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    ids = [t.split(":", 1)[1] for t in issue.targets if t.startswith("opening:")]
    if not ids:
        raise _Skip
    # overlapping pair: move the second one first, then the first
    for oid in (list(reversed(ids)) if issue.code == "E_OPENING_OVERLAP" else ids[:1]):
        try:
            scene2, text = _slide_opening(scene, oid)
        except _Skip:
            continue
        reason = {"E_OPENING_AT_JUNCTION": "cut into the corner with a joining wall",
                  "E_OPENING_OVERLAP": "overlapped another opening",
                  "E_OPENING_OUTSIDE_WALL": "ran past the end of its wall"}[issue.code]
        return scene2, text.replace(":", f" {reason}:", 1)
    raise _Skip


def fix_opening_too_tall(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    oid = _target(issue, "opening")
    o = next((x for x in scene.openings if x.id == oid), None)
    if o is None:
        raise _Skip
    h = scene.wall_height(scene.wall(o.wall_id))
    height = round(h - OPENING_HEAD_CLEARANCE - 0.01 - o.sill_height, 3)
    if height < 0.2 or height >= o.height:
        raise _Skip
    moved = o.model_copy(update={"height": height, "certainty": _auto(o.certainty, "auto-fix: lowered under the wall top")})
    return (scene.model_copy(update={"openings": [moved if x.id == o.id else x for x in scene.openings]}),
            f"{o.kind.title()} {o.id} was taller than its wall: lowered it to {height:.2f} m.")


# ---- walls -------------------------------------------------------------------------------------
def fix_wall_overshoot(scene: Scene, issue: Issue) -> tuple[Scene, str]:
    """Two walls cross with a short stub: cut the stub back to the crossing point."""
    ids = [t.split(":", 1)[1] for t in issue.targets if t.startswith("wall:")]
    if len(ids) != 2:
        raise _Skip
    wa, wb = scene.wall(ids[0]), scene.wall(ids[1])
    inter = g.wall_line(wa).intersection(g.wall_line(wb))
    if inter.is_empty or not isinstance(inter, Point):
        raise _Skip
    best = None
    for w in (wa, wb):
        f = g.wall_frame(w)
        s, _ = f.to_local(inter.x, inter.y)
        for end, stub in (("start", s), ("end", f.length - s)):
            if best is None or stub < best[2]:
                best = (w, end, stub, s)
    w, end, stub, s = best
    if stub >= 0.3:
        raise _Skip
    f = g.wall_frame(w)
    new_len = f.length - stub
    shift = stub if end == "start" else 0.0
    ops = [o for o in scene.openings if o.wall_id == w.id]
    if any(o.offset - shift - o.width / 2 < 0 or o.offset - shift + o.width / 2 > new_len for o in ops):
        raise _Skip
    p = (round(inter.x, 4), round(inter.y, 4))
    trimmed = w.model_copy(update={end: p})
    openings = [o.model_copy(update={"offset": round(o.offset - shift, 4)}) if o.wall_id == w.id else o for o in scene.openings]
    walls = [trimmed if x.id == w.id else x for x in scene.walls]
    return (scene.model_copy(update={"walls": walls, "openings": openings}),
            f"Wall {w.id} stuck out {stub:.2f} m past wall {(wb if w is wa else wa).id}: trimmed it.")


RULES: dict[str, Callable[[Scene, Issue], tuple[Scene, str]]] = {
    "W_FURNITURE_SCALE": fix_furniture_scale,
    "E_FURNITURE_OUTSIDE_ROOM": fix_furniture_into_room,
    "W_FURNITURE_CROSSES_BOUNDARY": fix_furniture_into_room,
    "W_DOOR_SWING_BLOCKED": fix_door_swing,
    "W_FURNITURE_OVERLAP": fix_furniture_overlap,
    "W_FURNITURE_TOO_TALL": fix_furniture_too_tall,
    "E_OPENING_AT_JUNCTION": fix_opening_position,
    "E_OPENING_OVERLAP": fix_opening_position,
    "E_OPENING_OUTSIDE_WALL": fix_opening_position,
    "E_OPENING_TOO_TALL": fix_opening_too_tall,
    "W_WALL_OVERSHOOT": fix_wall_overshoot,
}


def issue_key(issue: Issue | dict) -> tuple[str, tuple[str, ...]]:
    if isinstance(issue, dict):
        return issue["code"], tuple(issue.get("targets") or ())
    return issue.code, tuple(issue.targets)


def autofix(scene: Scene, brief: Brief | None = None, *, codes: set[str] | None = None,
            targets: set[str] | None = None, max_passes: int = 4) -> tuple[Scene, list[FixAction], ValidationReport]:
    """Apply every rule that matches (optionally only these codes / problems touching these targets).
    Returns the fixed scene, what was done, and its validation report."""
    actions: list[FixAction] = []
    attempted: set[tuple[str, tuple[str, ...]]] = set()
    report = validate_scene(scene, brief)
    for _ in range(max_passes):
        progressed = False
        for issue in report.issues:
            rule = RULES.get(issue.code)
            if rule is None or (codes is not None and issue.code not in codes):
                continue
            if targets is not None and not (set(issue.targets) & targets):
                continue
            key = issue_key(issue)
            if key in attempted:
                continue
            attempted.add(key)
            before = {f.id for f in scene.furniture}
            try:
                scene, text = rule(scene, issue)
            except (_Skip, KeyError, StopIteration):
                continue
            actions.append(FixAction(issue.code, issue.targets[0] if issue.targets else "", text))
            progressed = True
            if targets is not None:   # follow-on problems of what was just fixed (a split shelf now pokes into a wall)
                targets = targets | set(issue.targets) | {f"furniture:{f.id}" for f in scene.furniture if f.id not in before}
        if not progressed:
            break
        report = validate_scene(scene, brief)
    return scene, actions, report


# ---- user actions for problems that need a decision ------------------------------------------------
def add_missing_walls(scene: Scene, room_id: str, thickness: float | None = None) -> tuple[Scene, list[str]]:
    """The room's edges without a wall really are walls: add one along every uncovered stretch longer than
    30 cm (exterior when no other room is on the far side)."""
    room = scene.room(room_id)
    walls = g.walls_on_level(scene, room.level_id)
    cover = unary_union([g.wall_line(w).buffer(0.02) for w in walls]) if walls else None
    others = [g.room_polygon(r) for r in g.rooms_on_level(scene, room.level_id) if r.id != room.id]
    nums = [int(w.id[1:]) for w in scene.walls if w.id.startswith("W") and w.id[1:].isdigit()]
    counter = max(nums, default=0)
    ext_t = max((w.thickness for w in walls if w.kind == "exterior"), default=0.2)
    int_t = min((w.thickness for w in walls if w.kind == "interior"), default=0.1)
    added: list[Wall] = []
    pts = [tuple(p) for p in room.polygon]
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        edge = LineString([a, b])
        bare = edge if cover is None else edge.difference(cover)
        for piece in getattr(bare, "geoms", [bare]):
            if piece.is_empty or piece.length < 0.3:
                continue
            (x0, y0), (x1, y1) = piece.coords[0], piece.coords[-1]
            mid = piece.interpolate(0.5, normalized=True)
            nx, ny = -(y1 - y0) / piece.length, (x1 - x0) / piece.length
            probe = [Point(mid.x + nx * 0.2, mid.y + ny * 0.2), Point(mid.x - nx * 0.2, mid.y - ny * 0.2)]
            shared = any(o.contains(p) for o in others for p in probe)
            counter += 1
            added.append(Wall(id=f"W{counter}", level_id=room.level_id, start=(round(x0, 4), round(y0, 4)),
                              end=(round(x1, 4), round(y1, 4)), kind="interior" if shared else "exterior",
                              thickness=thickness or (int_t if shared else ext_t),
                              certainty=Certainty(status="confirmed", source="user",
                                                  note=f"added along the open edge of '{room.name}'")))
    if not added:
        return scene, []
    return (scene.model_copy(update={"walls": [*scene.walls, *added]}),
            [f"Wall {w.id} added ({math.hypot(w.end[0] - w.start[0], w.end[1] - w.start[1]):.2f} m, {w.kind})" for w in added])
