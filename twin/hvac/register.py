"""Register a services sheet (HVAC) to the floor-plan sheet.

Both drawings of one building share the structural grid (e.g. E.1..E.7 / E.A..E.M).
Grid lines are long dash-dot lines, so they are found as axis-aligned lines made of many collinear
pieces that span a large part of the sheet. Matching the two sets of positions per axis gives the
translation (in sheet points, same paper scale) that maps the services sheet onto the plan sheet:

    x_plan_pt = s * x_hvac_pt + dx,   y_plan_pt = s * y_hvac_pt + dy

The two sheets need not share a paper scale (e.g. an HVAC sheet at 1:100 on a floor plan at 1:75): the ratio s is
searched over the standard drawing scales and the one that matches the most grid lines wins.
A raster phase correlation of the two drawings is the fallback when too few grid lines match.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

import numpy as np

from twin.hvac.sheet import Sheet, render

AXIS_TOL_PT = 0.06          # a segment is axis-aligned if its other coordinate varies less than this
MATCH_TOL_PT = 1.2          # candidate matches for the offset search (0.04 m at 1:100)
TIGHT_TOL_PT = 0.35         # a confirmed match after refining the offset (12 mm at 1:100)
MIN_DASHES = 20             # pieces of a dash-dot grid line (1:75 sheets draw ~18 pt dashes: ~30 per line)
STANDARD_SCALES = (20, 25, 50, 75, 100, 125, 150, 200, 250, 500)


@dataclass
class Registration:
    dx: float
    dy: float
    scale: float
    method: str
    matched_x: int
    matched_y: int
    residual_pt: float
    grid_x: list[float]
    grid_y: list[float]

    def apply(self, x: np.ndarray | float, y: np.ndarray | float):
        return self.scale * np.asarray(x) + self.dx, self.scale * np.asarray(y) + self.dy

    def public(self) -> dict:
        d = asdict(self)
        d.pop("grid_x"), d.pop("grid_y")
        return d


def grid_lines(segs: np.ndarray, width_pt: float, height_pt: float) -> tuple[list[float], list[float]]:
    """Positions (x of vertical lines, y of horizontal lines) of dash-dot grid lines."""
    out: list[list[float]] = []
    for axis, span in ((0, height_pt), (1, width_pt)):        # axis 0: vertical lines (constant x)
        c0, c1 = (0, 2) if axis == 0 else (1, 3)               # the constant coordinate
        r0, r1 = (1, 3) if axis == 0 else (0, 2)               # the running coordinate
        aligned = np.abs(segs[:, c0] - segs[:, c1]) < AXIS_TOL_PT
        run = np.abs(segs[:, r0] - segs[:, r1])
        sel = segs[aligned & (run > 0.3)]
        groups: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
        for s in sel:
            pos = (s[c0] + s[c1]) / 2
            groups[int(round(pos * 10))].append((pos, min(s[r0], s[r1]), max(s[r0], s[r1])))
        found = []
        for g in groups.values():
            lo, hi = min(p[1] for p in g), max(p[2] for p in g)
            extent = hi - lo
            pieces = np.array([p[2] - p[1] for p in g])
            cover = pieces.sum() / max(extent, 1e-6)
            # dash-dot: many short pieces spread over most of the sheet
            if (extent >= 0.45 * span and len(g) >= MIN_DASHES and 0.55 <= cover <= 1.1
                    and np.median(pieces) < 0.05 * extent):
                found.append(float(np.mean([p[0] for p in g])))
        found.sort()
        dedup: list[float] = []
        for f in found:  # one line drawn twice lands in neighbouring bins
            if dedup and f - dedup[-1] < 0.3:
                continue
            dedup.append(f)
        found = dedup
        out.append(sorted(found))
    return out[0], out[1]


def match_axis(a: list[float], b: list[float], scale: float = 1.0) -> tuple[float, int, float]:
    """Offset o with b ~ scale*a + o that matches the most lines; returns (offset, matches, rms)."""
    best = (0.0, 0, float("inf"))
    if not a or not b:
        return best
    B = np.array(b)
    for ai in a:
        for bj in b:
            o = bj - scale * ai
            res = []
            for ak in a:
                d = np.abs(B - (scale * ak + o))
                j = int(np.argmin(d))
                if d[j] <= MATCH_TOL_PT:
                    res.append(B[j] - (scale * ak + o))
            if not res:
                continue
            o2 = o + float(np.median(res))
            tight = [r - float(np.median(res)) for r in res if abs(r - float(np.median(res))) <= TIGHT_TOL_PT]
            rms = float(np.sqrt(np.mean(np.square(tight)))) if tight else float("inf")
            if (len(tight), -rms) > (best[1], -best[2]):
                best = (o2 + (float(np.mean(tight)) if tight else 0.0), len(tight), rms)
    return best


def phase_correlate(a_img: np.ndarray, b_img: np.ndarray) -> tuple[float, float, float]:
    """Translation (dx, dy) in pixels that moves image a onto image b, and the peak strength."""
    H = max(a_img.shape[0], b_img.shape[0])
    W = max(a_img.shape[1], b_img.shape[1])
    A = np.zeros((H, W)); A[:a_img.shape[0], :a_img.shape[1]] = a_img
    B = np.zeros((H, W)); B[:b_img.shape[0], :b_img.shape[1]] = b_img
    FA, FB = np.fft.rfft2(A), np.fft.rfft2(B)
    R = FB * np.conj(FA)
    R /= np.abs(R) + 1e-9
    corr = np.fft.irfft2(R, s=(H, W))
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    peak = float(corr[iy, ix] / (np.abs(corr).mean() + 1e-12))
    dy = iy if iy <= H // 2 else iy - H
    dx = ix if ix <= W // 2 else ix - W
    return float(dx), float(dy), peak


def underlay_segs(sheet: Sheet) -> np.ndarray:
    return sheet.segs[~sheet.seg_fg] if (~sheet.seg_fg).any() else sheet.segs


def register_grids(hx: list[float], hy: list[float], px: list[float], py: list[float],
                   scale: float = 1.0) -> Registration | None:
    """Translation from matched grid lines, or None when fewer than 3 lines match on either axis."""
    ox, nx, rx = match_axis(hx, px, scale)
    oy, ny, ry = match_axis(hy, py, scale)
    if nx >= 3 and ny >= 3:
        return Registration(ox, oy, scale, f"structural grid lines ({nx} vertical, {ny} horizontal matched)",
                            nx, ny, round(max(rx, ry), 4), hx, hy)
    return None


def scale_ratios(plan_scale: int | None) -> list[float]:
    """Candidate paper-scale ratios s (hvac points -> plan points): the same scale first, then every standard
    scale the services sheet could be drawn at (s = hvac_scale / plan_scale)."""
    out = [1.0]
    if plan_scale:
        out += [h / plan_scale for h in STANDARD_SCALES if h != plan_scale and 0.2 <= h / plan_scale <= 5]
    return out


def register_grids_scaled(hx: list[float], hy: list[float], px: list[float], py: list[float],
                          ratios: list[float]) -> Registration | None:
    """The ratio whose grid match is best (most matched lines, then lowest residual)."""
    best = None
    for s in ratios:
        r = register_grids(hx, hy, px, py, s)
        if r is not None and (best is None or (r.matched_x + r.matched_y, -r.residual_pt) > (best.matched_x + best.matched_y, -best.residual_pt)):
            best = r
    if best is not None and abs(best.scale - 1.0) > 1e-9:
        best.method += f", sheet scale ratio {best.scale:.4g}"
    return best


def register_raster(hvac: Sheet, plan: Sheet, scale: float = 1.0) -> Registration:
    """Fallback: correlate the hvac underlay with the whole floor plan at 1 px = 2 pt."""
    z = 0.5
    under = underlay_segs(hvac)
    a = 255 - np.asarray(render(under * scale, (0, 0, hvac.width_pt * scale, hvac.height_pt * scale), z), dtype=float)
    b = 255 - np.asarray(render(plan.segs, (0, 0, plan.width_pt, plan.height_pt), z), dtype=float)
    dx, dy, peak = phase_correlate(a, b)
    return Registration(dx / z, dy / z, scale, f"raster correlation of the underlay (peak {peak:.1f}x mean)",
                        0, 0, 1.0 / z, [], [])


def register(hvac: Sheet, plan: Sheet, scale: float = 1.0) -> Registration:
    """Translation (and fixed scale ratio) that maps hvac sheet points onto plan sheet points."""
    hx, hy = grid_lines(underlay_segs(hvac), hvac.width_pt, hvac.height_pt)
    px, py = grid_lines(plan.segs, plan.width_pt, plan.height_pt)
    return register_grids(hx, hy, px, py, scale) or register_raster(hvac, plan, scale)
