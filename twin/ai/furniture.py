"""Furniture & equipment detection on a vector PDF floor plan: high-resolution tiles, one AI call per tile.

The interpretation call sees a whole A1 sheet at ~2048 px (about 2.5 cm per pixel): a hand sink is a dozen
pixels and the text inside an equipment box is unreadable, so equipment layouts lose most of their furniture.
This pass renders the plan area of the PDF in tiles (12 m core + 1.5 m context margin, ~85 px per metre,
crisp vector rendering), gives each tile the exact PDF text labels and the rooms inside it, and asks for
every drawn item with its measured footprint, mapped to the catalog (aliases = words printed on drawings).

    detect_furniture(client, scene, pdf, sheet)  ->  FurnitureOutcome (items + raw tile answers + notes)
    furniture_scene(scene, outcome)                ->  new scene: detected items replace AI-placed furniture

Each tile only reports items whose centre lies in its core (outlined on the image), so tiles do not
double count; the merge also drops near-duplicates. Detected items are `extracted` (read from the plan,
to be confirmed in review). Raw answers are kept so a merge can be re-run without paying again.
"""

from __future__ import annotations

import base64
import io
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, box
from shapely.ops import nearest_points
from shapely.prepared import prep

from twin.ai.client import AIClient, AIError, ContentPart
from twin.assets.catalog import load_catalog
from twin.imaging.pdf_text import labels_in_metres
from twin.scene import geometry as g
from twin.scene.model import Certainty, FurnitureItem, Scene, Size3

TILE_M = 12.0            # core size of a tile (metres)
MARGIN_M = 1.5           # context around the core
PX_PER_M = 85.0          # ~1.2 cm per pixel: equipment outlines and their text stay legible
MAX_TILES = 40
MAX_TILE_LABELS = 400
TILE_OUTPUT_TOKENS = 24000
EXPECTED_TILE_TOKENS = (8000, 5000)   # measured on a dense A1 kitchen equipment sheet (7.9k in, 4.5k out)
PARALLEL = 3

SYSTEM_FURNITURE = """\
You find furniture and equipment on architectural floor plans (furniture plans, kitchen equipment layouts)
for a deterministic 3D generator. You see ONE TILE of the plan at high resolution plus the exact text labels
of the PDF inside it. Coordinates are metres in the PLAN FRAME (x to the right, y up); the user message gives
the pixel-to-metre mapping. Report data only; do not draw or invent anything.

What to report: every piece of furniture or equipment DRAWN in the core area (the red rectangle):
counters and worktables, cabinets (base, wall, tall), fridges and chillers, sinks and hand-wash basins,
cooking equipment (ranges, griddles, fryers, ovens, grills), dishwashers, exhaust hoods (usually a dashed
outline labelled hood), shelving, trolleys, bins, small appliances on counters, service counters, display
cases, tables, EACH chair and stool, benches, sofas, lockers, desks, toilets, urinals, basins, plants.
Not furniture: walls, doors and their swings, windows, columns, stairs, lifts, shafts, HVAC, sprinklers,
dimension lines, grid lines, hatching, floor drains/gutters, text, tag bubbles, section marks.

How to report an item (short keys keep the answer compact):
- a: asset_id, the closest catalog asset (match the drawing's words against the aliases); use
  "equipment_box" for drawn equipment that matches nothing else.
- x, y: centre of the item's drawn outline in the plan frame (metres, 2 decimals).
- w: width = length along the item's front; d: depth = front to back; both measured from the outline (m).
- r: rotation, degrees counter-clockwise; at 0 the front faces -y (down in the image), 90 faces +x, 180 faces
  +y, 270 faces -x. The front is the side people use (doors, controls, seat); an item against a wall faces
  away from the wall.
- h: height only when a label states it (e.g. "H. 900mm" -> 0.9), else null.
- room: the id of the room containing the item (from the room list), or null if unsure.
- l: the printed name of the item (e.g. "Combi Oven GN 2/3") plus its tag if one points to it
  (e.g. "RA-12"), max 40 characters; "" if nothing is printed (chairs, tables usually have none).
- c: confidence 0..1 (1 decimal).
Separately drawn modules in a run of counters are separate items. Only report items whose CENTRE lies inside
the core area; items centred in the margin belong to the neighbouring tile. If the tile has no furniture,
return an empty list. Keep `notes` under 300 characters.
"""


def furniture_schema(asset_ids: list[str]) -> dict:
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["a", "x", "y", "w", "d", "h", "r", "room", "l", "c"],
        "properties": {
            "a": {"type": "string", "enum": asset_ids},
            "x": {"type": "number"}, "y": {"type": "number"},
            "w": {"type": "number"}, "d": {"type": "number"},
            "h": {"type": ["number", "null"]},
            "r": {"type": "number"},
            "room": {"type": ["string", "null"]},
            "l": {"type": "string"},
            "c": {"type": "number"},
        },
    }
    return {"type": "object", "additionalProperties": False, "required": ["items", "notes"],
            "properties": {"items": {"type": "array", "items": item}, "notes": {"type": "string"}}}


def catalog_for_furniture() -> str:
    rows = ["asset_id | name | nominal w x d x h (m) | mounted at (m) | drawing words (aliases)"]
    for a in load_catalog().assets:
        w, d, h = a.size
        rows.append(f"{a.id} | {a.name} | {w:.2f} x {d:.2f} x {h:.2f} | {a.default_elevation:.2f} | "
                    f"{', '.join(a.aliases)}")
    return "\n".join(rows)


def plan_sheet(storage, pid: str, file_id: str | None = None) -> tuple[Path, dict]:
    """The vector PDF floor plan the scene was traced from (with its text layer and scale)."""
    import json
    files = [f for f in storage.list_files(pid) if f["kind"] == "plan"]
    if file_id:
        files = [f for f in files if f["id"] == file_id]
    for f in reversed(files):
        folder = storage.upload_dir(pid, f["id"])
        info_file, pdf = folder / "pdf_text.json", folder / "original.pdf"
        if info_file.is_file() and pdf.is_file():
            info = json.loads(info_file.read_text(encoding="utf-8"))
            if info.get("m_per_pt"):
                return pdf, info
    raise AIError("FURNITURE_NEEDS_VECTOR_PLAN",
                  "furniture detection needs the floor plan as a vector PDF with a printed scale (e.g. 1:100)")


@dataclass
class Tile:
    n: int
    core: tuple[float, float, float, float]
    view: tuple[float, float, float, float]
    rooms: list[dict]


@dataclass
class FurnitureOutcome:
    items: list[dict]                      # validated, merged items (plan frame), before conversion
    raw: list[dict] = field(default_factory=list)   # per tile: {"tile", "core", "data", "usage", "model"}
    notes: list[str] = field(default_factory=list)
    tiles: int = 0
    cost_usd: float = 0.0
    missing_tiles: list[int] = field(default_factory=list)   # tiles without an answer (provider errors)


def plan_tiles(scene: Scene, tile_m: float = TILE_M, margin_m: float = MARGIN_M) -> list[Tile]:
    level = scene.levels[0]
    rooms = g.rooms_on_level(scene, level.id)
    polys = {r.id: g.room_polygon(r) for r in rooms}
    minx = min(p.bounds[0] for p in polys.values())
    miny = min(p.bounds[1] for p in polys.values())
    maxx = max(p.bounds[2] for p in polys.values())
    maxy = max(p.bounds[3] for p in polys.values())
    nx, ny = max(1, math.ceil((maxx - minx) / tile_m)), max(1, math.ceil((maxy - miny) / tile_m))
    # spread the tiles evenly over the plan instead of leaving a sliver at the end
    tw, th = (maxx - minx) / nx, (maxy - miny) / ny
    tiles: list[Tile] = []
    for j in range(ny):
        for i in range(nx):
            core = (minx + i * tw, miny + j * th, minx + (i + 1) * tw, miny + (j + 1) * th)
            cb = box(*core)
            inside = [r for r in rooms if polys[r.id].intersects(cb) and polys[r.id].intersection(cb).area > 0.5]
            if not inside:
                continue
            view = (core[0] - margin_m, core[1] - margin_m, core[2] + margin_m, core[3] + margin_m)
            vb = box(*view)
            room_rows = []
            for r in rooms:
                if not polys[r.id].intersects(vb):
                    continue
                clip = polys[r.id].intersection(vb)
                pts = list(clip.exterior.coords)[:-1] if clip.geom_type == "Polygon" else list(polys[r.id].exterior.coords)[:-1]
                room_rows.append({"id": r.id, "name": r.name, "type": r.type,
                                  "polygon": [[round(x, 2), round(y, 2)] for x, y in pts[:40]]})
            tiles.append(Tile(len(tiles) + 1, core, view, room_rows))
    if len(tiles) > MAX_TILES:
        raise AIError("FURNITURE_TOO_MANY_TILES", f"the plan needs {len(tiles)} tiles (max {MAX_TILES}); use larger tiles")
    return tiles


def render_tile(pdf: Path, sheet: dict, tile: Tile, px_per_m: float = PX_PER_M) -> tuple[str, tuple[int, int], float]:
    """The tile as a PNG data URL rendered straight from the vector PDF, the core area outlined in red."""
    import pymupdf
    from PIL import Image, ImageDraw
    k = sheet["m_per_pt"]
    H = sheet["page_h_pt"]
    x0, y0, x1, y1 = tile.view
    clip = pymupdf.Rect(x0 / k, H - y1 / k, x1 / k, H - y0 / k)
    zoom = px_per_m * k                     # pixels per PDF point
    with pymupdf.open(pdf) as doc:
        page = doc[sheet.get("page", 0)]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    m_per_px = (x1 - x0) / img.width
    d = ImageDraw.Draw(img)
    cx0, cy0, cx1, cy1 = tile.core
    to_px = lambda x, y: ((x - x0) / m_per_px, (y1 - y) / m_per_px)  # noqa: E731
    a, b = to_px(cx0, cy1), to_px(cx1, cy0)
    d.rectangle([a, b], outline=(230, 30, 30), width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"), img.size, m_per_px


def tile_parts(pdf: Path, sheet: dict, tile: Tile, catalog_text: str) -> list[ContentPart]:
    url, size, k = render_tile(pdf, sheet, tile)
    x0, y0, x1, y1 = tile.view
    cx0, cy0, cx1, cy1 = tile.core
    labels = labels_in_metres(sheet, tile.view)
    labels.sort(key=lambda lab: (-lab["y"], lab["x"]))
    frame = (f"TILE {tile.n}. The image covers x {x0:.2f}..{x1:.2f} m and y {y0:.2f}..{y1:.2f} m of the plan frame. "
             f"It is {size[0]}x{size[1]} px and 1 px = {k:.4f} m, so pixel (px, py) is at x = {x0:.2f} + px*{k:.4f}, "
             f"y = {y1:.2f} - py*{k:.4f}. CORE AREA (red rectangle; report only items centred here): "
             f"x {cx0:.2f}..{cx1:.2f}, y {cy0:.2f}..{cy1:.2f}.")
    rooms = "\n".join(f"{r['id']} | {r['name']} ({r['type']}) | {r['polygon']}" for r in tile.rooms)
    table = "\n".join(f"{lab['x']:.2f}, {lab['y']:.2f}: {lab['text']}" for lab in labels[:MAX_TILE_LABELS])
    return [ContentPart("text", frame),
            ContentPart("text", "ROOMS in this tile (id | name (type) | polygon in the plan frame, clipped to the tile):\n" + rooms),
            ContentPart("text", "PDF TEXT LABELS in this tile (label centre x, y in metres: text):\n" + (table or "(none)")),
            ContentPart("text", "CATALOG (allowed asset_id values):\n" + catalog_text),
            ContentPart("text", "TILE IMAGE (vector PDF rendered at high resolution). List every furniture and equipment "
                                "item centred in the core area."),
            ContentPart("image", image_url=url, detail="high")]


def _tile_items(data: dict, tile: Tile, room_ids: set[str], notes: list[str]) -> list[dict]:
    catalog = load_catalog()
    out = []
    cx0, cy0, cx1, cy1 = tile.core
    for it in data.get("items") or []:
        try:
            x, y, w, d = float(it["x"]), float(it["y"]), float(it["w"]), float(it["d"])
            rot = float(it.get("r") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        asset = catalog.get(it.get("a", ""))
        if asset is None or not all(map(math.isfinite, (x, y, w, d, rot))):
            continue
        if not (cx0 - 0.05 <= x <= cx1 + 0.05 and cy0 - 0.05 <= y <= cy1 + 0.05):
            continue            # centred in the margin: the neighbouring tile reports it
        if not (0.08 <= w <= 15 and 0.05 <= d <= 15):
            notes.append(f"tile {tile.n}: skipped {asset.id} with an implausible size {w:.2f} x {d:.2f} m")
            continue
        hgt = it.get("h")
        hgt = float(hgt) if isinstance(hgt, (int, float)) and 0.05 <= float(hgt) <= 3.5 else None
        room = it.get("room") if it.get("room") in room_ids else None
        conf = it.get("c")
        conf = min(1.0, max(0.0, float(conf))) if isinstance(conf, (int, float)) else 0.6
        out.append({"asset_id": asset.id, "x": x, "y": y, "width": w, "depth": d, "height": hgt,
                    "rotation": rot % 360.0, "room": room, "label": str(it.get("l") or "")[:80].strip(),
                    "confidence": conf, "tile": tile.n})
    return out


def _dedupe(items: list[dict]) -> tuple[list[dict], int]:
    """Drop near-duplicates (same item reported twice across a tile edge or twice in one tile)."""
    kept: list[dict] = []
    fps = []
    dropped = 0
    for it in sorted(items, key=lambda i: -i["confidence"]):
        fp = g.footprint_rect(it["x"], it["y"], it["width"], it["depth"], it["rotation"])
        dup = False
        for other, ofp in zip(kept, fps):
            if other["asset_id"] != it["asset_id"]:
                continue
            if math.hypot(other["x"] - it["x"], other["y"] - it["y"]) < 0.2:
                dup = True
                break
            inter = fp.intersection(ofp).area
            if inter > 0.6 * min(fp.area, ofp.area):
                dup = True
                break
        if dup:
            dropped += 1
            continue
        kept.append(it)
        fps.append(fp)
    return kept, dropped


def detect_furniture(client: AIClient, scene: Scene, pdf: Path, sheet: dict, *, progress=None, raw_sink=None,
                     tile_m: float = TILE_M, parallel: int = PARALLEL,
                     previous: list[dict] | None = None) -> FurnitureOutcome:
    """`previous`: saved tile answers of an earlier run; only the tiles they lack are sent (resume)."""
    tiles = plan_tiles(scene, tile_m)
    catalog_text = catalog_for_furniture()
    schema = furniture_schema(load_catalog().ids())
    room_ids = {r.id for r in scene.rooms}
    notes: list[str] = []
    key = lambda core: tuple(round(v, 3) for v in core)  # noqa: E731
    have = {(e["tile"], key(e["core"])): e for e in previous or []}
    raw: list[dict | None] = [have.get((t.n, key(t.core))) for t in tiles]
    todo = [t for t, r in zip(tiles, raw) if r is None]
    if previous:
        notes.append(f"{len(tiles) - len(todo)} tile answers reused, {len(todo)} tiles sent")
    done = [0]

    def run(tile: Tile) -> None:
        parts = tile_parts(pdf, sheet, tile, catalog_text)
        res = client.structured(system=SYSTEM_FURNITURE, parts=parts, schema_name="furniture_tile", schema=schema,
                                max_output_tokens=TILE_OUTPUT_TOKENS)
        entry = {"tile": tile.n, "core": tile.core, "view": tile.view, "rooms": tile.rooms, "data": res.data,
                 "usage": res.usage, "model": res.model, "request_id": res.request_id}
        raw[tile.n - 1] = entry
        if raw_sink is not None:
            raw_sink(entry)
        done[0] += 1
        if progress:
            progress(done[0] / len(todo), f"tile {done[0]}/{len(todo)}: {len(res.data.get('items') or [])} items")

    errors: list[str] = []
    budget_refused: list[Tile] = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {pool.submit(run, t): t for t in todo}
        for fut, t in futures.items():
            try:
                fut.result()
            except AIError as exc:
                if exc.code == "AI_BUDGET_EXCEEDED":
                    budget_refused.append(t)   # maybe only the parallel worst-case reservations: retried below
                    continue
                errors.append(f"tile {t.n}: {exc.code}: {exc}")
                if exc.code in ("AI_AUTH", "AI_NOT_CONFIGURED", "AI_BUDGET_PRICES_MISSING"):
                    for other in futures:
                        other.cancel()
    # near the spending cap, three parallel worst-case reservations can be refused while one at a time still fits
    for t in sorted(budget_refused, key=lambda x: x.n):
        try:
            run(t)
        except AIError as exc:
            errors.append(f"tile {t.n}: {exc.code}: {exc}")
    if errors and not any(raw):
        raise AIError("AI_FURNITURE_FAILED", "furniture detection failed for every tile", detail=errors)
    notes.extend(errors)
    out = merge_tiles([r for r in raw if r], room_ids, notes)
    out.tiles = len(tiles)
    out.missing_tiles = [t.n for t, r in zip(tiles, raw) if r is None]
    return out


def merge_tiles(raw: list[dict], room_ids: set[str], notes: list[str] | None = None) -> FurnitureOutcome:
    """Validated, de-duplicated items from saved tile answers (re-runnable without calling the AI)."""
    notes = list(notes or [])
    items: list[dict] = []
    for entry in raw:
        tile = Tile(entry["tile"], tuple(entry["core"]), tuple(entry["view"]), entry.get("rooms", []))
        items.extend(_tile_items(entry["data"], tile, room_ids, notes))
        if entry["data"].get("notes"):
            notes.append(f"tile {tile.n}: {entry['data']['notes'][:300]}")
    items, dropped = _dedupe(items)
    if dropped:
        notes.append(f"{dropped} duplicate detections removed")
    return FurnitureOutcome(items=items, raw=raw, notes=notes)


def furniture_scene(scene: Scene, outcome: FurnitureOutcome, keep_user_items: bool = True) -> tuple[Scene, dict]:
    """A new scene whose furniture is the detected items (items the user placed by hand are kept)."""
    catalog = load_catalog()
    level = scene.levels[0]
    polys = {r.id: g.room_polygon(r) for r in g.rooms_on_level(scene, level.id)}
    preps = {rid: prep(p) for rid, p in polys.items()}
    kept = [f for f in scene.furniture if keep_user_items and f.certainty.source == "user"]
    kept_fps = [g.furniture_footprint(f, catalog.get(f.asset_id)) for f in kept if catalog.get(f.asset_id)]
    start = max([int(f.id[1:]) for f in scene.furniture if f.id[1:].isdigit()] + [0]) + 1
    new: list[FurnitureItem] = []
    unplaced = 0
    for n, it in enumerate(outcome.items):
        asset = catalog.get(it["asset_id"])
        p = Point(it["x"], it["y"])
        room = it["room"] if it["room"] in preps and preps[it["room"]].contains(p) else None
        if room is None:
            room = next((rid for rid, pp in preps.items() if pp.contains(p)), None)
        if room is None:   # centre inside a wall: nearest room within 0.3 m, centre moved just inside it
            near = min(polys.items(), key=lambda kv: kv[1].distance(p))
            if near[1].distance(p) <= 0.3:
                room = near[0]
                inner = near[1].buffer(-0.05)
                target = inner if not inner.is_empty else near[1]
                q = nearest_points(target, p)[0]
                it = {**it, "x": q.x, "y": q.y}
        if room is None:
            unplaced += 1
            continue
        fp = g.footprint_rect(it["x"], it["y"], it["width"], it["depth"], it["rotation"])
        if any(fp.intersection(k).area > 0.5 * min(fp.area, k.area) for k in kept_fps):
            continue        # the user already placed something here
        w, d = it["width"], it["depth"]
        h = it["height"] or asset.size[2]
        size = None
        if any(abs(a - b) > 0.02 for a, b in zip((w, d, h), asset.size)):
            size = Size3(width=round(w, 3), depth=round(d, 3), height=round(h, 3))
        new.append(FurnitureItem(
            id=f"F{start + n}", asset_id=asset.id, room_id=room, position=(round(it["x"], 3), round(it["y"], 3)),
            elevation=asset.default_elevation, rotation_deg=round(it["rotation"], 1), size=size,
            label=it["label"] or None,
            certainty=Certainty(status="extracted", source="ai", confidence=round(it["confidence"], 2),
                                note="plan_symbol (furniture detection)")))
    data = scene.model_dump(mode="json")
    data["furniture"] = [f.model_dump(mode="json") for f in kept + new]
    out = Scene.model_validate(data)
    by_asset: dict[str, int] = {}
    for f in new:
        by_asset[f.asset_id] = by_asset.get(f.asset_id, 0) + 1
    return out, {"items": len(new), "kept_user_items": len(kept), "unplaced": unplaced,
                 "by_asset": dict(sorted(by_asset.items(), key=lambda kv: -kv[1])),
                 "rooms_with_furniture": len({f.room_id for f in new})}
