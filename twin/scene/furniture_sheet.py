"""Furniture lists (CSV): one row per item, merged into a scene as the furniture the user asked for.

A row needs a catalog asset (id or one of its drawing words) and a position in plan metres. Columns are matched by
name, case-insensitive; ``plan_x``/``plan_y`` win over ``x``/``y`` when a sheet carries both (the reference sets give
grid-relative and plan-frame positions side by side). The room comes from the ``room`` column (name or id) or, when it
is missing or unknown, from where the item stands. Rows that cannot be placed are skipped and reported, never guessed.

Items from the list are the user's own figures (``confirmed``, source ``user``). With ``replace`` the list becomes the
furniture of the scene: items an earlier list or the AI placed are removed, items placed by hand in the editor stay.
"""

from __future__ import annotations

import csv
import io

from shapely.geometry import Point

from twin.assets.catalog import load_catalog
from twin.scene import geometry as g
from twin.scene.model import Certainty, FurnitureItem, Scene, Size3

COLUMNS = {
    "id": ("id", "item_id", "ref", "no"),
    "asset": ("catalog_id", "asset_id", "asset", "catalog", "type"),
    "label": ("item", "label", "name", "description"),
    "room": ("room", "room_name", "room_id", "space"),
    "x": ("x", "x_m"), "y": ("y", "y_m"),
    "plan_x": ("plan_x_m", "plan_x"), "plan_y": ("plan_y_m", "plan_y"),
    "width": ("width_m", "width", "w"), "depth": ("depth_m", "depth", "d"), "height": ("height_m", "height", "h"),
    "rotation": ("rotation_deg", "rotation", "rot", "angle"),
    "elevation": ("elevation_m", "elevation", "z"),
}
SHEET_NOTE = "furniture list"
MAX_ROWS = 4000
ROOM_REACH_M = 0.5     # an item whose centre is in a wall joins the nearest room within this distance


def looks_like_furniture_sheet(header: list[str]) -> bool:
    cols = {c.strip().lower().replace(" ", "_") for c in header}
    return any(n in cols for n in COLUMNS["asset"][:4])


def _num(v: str) -> float | None:
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


def import_furniture_sheet(scene: Scene, text: str, replace: bool = True) -> tuple[Scene, dict]:
    """Merge a CSV furniture list into the scene. Returns the new scene and {"added", "removed", "problems"}."""
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not rows:
        return scene, {"added": 0, "removed": 0, "problems": ["the list is empty"]}
    if len(rows) > MAX_ROWS + 1:
        return scene, {"added": 0, "removed": 0, "problems": [f"the list has more than {MAX_ROWS} rows"]}
    header = [c.strip().lower().replace(" ", "_") for c in rows[0]]
    col: dict[str, int] = {}
    for key, names in COLUMNS.items():
        for n in names:
            if n in header:
                col[key] = header.index(n)
                break
    if "asset" not in col:
        return scene, {"added": 0, "removed": 0, "problems": [
            "no catalog column (expected a header such as id,room,item,catalog_id,width_m,depth_m,x_m,y_m,rotation_deg)"]}
    xy = ("plan_x", "plan_y") if "plan_x" in col and "plan_y" in col else ("x", "y")
    if xy[0] not in col or xy[1] not in col:
        return scene, {"added": 0, "removed": 0, "problems": ["no x / y position columns"]}

    catalog = load_catalog()
    by_word = {a.id.lower(): a for a in catalog.assets}
    for a in catalog.assets:
        for w in [a.name, *a.aliases]:
            by_word.setdefault(w.strip().lower(), a)
    level = scene.levels[0]
    rooms = g.rooms_on_level(scene, level.id)
    polys = {r.id: g.room_polygon(r) for r in rooms}
    by_room = {r.name.strip().lower(): r for r in rooms} | {r.id.lower(): r for r in rooms}

    kept = [f for f in scene.furniture
            if not replace or (f.certainty.source == "user" and f.certainty.note != SHEET_NOTE)]
    taken = {f.id for f in kept}
    nums = [int(f.id[1:]) for f in scene.furniture if f.id[1:].isdigit()]
    counter = max(nums + [0])
    added: list[FurnitureItem] = []
    problems: list[str] = []
    for line_no, r in enumerate(rows[1:], start=2):
        cell = lambda k: (r[col[k]].strip() if k in col and col[k] < len(r) else "")  # noqa: E731
        try:
            word = cell("asset").lower()
            asset = by_word.get(word)
            if asset is None:
                raise ValueError(f"'{cell('asset')}' is not in the furniture catalog")
            x, y = _num(cell(xy[0])), _num(cell(xy[1]))
            if x is None or y is None:
                raise ValueError("no position")
            p = Point(x, y)
            room = by_room.get(cell("room").lower()) if cell("room") else None
            if room is not None and not polys[room.id].buffer(ROOM_REACH_M).contains(p):
                room = None   # the named room is elsewhere: trust the position
            if room is None:
                room = next((rm for rm in rooms if polys[rm.id].contains(p)), None)
            if room is None and rooms:
                near = min(rooms, key=lambda rm: polys[rm.id].distance(p))
                room = near if polys[near.id].distance(p) <= ROOM_REACH_M else None
            if room is None:
                raise ValueError(f"({x:.2f}, {y:.2f}) is outside every room")
            w, d, h = _num(cell("width")), _num(cell("depth")), _num(cell("height"))
            nominal = asset.size
            dims = (w or nominal[0], d or nominal[1], h or nominal[2])
            size = Size3(width=round(dims[0], 3), depth=round(dims[1], 3), height=round(dims[2], 3)) \
                if any(abs(a - b) > 0.02 for a, b in zip(dims, nominal)) else None
            fid = cell("id")
            if not fid or fid in taken or not fid[0].isalpha():
                counter += 1
                while f"F{counter}" in taken:
                    counter += 1
                fid = f"F{counter}"
            elev = _num(cell("elevation"))
            item = FurnitureItem(
                id=fid, asset_id=asset.id, room_id=room.id, position=(round(x, 3), round(y, 3)),
                elevation=asset.default_elevation if elev is None else elev,
                rotation_deg=_num(cell("rotation")) or 0.0, size=size, label=(cell("label") or None) and cell("label")[:80],
                certainty=Certainty(status="confirmed", source="user", note=SHEET_NOTE))
        except ValueError as exc:
            problems.append(f"row {line_no}: {exc}")
            continue
        taken.add(item.id)
        added.append(item)
    data = scene.model_dump(mode="json")
    data["furniture"] = [f.model_dump(mode="json") for f in kept + added]
    return Scene.model_validate(data), {"added": len(added), "removed": len(scene.furniture) - len(kept),
                                        "problems": problems[:60]}
