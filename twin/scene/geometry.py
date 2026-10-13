"""Deterministic 2D geometry shared by validation and the mesh compiler.

Everything here is derived from the scene model only (never from AI descriptions).
Uses shapely for robust polygon booleans.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from twin.assets.catalog import Asset
from twin.scene.model import FurnitureItem, Room, Scene, Wall

# geometric tolerances (metres)
EPS = 1e-6
JOIN_TOL = 0.01          # wall endpoints closer than this are treated as joined
PRECISION_GRID = 1e-5    # snap boolean results to 0.01 mm to avoid slivers


@dataclass(frozen=True)
class WallFrame:
    """Local frame of a wall: s along the centreline from start, d to the left (+normal)."""

    wall_id: str
    ox: float
    oy: float
    ux: float
    uy: float
    length: float
    thickness: float

    @property
    def nx(self) -> float:
        return -self.uy

    @property
    def ny(self) -> float:
        return self.ux

    def to_world(self, s: float, d: float = 0.0) -> tuple[float, float]:
        return (self.ox + self.ux * s + self.nx * d, self.oy + self.uy * s + self.ny * d)

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = x - self.ox, y - self.oy
        return (dx * self.ux + dy * self.uy, dx * self.nx + dy * self.ny)

    def rect(self, s0: float, s1: float, d0: float, d1: float) -> list[tuple[float, float]]:
        """Counter-clockwise rectangle in world coordinates."""
        return [self.to_world(s0, d0), self.to_world(s1, d0), self.to_world(s1, d1), self.to_world(s0, d1)]

    @property
    def angle_deg(self) -> float:
        return math.degrees(math.atan2(self.uy, self.ux))


def wall_frame(wall: Wall) -> WallFrame:
    dx, dy = wall.end[0] - wall.start[0], wall.end[1] - wall.start[1]
    length = math.hypot(dx, dy)
    return WallFrame(wall.id, wall.start[0], wall.start[1], dx / length, dy / length, length, wall.thickness)


def wall_line(wall: Wall) -> LineString:
    return LineString([wall.start, wall.end])


def walls_on_level(scene: Scene, level_id: str) -> list[Wall]:
    return [w for w in scene.walls if w.level_id == level_id]


def rooms_on_level(scene: Scene, level_id: str) -> list[Room]:
    return [r for r in scene.rooms if r.level_id == level_id]


def room_polygon(room: Room) -> Polygon:
    return Polygon(room.polygon)


def _touching_walls(point: tuple[float, float], wall: Wall, walls: list[Wall]) -> list[Wall]:
    p = Point(point)
    return [o for o in walls if o.id != wall.id and wall_line(o).distance(p) <= JOIN_TOL]


def junction_extensions(walls: list[Wall]) -> dict[str, tuple[float, float]]:
    """How far each wall must extend past its endpoints to close corners and T-junctions.

    An endpoint that touches another wall (its end or its body) extends by half of the
    thickest touching wall, so the footprints overlap; ``wall_footprints`` then removes
    the overlap by priority so no two wall solids intersect.
    """
    out: dict[str, tuple[float, float]] = {}
    for w in walls:
        ext = []
        for pt in (w.start, w.end):
            touching = _touching_walls(pt, w, walls)
            ext.append(max((o.thickness for o in touching), default=0.0) / 2.0)
        out[w.id] = (ext[0], ext[1])
    return out


def wall_priority(walls: list[Wall]) -> list[Wall]:
    """Exterior walls first, then longer walls, then id: earlier walls keep the junction volume."""
    return sorted(walls, key=lambda w: (0 if w.kind == "exterior" else 1, -wall_frame(w).length, w.id))


def wall_footprints(walls: list[Wall]) -> dict[str, BaseGeometry]:
    """Non-overlapping 2D footprints of all walls on one level (centreline +/- thickness/2)."""
    ext = junction_extensions(walls)
    taken: BaseGeometry | None = None
    out: dict[str, BaseGeometry] = {}
    for w in wall_priority(walls):
        f = wall_frame(w)
        e0, e1 = ext[w.id]
        rect = Polygon(f.rect(-e0, f.length + e1, -w.thickness / 2, w.thickness / 2))
        geom = rect if taken is None else rect.difference(taken)
        geom = shapely.set_precision(geom, PRECISION_GRID)
        out[w.id] = geom
        taken = rect if taken is None else unary_union([taken, rect])
    return out


def polygons_of(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > EPS else []
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and g.area > EPS]


def footprint_rect(cx: float, cy: float, width: float, depth: float, rotation_deg: float) -> Polygon:
    a = math.radians(rotation_deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for lx, ly in ((-width / 2, -depth / 2), (width / 2, -depth / 2), (width / 2, depth / 2), (-width / 2, depth / 2)):
        pts.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
    return Polygon(pts)


def furniture_size(item: FurnitureItem, asset: Asset) -> tuple[float, float, float]:
    if item.size is not None:
        return (item.size.width, item.size.depth, item.size.height)
    return asset.size


def furniture_footprint(item: FurnitureItem, asset: Asset) -> Polygon:
    w, d, _ = furniture_size(item, asset)
    return footprint_rect(item.position[0], item.position[1], w, d, item.rotation_deg)


def room_net_polygon(room: Room, footprints: dict[str, BaseGeometry]) -> BaseGeometry:
    """Usable floor area: room centreline polygon minus wall footprints."""
    walls_union = unary_union(list(footprints.values())) if footprints else Polygon()
    return shapely.set_precision(room_polygon(room).difference(walls_union), PRECISION_GRID)


def opening_world_center(scene: Scene, opening_wall: Wall, offset: float) -> tuple[float, float]:
    return wall_frame(opening_wall).to_world(offset, 0.0)


def room_at(scene: Scene, level_id: str, x: float, y: float) -> Room | None:
    p = Point(x, y)
    for r in rooms_on_level(scene, level_id):
        if room_polygon(r).contains(p):
            return r
    return None


def signed_area(points: list[tuple[float, float]]) -> float:
    a = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def bbox_dims(geom: BaseGeometry) -> tuple[float, float]:
    minx, miny, maxx, maxy = geom.bounds
    return (maxx - minx, maxy - miny)
