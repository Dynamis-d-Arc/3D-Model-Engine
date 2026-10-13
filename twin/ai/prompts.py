"""Prompt text for the AI interpretation and room redesign calls."""

from __future__ import annotations

import json

from twin.assets.catalog import load_catalog
from twin.scene.model import Brief

CONVENTIONS = """\
Coordinate system (mandatory):
- Metres. x increases to the RIGHT of the plan image (east); y increases toward the TOP of the image (north).
  Image pixel rows grow downward, so flip them.
- Origin (0, 0) is the bottom-left (south-west) corner of the bounding box of the exterior wall CENTRELINES,
  UNLESS the user message contains a "COORDINATE FRAME FOR THIS PLAN" instruction: then that frame wins.
- Room polygons and wall start/end points lie on wall CENTRELINES; neighbouring rooms share edges with
  identical coordinates. Polygons are counter-clockwise without a repeated closing point.
- An opening's `center` is the point on the wall centreline in the middle of the opening.
- Furniture `center` is the centre of its footprint. rotation_deg is counter-clockwise; at 0 the item's front
  faces -y (bottom of the image), 90 faces +x, 180 faces +y, 270 faces -x. Wall-backed items sit with their
  back against the wall.
"""

HONESTY = """\
Measurement honesty (mandatory):
- Priority of sources: dimensions the user provided > dimension labels printed on the plan > scaling from the
  drawing > estimation. Report the true source in every `dimension_source` / `source` field. Never present a
  guess as a label or a user value.
- When something cannot be determined (illegible label, no scale, ambiguous wall) give your best estimate with
  a low confidence AND add an `uncertainties` entry with a concrete question for the user. Use severity
  "blocking" only if no sensible geometry can be produced without the answer.
- If the image is not a floor plan or is unreadable, set plan_readable=false, explain why in
  unreadable_reason, and return empty lists. Do not invent a layout in that case.
"""

GUIDANCE = """\
Walls: exterior walls are usually 0.2-0.3 m thick, interior walls 0.08-0.15 m, unless labelled. Every room
edge should lie on a wall unless the plan shows an open-plan boundary. Prefer long continuous straight walls.
Openings: doors start at the floor (sill_height_m = 0), typically 0.8-0.9 m x 2.1 m. Windows have a sill,
typically 0.8-1.0 m. `wall_key` is the wall containing the opening. hinge_side "start"/"end" refers to that
wall's start/end point ("unknown" if not drawn). opens_into_room_key is the room the leaf swings into, or null.
Furniture: use ONLY asset ids from the catalog; match the words printed on the drawing against the catalog's
drawing words (aliases) and use equipment_box for drawn equipment that matches nothing else. Include EVERY
piece of furniture and equipment drawn on the plan (plan_symbol) - equipment layouts can have hundreds - items
the user asked for (user_requested) and, for homes only, a sensible furnishing of empty rooms in the requested
style (ai_suggested, >= 0.6 m walkways, door swings clear). For non-residential buildings (kitchens, labs,
offices, shops) place only drawn items and never invent equipment. Give width/depth/height when the drawing
differs from the catalog size (measure the drawn outline), otherwise null. Wall-mounted items (wall cabinets,
hoods) may overlap the counters below them.
Text fields are short notes: scale_basis and room notes <= 300 characters, material descriptions <= 100,
questions <= 400, summary <= 1500. Put detail into structured fields, not prose.
Materials: choose floor/wall/ceiling finishes and furniture role colours that match the style, the user's
description and any reference images. Colours are sRGB #RRGGBB.
"""

SYSTEM_INTERPRET = (
    "You turn architectural floor plans into structured data for a deterministic 3D generator. "
    "You do not draw geometry; a human reviews your numbers and the generator then uses them exactly.\n\n"
    + CONVENTIONS + "\n" + HONESTY + "\n" + GUIDANCE
)

SYSTEM_REDESIGN = (
    "You furnish and finish ONE room of an existing apartment for a deterministic 3D generator. "
    "Walls, doors and windows are fixed and must not be changed. Return only furniture and finishes for "
    "this room; keep every item inside the room polygon, keep >= 0.6 m walkways and keep door swings and "
    "windows clear.\n\n" + CONVENTIONS
)


def catalog_table() -> str:
    rows = ["asset_id | name | width x depth x height (m) | typical rooms | placement | drawing words (aliases)"]
    for a in load_catalog().assets:
        w, d, h = a.size
        mount = f" at {a.default_elevation:.2f} m" if a.default_elevation else ""
        rows.append(f"{a.id} | {a.name} | {w:.2f} x {d:.2f} x {h:.2f} | {', '.join(a.room_types)} | "
                    f"{a.placement}{mount} | {', '.join(a.aliases)}")
    return "\n".join(rows)


def brief_text(brief: Brief, project_name: str) -> str:
    data = {
        "project": project_name,
        "style": brief.style,
        "user_description": brief.prompt or None,
        "overall_outer_dimensions_m": ({"width": brief.overall_width, "depth": brief.overall_depth}
                                       if brief.overall_width and brief.overall_depth else None),
        "wall_height_m": brief.wall_height,
        "known_room_dimensions": [k.model_dump() for k in brief.known_rooms],
    }
    return ("USER BRIEF (authoritative where given; 'clear' = wall face to wall face, 'centreline' = between wall "
            "centrelines; overall dimensions are measured to the OUTER faces of the exterior walls):\n"
            + json.dumps(data, indent=1))
