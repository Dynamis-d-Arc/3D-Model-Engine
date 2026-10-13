"""Vector primitives of one PDF drawing sheet, split into the services layer and the architectural underlay.

MEP sheets exported from CAD draw the architecture as a grey (halftone) underlay and the services (ducts,
units, diffusers, their labels) in black or colour. ``load_sheet`` returns every stroked line segment and
quadrilateral in sheet points (origin top-left, y down, as PyMuPDF reports them) with a ``fg`` flag:
True = services layer, False = underlay. When a sheet has no grey underlay every stroke is foreground.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

PT_TO_M_PAPER = 0.0254 / 72.0


def _is_grey_underlay(rgb: tuple[float, ...] | None) -> bool:
    if not rgb or len(rgb) < 3:
        return False
    r, g, b = rgb[:3]
    return max(r, g, b) - min(r, g, b) < 0.06 and 0.25 <= (r + g + b) / 3 <= 0.88


def _is_paper(rgb: tuple[float, ...] | None) -> bool:
    return bool(rgb) and min(rgb[:3]) > 0.93


@dataclass
class Sheet:
    width_pt: float
    height_pt: float
    segs: np.ndarray                       # (N, 4) x0, y0, x1, y1 in sheet points
    seg_fg: np.ndarray                     # (N,) bool
    quads: np.ndarray                      # (M, 4, 2) corner points ul, ur, lr, ll
    quad_fg: np.ndarray                    # (M,) bool
    text: str = ""
    layer_note: str = ""
    colour_counts: dict[str, int] = field(default_factory=dict)

    @property
    def fg_segs(self) -> np.ndarray:
        return self.segs[self.seg_fg]

    @property
    def fg_quads(self) -> np.ndarray:
        return self.quads[self.quad_fg]


def _bezier(p0, p1, p2, p3, n: int = 6) -> list[tuple[float, float]]:
    out = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        out.append((a * p0.x + b * p1.x + c * p2.x + d * p3.x, a * p0.y + b * p1.y + c * p2.y + d * p3.y))
    return out


def load_sheet(data: bytes, page: int = 0) -> Sheet:
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pg = doc[page]
        drawings = pg.get_drawings()
        text = pg.get_text()
        w_pt, h_pt = pg.rect.width, pg.rect.height
    segs: list[tuple[float, float, float, float]] = []
    seg_col: list[tuple] = []
    quads: list[list[tuple[float, float]]] = []
    quad_col: list[tuple] = []
    for p in drawings:
        col = p.get("color") if p.get("type") != "f" else p.get("fill")
        col = tuple(round(c, 3) for c in col) if col else None
        if col is None or _is_paper(col):
            continue  # unstroked fills and white masking carry no linework
        for it in p["items"]:
            op = it[0]
            if op == "l":
                a, b = it[1], it[2]
                segs.append((a.x, a.y, b.x, b.y))
                seg_col.append(col)
            elif op == "c":
                pts = _bezier(*it[1:5])
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    segs.append((x0, y0, x1, y1))
                    seg_col.append(col)
            elif op in ("qu", "re"):
                q = it[1].quad if op == "re" else it[1]
                quads.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)])
                quad_col.append(col)
    counts = Counter(seg_col) + Counter(quad_col)
    grey = {c for c in counts if _is_grey_underlay(c)}
    grey_n = sum(counts[c] for c in grey)
    total = max(1, sum(counts.values()))
    if grey and grey_n >= 0.3 * total:
        is_fg = lambda c: c not in grey  # noqa: E731
        note = (f"grey underlay {sorted(grey)} = architecture ({grey_n} strokes); "
                f"{total - grey_n} strokes in other colours = services layer")
    else:
        is_fg = lambda c: True  # noqa: E731
        note = "no grey underlay found: every stroke is treated as the services layer"
    return Sheet(
        width_pt=w_pt, height_pt=h_pt,
        segs=np.array(segs, dtype=float).reshape(-1, 4),
        seg_fg=np.array([is_fg(c) for c in seg_col], dtype=bool),
        quads=np.array(quads, dtype=float).reshape(-1, 4, 2),
        quad_fg=np.array([is_fg(c) for c in quad_col], dtype=bool),
        text=text, layer_note=note,
        colour_counts={str(c): n for c, n in counts.most_common(12)},
    )


def quad_edges(quads: np.ndarray) -> np.ndarray:
    """(M, 4, 2) quads -> (4M, 4) edge segments."""
    if not len(quads):
        return np.zeros((0, 4))
    a = quads
    b = np.roll(quads, -1, axis=1)
    return np.concatenate([a, b], axis=2).reshape(-1, 4)


def render(segs: np.ndarray, box: tuple[float, float, float, float], zoom: float, width: float = 1.0):
    """Black-on-white raster of segments inside box (x0, y0, x1, y1 in points) at zoom px/pt."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    W, H = max(1, int(math.ceil((x1 - x0) * zoom))), max(1, int(math.ceil((y1 - y0) * zoom)))
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    if len(segs):
        keep = ~((np.maximum(segs[:, 0], segs[:, 2]) < x0) | (np.minimum(segs[:, 0], segs[:, 2]) > x1) |
                 (np.maximum(segs[:, 1], segs[:, 3]) < y0) | (np.minimum(segs[:, 1], segs[:, 3]) > y1))
        lw = max(1, int(round(width)))
        for sx0, sy0, sx1, sy1 in segs[keep]:
            dr.line([((sx0 - x0) * zoom, (sy0 - y0) * zoom), ((sx1 - x0) * zoom, (sy1 - y0) * zoom)], fill=0, width=lw)
    return img
