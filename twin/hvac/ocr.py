"""Read the labels of the services layer.

CAD sheets often draw their text as vector strokes (some services sheets have no text layer at all), so the services
layer is rendered to a raster and read with RapidOCR (ONNX, runs offline on the CPU). The sheet is cut
into overlapping tiles so small labels keep enough pixels; duplicates from the overlaps are merged.
Positions are returned in sheet points.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass

import numpy as np

from twin.hvac.sheet import render

ZOOM = 3.5          # px per point: 2.5 mm CAD text -> ~25 px tall
TILE_PX = 1600
OVERLAP_PX = 220

_engine = None
_lock = threading.Lock()


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class TextItem:
    text: str
    x: float          # centre, sheet points
    y: float
    w: float          # box size, points
    h: float
    angle: float      # degrees of the text baseline (0 = horizontal)
    conf: float

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _get_engine():
    global _engine
    with _lock:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise OcrUnavailable("rapidocr_onnxruntime is not installed (pip install rapidocr_onnxruntime)") from exc
            _engine = RapidOCR()
        return _engine


def read_labels(segs: np.ndarray, box: tuple[float, float, float, float], zoom: float = ZOOM,
                on_progress=None) -> list[TextItem]:
    """OCR every label of the given segments inside box (sheet points)."""
    engine = _get_engine()
    x0, y0, x1, y1 = box
    step = (TILE_PX - OVERLAP_PX) / zoom
    tile = TILE_PX / zoom
    nx = max(1, math.ceil((x1 - x0 - OVERLAP_PX / zoom) / step))
    ny = max(1, math.ceil((y1 - y0 - OVERLAP_PX / zoom) / step))
    items: list[TextItem] = []
    done = 0
    for j in range(ny):
        for i in range(nx):
            tx0, ty0 = x0 + i * step, y0 + j * step
            tbox = (tx0, ty0, min(tx0 + tile, x1), min(ty0 + tile, y1))
            img = render(segs, tbox, zoom, width=max(1.0, zoom * 0.35))
            arr = np.asarray(img)
            if (arr < 128).mean() > 0.0005:
                res, _ = engine(np.stack([arr] * 3, axis=-1))
                for quad, text, conf in res or []:
                    q = np.asarray(quad, dtype=float)
                    cx, cy = q[:, 0].mean() / zoom + tx0, q[:, 1].mean() / zoom + ty0
                    w = np.linalg.norm(q[1] - q[0]) / zoom
                    hgt = np.linalg.norm(q[3] - q[0]) / zoom
                    ang = math.degrees(math.atan2(q[1][1] - q[0][1], q[1][0] - q[0][0]))
                    # skip text cut by a tile edge that is not the sheet edge; the neighbour tile reads it whole
                    margin = 6 / zoom
                    if ((q[:, 0].min() / zoom < margin and i > 0) or (q[:, 1].min() / zoom < margin and j > 0) or
                            (q[:, 0].max() / zoom > tile - margin and i < nx - 1) or
                            (q[:, 1].max() / zoom > tile - margin and j < ny - 1)):
                        continue
                    items.append(TextItem(str(text).strip(), cx, cy, w, hgt, ang, float(conf)))
            done += 1
            if on_progress:
                on_progress(done / (nx * ny))
    return dedupe(items)


def dedupe(items: list[TextItem]) -> list[TextItem]:
    out: list[TextItem] = []
    for it in sorted(items, key=lambda t: -t.conf * len(t.text)):
        if any(abs(it.x - o.x) < max(o.w, it.w) / 2 and abs(it.y - o.y) < max(o.h, it.h) * 0.6 for o in out):
            continue
        out.append(it)
    return out
