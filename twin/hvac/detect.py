"""Find HVAC elements in the services layer of a vector sheet (all coordinates in sheet points).

Symbols (as drawn on typical CAD services / ACMV sheets):
    supply diffuser   nested squares (the 4-way pyramid symbol), 0.2-1.0 m
    return grille     a rectangle with one diagonal, face size = the drawn rectangle
    fan coil unit     a casing outline with two small concentric "marker" boxes on its long axis; the tag
                      (e.g. 1FC-23) and design airflow (635 L/s) are printed next to it
    duct              two parallel lines (spacing = duct width); transitions and plenums are drawn as quads

Labels come from OCR (twin.hvac.ocr): unit tags, airflows (L/s) and size callouts such as
'SAD 750x250' (supply air duct 750 mm wide x 250 mm high) or 'RAG 1000x400' (return air grille).
Every detection keeps its evidence so the importer can report how it was found.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from twin.hvac.ocr import TextItem

TAG_RE = re.compile(r"(?<![A-Z0-9])(\d{0,2}[A-Z]{1,4})\s*-\s*(\d{1,3}[A-Z]?)(?![0-9])")
FLOW_RE = re.compile(r"(\d{2,4})\s*L\s*(?:/\s*[sS5]?)?(?![A-Za-z])")
SIZE_RE = re.compile(r"(?<![A-Z])(SAD|RAD|SAG|RAG|SAR|RAR|EAD|EAG|FAD|FAG|CD|LD|SD|RD|EAR|FAR)\s*[-_~]?\s*(\d{2,4})\s*[xX×]\s*(\d{2,4})")
NUM_RE = re.compile(r"^[^\dA-Za-z]{0,2}(\d{2,4})[^\dA-Za-z]{0,2}$")
SUPPLY_CODES = {"SAD", "SAG", "SAR", "CD", "LD", "SD"}
DUCT_CODES = {"SAD", "RAD", "EAD", "FAD", "SD", "RD"}
RETURN_CODES = {"RAD", "RAG", "RAR", "RD"}


@dataclass
class Label:
    kind: str            # tag | flow | size | number
    text: str
    x: float
    y: float
    value: float | None = None
    code: str = ""
    w_mm: float | None = None
    h_mm: float | None = None
    used: bool = False


@dataclass
class Found:
    kind: str                        # supply_diffuser | return_grille | unit | duct
    x: float
    y: float
    angle: float = 0.0               # degrees, sheet frame (y down)
    w: float = 0.0                   # extent along the local x axis, points
    d: float = 0.0                   # extent along the local y axis, points
    tag: str = ""
    flow_lps: float | None = None
    flow_source: str = ""
    path: list[tuple[float, float]] = field(default_factory=list)   # ducts: centreline
    width_end: float | None = None   # ducts: tapered transition end width (points)
    system: str = ""
    height_mm: float | None = None
    label: str = ""
    evidence: str = ""
    unit: str = ""                   # tag of the serving unit
    anchor: tuple[float, float] | None = None   # units: where the tag was printed


# ---------------------------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------------------------

def _token_x(item: TextItem, start: int, end: int) -> tuple[float, float]:
    """Position of a substring of a (possibly merged) OCR line, assuming even character spacing."""
    n = max(1, len(item.text))
    f = (start + end) / 2 / n - 0.5
    a = math.radians(item.angle)
    return item.x + f * item.w * math.cos(a), item.y + f * item.w * math.sin(a)


def parse_labels(items: list[TextItem]) -> list[Label]:
    out: list[Label] = []
    for it in items:
        up = it.text.upper().replace("—", "-").replace("–", "-")
        taken: list[tuple[int, int]] = []
        for m in SIZE_RE.finditer(up):
            x, y = _token_x(it, m.start(), m.end())
            out.append(Label("size", m.group(0), x, y, code=m.group(1), w_mm=float(m.group(2)), h_mm=float(m.group(3))))
            taken.append(m.span())
        for m in TAG_RE.finditer(up):
            if any(a <= m.start() < b for a, b in taken):
                continue
            prefix = m.group(1)
            if prefix in SUPPLY_CODES | RETURN_CODES:
                continue
            x, y = _token_x(it, m.start(), m.end())
            out.append(Label("tag", f"{prefix}-{m.group(2)}", x, y))
            taken.append(m.span())
        for m in FLOW_RE.finditer(up):
            if any(a <= m.start() < b for a, b in taken):
                continue
            v = float(m.group(1))
            if 10 <= v <= 20000:
                x, y = _token_x(it, m.start(), m.end())
                out.append(Label("flow", m.group(0), x, y, value=v))
                taken.append(m.span())
        if not taken:
            m = NUM_RE.match(up.strip())
            if m and 20 <= float(m.group(1)) <= 3000:
                out.append(Label("number", m.group(1), it.x, it.y, value=float(m.group(1))))
    return out


# ---------------------------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------------------------

def _cross(a: np.ndarray, b: np.ndarray):
    """z component of the 2-D cross product (works on arrays of vectors)."""
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _quad_frame(q: np.ndarray) -> tuple[float, float, float, float, float]:
    c = q.mean(axis=0)
    w = float(np.linalg.norm(q[1] - q[0]))
    d = float(np.linalg.norm(q[3] - q[0]))
    ang = math.degrees(math.atan2(q[1][1] - q[0][1], q[1][0] - q[0][0]))
    return float(c[0]), float(c[1]), w, d, ang


def _is_rect(q: np.ndarray, tol: float = 0.12) -> bool:
    a, b, d = q[1] - q[0], q[2] - q[1], q[3] - q[0]
    la, lb = np.linalg.norm(a), np.linalg.norm(b)
    if la < 1e-6 or lb < 1e-6:
        return False
    return abs(np.dot(a, b)) / (la * lb) < tol and np.linalg.norm(q[2] - q[3] - a) < 0.15 * la + 0.3


def _seg_dir(s: np.ndarray) -> tuple[float, float, float]:
    dx, dy = s[2] - s[0], s[3] - s[1]
    L = math.hypot(dx, dy)
    return dx / L, dy / L, L


def _norm_angle(a: float) -> float:
    a = a % 180.0
    return a - 180.0 if a >= 90.0 else a


# ---------------------------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------------------------

def concentric_groups(quads: np.ndarray, tol: float = 0.8) -> list[list[int]]:
    cen = quads.mean(axis=1)
    used = np.zeros(len(quads), bool)
    groups = []
    order = np.argsort(-np.abs(_cross(quads[:, 1] - quads[:, 0], quads[:, 3] - quads[:, 0])))
    for i in order:
        if used[i]:
            continue
        d = np.hypot(cen[:, 0] - cen[i, 0], cen[:, 1] - cen[i, 1])
        mem = [int(j) for j in np.where((d < tol) & ~used)[0]]
        for j in mem:
            used[j] = True
        groups.append(sorted(mem, key=lambda j: -abs(_cross(quads[j, 1] - quads[j, 0], quads[j, 3] - quads[j, 0]))))
    return groups


def diagonals(q: np.ndarray, segs: np.ndarray, tol: float = 0.9) -> int:
    """How many of the quad's two diagonals are drawn corner to corner (one = the return-grille symbol)."""
    n_found = 0
    for a, b in ((q[0], q[2]), (q[1], q[3])):
        ab = b - a
        L = float(np.linalg.norm(ab))
        if L < 1e-6:
            continue
        u = ab / L
        n = np.array([-u[1], u[0]])
        p0, p1 = segs[:, :2] - a, segs[:, 2:] - a
        d0, d1 = p0 @ n, p1 @ n
        s0, s1 = p0 @ u, p1 @ u
        on = (np.abs(d0) < tol) & (np.abs(d1) < tol)
        if not on.any():
            continue
        lo = np.clip(np.minimum(s0[on], s1[on]), 0, L)
        hi = np.clip(np.maximum(s0[on], s1[on]), 0, L)
        if (hi - lo).sum() >= 0.7 * L:
            n_found += 1
    return n_found


def detect(segs: np.ndarray, quads: np.ndarray, labels: list[Label], m_per_pt: float,
           text_boxes: list[tuple[float, float, float, float]] | None = None) -> tuple[list[Found], list[str]]:
    """Find terminals, units and ducts. Returns detections and notes (sheet points)."""
    M = 1.0 / m_per_pt               # points per metre
    notes: list[str] = []
    found: list[Found] = []
    claimed = np.zeros(len(quads), bool)

    # ---- supply diffusers: nested near-square quads ----------------------------------------
    groups = concentric_groups(quads)
    markers: list[tuple[float, float, float]] = []      # small concentric boxes of unit symbols
    for g in groups:
        x, y, w, d, ang = _quad_frame(quads[g[0]])
        lo, hi = min(w, d), max(w, d)
        if len(g) >= 2 and 0.2 * M <= lo and hi <= 1.0 * M and hi / max(lo, 1e-6) <= 1.35:
            found.append(Found("supply_diffuser", x, y, _norm_angle(ang), w, d,
                               evidence=f"{len(g)} nested squares {lo / M:.2f} m"))
            claimed[g] = True
        elif len(g) == 2 and 0.11 * M <= lo and hi <= 0.21 * M and hi / max(lo, 1e-6) <= 1.25:
            markers.append((x, y, ang))
            claimed[g] = True

    # ---- return grilles: rectangle + one diagonal ---------------------------------------------
    for i, q in enumerate(quads):
        if claimed[i] or not _is_rect(q):
            continue
        x, y, w, d, ang = _quad_frame(q)
        lo, hi = min(w, d), max(w, d)
        if 0.15 * M <= lo and 0.25 * M <= hi <= 2.0 * M and diagonals(q, segs) == 1:
            if w < d:   # local x = long side
                w, d, ang = d, w, ang + 90.0
            found.append(Found("return_grille", x, y, _norm_angle(ang), w, d,
                               evidence=f"rectangle {hi / M:.2f} x {lo / M:.2f} m with a diagonal"))
            claimed[i] = True

    # ---- fan coil units: pairs of marker boxes, refined by the casing outline ------------------
    units = _units_from_markers(markers, segs, M)
    tags = [lab for lab in labels if lab.kind == "tag"]
    flows = [lab for lab in labels if lab.kind == "flow"]
    pairs = sorted(((math.hypot(t.x - u.x, t.y - u.y), ti, ui) for ti, t in enumerate(tags) for ui, u in enumerate(units)),
                   key=lambda p: p[0])
    tag_of: dict[int, int] = {}
    unit_taken: set[int] = set()
    for dist, ti, ui in pairs:
        if dist > 3.6 * M or ti in tag_of or ui in unit_taken:
            continue
        tag_of[ti] = ui
        unit_taken.add(ui)
    for ti, t in enumerate(tags):
        t.used = True
        flow = _nearest(flows, t.x, t.y + 0.35 * M, 1.1 * M, below_of=t)
        if ti in tag_of:
            u = units[tag_of[ti]]
            u.tag, u.evidence = t.text, u.evidence + f"; tag '{t.text}' {math.hypot(t.x - u.x, t.y - u.y) / M:.1f} m away"
        else:
            u = _box_near(t, segs, M, text_boxes or [])
            if u is None:  # symbol not recognised: place the unit next to its tag and flag it for review
                u = Found("unit", t.x, t.y + 0.9 * M, 0.0, 1.2 * M, 0.7 * M, tag=t.text,
                          evidence=f"tag '{t.text}' found but no unit symbol nearby; position estimated next to the tag")
            u.tag = t.text
            units.append(u)
            unit_taken.add(len(units) - 1)
        u.anchor = (t.x, t.y)
        if flow is not None:
            flow.used = True
            u.flow_lps, u.flow_source = flow.value, f"label '{flow.text}'"
    untagged = [u for i, u in enumerate(units) if i not in unit_taken]
    gap = _single_gap([t.text for t in tags])
    if len(untagged) == 1 and gap:
        # one unreadable tag and one missing number in an otherwise complete sequence (1FC-01..40 without 04)
        untagged[0].tag = gap
        untagged[0].evidence += f"; tag not readable, inferred as {gap} (the only number missing from the tag sequence)"
        notes.append(f"one unit symbol had no readable tag; named {gap}, the only number missing from the sequence")
    else:
        for k, u in enumerate(untagged, start=1):
            u.tag = f"FCU-?{k}"
            u.evidence += "; no tag found nearby"
        if untagged:
            notes.append(f"{len(untagged)} unit symbol(s) without a readable tag")
    found.extend(units)

    # ---- ducts ---------------------------------------------------------------------------------
    found.extend(_ducts(segs, quads, claimed, found, labels, M, text_boxes or []))
    return found, notes


def _single_gap(tags: list[str]) -> str | None:
    """'1FC-05' when the tags are 1FC-01..1FC-40 with exactly that one number missing."""
    by_prefix: dict[str, list[tuple[int, str]]] = {}
    for t in tags:
        m = re.match(r"^(.*-)(\d+)$", t)
        if m:
            by_prefix.setdefault(m.group(1), []).append((int(m.group(2)), m.group(2)))
    if len(by_prefix) != 1:
        return None
    prefix, nums = next(iter(by_prefix.items()))
    have = {n for n, _ in nums}
    lo, hi = min(have), max(have)
    missing = [n for n in range(max(1, lo), hi + 1) if n not in have]
    if len(missing) != 1 or len(have) < 5:
        return None
    width = max(len(txt) for _, txt in nums)
    return f"{prefix}{missing[0]:0{width}d}"


def _nearest(labels: list[Label], x: float, y: float, r: float, below_of: Label | None = None) -> Label | None:
    best, bd = None, r
    for lab in labels:
        if lab.used:
            continue
        if below_of is not None and lab.y < below_of.y - 2:
            continue
        d = math.hypot(lab.x - x, lab.y - y)
        if d < bd:
            best, bd = lab, d
    return best


def _box_near(tag: Label, segs: np.ndarray, M: float, text_boxes: list[tuple[float, float, float, float]]) -> Found | None:
    """Fallback unit symbol next to a tag: the nearest pair of parallel strokes forming a small casing
    (e.g. a wall-mounted split unit, 0.6-1.6 m long and 0.15-0.6 m deep)."""
    mid = (segs[:, :2] + segs[:, 2:]) / 2
    L = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    near = (np.hypot(mid[:, 0] - tag.x, mid[:, 1] - tag.y) < 2.5 * M) & (L >= 0.5 * M) & (L <= 1.8 * M)
    for x0, y0, x1, y1 in text_boxes:
        hgt = y1 - y0
        near &= ~((mid[:, 0] > x0 - 2) & (mid[:, 0] < x1 + 2) & (mid[:, 1] > y0 - 0.9 * hgt) & (mid[:, 1] < y1 + 0.9 * hgt))
    idx = np.where(near)[0]
    best = None
    for a_i in idx:
        ux, uy, La = _seg_dir(segs[a_i])
        u, n = np.array([ux, uy]), np.array([-uy, ux])
        for b_i in idx:
            if b_i <= a_i:
                continue
            vx, vy, Lb = _seg_dir(segs[b_i])
            if abs(ux * vy - uy * vx) > 0.03:
                continue
            p0, p1 = segs[b_i, :2] - segs[a_i, :2], segs[b_i, 2:] - segs[a_i, :2]
            d = float((p0 @ n + p1 @ n) / 2)
            lo, hi = max(0.0, min(p0 @ u, p1 @ u)), min(La, max(p0 @ u, p1 @ u))
            if not (0.15 * M <= abs(d) <= 0.8 * M) or hi - lo < 0.45 * M:
                continue
            c = segs[a_i, :2] + u * (lo + hi) / 2 + n * d / 2
            dist = float(np.hypot(c[0] - tag.x, c[1] - tag.y))
            if best is None or dist < best[0]:
                best = (dist, c, hi - lo, abs(d), math.degrees(math.atan2(uy, ux)))
    if best is None:
        return None
    dist, c, length, depth, ang = best
    wall = depth <= 0.45 * M
    kind_note = "wall-mounted split unit" if wall else "exposed unit casing (discharges into the room)"
    f = Found("unit", float(c[0]), float(c[1]), _norm_angle(ang), length, depth,
              evidence=f"{kind_note}: outline {dist / M:.1f} m from the tag (no fan-coil marker boxes)")
    f.system = "split_wall" if wall else "exposed"
    return f


def _units_from_markers(markers: list[tuple[float, float, float]], segs: np.ndarray, M: float) -> list[Found]:
    """Pair marker boxes (two per unit symbol) and measure the casing from the outline around them."""
    pts = np.array([(m[0], m[1]) for m in markers]).reshape(-1, 2)
    used = set()
    cand = sorted(((float(np.hypot(*(pts[i] - pts[j]))), i, j) for i in range(len(pts)) for j in range(i + 1, len(pts))))
    units: list[Found] = []
    seg_dirs = np.arctan2(segs[:, 3] - segs[:, 1], segs[:, 2] - segs[:, 0])
    seg_len = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    for dist, i, j in cand:
        if dist < 0.25 * M or dist > 1.3 * M or i in used or j in used:
            continue
        used.update((i, j))
        c = (pts[i] + pts[j]) / 2
        u = (pts[j] - pts[i]) / dist
        n = np.array([-u[1], u[0]])
        ang = math.atan2(u[1], u[0])
        par = np.abs(np.sin(seg_dirs - ang)) < 0.035
        p0, p1 = segs[:, :2] - c, segs[:, 2:] - c
        v0, v1 = p0 @ n, p1 @ n
        s0, s1 = p0 @ u, p1 @ u
        half = dist / 2
        spans = (np.minimum(s0, s1) <= -half + 1) & (np.maximum(s0, s1) >= half - 1)
        near = par & spans & (np.abs(v0 - v1) < 0.5) & (np.abs(v0) <= 0.6 * M) & (np.abs(v0) >= 0.06 * M) & (seg_len >= dist)
        vs = v0[near]
        if (vs < 0).any() and (vs > 0).any():
            vlo, vhi = float(vs[vs < 0].min()), float(vs[vs > 0].max())
            ss = np.concatenate([s0[near], s1[near]])
            slo, shi = float(np.clip(ss.min(), -half - 0.35 * M, -half)), float(np.clip(ss.max(), half, half + 0.35 * M))
            ev = "two marker boxes + casing outline"
        else:
            vlo, vhi = -0.35 * M, 0.35 * M
            slo, shi = -half - 0.25 * M, half + 0.25 * M
            ev = "two marker boxes (casing outline not found; default casing size)"
        cx = c + u * (slo + shi) / 2 + n * (vlo + vhi) / 2
        units.append(Found("unit", float(cx[0]), float(cx[1]), _norm_angle(math.degrees(ang)),
                           shi - slo, vhi - vlo, evidence=ev))
    return units


def _ducts(segs: np.ndarray, quads: np.ndarray, claimed: np.ndarray, found: list[Found], labels: list[Label],
           M: float, text_boxes: list[tuple[float, float, float, float]]) -> list[Found]:
    """Duct runs from pairs of parallel strokes and from duct-shaped quads."""
    L = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    keep = L >= 0.3 * M
    mid = (segs[:, :2] + segs[:, 2:]) / 2
    # exclude strokes that belong to other symbols or to text (underlines, frames)
    for f in found:
        r = max(f.w, f.d) / 2 + 0.05 * M
        keep &= np.hypot(mid[:, 0] - f.x, mid[:, 1] - f.y) > r
    for x0, y0, x1, y1 in text_boxes:
        hgt = y1 - y0
        keep &= ~((mid[:, 0] > x0 - 2) & (mid[:, 0] < x1 + 2) & (mid[:, 1] > y0 - 0.9 * hgt) & (mid[:, 1] < y1 + 0.9 * hgt))
    keep &= ~_leader_mask(segs, text_boxes)
    S = segs[keep]
    pieces: list[tuple[np.ndarray, np.ndarray, float]] = []   # (a, b, width) centreline in points
    transitions: list[Found] = []
    duct_widths = sorted({lab.w_mm / 1000 * M for lab in labels if lab.kind == "size" and lab.code in DUCT_CODES})
    width_ok = (lambda w: any(abs(w - dw) <= 0.03 * M for dw in duct_widths)) if duct_widths else (lambda w: True)
    # canonical direction (angle in [0, pi)) so "left" means the same side for every parallel stroke
    flip = (S[:, 3] - S[:, 1] < 0) | ((np.abs(S[:, 3] - S[:, 1]) < 1e-9) & (S[:, 2] < S[:, 0]))
    S = np.where(flip[:, None], S[:, [2, 3, 0, 1]], S)
    ang = np.arctan2(S[:, 3] - S[:, 1], S[:, 2] - S[:, 0])
    nearest = np.full((len(S), 2), -1)            # nearest parallel stroke on the left (0) / right (1)
    for i in range(len(S)):
        cand = np.where((np.abs(np.sin(ang - ang[i])) < 0.02) & (np.arange(len(S)) != i))[0]
        if not len(cand):
            continue
        ux, uy, Li = _seg_dir(S[i])
        u, n = np.array([ux, uy]), np.array([-uy, ux])
        p0, p1 = S[cand, :2] - S[i, :2], S[cand, 2:] - S[i, :2]
        d0, d1 = p0 @ n, p1 @ n
        dmean = (d0 + d1) / 2
        lo = np.maximum(np.minimum(p0 @ u, p1 @ u), 0)
        hi = np.minimum(np.maximum(p0 @ u, p1 @ u), Li)
        ok = (np.abs(d0 - d1) < 0.6) & (np.abs(dmean) >= 0.08 * M) & (np.abs(dmean) <= 1.5 * M) & (hi - lo >= 0.2 * M)
        for side, sign in ((0, 1), (1, -1)):
            sel = ok & (np.sign(dmean) == sign)
            if sel.any():
                nearest[i, side] = cand[sel][np.argmin(np.abs(dmean[sel]))]
    for i in range(len(S)):
        k = nearest[i, 0]
        if k < 0 or nearest[k, 1] != i:       # mutual nearest neighbours only
            continue
        ux, uy, Li = _seg_dir(S[i])
        u, n = np.array([ux, uy]), np.array([-uy, ux])
        pk0, pk1 = S[k, :2] - S[i, :2], S[k, 2:] - S[i, :2]
        w = float(((pk0 + pk1) / 2) @ n)
        lo = max(0.0, min(pk0 @ u, pk1 @ u))
        hi = min(Li, max(pk0 @ u, pk1 @ u))
        if hi - lo < 0.2 * M or not width_ok(abs(w)):
            continue
        p = S[i, :2] + n * (w / 2)
        pieces.append((p + u * lo, p + u * hi, abs(w)))
    # duct-shaped quads (plenums, straight sections, tapered transitions)
    for i, q in enumerate(quads):
        if claimed[i]:
            continue
        x, y, w, d, angq = _quad_frame(q)
        if any(f.kind == "unit" and abs(f.x - x) < f.w / 2 and abs(f.y - y) < f.w / 2 for f in found):
            continue
        e = [float(np.linalg.norm(q[(k + 1) % 4] - q[k])) for k in range(4)]
        if _is_rect(q):
            lo_, hi_ = min(w, d), max(w, d)
            if 0.12 * M <= lo_ <= 1.3 * M and hi_ >= 0.3 * M:
                if w >= d:
                    a_, b_ = (q[0] + q[3]) / 2, (q[1] + q[2]) / 2
                else:
                    a_, b_ = (q[0] + q[1]) / 2, (q[3] + q[2]) / 2
                pieces.append((a_, b_, lo_))
        else:  # trapezoid transition: two parallel sides of different length
            for k in (0, 1):
                s1v, s2v = q[(k + 1) % 4] - q[k], q[(k + 2) % 4] - q[(k + 3) % 4]
                if abs(_cross(s1v, s2v)) / (np.linalg.norm(s1v) * np.linalg.norm(s2v) + 1e-9) < 0.03:
                    a_ = (q[k] + q[(k + 1) % 4]) / 2
                    b_ = (q[(k + 2) % 4] + q[(k + 3) % 4]) / 2
                    wa, wb = e[k], e[(k + 2) % 4]
                    if 0.1 * M <= min(wa, wb) and max(wa, wb) <= 1.6 * M and np.linalg.norm(b_ - a_) >= 0.1 * M:
                        transitions.append(Found("duct", float((a_[0] + b_[0]) / 2), float((a_[1] + b_[1]) / 2),
                                                 path=[(float(a_[0]), float(a_[1])), (float(b_[0]), float(b_[1]))],
                                                 w=wa, width_end=wb, evidence="tapered transition (quad)"))
                    break
    runs = _merge_pieces(pieces, M)
    runs = _join_runs(runs, M)
    out = []
    size_labels = [lab for lab in labels if lab.kind == "size" and lab.code in SUPPLY_CODES | RETURN_CODES and lab.code not in ("CD", "RAG", "SAR", "SAG", "RAR")]
    for a, b, w in runs:
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        lab = None
        bd = 5.0 * M
        for s in size_labels:
            if abs(s.w_mm / 1000 * M - w) <= 0.04 * M:
                dd = math.hypot(s.x - cx, s.y - cy)
                if dd < bd:
                    lab, bd = s, dd
        f = Found("duct", float(cx), float(cy), path=[(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))], w=float(w),
                  evidence="parallel strokes" + (f"; size from '{lab.text}'" if lab else "; height estimated"))
        if lab is not None:
            f.height_mm, f.label = lab.h_mm, lab.text
            f.system = "return" if lab.code in RETURN_CODES else "supply"
        out.append(f)
    return out + transitions


def _leader_mask(segs: np.ndarray, text_boxes: list[tuple[float, float, float, float]]) -> np.ndarray:
    """Strokes of label leaders: they start at a text box and continue as a chain of single strokes."""
    out = np.zeros(len(segs), bool)
    if not text_boxes or not len(segs):
        return out
    ends = np.concatenate([segs[:, :2], segs[:, 2:]])            # 2N endpoints
    near = np.zeros(len(ends), bool)
    for x0, y0, x1, y1 in text_boxes:
        hgt = max(y1 - y0, 1.0)
        near |= ((ends[:, 0] > x0 - 0.6 * hgt) & (ends[:, 0] < x1 + 0.6 * hgt) &
                 (ends[:, 1] > y0 - 0.8 * hgt) & (ends[:, 1] < y1 + 0.8 * hgt))
    n = len(segs)
    out = near[:n] | near[n:]
    key = np.round(ends / 0.2).astype(np.int64)
    from collections import defaultdict
    at: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (kx, ky) in enumerate(key):
        at[(kx, ky)].append(idx % n)
    frontier = list(np.where(out)[0])
    for _ in range(3):  # follow the leader through up to three bends
        nxt = []
        for i in frontier:
            for e in (key[i], key[i + n]):
                touching = [j for j in at[(e[0], e[1])] if j != i]
                if len(touching) == 1 and not out[touching[0]]:
                    out[touching[0]] = True
                    nxt.append(touching[0])
        frontier = nxt
    return out


def _merge_pieces(pieces: list[tuple[np.ndarray, np.ndarray, float]], M: float) -> list[tuple[np.ndarray, np.ndarray, float]]:
    """Merge collinear pieces of equal width into runs; drop exact duplicates."""
    runs = [(np.array(a, float), np.array(b, float), w) for a, b, w in pieces]
    changed = True
    while changed:
        changed = False
        for i in range(len(runs)):
            if changed:
                break
            a, b, w = runs[i]
            u = (b - a) / (np.linalg.norm(b - a) + 1e-9)
            n = np.array([-u[1], u[0]])
            for j in range(i + 1, len(runs)):
                c, d, w2 = runs[j]
                if abs(w - w2) > 0.04 * M:
                    continue
                v = (d - c) / (np.linalg.norm(d - c) + 1e-9)
                if abs(u[0] * v[1] - u[1] * v[0]) > 0.03:
                    continue
                if abs((c - a) @ n) > 0.06 * M or abs((d - a) @ n) > 0.06 * M:
                    continue
                s = sorted([0.0, float((b - a) @ u), float((c - a) @ u), float((d - a) @ u)])
                ia = (0.0, float((b - a) @ u))
                ib = tuple(sorted((float((c - a) @ u), float((d - a) @ u))))
                gap = max(ib[0] - ia[1], ia[0] - ib[1])
                if gap > max(0.35 * M, 0.8 * w):
                    continue
                runs[i] = (a + u * s[0], a + u * s[-1], max(w, w2))
                runs.pop(j)
                changed = True
                break
    return runs


def _join_runs(runs: list[tuple[np.ndarray, np.ndarray, float]], M: float) -> list[tuple[np.ndarray, np.ndarray, float]]:
    """Close elbows and tees: extend a run end to the centreline of a crossing run close by."""
    out = [list(r) for r in runs]
    for i, (a, b, w) in enumerate(out):
        for end in (0, 1):
            p = out[i][end]
            q = out[i][1 - end]
            u = (p - q) / (np.linalg.norm(p - q) + 1e-9)
            best = None
            for j, (c, d, w2) in enumerate(out):
                if j == i:
                    continue
                v = (d - c) / (np.linalg.norm(d - c) + 1e-9)
                den = u[0] * v[1] - u[1] * v[0]
                if abs(den) < 0.5:          # need at least ~30 degrees between the runs
                    continue
                # intersection of the ray p + t*u with the line c + s*v
                diff = c - p
                t = (diff[0] * v[1] - diff[1] * v[0]) / den
                s = (diff[0] * u[1] - diff[1] * u[0]) / den
                Lj = float(np.linalg.norm(d - c))
                reach = max(w, w2) / 2 + 0.6 * M
                if -max(w2, w) / 2 <= t <= reach and -w2 / 2 - 0.6 * M <= s <= Lj + w2 / 2 + 0.6 * M:
                    if best is None or t < best[0]:
                        best = (t, p + u * t)
            if best is not None and best[0] > 0:
                out[i][end] = best[1]
    return [(np.array(a), np.array(b), w) for a, b, w in out]


def plausible_terminal_flow(lab: Label) -> bool:
    """Design airflows are round numbers: bare OCR numbers must be multiples of 5 in 20..900 L/s."""
    v = lab.value or 0
    if lab.kind == "number":
        return 20 <= v <= 900 and v % 5 == 0
    return 10 <= v <= 1500


def assign_flows(found: list[Found], labels: list[Label], M: float) -> None:
    """Airflow labels ('185' + 'L/s', or '185 L/s') to the nearest supply diffuser."""
    diffusers = [f for f in found if f.kind == "supply_diffuser"]
    cands = [lab for lab in labels if lab.kind in ("flow", "number") and not lab.used and plausible_terminal_flow(lab)]
    pairs = sorted(((math.hypot(lab.x - f.x, lab.y - f.y), li, fi) for li, lab in enumerate(cands)
                    for fi, f in enumerate(diffusers)), key=lambda p: p[0])
    done_l, done_f = set(), set()
    for dist, li, fi in pairs:
        if dist > 1.6 * M or li in done_l or fi in done_f:
            continue
        done_l.add(li); done_f.add(fi)
        diffusers[fi].flow_lps = cands[li].value
        diffusers[fi].flow_source = f"label '{cands[li].text}' {dist / M:.1f} m away"
        cands[li].used = True
