"""Turn a validated AI interpretation into a scene-model DRAFT for human review.

Nothing is silently invented or fixed: estimated values keep an 'estimated' status,
plan-label readings are 'extracted' (to be confirmed), user-provided values are only
'confirmed' when they actually match the brief, and anything that cannot be mapped (e.g. a
door that is not on any wall) becomes an open question instead of being moved.
"""

from __future__ import annotations

import math

from twin.ai.schema import AIFurniture, AIInterpretation, AIRoleMaterial, AISurfaceMaterial
from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.model import (Brief, Certainty, FurnitureItem, Level, Lighting, Material, OpenQuestion, Opening,
                              ProjectMeta, Provenance, Room, RoomFinishes, Scene, Size3, Wall)

OPENING_SNAP = 0.25  # metres beyond half the wall thickness an opening centre may sit


def _status(source: str) -> str:
    return {"user_provided": "confirmed", "plan_label": "extracted", "scaled_from_drawing": "estimated",
            "estimated": "estimated"}[source]


def _round(v: float) -> float:
    return round(v, 3)


def _ccw(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return points if g.signed_area(points) >= 0 else list(reversed(points))


def _known_match(brief: Brief, name: str, w: float, d: float) -> bool | None:
    """True/False when the brief has dimensions for this room, None if it doesn't."""
    k = next((k for k in brief.known_rooms if k.name.strip().lower() == name.strip().lower()), None)
    if k is None:
        return None
    if k.basis == "clear":  # compare roughly: centreline dims are about one wall thickness larger
        w, d = w - 0.15, d - 0.15
    return (abs(w - k.width) <= 0.1 and abs(d - k.depth) <= 0.1) or (abs(w - k.depth) <= 0.1 and abs(d - k.width) <= 0.1)


def furniture_from_ai(items: list[AIFurniture], room_ids: dict[str, str], start: int,
                      role_overrides: dict[str, str]) -> list[FurnitureItem]:
    catalog = load_catalog()
    out = []
    for n, f in enumerate(items, start=start):
        asset = catalog.get(f.asset_id)
        dims = (f.width_m, f.depth_m, f.height_m)
        size = None
        if any(v is not None for v in dims):
            size = Size3(width=dims[0] or asset.size[0], depth=dims[1] or asset.size[1], height=dims[2] or asset.size[2])
        out.append(FurnitureItem(
            id=f"F{n}", asset_id=f.asset_id, room_id=room_ids[f.room_key],
            position=(_round(f.center.x), _round(f.center.y)), rotation_deg=round(f.rotation_deg % 360.0, 2),
            size=size, materials={r: m for r, m in role_overrides.items() if r in asset.roles},
            certainty=Certainty(status="confirmed" if f.source == "user_requested" else "estimated",
                                source="ai", confidence=f.confidence, note=f.source)))
    return out


def materials_from_ai(surfaces: list[AISurfaceMaterial], role_materials: list[AIRoleMaterial], room_ids: dict[str, str],
                      prefix: str = "ai") -> tuple[list[Material], dict[str, dict[str, str]], dict[str, str]]:
    """Returns (materials, room finishes {room_id: {target: material_id}}, role overrides {role: material_id})."""
    mats: list[Material] = []
    finishes: dict[str, dict[str, str]] = {}
    for m in surfaces:
        scope = room_ids.get(m.room_key, "all") if m.room_key else "all"
        mid = f"{prefix}_{m.target}_{scope}"
        mats.append(Material(id=mid, name=(m.description or f"{m.target} finish")[:80], base_color=m.base_color_hex.upper(),
                             roughness=m.roughness, metallic=m.metallic, pattern=m.pattern,
                             tile_size=1.2 if m.pattern == "tile" else (1.0 if m.pattern == "brick" else 2.0)))
        targets = list(room_ids.values()) if scope == "all" else [scope]
        for rid in targets:
            if scope == "all" and m.target in finishes.get(rid, {}):
                continue  # a room-specific choice wins over a global one
            finishes.setdefault(rid, {})[m.target] = mid
    roles: dict[str, str] = {}
    for r in roles_list(role_materials):
        mid = f"{prefix}_role_{r.role}"
        mats.append(Material(id=mid, name=(r.description or r.role)[:80], base_color=r.base_color_hex.upper(),
                             roughness=r.roughness, metallic=r.metallic, pattern=r.pattern,
                             tile_size=0.5 if r.pattern == "fabric" else 1.0, opacity=0.25 if r.role == "glass" else 1.0))
        roles[r.role] = mid
    # dedupe ids (last wins) while keeping order stable
    seen: dict[str, Material] = {}
    for m in mats:
        seen[m.id] = m
    return list(seen.values()), finishes, roles


def roles_list(roles: list[AIRoleMaterial]) -> list[AIRoleMaterial]:
    by_role: dict[str, AIRoleMaterial] = {}
    for r in roles:
        by_role[r.role] = r
    return list(by_role.values())


def interpretation_to_scene(ai: AIInterpretation, brief: Brief, project_name: str, *, model: str,
                            request_id: str | None) -> tuple[Scene, list[str]]:
    notes: list[str] = []
    questions: list[OpenQuestion] = []
    room_ids = {r.key: f"R{i}" for i, r in enumerate(ai.rooms, start=1)}
    wall_ids = {w.key: f"W{i}" for i, w in enumerate(ai.walls, start=1)}

    rooms: list[Room] = []
    for r in ai.rooms:
        pts = _ccw([(_round(p.x), _round(p.y)) for p in r.polygon])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        status = _status(r.dimension_source)
        if r.dimension_source == "user_provided":
            match = _known_match(brief, r.name, max(xs) - min(xs), max(ys) - min(ys))
            if match is not True:
                status = "extracted"
                notes.append(f"room '{r.name}': AI says user-provided but it does not match the brief; marked for review")
        rooms.append(Room(id=room_ids[r.key], name=r.name, type=r.type, level_id="L0", polygon=pts,
                          certainty=Certainty(status=status, source="ai", confidence=r.confidence,
                                              note=r.notes[:500])))

    walls: list[Wall] = []
    for w in ai.walls:
        start, end = (_round(w.start.x), _round(w.start.y)), (_round(w.end.x), _round(w.end.y))
        if math.dist(start, end) < 0.05:
            notes.append(f"wall {w.key} is shorter than 5 cm and was dropped")
            continue
        walls.append(Wall(id=wall_ids[w.key], level_id="L0", start=start, end=end, thickness=round(w.thickness_m, 3),
                          kind="exterior" if w.exterior else "interior",
                          certainty=Certainty(status=_status(w.source), source="ai", confidence=w.confidence)))
    by_id = {w.id: w for w in walls}

    openings: list[Opening] = []
    for n, o in enumerate(ai.openings, start=1):
        wall = by_id.get(wall_ids.get(o.wall_key, ""))
        qid = f"Q{len(questions) + 1}"
        if wall is None:
            questions.append(OpenQuestion(id=qid, target=f"opening:{o.key}", severity="warning",
                                          question=f"The AI placed a {o.kind} ({o.width_m:.2f} m) on a wall that was "
                                                   f"dropped. Add it manually if it exists."))
            continue
        f = g.wall_frame(wall)
        s, d = f.to_local(o.center.x, o.center.y)
        if abs(d) > wall.thickness / 2 + OPENING_SNAP or not (0 <= s <= f.length):
            questions.append(OpenQuestion(id=qid, target=f"wall:{wall.id}", severity="warning",
                                          question=f"The AI placed a {o.kind} at ({o.center.x:.2f}, {o.center.y:.2f}), "
                                                   f"{abs(d):.2f} m away from wall {wall.id}. It was not added; "
                                                   f"please place it manually."))
            continue
        hinge = "start" if o.hinge_side == "unknown" else o.hinge_side
        sill = 0.0 if o.kind == "door" else o.sill_height_m
        openings.append(Opening(id=f"O{n}", kind=o.kind, wall_id=wall.id, offset=round(s, 3), width=round(o.width_m, 3),
                                height=round(o.height_m, 3), sill_height=round(sill, 3), hinge_side=hinge,
                                opens_into=room_ids.get(o.opens_into_room_key) if o.opens_into_room_key else None,
                                certainty=Certainty(status=_status(o.source), source="ai", confidence=o.confidence)))

    mats, finishes, roles = materials_from_ai(ai.surface_materials, ai.furniture_materials, room_ids)
    rooms = [r.model_copy(update={"finishes": RoomFinishes(**finishes.get(r.id, {}))}) for r in rooms]
    furniture = furniture_from_ai(ai.furniture, room_ids, 1, roles)

    level_status = "confirmed"  # the brief's wall height is user input
    if ai.wall_height_m is not None and abs(ai.wall_height_m - brief.wall_height) > 0.05:
        questions.append(OpenQuestion(id=f"Q{len(questions) + 1}", target="level:L0", severity="warning",
                                      question=f"The plan suggests a wall height of {ai.wall_height_m:.2f} m "
                                               f"({ai.wall_height_source}) but the brief says {brief.wall_height:.2f} m. "
                                               "Which is correct?"))
    for u in ai.uncertainties:
        questions.append(OpenQuestion(id=f"Q{len(questions) + 1}", target=u.target[:120], question=u.question,
                                      severity=u.severity))

    scene = Scene(
        project=ProjectMeta(name=project_name, style=brief.style, prompt=brief.prompt,
                            description=ai.summary[:2000]),
        levels=[Level(id="L0", name="Ground floor", wall_height=brief.wall_height,
                      certainty=Certainty(status=level_status, source="user"))],
        rooms=rooms, walls=walls, openings=openings, furniture=furniture, materials=mats,
        lighting=Lighting(mood=ai.lighting_mood), open_questions=questions,
        provenance=Provenance(source="ai", ai_model=model, ai_request_id=request_id,
                              note=f"scale basis: {ai.scale_basis[:300]}; units: {ai.detected_units}"),
    )
    return scene, notes
