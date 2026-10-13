"""Draw a conventional 2D floor plan image of a scene (used as a test fixture and AI test input).

    python -m twin.fixtures.render_plan [out.png]

The drawing is generated from the scene model, so its true dimensions are known exactly:
scale = PX_PER_M pixels per metre, origin (scene 0,0) at pixel (MARGIN, MARGIN + depth*PX_PER_M).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from twin.fixtures import load_sample_apartment
from twin.scene import geometry as g
from twin.scene.model import Scene

PX_PER_M = 100
MARGIN = 140


def _font(size: int):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(scene: Scene, out: Path) -> dict:
    fps = g.wall_footprints(scene.walls)
    xs = [x for w in scene.walls for x in (w.start[0], w.end[0])]
    ys = [y for w in scene.walls for y in (w.start[1], w.end[1])]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    W = int((maxx - minx) * PX_PER_M + 2 * MARGIN)
    H = int((maxy - miny) * PX_PER_M + 2 * MARGIN)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    P = lambda x, y: (MARGIN + (x - minx) * PX_PER_M, MARGIN + (maxy - y) * PX_PER_M)  # noqa: E731
    f_room, f_dim, f_small = _font(26), _font(22), _font(16)

    for r in scene.rooms:
        d.polygon([P(*p) for p in r.polygon], fill=(250, 248, 242))
    for fp in fps.values():
        for poly in g.polygons_of(fp):
            d.polygon([P(*p) for p in poly.exterior.coords], fill=(35, 35, 35))
    for o in scene.openings:
        w = scene.wall(o.wall_id)
        f = g.wall_frame(w)
        a, b = o.offset - o.width / 2, o.offset + o.width / 2
        t = w.thickness / 2 + 0.005
        d.polygon([P(*f.to_world(a, -t)), P(*f.to_world(b, -t)), P(*f.to_world(b, t)), P(*f.to_world(a, t))], fill="white")
        if o.kind == "window":
            for dd in (-w.thickness / 4, 0, w.thickness / 4):
                d.line([P(*f.to_world(a, dd)), P(*f.to_world(b, dd))], fill=(30, 30, 30), width=2)
        elif o.kind == "door":
            side = 1.0
            if o.opens_into:
                probe = f.to_world(o.offset, w.thickness / 2 + 0.05)
                if not g.room_polygon(scene.room(o.opens_into)).contains(g.Point(*probe)):
                    side = -1.0
            hinge = a if o.hinge_side == "start" else b
            sweep = 1.0 if o.hinge_side == "start" else -1.0
            d.line([P(*f.to_world(hinge, side * w.thickness / 2)), P(*f.to_world(hinge, side * (w.thickness / 2 + o.width)))],
                   fill=(30, 30, 30), width=3)
            arc = [P(*f.to_world(hinge + sweep * o.width * math.cos(math.radians(k * 7.5)),
                                 side * (w.thickness / 2 + o.width * math.sin(math.radians(k * 7.5))))) for k in range(13)]
            d.line(arc, fill=(90, 90, 90), width=1)
    report_rooms = {}
    for r in scene.rooms:
        poly = g.room_polygon(r)
        net = g.room_net_polygon(r, fps)
        cx, cy = net.representative_point().coords[0]
        wn, dn = g.bbox_dims(net)
        report_rooms[r.name] = (round(wn, 2), round(dn, 2))
        px, py = P(cx, cy)
        d.text((px, py - 16), r.name.upper(), fill=(20, 20, 20), font=f_room, anchor="mm")
        d.text((px, py + 16), f"{wn:.2f} x {dn:.2f}", fill=(60, 60, 60), font=f_dim, anchor="mm")
        del poly
    # overall dimension lines (outer faces)
    outer = g.bbox_dims(g.unary_union(list(fps.values()))) if hasattr(g, "unary_union") else None
    from shapely.ops import unary_union
    ox0, oy0, ox1, oy1 = unary_union(list(fps.values())).bounds
    y_line = P(0, oy1)[1] - 60
    d.line([P(ox0, 0)[0], y_line, P(ox1, 0)[0], y_line], fill=(0, 0, 0), width=2)
    for x in (ox0, ox1):
        d.line([P(x, 0)[0], y_line - 12, P(x, 0)[0], y_line + 12], fill=(0, 0, 0), width=2)
    d.text(((P(ox0, 0)[0] + P(ox1, 0)[0]) / 2, y_line - 18), f"{ox1 - ox0:.2f} m", fill=(0, 0, 0), font=f_dim, anchor="mm")
    x_line = P(ox0, 0)[0] - 60
    d.line([x_line, P(0, oy0)[1], x_line, P(0, oy1)[1]], fill=(0, 0, 0), width=2)
    for y in (oy0, oy1):
        d.line([x_line - 12, P(0, y)[1], x_line + 12, P(0, y)[1]], fill=(0, 0, 0), width=2)
    d.text((x_line - 10, (P(0, oy0)[1] + P(0, oy1)[1]) / 2), f"{oy1 - oy0:.2f} m", fill=(0, 0, 0), font=f_dim, anchor="rm")
    # scale bar + note
    sx0, sy0 = MARGIN, H - 50
    d.line([sx0, sy0, sx0 + 2 * PX_PER_M, sy0], fill=(0, 0, 0), width=4)
    d.text((sx0 + PX_PER_M, sy0 - 16), "2 m", fill=(0, 0, 0), font=f_small, anchor="mm")
    d.text((W - MARGIN, H - 40), "SAMPLE APARTMENT - interior dims in metres (clear)", fill=(80, 80, 80), font=f_small, anchor="rm")
    del outer
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    return {"px_per_m": PX_PER_M, "origin_px": P(0, 0), "size": (W, H), "rooms_clear": report_rooms}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("sample_apartment_plan.png")
    info = render(load_sample_apartment(), target)
    print(target, info)
