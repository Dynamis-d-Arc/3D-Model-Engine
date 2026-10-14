"""Versioned architectural scene model: the single source of truth for the pipeline.

Coordinate system (see docs/SCENE_MODEL.md):
    * Units are metres (and degrees for angles) everywhere.
    * Right-handed, +X = east (right on the plan image), +Y = north (up on the plan image), +Z = up.
    * Plan coordinates describe wall CENTRELINES; room polygons follow wall centrelines,
      so neighbouring rooms share edges exactly.
    * Angles are degrees counter-clockwise about +Z seen from above, 0 = +X.
    * Furniture: ``position`` is the centre of the footprint at floor level; with
      ``rotation_deg = 0`` the asset's front faces -Y (south). 90 faces +X (east).
    * Elevations are relative to the level's floor elevation; level.elevation is absolute.

Structural integrity (unique ids, valid references, sane ranges) is enforced here and
raises ``pydantic.ValidationError``. Geometric checks (overlaps, connectivity, furniture
inside rooms, ...) live in ``twin.scene.validate`` and produce a report instead.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0.0"
COORDINATE_SYSTEM = "metres; right-handed; +X east, +Y north, +Z up; wall centrelines; angles CCW deg from +X"

Id = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Coord = Annotated[float, Field(ge=-1000.0, le=1000.0, allow_inf_nan=False)]
Vec2 = tuple[Coord, Coord]

RoomType = Literal[
    "living", "dining", "kitchen", "bedroom", "bathroom", "toilet", "hallway", "entry",
    "office", "storage", "laundry", "balcony", "utility", "other",
]
MeasurementStatus = Literal["confirmed", "extracted", "estimated", "accepted_estimate", "unresolved"]
Source = Literal["user", "ai", "fixture", "derived", "manual"]
StyleId = Literal["modern", "minimalist", "industrial", "luxury", "scandinavian"]
Pattern = Literal["none", "wood", "tile", "stone", "concrete", "fabric", "marble", "plaster", "brick",
                  "resin", "panel", "perforated", "stack_tile", "endgrain", "chevron", "boards", "brushed"]
MaterialRole = Literal[
    "upholstery", "cushion", "wood", "frame_metal", "metal_accent", "ceramic", "stone_top",
    "glass", "appliance", "screen", "plant", "pot", "rug", "lamp_shade", "bedding", "lacquer", "mirror",
    "stainless",
]

# statuses that still need a human decision before generation
NEEDS_REVIEW_STATUSES: frozenset[str] = frozenset({"extracted", "estimated"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Certainty(_Strict):
    """Where a value came from and whether a human has confirmed it.

    confirmed          value given or checked by the user (or a known fixture)
    extracted          read from the plan by the AI (e.g. a dimension label) - needs confirmation
    estimated          scaled or guessed - needs confirmation or explicit acceptance
    accepted_estimate  the user knowingly accepted an estimate; recorded in build outputs
    unresolved         no usable value; generation is blocked until fixed
    """

    status: MeasurementStatus = "confirmed"
    source: Source = "user"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    note: str = Field(default="", max_length=500)


class Size3(_Strict):
    width: float = Field(gt=0.01, le=20.0, description="local X extent, metres")
    depth: float = Field(gt=0.005, le=20.0, description="local Y extent, metres")
    height: float = Field(gt=0.005, le=10.0, description="local Z extent, metres")


class Brief(_Strict):
    """What the user told us up front. Used to guide the AI and to cross-check results."""

    overall_width: float | None = Field(default=None, gt=0.5, le=500.0)
    overall_depth: float | None = Field(default=None, gt=0.5, le=500.0)
    wall_height: float = Field(default=2.7, ge=1.8, le=12.0)
    style: StyleId = "modern"
    prompt: str = Field(default="", max_length=4000)
    known_rooms: list["KnownRoom"] = Field(default_factory=list, max_length=100)


class KnownRoom(_Strict):
    name: str = Field(min_length=1, max_length=80)
    width: float = Field(gt=0.3, le=200.0)
    depth: float = Field(gt=0.3, le=200.0)
    # "centreline" dimensions match room polygons; "clear" = interior wall face to wall face
    basis: Literal["clear", "centreline"] = "clear"


class ProjectMeta(_Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    style: StyleId = "modern"
    prompt: str = Field(default="", max_length=4000)


class Level(_Strict):
    id: Id
    name: str = Field(min_length=1, max_length=80)
    elevation: float = Field(default=0.0, ge=-50.0, le=500.0, description="absolute floor finish elevation")
    wall_height: float = Field(default=2.7, ge=1.8, le=12.0)
    slab_thickness: float = Field(default=0.2, ge=0.05, le=1.0)
    ceilings: bool = True
    certainty: Certainty = Field(default_factory=Certainty)


class RoomFinishes(_Strict):
    """Surface materials facing this room. Walls can carry two horizontal bands:
    ``dado`` from the floor up to ``dado_height`` and ``frieze`` from ``frieze_from`` up to the
    top of the wall (heights in metres above the level's floor); ``walls`` covers the rest."""

    floor: Id | None = None
    walls: Id | None = None
    ceiling: Id | None = None
    dado: Id | None = None
    dado_height: float = Field(default=1.1, ge=0.05, le=6.0)
    frieze: Id | None = None
    frieze_from: float = Field(default=2.4, ge=0.3, le=12.0)

    @model_validator(mode="after")
    def _bands_ordered(self) -> "RoomFinishes":
        if self.dado and self.frieze and self.dado_height >= self.frieze_from:
            raise ValueError("finishes: dado_height must be below frieze_from")
        return self


class Room(_Strict):
    id: Id
    name: str = Field(min_length=1, max_length=80)
    type: RoomType = "other"
    level_id: Id
    polygon: list[Vec2] = Field(min_length=3, max_length=256, description="wall centreline polygon")
    floor_offset: float = Field(default=0.0, ge=-1.0, le=1.0)
    ceiling_height: float | None = Field(default=None, ge=1.8, le=12.0)
    finishes: RoomFinishes = Field(default_factory=RoomFinishes)
    certainty: Certainty = Field(default_factory=Certainty)


class Wall(_Strict):
    id: Id
    level_id: Id
    start: Vec2
    end: Vec2
    thickness: float = Field(ge=0.05, le=1.0)
    height: float | None = Field(default=None, ge=0.3, le=12.0, description="None = level wall height")
    kind: Literal["exterior", "interior"] = "interior"
    material: Id | None = None
    certainty: Certainty = Field(default_factory=Certainty)

    @model_validator(mode="after")
    def _non_degenerate(self) -> "Wall":
        dx, dy = self.end[0] - self.start[0], self.end[1] - self.start[1]
        if (dx * dx + dy * dy) ** 0.5 < 0.05:
            raise ValueError(f"wall {self.id}: start and end are closer than 5 cm")
        return self


class Opening(_Strict):
    id: Id
    kind: Literal["door", "window", "opening"]
    wall_id: Id
    offset: float = Field(ge=0.0, le=1000.0, description="centre distance from wall start along the wall")
    width: float = Field(ge=0.2, le=10.0)
    height: float = Field(ge=0.2, le=10.0)
    sill_height: float = Field(default=0.0, ge=0.0, le=10.0)
    hinge_side: Literal["start", "end"] = "start"
    opens_into: Id | None = Field(default=None, description="room the door leaf swings into")
    certainty: Certainty = Field(default_factory=Certainty)

    @model_validator(mode="after")
    def _door_on_floor(self) -> "Opening":
        if self.kind == "door" and self.sill_height > 0.05:
            raise ValueError(f"opening {self.id}: doors must start at floor level (sill_height <= 0.05)")
        return self


class FurnitureItem(_Strict):
    id: Id
    asset_id: Id
    room_id: Id
    position: Vec2
    elevation: float = Field(default=0.0, ge=0.0, le=5.0)
    rotation_deg: float = Field(default=0.0, ge=-720.0, le=720.0)
    size: Size3 | None = Field(default=None, description="None = catalog nominal size")
    materials: dict[MaterialRole, Id] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=80)
    certainty: Certainty = Field(default_factory=Certainty)

    @model_validator(mode="before")
    @classmethod
    def _catalog_elevation(cls, data):
        """An omitted elevation defaults to the asset's mounting height (wall cabinet, hood, countertop
        appliance). Saved scenes always carry an explicit elevation, so this only affects new items."""
        if isinstance(data, dict) and data.get("elevation") is None and isinstance(data.get("asset_id"), str):
            from twin.assets.catalog import load_catalog  # local: keep the model importable on its own
            asset = load_catalog().get(data["asset_id"])
            if asset is not None and asset.default_elevation:
                data = {**data, "elevation": asset.default_elevation}
        return data


class Material(_Strict):
    id: Id
    name: str = Field(min_length=1, max_length=80)
    base_color: HexColor
    roughness: float = Field(default=0.6, ge=0.0, le=1.0)
    metallic: float = Field(default=0.0, ge=0.0, le=1.0)
    pattern: Pattern = "none"
    tile_size: float = Field(default=1.0, gt=0.02, le=20.0, description="metres covered by one texture tile")
    opacity: float = Field(default=1.0, ge=0.05, le=1.0)
    emission: float = Field(default=0.0, ge=0.0, le=50.0)
    palette: list[HexColor] = Field(default_factory=list, max_length=8,
                                    description="extra colours mixed with base_color by multi-colour patterns")
    joint_color: HexColor | None = Field(default=None, description="grout, joints or perforations; None = derived")


class Lighting(_Strict):
    mood: Literal["warm", "neutral", "cool", "daylight"] = "neutral"
    ceiling_lights: bool = True
    sun_azimuth_deg: float = Field(default=135.0, ge=-360.0, le=360.0, description="CCW from +X (east)")
    sun_elevation_deg: float = Field(default=40.0, ge=1.0, le=90.0)
    sun_strength: float = Field(default=3.0, ge=0.0, le=20.0)


class OpenQuestion(_Strict):
    id: Id
    target: str = Field(default="", max_length=120, description="e.g. 'room:R2' or 'wall:W3'")
    question: str = Field(min_length=1, max_length=600)
    severity: Literal["info", "warning", "blocking"] = "warning"
    resolved: bool = False
    answer: str = Field(default="", max_length=600)


HvacUnitKind = Literal["fcu_ducted", "fcu_exposed", "fcu_cassette", "ahu", "split_wall", "exhaust_fan", "other"]
TerminalKind = Literal["supply_diffuser", "supply_grille", "return_grille", "exhaust_grille", "linear_slot"]
DuctSystem = Literal["supply", "return", "exhaust", "fresh_air"]


class HvacUnit(_Strict):
    """An air-handling unit (e.g. a ducted fan coil above the ceiling). ``position`` is the centre of its
    footprint; ``rotation_deg`` turns its local +X (the long axis) CCW from +X. ``elevation`` is the bottom of
    the casing above the level floor; None = ducted units hang in the ceiling void just above the room's
    ceiling, exposed/cassette units sit just below it. A unit without supply terminals discharges directly
    into the room."""

    id: Id
    tag: str = Field(min_length=1, max_length=40, description="tag printed on the drawing, e.g. '1FC-23'")
    kind: HvacUnitKind = "fcu_ducted"
    level_id: Id
    room_id: Id | None = None
    position: Vec2
    rotation_deg: float = Field(default=0.0, ge=-720.0, le=720.0)
    size: Size3 = Field(default_factory=lambda: Size3(width=1.2, depth=0.7, height=0.35))
    elevation: float | None = Field(default=None, ge=0.0, le=12.0)
    airflow_lps: float | None = Field(default=None, gt=0.0, le=20000.0, description="design supply airflow, L/s")
    cooling_kw: float | None = Field(default=None, gt=0.0, le=2000.0)
    certainty: Certainty = Field(default_factory=Certainty)


class HvacTerminal(_Strict):
    """A ceiling diffuser or grille. ``width``/``depth`` are the face size; the face sits in the ceiling
    plane (``elevation`` None) or at an explicit height above the level floor."""

    id: Id
    kind: TerminalKind
    level_id: Id
    room_id: Id | None = None
    position: Vec2
    rotation_deg: float = Field(default=0.0, ge=-720.0, le=720.0)
    width: float = Field(default=0.6, gt=0.05, le=4.0)
    depth: float = Field(default=0.6, gt=0.02, le=4.0)
    elevation: float | None = Field(default=None, ge=0.0, le=12.0)
    airflow_lps: float | None = Field(default=None, gt=0.0, le=5000.0)
    throw: Literal["4way", "2way", "1way", "down", "none"] = "4way"
    unit_id: Id | None = Field(default=None, description="unit that feeds (supply) or serves (return) it")
    label: str = Field(default="", max_length=80)
    certainty: Certainty = Field(default_factory=Certainty)


class Duct(_Strict):
    """Rectangular duct run along ``path`` (centreline, plan coordinates). ``elevation`` is the duct bottom
    above the level floor; None = in the ceiling void above the ceiling of the room it crosses."""

    id: Id
    system: DuctSystem = "supply"
    level_id: Id
    path: list[Vec2] = Field(min_length=2, max_length=64)
    width: float = Field(gt=0.05, le=3.0)
    width_end: float | None = Field(default=None, gt=0.05, le=3.0,
                                    description="tapered transition: width at the path end (2-point paths only)")
    height: float = Field(default=0.25, gt=0.05, le=2.0)
    elevation: float | None = Field(default=None, ge=0.0, le=12.0)
    unit_id: Id | None = None
    label: str = Field(default="", max_length=80)
    certainty: Certainty = Field(default_factory=Certainty)

    @model_validator(mode="after")
    def _non_degenerate(self) -> "Duct":
        length = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(self.path, self.path[1:]))
        if length < 0.05:
            raise ValueError(f"duct {self.id}: path is shorter than 5 cm")
        if self.width_end is not None and len(self.path) != 2:
            raise ValueError(f"duct {self.id}: a tapered transition (width_end) needs a 2-point path")
        return self


class HvacSource(_Strict):
    """How the HVAC layer was obtained (drawing registration and extraction statistics)."""

    file_id: str | None = None
    file_name: str = Field(default="", max_length=200)
    method: str = Field(default="", max_length=200)
    transform: dict[str, float] = Field(default_factory=dict, description="sheet pt -> plan frame metres")
    residual_m: float | None = None
    note: str = Field(default="", max_length=1000)


class Hvac(_Strict):
    units: list[HvacUnit] = Field(default_factory=list, max_length=1000)
    terminals: list[HvacTerminal] = Field(default_factory=list, max_length=4000)
    ducts: list[Duct] = Field(default_factory=list, max_length=4000)
    plenum_clearance: float = Field(default=0.08, ge=0.0, le=2.0,
                                    description="gap between the ceiling and units/ducts placed in the void")
    source: HvacSource = Field(default_factory=HvacSource)


SensorKind = Literal["temperature", "humidity", "temp_humidity", "co2", "pressure", "occupancy", "airflow",
                     "energy", "other"]
SensorMounting = Literal["wall", "ceiling", "free"]


class Sensor(_Strict):
    """A monitoring point added to the twin (often after the building was generated). ``position`` is the
    sensor's plan location; ``elevation`` is its centre height above the level floor (None = the mounting
    default: 1.5 m on a wall or free-standing, just under the room ceiling when ceiling-mounted).
    ``unit_id`` links it to the HVAC unit it monitors and ``bms_point`` to the live data feed."""

    id: Id
    tag: str = Field(min_length=1, max_length=40, description="tag on site / in the BMS, e.g. 'TS-01'")
    kind: SensorKind = "temperature"
    level_id: Id
    room_id: Id | None = None
    position: Vec2
    elevation: float | None = Field(default=None, ge=0.0, le=12.0)
    mounting: SensorMounting = "wall"
    unit_id: Id | None = Field(default=None, description="HVAC unit this sensor monitors")
    bms_point: str = Field(default="", max_length=120, description="BMS / IoT point name of the live reading")
    label: str = Field(default="", max_length=80)
    certainty: Certainty = Field(default_factory=Certainty)


class Provenance(_Strict):
    source: Source = "manual"
    ai_model: str | None = None
    ai_request_id: str | None = None
    based_on_version: int | None = None
    note: str = Field(default="", max_length=1000)


class Scene(_Strict):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    units: Literal["m"] = "m"
    coordinate_system: Literal[
        "metres; right-handed; +X east, +Y north, +Z up; wall centrelines; angles CCW deg from +X"
    ] = COORDINATE_SYSTEM
    project: ProjectMeta
    levels: list[Level] = Field(min_length=1, max_length=20)
    rooms: list[Room] = Field(default_factory=list, max_length=500)
    walls: list[Wall] = Field(default_factory=list, max_length=4000)
    openings: list[Opening] = Field(default_factory=list, max_length=4000)
    furniture: list[FurnitureItem] = Field(default_factory=list, max_length=4000)
    materials: list[Material] = Field(default_factory=list, max_length=500)
    lighting: Lighting = Field(default_factory=Lighting)
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=200)
    provenance: Provenance = Field(default_factory=Provenance)
    hvac: Hvac = Field(default_factory=Hvac)
    sensors: list[Sensor] = Field(default_factory=list, max_length=4000)

    @model_validator(mode="after")
    def _references(self) -> "Scene":
        errors: list[str] = []

        def unique(kind: str, ids: list[str]) -> set[str]:
            seen: set[str] = set()
            for i in ids:
                if i in seen:
                    errors.append(f"duplicate {kind} id '{i}'")
                seen.add(i)
            return seen

        level_ids = unique("level", [l.id for l in self.levels])
        room_ids = unique("room", [r.id for r in self.rooms])
        wall_ids = unique("wall", [w.id for w in self.walls])
        unique("opening", [o.id for o in self.openings])
        unique("furniture", [f.id for f in self.furniture])
        unique("material", [m.id for m in self.materials])
        unique("question", [q.id for q in self.open_questions])

        for r in self.rooms:
            if r.level_id not in level_ids:
                errors.append(f"room {r.id}: unknown level '{r.level_id}'")
        for w in self.walls:
            if w.level_id not in level_ids:
                errors.append(f"wall {w.id}: unknown level '{w.level_id}'")
        for o in self.openings:
            if o.wall_id not in wall_ids:
                errors.append(f"opening {o.id}: unknown wall '{o.wall_id}'")
            if o.opens_into is not None and o.opens_into not in room_ids:
                errors.append(f"opening {o.id}: opens_into unknown room '{o.opens_into}'")
        for f in self.furniture:
            if f.room_id not in room_ids:
                errors.append(f"furniture {f.id}: unknown room '{f.room_id}'")
        hv = self.hvac
        unit_ids = unique("hvac unit", [u.id for u in hv.units])
        unique("hvac terminal", [t.id for t in hv.terminals])
        unique("duct", [d.id for d in hv.ducts])
        for kind, items in (("hvac unit", hv.units), ("hvac terminal", hv.terminals), ("duct", hv.ducts)):
            for it in items:
                if it.level_id not in level_ids:
                    errors.append(f"{kind} {it.id}: unknown level '{it.level_id}'")
                if getattr(it, "room_id", None) is not None and it.room_id not in room_ids:
                    errors.append(f"{kind} {it.id}: unknown room '{it.room_id}'")
                if getattr(it, "unit_id", None) is not None and it.unit_id not in unit_ids:
                    errors.append(f"{kind} {it.id}: unknown unit '{it.unit_id}'")
        unique("sensor", [s.id for s in self.sensors])
        for s in self.sensors:
            if s.level_id not in level_ids:
                errors.append(f"sensor {s.id}: unknown level '{s.level_id}'")
            if s.room_id is not None and s.room_id not in room_ids:
                errors.append(f"sensor {s.id}: unknown room '{s.room_id}'")
            if s.unit_id is not None and s.unit_id not in unit_ids:
                errors.append(f"sensor {s.id}: unknown unit '{s.unit_id}'")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    # convenience lookups (not serialised)
    def level(self, level_id: str) -> Level:
        return next(l for l in self.levels if l.id == level_id)

    def room(self, room_id: str) -> Room:
        return next(r for r in self.rooms if r.id == room_id)

    def wall(self, wall_id: str) -> Wall:
        return next(w for w in self.walls if w.id == wall_id)

    def wall_height(self, wall: Wall) -> float:
        return wall.height if wall.height is not None else self.level(wall.level_id).wall_height


Brief.model_rebuild()


def scene_json_schema() -> dict:
    """JSON Schema of the scene model (published at /api/schema/scene and in docs)."""
    return Scene.model_json_schema()
