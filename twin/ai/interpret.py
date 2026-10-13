"""AI interpretation service: plan image + brief -> validated interpretation -> scene draft.

Separation of concerns: this module only produces DATA (a scene-model draft with
certainty flags and open questions). Geometry is computed later, deterministically, by
twin.scene.compile from the reviewed scene.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from twin.ai.client import AIClient, AIError, AIResult, ContentPart
from twin.ai.convert import furniture_from_ai, interpretation_to_scene, materials_from_ai
from twin.ai.prompts import SYSTEM_INTERPRET, SYSTEM_REDESIGN, brief_text, catalog_table
from twin.ai.schema import (ROOM_DESIGN_TEXT_LIMITS, AIInterpretation, AIRoomDesign, interpretation_json_schema,
                            room_design_json_schema, trim_free_text)
from twin.imaging.pdf_text import labels_in_metres
from twin.imaging.preprocess import image_data_url, pil_data_url
from twin.scene import geometry as g
from twin.scene.diff import apply_room_update
from twin.scene.model import Brief, RoomFinishes, Scene

PLAN_MAX_SIDE = 2048
REFERENCE_MAX_SIDE = 1024
MAX_REFERENCES = 4


@dataclass
class InterpretOutcome:
    scene: Scene
    interpretation: dict
    result: AIResult
    notes: list[str] = field(default_factory=list)


def schema_errors(exc: ValidationError, limit: int = 15) -> list[str]:
    out = []
    for e in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in e.get("loc", ()))
        out.append(f"{loc or '(root)'}: {e.get('msg')}")
    return out


REGION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["found", "x0", "y0", "x1", "y1", "note"],
    "properties": {"found": {"type": "boolean"}, "x0": {"type": "number"}, "y0": {"type": "number"},
                   "x1": {"type": "number"}, "y1": {"type": "number"}, "note": {"type": "string"}},
}
MAX_SHEET_LABELS = 900


def locate_plan_region(client: AIClient, plan_image: Path) -> tuple[float, float, float, float] | None:
    """Ask the model where the floor plan drawing is on a drawing sheet (cheap, small output)."""
    url, _ = image_data_url(plan_image, 1536)
    res = client.structured(
        system="You locate the main floor plan drawing on an architectural drawing sheet.",
        parts=[ContentPart("text", "Return the bounding box of the main floor plan drawing (its walls, rooms, grid lines "
                                   "and dimension lines) as fractions of the image width and height (0..1, origin at the "
                                   "top-left). Exclude the title block, revision table, legends, notes and other "
                                   "drawings. Set found=false if there is no floor plan."),
               ContentPart("image", image_url=url, detail="high")],
        schema_name="plan_region", schema=REGION_SCHEMA, max_output_tokens=2000)
    d = res.data
    try:
        x0, y0, x1, y1 = (float(d[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    if not d.get("found") or not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1) or (x1 - x0) * (y1 - y0) < 0.05:
        return None
    pad = 0.02  # keep a margin: the model's box is approximate
    return (max(0.0, x0 - pad), max(0.0, y0 - pad), min(1.0, x1 + pad), min(1.0, y1 + pad))


def plan_sheet_parts(client: AIClient, plan_image: Path, sheet: dict, notes: list[str]) -> list[ContentPart]:
    """Vector PDF with a known scale: crop to the plan and give exact label positions in metres."""
    from PIL import Image
    img = Image.open(plan_image)
    W, H = img.size
    k = sheet["page_w_pt"] * sheet["m_per_pt"] / W            # metres per normalised-image pixel
    region = locate_plan_region(client, plan_image)
    if region is None:
        notes.append("could not locate the plan on the sheet; the whole sheet was sent")
        region = (0.0, 0.0, 1.0, 1.0)
    px0, py0, px1, py1 = int(region[0] * W), int(region[1] * H), int(region[2] * W), int(region[3] * H)
    crop = img.crop((px0, py0, px1, py1))
    url, size = pil_data_url(crop, PLAN_MAX_SIDE)
    k_sent = k * crop.width / size[0]
    x0m, x1m = px0 * k, px1 * k
    y0m, y1m = (H - py1) * k, (H - py0) * k
    labels = labels_in_metres(sheet, (x0m, y0m, x1m, y1m))
    labels.sort(key=lambda lab: (-lab["y"], lab["x"]))
    if len(labels) > MAX_SHEET_LABELS:
        notes.append(f"{len(labels)} text labels in the plan area; only the first {MAX_SHEET_LABELS} were sent")
        labels = labels[:MAX_SHEET_LABELS]
    frame = (f"COORDINATE FRAME FOR THIS PLAN (overrides the origin rule in the instructions): the plan comes from a "
             f"vector PDF drawn at 1:{sheet['scale']}. Use the PLAN FRAME: metres at full scale, origin at the "
             f"bottom-left corner of the drawing sheet, x to the right, y up. The image below is a crop of the sheet "
             f"covering x {x0m:.2f}..{x1m:.2f} m and y {y0m:.2f}..{y1m:.2f} m. It is {size[0]}x{size[1]} px and "
             f"1 px = {k_sent:.4f} m, so image pixel (px, py) is at x = {x0m:.2f} + px*{k_sent:.4f}, "
             f"y = {y1m:.2f} - py*{k_sent:.4f}. Return ALL coordinates in this frame.")
    table = "\n".join(f"{lab['x']:.2f}, {lab['y']:.2f}: {lab['text']}" for lab in labels)
    return [ContentPart("text", frame),
            ContentPart("text", "PDF TEXT LABELS - exact vector text from the PDF (label centre x, y in the plan frame, "
                                "metres: text). Use them to name rooms and place them; areas such as '48 SQM' are the "
                                f"architect's own figures and dimension strings are in metres or mm.\n{table}"),
            ContentPart("text", f"FLOOR PLAN IMAGE (crop of the sheet, {size[0]}x{size[1]} px). Interpret this plan."),
            ContentPart("image", image_url=url, detail="high")]


def build_interpret_parts(brief: Brief, project_name: str, plan_image: Path | None,
                          plan_info: dict | None, references: list[Path],
                          plan_parts: list[ContentPart] | None = None) -> list[ContentPart]:
    parts = [ContentPart("text", brief_text(brief, project_name)),
             ContentPart("text", "FURNITURE CATALOG (the only allowed asset_id values):\n" + catalog_table())]
    if plan_parts:
        parts.extend(plan_parts)
    elif plan_image is not None:
        url, size = image_data_url(plan_image, PLAN_MAX_SIDE)
        info = f" Source: {plan_info.get('kind', 'image').upper()}" if plan_info else ""
        parts.append(ContentPart("text", f"FLOOR PLAN IMAGE ({size[0]}x{size[1]} px).{info} Interpret this plan."))
        parts.append(ContentPart("image", image_url=url, detail="high"))
    else:
        parts.append(ContentPart("text", "No floor plan image was provided. Design a plausible single-floor layout "
                                         "that satisfies the brief; mark every dimension that is not in the brief "
                                         "as 'estimated'."))
    for i, ref in enumerate(references[:MAX_REFERENCES], start=1):
        url, _ = image_data_url(ref, REFERENCE_MAX_SIDE, photo=True)
        parts.append(ContentPart("text", f"STYLE REFERENCE IMAGE {i}: use for materials, colours and furniture "
                                         "style only, never for geometry."))
        parts.append(ContentPart("image", image_url=url, detail="low"))
    return parts


def parse_interpretation(data: dict, notes: list[str] | None = None) -> AIInterpretation:
    trimmed = trim_free_text(data)
    if notes is not None:
        notes.extend(trimmed)
    try:
        return AIInterpretation.model_validate(data)
    except ValidationError as exc:
        raise AIError("AI_SCHEMA_VIOLATION", "the AI response did not match the required schema",
                      detail=schema_errors(exc)) from exc


def interpret_plan(client: AIClient, brief: Brief, project_name: str, plan_image: Path | None,
                   plan_info: dict | None = None, references: list[Path] | None = None,
                   raw_sink=None) -> InterpretOutcome:
    sheet_notes: list[str] = []
    sheet = (plan_info or {}).get("pdf_text")
    plan_parts = None
    if plan_image is not None and sheet and sheet.get("m_per_pt"):
        plan_parts = plan_sheet_parts(client, plan_image, sheet, sheet_notes)
    parts = build_interpret_parts(brief, project_name, plan_image, plan_info, references or [], plan_parts)
    result = client.structured(system=SYSTEM_INTERPRET, parts=parts, schema_name="floor_plan_interpretation",
                               schema=interpretation_json_schema())
    if raw_sink is not None:  # keep the paid answer before validating it
        raw_sink({"model": result.model, "request_id": result.request_id, "usage": result.usage,
                  "data": result.data, "notes": sheet_notes})
    return outcome_from_response(result.data, brief, project_name, result, sheet_notes)


def outcome_from_response(data: dict, brief: Brief, project_name: str, result: AIResult,
                          sheet_notes: list[str] | None = None) -> InterpretOutcome:
    """Validate + convert a (possibly saved) AI answer. Re-runnable without calling the AI again."""
    sheet_notes = list(sheet_notes or [])
    ai = parse_interpretation(data, sheet_notes)
    if not ai.plan_readable:
        raise AIError("AI_PLAN_UNREADABLE", f"the AI could not read the plan: {ai.unreadable_reason or 'no reason given'}")
    try:
        scene, notes = interpretation_to_scene(ai, brief, project_name, model=result.model, request_id=result.request_id)
    except ValidationError as exc:
        raise AIError("AI_SCHEMA_VIOLATION", "the AI interpretation could not be converted into a valid scene",
                      detail=schema_errors(exc)) from exc
    return InterpretOutcome(scene, ai.model_dump(mode="json"), result, sheet_notes + notes)


def room_context(scene: Scene, room_id: str) -> dict:
    room = scene.room(room_id)
    fps = g.wall_footprints(g.walls_on_level(scene, room.level_id))
    net = g.room_net_polygon(room, fps)
    poly = g.room_polygon(room)
    openings = []
    for o in scene.openings:
        w = scene.wall(o.wall_id)
        f = g.wall_frame(w)
        c = f.to_world(o.offset)
        if poly.buffer(w.thickness / 2 + 0.05).contains(g.Point(*c)):
            a, b = f.to_world(o.offset - o.width / 2), f.to_world(o.offset + o.width / 2)
            openings.append({"kind": o.kind, "from": [round(a[0], 3), round(a[1], 3)],
                             "to": [round(b[0], 3), round(b[1], 3)], "sill_height_m": o.sill_height,
                             "opens_into_this_room": o.opens_into == room_id})
    minx, miny, maxx, maxy = net.bounds
    return {
        "room_key": room.id, "name": room.name, "type": room.type,
        "polygon_centreline": [list(p) for p in room.polygon],
        "usable_floor_bounds": {"min": [round(minx, 3), round(miny, 3)], "max": [round(maxx, 3), round(maxy, 3)]},
        "ceiling_height_m": room.ceiling_height or scene.level(room.level_id).wall_height,
        "openings": openings,
        "current_furniture": [{"asset_id": f.asset_id, "center": list(f.position), "rotation_deg": f.rotation_deg}
                              for f in scene.furniture if f.room_id == room_id],
    }


def redesign_room(client: AIClient, scene: Scene, room_id: str, prompt: str,
                  references: list[Path] | None = None) -> tuple[Scene, AIResult, dict]:
    """Ask the AI for new furniture/finishes for one room; everything else is kept verbatim."""
    ctx = room_context(scene, room_id)
    parts = [ContentPart("text", f"STYLE: {scene.project.style}\nUSER REQUEST FOR THIS ROOM: {prompt}"),
             ContentPart("text", "ROOM (all rooms other than this one are out of scope):\n" + json.dumps(ctx, indent=1)),
             ContentPart("text", "FURNITURE CATALOG (only allowed asset_id values; use room_key "
                                 f"'{room_id}' for every item):\n" + catalog_table())]
    for i, ref in enumerate((references or [])[:MAX_REFERENCES], start=1):
        url, _ = image_data_url(ref, REFERENCE_MAX_SIDE, photo=True)
        parts.append(ContentPart("text", f"STYLE REFERENCE IMAGE {i} (materials/colours only)."))
        parts.append(ContentPart("image", image_url=url, detail="low"))
    result = client.structured(system=SYSTEM_REDESIGN, parts=parts, schema_name="room_design",
                               schema=room_design_json_schema())
    trim_free_text(result.data, ROOM_DESIGN_TEXT_LIMITS)
    try:
        design = AIRoomDesign.model_validate(result.data)
    except ValidationError as exc:
        raise AIError("AI_SCHEMA_VIOLATION", "the AI response did not match the required schema",
                      detail=schema_errors(exc)) from exc
    wrong = [f.key for f in design.furniture if f.room_key != room_id]
    if wrong:
        raise AIError("AI_SCHEMA_VIOLATION", f"the AI placed furniture in other rooms ({wrong}); nothing was changed")
    prefix = f"ai_{room_id.lower()}_{(result.request_id or 'x')[-6:].lower()}".replace("-", "_")
    mats, finishes, roles = materials_from_ai([m.model_copy(update={"room_key": room_id}) for m in design.surface_materials],
                                              design.furniture_materials, {room_id: room_id}, prefix=prefix)
    existing = {m.id for m in scene.materials}
    mats = [m for m in mats if m.id not in existing]
    start = max([int(f.id[1:]) for f in scene.furniture if f.id[1:].isdigit()] + [0]) + 1
    items = furniture_from_ai(design.furniture, {room_id: room_id}, start, roles)
    current = scene.room(room_id).finishes
    fin = finishes.get(room_id, {})
    new_finishes = RoomFinishes(floor=fin.get("floor", current.floor), walls=fin.get("walls", current.walls),
                                ceiling=fin.get("ceiling", current.ceiling))
    updated = apply_room_update(scene, room_id, items, finishes=new_finishes, new_materials=mats,
                                note=f"AI redesign of {scene.room(room_id).name}: {prompt[:200]}")
    # questions about this room are reported as notes; adding open questions would change scene scope
    notes = [f"{u.severity}: {u.question}" for u in design.uncertainties]
    return updated, result, {"summary": design.summary, "notes": notes}
