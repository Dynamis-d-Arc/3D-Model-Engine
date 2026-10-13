"""Structured output contract for the AI interpretation step.

The model must answer with JSON matching ``interpretation_json_schema()`` (sent as a strict
json_schema response format). Every response is then re-validated with the pydantic models
below, including value ranges and cross-references, before anything uses it. The AI output
is an *interpretation*; ``twin.ai.convert`` turns it into a scene-model draft for review.
"""

from __future__ import annotations

import copy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from twin.assets.catalog import load_catalog
from twin.scene.model import MaterialRole, Pattern, RoomType

Num = Annotated[float, Field(allow_inf_nan=False)]
Coord = Annotated[float, Field(ge=-500.0, le=500.0, allow_inf_nan=False)]
Conf = Annotated[float, Field(ge=0.0, le=1.0)]
Hex = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Key = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")]
DimSource = Literal["user_provided", "plan_label", "scaled_from_drawing", "estimated"]

AssetId = Literal[tuple(load_catalog().ids())]  # type: ignore[valid-type]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XY(_S):
    x: Coord
    y: Coord


class AIRoom(_S):
    key: Key
    name: Annotated[str, Field(min_length=1, max_length=80)]
    type: RoomType
    polygon: Annotated[list[XY], Field(min_length=3, max_length=64)]
    dimension_source: DimSource
    confidence: Conf
    notes: Annotated[str, Field(max_length=400)]


class AIWall(_S):
    key: Key
    start: XY
    end: XY
    thickness_m: Annotated[float, Field(ge=0.05, le=1.0)]
    exterior: bool
    source: DimSource
    confidence: Conf


class AIOpening(_S):
    key: Key
    kind: Literal["door", "window", "opening"]
    wall_key: Key
    center: XY
    width_m: Annotated[float, Field(ge=0.2, le=10.0)]
    height_m: Annotated[float, Field(ge=0.2, le=10.0)]
    sill_height_m: Annotated[float, Field(ge=0.0, le=10.0)]
    hinge_side: Literal["start", "end", "unknown"]
    opens_into_room_key: Key | None
    source: DimSource
    confidence: Conf


class AIFurniture(_S):
    key: Key
    asset_id: AssetId
    room_key: Key
    center: XY
    rotation_deg: Annotated[float, Field(ge=-360.0, le=360.0)]
    width_m: Annotated[float, Field(gt=0.01, le=20.0)] | None
    depth_m: Annotated[float, Field(gt=0.005, le=20.0)] | None
    height_m: Annotated[float, Field(gt=0.005, le=10.0)] | None
    source: Literal["user_requested", "plan_symbol", "ai_suggested"]
    confidence: Conf


class AISurfaceMaterial(_S):
    target: Literal["floor", "walls", "ceiling"]
    room_key: Key | None
    description: Annotated[str, Field(max_length=120)]
    base_color_hex: Hex
    roughness: Conf
    metallic: Conf
    pattern: Pattern


class AIRoleMaterial(_S):
    role: MaterialRole
    description: Annotated[str, Field(max_length=120)]
    base_color_hex: Hex
    roughness: Conf
    metallic: Conf
    pattern: Pattern


class AIUncertainty(_S):
    target: Annotated[str, Field(max_length=80)]
    question: Annotated[str, Field(min_length=1, max_length=500)]
    severity: Literal["info", "warning", "blocking"]


class AIInterpretation(_S):
    plan_readable: bool
    unreadable_reason: Annotated[str, Field(max_length=500)] | None
    detected_units: Literal["m", "cm", "mm", "ft_in", "none"]
    scale_basis: Annotated[str, Field(max_length=400)]
    wall_height_m: Annotated[float, Field(ge=1.8, le=12.0)] | None
    wall_height_source: DimSource
    rooms: Annotated[list[AIRoom], Field(max_length=80)]
    walls: Annotated[list[AIWall], Field(max_length=400)]
    openings: Annotated[list[AIOpening], Field(max_length=400)]
    furniture: Annotated[list[AIFurniture], Field(max_length=400)]
    surface_materials: Annotated[list[AISurfaceMaterial], Field(max_length=120)]
    furniture_materials: Annotated[list[AIRoleMaterial], Field(max_length=40)]
    lighting_mood: Literal["warm", "neutral", "cool", "daylight"]
    uncertainties: Annotated[list[AIUncertainty], Field(max_length=60)]
    summary: Annotated[str, Field(max_length=2000)]

    @model_validator(mode="after")
    def _cross_refs(self) -> "AIInterpretation":
        errs: list[str] = []
        for coll, items in (("room", self.rooms), ("wall", self.walls), ("opening", self.openings),
                            ("furniture", self.furniture)):
            keys = [i.key for i in items]
            dup = {k for k in keys if keys.count(k) > 1}
            if dup:
                errs.append(f"duplicate {coll} keys {sorted(dup)}")
        rooms = {r.key for r in self.rooms}
        walls = {w.key for w in self.walls}
        for o in self.openings:
            if o.wall_key not in walls:
                errs.append(f"opening {o.key} references unknown wall '{o.wall_key}'")
            if o.opens_into_room_key is not None and o.opens_into_room_key not in rooms:
                errs.append(f"opening {o.key} opens into unknown room '{o.opens_into_room_key}'")
        for f in self.furniture:
            if f.room_key not in rooms:
                errs.append(f"furniture {f.key} references unknown room '{f.room_key}'")
        for m in self.surface_materials:
            if m.room_key is not None and m.room_key not in rooms:
                errs.append(f"material for {m.target} references unknown room '{m.room_key}'")
        if self.plan_readable and not self.rooms:
            errs.append("plan_readable is true but no rooms were returned")
        if errs:
            raise ValueError("; ".join(errs))
        return self


class AIRoomDesign(_S):
    """Answer format for 'redesign one room' requests (furniture + finishes only)."""
    furniture: Annotated[list[AIFurniture], Field(max_length=60)]
    surface_materials: Annotated[list[AISurfaceMaterial], Field(max_length=6)]
    furniture_materials: Annotated[list[AIRoleMaterial], Field(max_length=20)]
    uncertainties: Annotated[list[AIUncertainty], Field(max_length=20)]
    summary: Annotated[str, Field(max_length=1000)]


# ------------------------------------------------------------------------------------------
# prose length normalisation (never applied to geometry, ids, enums or numbers)
# ------------------------------------------------------------------------------------------

# Free-text fields and their limits. The provider schema cannot carry maxLength, so an overlong note
# is cut deterministically (and reported) instead of discarding a whole, otherwise valid answer.
FREE_TEXT_LIMITS: dict[tuple[str, ...], int] = {
    ("unreadable_reason",): 500, ("scale_basis",): 400, ("summary",): 2000,
    ("rooms", "*", "name"): 80, ("rooms", "*", "notes"): 400,
    ("surface_materials", "*", "description"): 120, ("furniture_materials", "*", "description"): 120,
    ("uncertainties", "*", "target"): 80, ("uncertainties", "*", "question"): 500,
}


def trim_free_text(data: dict, limits: dict[tuple[str, ...], int] = FREE_TEXT_LIMITS) -> list[str]:
    """Shorten over-long prose fields in place; returns a note per change."""
    notes: list[str] = []

    def visit(node, path, rest, limit):
        if not rest:
            return
        key, tail = rest[0], rest[1:]
        if key == "*":
            if isinstance(node, list):
                for i, item in enumerate(node):
                    visit(item, path + [str(i)], tail, limit)
            return
        if not isinstance(node, dict) or key not in node:
            return
        if not tail:
            value = node[key]
            if isinstance(value, str) and len(value) > limit:
                node[key] = value[:limit - 1].rstrip() + "…"
                notes.append(f"{'.'.join(path + [key])}: text shortened from {len(value)} to {limit} characters")
            return
        visit(node[key], path + [key], tail, limit)

    for spec, limit in limits.items():
        visit(data, [], list(spec), limit)
    return notes


ROOM_DESIGN_TEXT_LIMITS: dict[tuple[str, ...], int] = {
    ("summary",): 1000, ("surface_materials", "*", "description"): 120,
    ("furniture_materials", "*", "description"): 120, ("uncertainties", "*", "target"): 80,
    ("uncertainties", "*", "question"): 500,
}


# ------------------------------------------------------------------------------------------
# strict JSON schema for the provider
# ------------------------------------------------------------------------------------------

_DROP = {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
         "maxLength", "minItems", "maxItems", "pattern"}


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> OpenAI strict structured-output schema.

    Strict mode requires every property to be required (nullable via anyOf/null) and
    additionalProperties: false everywhere. Range/length/pattern keywords are dropped for
    provider compatibility and enforced by pydantic after the response arrives.
    """
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node, name_map: bool = False):
        if isinstance(node, list):
            for v in node:
                walk(v)
            return
        if not isinstance(node, dict):
            return
        if name_map:  # keys are property / definition names, not schema keywords
            for v in node.values():
                walk(v)
            return
        for k in list(node):
            if k in _DROP:
                del node[k]
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}).keys())
        for k, v in node.items():
            walk(v, name_map=k in ("properties", "$defs"))

    walk(schema)
    return schema


def interpretation_json_schema() -> dict:
    return strict_schema(AIInterpretation)


def room_design_json_schema() -> dict:
    return strict_schema(AIRoomDesign)
