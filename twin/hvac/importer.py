"""HVAC drawing -> scene model: register, read, detect, place, associate, report.

    import_hvac(hvac_pdf, plan, scene) -> ImportResult(hvac, report)

1. the services layer and the architectural underlay are separated (twin.hvac.sheet);
2. the sheet is registered to the floor plan on the shared structural grid (twin.hvac.register), so
   every point maps into the scene's PLAN FRAME (metres, origin at the floor-plan sheet's bottom-left);
3. labels are read with OCR (twin.hvac.ocr), symbols are found from the vector strokes (twin.hvac.detect);
4. detections outside the building are dropped, the rest get a room, terminals and ducts are tied to the
   unit that serves them by following the duct network, and airflows come from the labels (or are
   estimated from the unit's airflow and flagged);
5. the result is a twin.scene.model.Hvac with certainty flags and a report of what was found.
Nothing here guesses a measurement silently: estimated values carry status 'estimated' and a note.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from twin.hvac import detect as det
from twin.hvac.ocr import ZOOM, TextItem, read_labels
from twin.hvac.register import (Registration, grid_lines, register_grids, register_grids_scaled, register_raster, scale_ratios,
                                underlay_segs)
from twin.hvac.sheet import PT_TO_M_PAPER, Sheet, load_sheet, quad_edges, render
from twin.scene import geometry as g
from twin.scene.model import Certainty, Duct, Hvac, HvacSource, HvacTerminal, HvacUnit, Scene, Size3

OCR_CACHE = "hvac_ocr.json"
GRID_CACHE = "grid_lines_v2.json"   # v2: dash-dot lines with >= 20 pieces (1:75 sheets)


class HvacImportError(ValueError):
    def __init__(self, code: str, message: str, detail=None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class PlanSheetInfo:
    """The floor plan the scene was traced from (plan frame = its sheet, bottom-left origin)."""
    pdf: bytes | None
    page_h_pt: float
    m_per_pt: float
    cache_dir: Path | None = None
    scale: int | None = None


@dataclass
class ImportResult:
    hvac: Hvac
    report: dict
    detections: list[det.Found] = field(default_factory=list)


def _safe_id(prefix: str, text: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", f"{prefix}{text}")[:56]
    out, k = base, 2
    while out in taken:
        out, k = f"{base}_{k}", k + 1
    taken.add(out)
    return out


def _rect(cx: float, cy: float, w: float, d: float, ang_deg: float) -> Polygon:
    a = math.radians(ang_deg)
    c, s = math.cos(a), math.sin(a)
    return Polygon([(cx + lx * c - ly * s, cy + lx * s + ly * c)
                    for lx, ly in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))])


def _duct_poly(path: list[tuple[float, float]], w: float, w_end: float | None = None) -> Polygon:
    if w_end is not None and len(path) == 2:
        (ax, ay), (bx, by) = path
        L = math.hypot(bx - ax, by - ay) or 1e-9
        nx, ny = -(by - ay) / L, (bx - ax) / L
        return Polygon([(ax + nx * w / 2, ay + ny * w / 2), (bx + nx * w_end / 2, by + ny * w_end / 2),
                        (bx - nx * w_end / 2, by - ny * w_end / 2), (ax - nx * w / 2, ay - ny * w / 2)])
    return LineString(path).buffer(w / 2, cap_style="flat", join_style="mitre")


# ---------------------------------------------------------------------------------------------
# cached steps
# ---------------------------------------------------------------------------------------------

def plan_grid(plan: PlanSheetInfo) -> tuple[list[float], list[float], Sheet | None]:
    cache = plan.cache_dir / GRID_CACHE if plan.cache_dir else None
    if cache and cache.is_file():
        d = json.loads(cache.read_text(encoding="utf-8"))
        return d["x"], d["y"], None
    if plan.pdf is None:
        return [], [], None
    sheet = load_sheet(plan.pdf)
    px, py = grid_lines(sheet.segs, sheet.width_pt, sheet.height_pt)
    if cache:
        cache.write_text(json.dumps({"x": px, "y": py}), encoding="utf-8")
    return px, py, sheet


def ocr_labels(sheet: Sheet, box: tuple[float, float, float, float], cache_dir: Path | None,
               on_progress=None) -> list[TextItem]:
    key = {"box": [round(v, 1) for v in box], "zoom": ZOOM, "n_fg": int(sheet.seg_fg.sum()), "v": 1}
    cache = cache_dir / OCR_CACHE if cache_dir else None
    if cache and cache.is_file():
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("key") == key:
            return [TextItem(**t) for t in d["items"]]
    segs = np.concatenate([sheet.fg_segs, quad_edges(sheet.fg_quads)])
    items = read_labels(segs, box, on_progress=on_progress)
    if cache:
        cache.write_text(json.dumps({"key": key, "items": [t.as_dict() for t in items]}), encoding="utf-8")
    return items


def reread_numbers(sheet: Sheet, targets: list[det.Found], M: float, cache_dir: Path | None) -> list[TextItem]:
    """Second, closer OCR pass around diffusers whose airflow label was not read (labels overlap the
    throw arrows of the symbol, which the sheet-wide pass sometimes misses)."""
    if not targets:
        return []
    cache = cache_dir / "hvac_ocr_numbers.json" if cache_dir else None
    key = sorted([round(t.x, 1), round(t.y, 1)] for t in targets)
    if cache and cache.is_file():
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("key") == key:
            return [TextItem(**t) for t in d["items"]]
    segs = np.concatenate([sheet.fg_segs, quad_edges(sheet.fg_quads)])
    out: list[TextItem] = []
    r = 1.5 * M
    for t in targets:
        box = (t.x - r, t.y - r, t.x + r, t.y + r)
        out.extend(read_labels(segs, box, zoom=5.0))
    from twin.hvac.ocr import dedupe
    out = dedupe(out)
    if cache:
        cache.write_text(json.dumps({"key": key, "items": [i.as_dict() for i in out]}), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------------------------

def import_hvac(hvac_pdf: bytes, plan: PlanSheetInfo, scene: Scene, *, file_id: str | None = None,
                file_name: str = "", cache_dir: Path | None = None, manual: dict | None = None,
                on_progress=None) -> ImportResult:
    t0 = time.time()
    timings: dict[str, float] = {}
    prog = on_progress or (lambda f, s: None)
    if not scene.rooms:
        raise HvacImportError("HVAC_NO_LAYOUT", "The scene has no rooms; create the floor plan layout first.")
    level = scene.levels[0]

    prog(0.03, "reading the HVAC drawing")
    try:
        sheet = load_sheet(hvac_pdf)
    except Exception as exc:  # noqa: BLE001 - corrupt or non-vector PDF
        raise HvacImportError("HVAC_UNREADABLE", f"The HVAC drawing could not be read as a vector PDF: {exc}")
    if not sheet.seg_fg.any():
        raise HvacImportError("HVAC_NO_VECTORS", "The HVAC PDF has no vector linework (a scanned drawing?). "
                                                 "Raster HVAC plans are not supported yet; export the sheet from CAD as a vector PDF.")
    timings["load_s"] = round(time.time() - t0, 2)

    # ---- registration -------------------------------------------------------------------------
    prog(0.08, "registering the HVAC sheet to the floor plan")
    scale_ratio = 1.0
    hx, hy = grid_lines(underlay_segs(sheet), sheet.width_pt, sheet.height_pt)
    if manual:
        reg = Registration(float(manual.get("dx_pt", 0.0)), float(manual.get("dy_pt", 0.0)), float(manual.get("scale", 1.0)),
                           "manual alignment", 0, 0, 0.0, hx, hy)
    else:
        px, py, plan_sheet = plan_grid(plan)
        reg = register_grids_scaled(hx, hy, px, py, scale_ratios(plan.scale))
        if reg is None:
            if plan_sheet is None and plan.pdf is not None:
                plan_sheet = load_sheet(plan.pdf)
            if plan_sheet is None:
                raise HvacImportError("HVAC_REGISTRATION_FAILED",
                                      "The HVAC sheet could not be aligned: the floor plan is not a vector PDF and no "
                                      "grid lines match. Provide a manual alignment (dx_pt, dy_pt).")
            reg = register_raster(sheet, plan_sheet, scale_ratio)
    k = plan.m_per_pt
    H = plan.page_h_pt

    def to_plan(x: float, y: float) -> tuple[float, float]:
        X, Y = reg.apply(x, y)
        return float(X) * k, float(H - Y) * k

    M = 1.0 / (k * reg.scale)          # HVAC sheet points per metre
    timings["register_s"] = round(time.time() - t0 - sum(timings.values()), 2)

    # ---- OCR ----------------------------------------------------------------------------------
    if hx and hy:
        box = (min(hx) - 6 * M, min(hy) - 6 * M, max(hx) + 6 * M, max(hy) + 6 * M)
    else:
        fg = sheet.fg_segs
        box = (float(fg[:, [0, 2]].min()), float(fg[:, [1, 3]].min()), float(fg[:, [0, 2]].max()), float(fg[:, [1, 3]].max()))
    box = (max(0.0, box[0]), max(0.0, box[1]), min(sheet.width_pt, box[2]), min(sheet.height_pt, box[3]))
    prog(0.12, "reading labels (OCR)")
    items = ocr_labels(sheet, box, cache_dir, on_progress=lambda f: prog(0.12 + 0.45 * f, "reading labels (OCR)"))
    timings["ocr_s"] = round(time.time() - t0 - sum(timings.values()), 2)

    # ---- detection ------------------------------------------------------------------------------
    prog(0.6, "finding units, diffusers, grilles and ducts")
    labels = det.parse_labels(items)
    text_boxes = [(t.x - t.w / 2, t.y - t.h / 2, t.x + t.w / 2, t.y + t.h / 2) for t in items]
    found, notes = det.detect(sheet.fg_segs, sheet.fg_quads, labels, 1.0 / M, text_boxes)
    det.assign_flows(found, labels, M)
    missing = [f for f in found if f.kind == "supply_diffuser" and f.flow_lps is None]
    no_flow_units = [f for f in found if f.kind == "unit" and f.flow_lps is None and f.anchor]
    if missing or no_flow_units:
        prog(0.7, f"re-reading airflow labels around {len(missing)} diffusers and {len(no_flow_units)} units")
        spots = missing + [det.Found("spot", f.anchor[0], f.anchor[1] + 0.4 * M) for f in no_flow_units]
        extra = det.parse_labels(reread_numbers(sheet, spots, M, cache_dir))
        flows = [lab for lab in extra if lab.kind == "flow"]
        for f in no_flow_units:   # '380 L/s' printed under the tag
            lab = det._nearest(flows, f.anchor[0], f.anchor[1] + 0.4 * M, 0.9 * M)
            if lab is not None and lab.y > f.anchor[1]:
                lab.used = True
                f.flow_lps, f.flow_source = lab.value, f"label '{lab.text}' (second OCR pass)"
        det.assign_flows(found, [lab for lab in extra if lab.kind in ("flow", "number") and not lab.used], M)
    timings["detect_s"] = round(time.time() - t0 - sum(timings.values()), 2)

    # ---- into the plan frame; drop what lies outside the building ---------------------------------
    prog(0.85, "placing HVAC in the model")
    fps = g.wall_footprints(g.walls_on_level(scene, level.id)) if scene.walls else {}
    building = unary_union([g.room_polygon(r) for r in scene.rooms] + list(fps.values()))
    inside = building.buffer(0.6)
    room_polys = [(r, g.room_polygon(r)) for r in scene.rooms]

    def room_of(x: float, y: float) -> str | None:
        p = Point(x, y)
        for r, poly in room_polys:
            if poly.contains(p):
                return r.id
        best = min(room_polys, key=lambda rp: rp[1].distance(p))
        return best[0].id if best[1].distance(p) <= 1.5 else None

    placed: list[dict] = []
    dropped = 0
    for f in found:
        if f.kind == "duct":
            path = [to_plan(*p) for p in f.path]
            geom = LineString(path)
        else:
            path = []
            geom = Point(*to_plan(f.x, f.y))
        if not inside.intersects(geom):
            dropped += 1
            continue
        if f.kind == "duct" and not inside.contains(geom):
            clipped = geom.intersection(inside)
            if clipped.is_empty or clipped.geom_type != "LineString" or clipped.length < 0.3:
                dropped += 1
                continue
            path = [tuple(c) for c in clipped.coords]
        x, y = (geom.centroid.x, geom.centroid.y)
        placed.append({"f": f, "x": x, "y": y, "path": path, "ang": -f.angle,
                       "w": f.w / M, "d": f.d / M, "w_end": f.width_end / M if f.width_end else None})

    # ---- association: follow the duct network from every unit -----------------------------------
    units = [p for p in placed if p["f"].kind == "unit"]
    terms = [p for p in placed if p["f"].kind in ("supply_diffuser", "return_grille")]
    ducts = [p for p in placed if p["f"].kind == "duct"]
    for p in units:
        p["geom"] = _rect(p["x"], p["y"], p["w"], p["d"], p["ang"])
    for p in terms:
        p["geom"] = _rect(p["x"], p["y"], p["w"], p["d"], p["ang"])
    for p in ducts:
        p["geom"] = _duct_poly(p["path"], p["w"], p["w_end"])
    net = _network_distances(units, ducts, terms)
    owner = {di: min(range(len(units)), key=lambda ui: net["duct"][ui][di])
             for di in range(len(ducts)) if units and min(net["duct"][ui][di] for ui in range(len(units))) < math.inf}
    room_of_unit = [room_of(u["x"], u["y"]) for u in units]
    for ti, t in enumerate(terms):
        t["room"] = room_of(t["x"], t["y"])
        cost = []
        for ui, u in enumerate(units):
            dn = net["term"][ui][ti]
            de = math.hypot(u["x"] - t["x"], u["y"] - t["y"])
            if dn < math.inf:
                cost.append((dn, ui, "connected through the duct network" if dn > 0 else "attached to the unit"))
            elif de <= 15.0:
                same = room_of_unit[ui] == t["room"]
                cost.append((1.5 * de + (4.0 if same else 12.0), ui,
                             "nearest unit" + (" in the same room" if same else "") + " (no duct path found)"))
        cost.sort()
        t["cands"] = [c for c in cost if c[0] <= cost[0][0] + 15.0][:8] if cost else []
        t["unit"], t["link"] = (cost[0][1], cost[0][2]) if cost else (None, "no unit within 15 m")
    rebalanced = _rebalance(units, terms)
    # duct system: what the duct's own network component reaches
    comp = _components(ducts)
    comp_kind: dict[int, str] = {}
    for t in terms:
        for di, dct in enumerate(ducts):
            if t["geom"].buffer(0.75).intersects(dct["geom"]):
                c = comp[di]
                kind = "supply" if t["f"].kind == "supply_diffuser" else "return"
                if comp_kind.get(c) != "supply":
                    comp_kind[c] = kind

    # ---- airflows -------------------------------------------------------------------------------
    by_unit: dict[int, list[dict]] = defaultdict(list)
    for t in terms:
        if t["unit"] is not None:
            by_unit[t["unit"]].append(t)
    for ui, u in enumerate(units):
        sup = [t for t in by_unit[ui] if t["f"].kind == "supply_diffuser"]
        ret = [t for t in by_unit[ui] if t["f"].kind == "return_grille"]
        known = [t for t in sup if t["f"].flow_lps]
        unit_flow = u["f"].flow_lps
        if unit_flow is None and sup and len(known) == len(sup):
            unit_flow = sum(t["f"].flow_lps for t in sup)
            u["flow_est"] = "sum of its labelled diffusers"
        u["flow"] = unit_flow
        unknown = [t for t in sup if not t["f"].flow_lps]
        if unknown:
            if unit_flow:
                rest = unit_flow - sum(t["f"].flow_lps for t in known)
                # the labelled diffusers may already cover the unit (then this link is doubtful): keep a small share
                each = max(rest, 0.15 * unit_flow) / len(unknown)
                note = (f"estimated: {unit_flow:.0f} L/s of {u['f'].tag} shared over its unlabelled diffusers"
                        if rest > 0 else f"estimated: {u['f'].tag}'s labelled diffusers already supply its airflow; "
                                         "this link may be wrong")
            elif known:
                each, note = float(np.median([t["f"].flow_lps for t in known])), "estimated: typical of the unit's labelled diffusers"
            else:
                each, note = None, ""
            for t in unknown:
                t["flow"], t["flow_note"] = (round(float(each), 1) if each else None), note
        for t in known:
            t["flow"], t["flow_note"] = t["f"].flow_lps, t["f"].flow_source
        for t in ret:
            t["flow"] = round(unit_flow / len(ret), 1) if unit_flow else None
            t["flow_note"] = f"estimated: {u['f'].tag} airflow shared over {len(ret)} return grille(s)" if unit_flow else ""
        u["supply_sum"] = sum(t.get("flow") or 0 for t in sup)
        u["n_supply"], u["n_return"] = len(sup), len(ret)
        u["labelled_sum"] = sum(t["f"].flow_lps for t in known)
        u["n_labelled"] = len(known)

    # ---- scene objects ---------------------------------------------------------------------------
    taken: set[str] = set()
    hv_units: list[HvacUnit] = []
    unit_ids: dict[int, str] = {}
    for ui, u in enumerate(units):
        f = u["f"]
        uid = _safe_id("U_", f.tag, taken)
        unit_ids[ui] = uid
        split = f.system == "split_wall"
        kind = "split_wall" if split else "fcu_exposed" if f.system == "exposed" else "fcu_ducted"
        good = "marker" in f.evidence
        h = 0.32 if split else (0.35 if (u.get("flow") or 0) > 600 else 0.3)
        hv_units.append(HvacUnit(
            id=uid, tag=f.tag, kind=kind, level_id=level.id,
            room_id=room_of(u["x"], u["y"]), position=(round(u["x"], 3), round(u["y"], 3)),
            rotation_deg=round(u["ang"], 2),
            size=Size3(width=round(max(u["w"], 0.3), 3), depth=round(max(u["d"], 0.15), 3), height=h),
            elevation=2.2 if split else None,
            airflow_lps=u.get("flow"),
            certainty=Certainty(status="extracted" if good or f.system else "estimated", source="derived",
                                confidence=0.9 if good else 0.7 if f.system else 0.4,
                                note=(f.evidence + (f"; airflow {f.flow_source}" if f.flow_source else
                                                    f"; airflow {u['flow_est']}" if u.get("flow_est") else "; airflow not found"))[:500])))
    hv_terms: list[HvacTerminal] = []
    n_sd = n_rg = 0
    for t in terms:
        f = t["f"]
        sup = f.kind == "supply_diffuser"
        if sup:
            n_sd += 1
            tid = _safe_id("SD", str(n_sd), taken)
        else:
            n_rg += 1
            tid = _safe_id("RG", str(n_rg), taken)
        est = "estimated" in (t.get("flow_note") or "") or t.get("unit") is None or "no duct" in t["link"]
        hv_terms.append(HvacTerminal(
            id=tid, kind=f.kind, level_id=level.id, room_id=room_of(t["x"], t["y"]),
            position=(round(t["x"], 3), round(t["y"], 3)), rotation_deg=round(t["ang"], 2),
            width=round(max(t["w"], 0.06), 3), depth=round(max(t["d"], 0.03), 3),
            airflow_lps=t.get("flow"), throw="4way" if sup else "none",
            unit_id=unit_ids.get(t["unit"]) if t.get("unit") is not None else None,
            label=f"{t['flow']:.0f} L/s" if t.get("flow") else "",
            certainty=Certainty(status="estimated" if est else "extracted", source="derived",
                                confidence=0.6 if est else 0.85,
                                note=f"{f.evidence}; {t['link']}; airflow: {t.get('flow_note') or 'unknown'}"[:500])))
    hv_ducts: list[Duct] = []
    for di, p in enumerate(ducts):
        f = p["f"]
        system = f.system or comp_kind.get(comp[di], "supply")
        hgt = (f.height_mm / 1000) if f.height_mm else round(min(0.35, max(0.15, 0.35 * p["w"])), 2)
        hv_ducts.append(Duct(
            id=_safe_id("D", str(di + 1), taken), system=system, level_id=level.id,
            path=[(round(x, 3), round(y, 3)) for x, y in p["path"]], width=round(p["w"], 3),
            width_end=round(p["w_end"], 3) if p["w_end"] else None, height=hgt,
            unit_id=unit_ids.get(owner[di]) if di in owner else None, label=f.label,
            certainty=Certainty(status="extracted" if f.height_mm else "estimated", source="derived",
                                confidence=0.75 if f.height_mm else 0.55,
                                note=(f.evidence + ("" if f.height_mm else f"; height {hgt} m estimated (no size label)"))[:500])))
    src = HvacSource(file_id=file_id, file_name=file_name[:200], method=reg.method,
                     transform={"dx_pt": round(reg.dx, 4), "dy_pt": round(reg.dy, 4), "scale": reg.scale,
                                "m_per_pt": k, "plan_page_h_pt": H},
                     residual_m=round(reg.residual_pt * k, 4),
                     note=f"{sheet.layer_note}; OCR {len(items)} text items")
    hvac = Hvac(units=hv_units, terminals=hv_terms, ducts=hv_ducts, source=src)
    timings["place_s"] = round(time.time() - t0 - sum(timings.values()), 2)
    timings["total_s"] = round(time.time() - t0, 2)
    report = _report(hvac, units, labels, reg, k, dropped, notes, timings, sheet)
    report["counts"]["links_rebalanced"] = rebalanced
    prog(1.0, "done")
    return ImportResult(hvac=hvac, report=report, detections=found)


def _network_distances(units: list[dict], ducts: list[dict], terms: list[dict]) -> dict:
    """Path length along the duct network from every unit to every duct and terminal (inf = not
    connected). Gaps of up to 0.4 m between duct pieces are bridged (elbows and fittings the vector
    detection missed); terminals connect within 1.0 m (flexible connection to the diffuser neck)."""
    import heapq
    nd = len(ducts)
    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    grown = [d["geom"].buffer(0.4) for d in ducts]
    for i in range(nd):
        for j in range(i + 1, nd):
            if grown[i].intersects(ducts[j]["geom"]):
                w = ducts[i]["geom"].centroid.distance(ducts[j]["geom"].centroid)
                adj[i].append((j, w))
                adj[j].append((i, w))
    duct_d: list[list[float]] = []
    term_d: list[list[float]] = []
    t_zone = [t["geom"].buffer(1.0) for t in terms]
    for u in units:
        dist = [math.inf] * nd
        heap = []
        zone = u["geom"].buffer(0.6)
        for di, d in enumerate(ducts):
            if zone.intersects(d["geom"]):
                heapq.heappush(heap, (u["geom"].centroid.distance(d["geom"].centroid), di))
        while heap:
            w, di = heapq.heappop(heap)
            if w >= dist[di]:
                continue
            dist[di] = w
            for dj, wj in adj[di]:
                if w + wj < dist[dj]:
                    heapq.heappush(heap, (w + wj, dj))
        duct_d.append(dist)
        row = []
        for ti, t in enumerate(terms):
            if t["geom"].buffer(0.6).intersects(u["geom"]):
                row.append(0.0)
                continue
            best = math.inf
            for di, d in enumerate(ducts):
                if dist[di] < math.inf and t_zone[ti].intersects(d["geom"]):
                    best = min(best, dist[di] + t["geom"].distance(d["geom"]))
            row.append(best)
        term_d.append(row)
    return {"duct": duct_d, "term": term_d}


def _rebalance(units: list[dict], terms: list[dict], max_rounds: int = 200) -> int:
    """Use the designer's airflow balance (a unit's labelled airflow ~ the sum of its diffusers' labelled
    airflows) to settle ambiguous links: move a diffuser to another candidate unit when that reduces the
    total imbalance by more than the extra path length costs (5 L/s per metre). Returns the moves made."""
    sup = [t for t in terms if t["f"].kind == "supply_diffuser" and t["f"].flow_lps and t.get("cands")]
    units_ok = {ui for ui, u in enumerate(units) if u["f"].system not in ("split_wall", "exposed")}
    for t in sup:
        t["cands"] = [c for c in t["cands"] if c[1] in units_ok]
    rated = {ui: u["f"].flow_lps for ui, u in enumerate(units) if u["f"].flow_lps}
    if not sup or not rated:
        return 0
    load: dict[int, float] = defaultdict(float)
    for t in sup:
        if t["unit"] is not None:
            load[t["unit"]] += t["f"].flow_lps

    def imb(ui: int, extra: float = 0.0) -> float:
        return abs(load[ui] + extra - rated[ui]) if ui in rated else 0.0
    moves = 0
    for _ in range(max_rounds):
        best = None
        for t in sup:
            cur = t["unit"]
            q = t["f"].flow_lps
            cur_cost = next((c[0] for c in t["cands"] if c[1] == cur), 0.0)
            for cost, ui, why in t["cands"]:
                if ui == cur or ui not in rated:
                    continue
                gain = (imb(cur) + imb(ui)) - (imb(cur, -q) + imb(ui, q)) - 5.0 * (cost - cur_cost)
                if gain > 0.1 * q and (best is None or gain > best[0]):
                    best = (gain, t, ui, why)
        if best is None:
            break
        _, t, ui, why = best
        load[t["unit"]] -= t["f"].flow_lps
        load[ui] += t["f"].flow_lps
        t["unit"], t["link"] = ui, why + "; chosen to balance the unit airflows"
        moves += 1
    return moves


def _components(ducts: list[dict]) -> dict[int, int]:
    parent = list(range(len(ducts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    grown = [d["geom"].buffer(0.12) for d in ducts]
    for i in range(len(ducts)):
        for j in range(i + 1, len(ducts)):
            if grown[i].intersects(ducts[j]["geom"]):
                parent[find(i)] = find(j)
    return {i: find(i) for i in range(len(ducts))}


def _report(hvac: Hvac, units: list[dict], labels: list[det.Label], reg: Registration, k: float, dropped: int,
            notes: list[str], timings: dict, sheet: Sheet) -> dict:
    tags = sorted({lab.text for lab in labels if lab.kind == "tag"})
    unit_rows = []
    issues = list(notes)
    for u, hu in zip(units, hvac.units):
        sup = [t for t in hvac.terminals if t.unit_id == hu.id and t.kind == "supply_diffuser"]
        ret = [t for t in hvac.terminals if t.unit_id == hu.id and t.kind == "return_grille"]
        labelled = u.get("labelled_sum", 0.0)
        row = {"id": hu.id, "tag": hu.tag, "kind": hu.kind, "room_id": hu.room_id, "airflow_lps": hu.airflow_lps,
               "supply_diffusers": len(sup), "return_grilles": len(ret),
               "labelled_diffusers": u.get("n_labelled", 0), "labelled_supply_lps": round(labelled, 1),
               "status": hu.certainty.status, "evidence": hu.certainty.note}
        if hu.airflow_lps and u.get("n_labelled") and u.get("n_labelled") == len(sup) and sup:
            row["balance_pct"] = round(100.0 * labelled / hu.airflow_lps, 1)
            if abs(row["balance_pct"] - 100) > 15:
                issues.append(f"{hu.tag}: its diffusers add up to {labelled:.0f} L/s but the unit is labelled "
                              f"{hu.airflow_lps:.0f} L/s ({row['balance_pct']:.0f}%) - check the diffuser links")
        if not sup and hu.kind not in ("split_wall", "fcu_exposed"):
            issues.append(f"{hu.tag}: no supply diffuser linked")
        if hu.certainty.status == "estimated":
            issues.append(f"{hu.tag}: {hu.certainty.note}")
        unit_rows.append(row)
    orphans = [t.id for t in hvac.terminals if t.unit_id is None]
    if orphans:
        issues.append(f"{len(orphans)} terminal(s) not linked to any unit: {', '.join(orphans[:12])}")
    no_flow = [t.id for t in hvac.terminals if t.kind == "supply_diffuser" and t.airflow_lps is None]
    if no_flow:
        issues.append(f"{len(no_flow)} supply diffuser(s) without an airflow: {', '.join(no_flow[:12])}")
    sd = [t for t in hvac.terminals if t.kind == "supply_diffuser"]
    return {
        "registration": {**reg.public(), "residual_m": round(reg.residual_pt * k, 4),
                         "grid_lines_on_hvac_sheet": [len(reg.grid_x), len(reg.grid_y)]},
        "layers": sheet.layer_note,
        "labels": {"text_items": len(labels), "unit_tags": tags,
                   "size_callouts": sum(1 for lab in labels if lab.kind == "size"),
                   "airflow_labels": sum(1 for lab in labels if lab.kind in ("flow", "number"))},
        "counts": {"units": len(hvac.units), "supply_diffusers": len(sd),
                   "return_grilles": sum(1 for t in hvac.terminals if t.kind == "return_grille"),
                   "ducts": len(hvac.ducts),
                   "duct_length_m": round(sum(LineString(d.path).length for d in hvac.ducts), 1),
                   "diffusers_with_labelled_airflow": sum(1 for t in sd if t.certainty.status == "extracted"),
                   "dropped_outside_building": dropped},
        "units": unit_rows,
        "issues": issues,
        "timings": timings,
    }


# ---------------------------------------------------------------------------------------------
# overlay image (plan + HVAC) for review
# ---------------------------------------------------------------------------------------------

def render_overlay(scene: Scene, out: Path, plan_image: Path | None = None, cal: dict | None = None,
                   px_per_m: float = 40.0) -> Path:
    """PNG of the rooms (and the plan underlay when given) with the scene's HVAC on top."""
    from PIL import Image, ImageDraw, ImageFont
    xs = [p[0] for r in scene.rooms for p in r.polygon]
    ys = [p[1] for r in scene.rooms for p in r.polygon]
    x0, y0, x1, y1 = min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2
    W, Hh = int((x1 - x0) * px_per_m), int((y1 - y0) * px_per_m)
    img = Image.new("RGB", (W, Hh), "white")
    if plan_image is not None and cal and plan_image.is_file():
        src = Image.open(plan_image).convert("L")
        mpp = cal["m_per_px"]
        # plan pixel (px, py) <-> x = (px - ox) * mpp, y = (oy - py) * mpp
        left = cal["origin_px"] + x0 / mpp
        top = cal["origin_py"] - y1 / mpp
        crop = src.crop((int(left), int(top), int(left + (x1 - x0) / mpp), int(top + (y1 - y0) / mpp)))
        crop = crop.resize((W, Hh)).point(lambda v: 255 - (255 - v) * 0.45)
        img = Image.merge("RGB", (crop, crop, crop))
    dr = ImageDraw.Draw(img, "RGBA")
    T = lambda x, y: ((x - x0) * px_per_m, (y1 - y) * px_per_m)  # noqa: E731
    for r in scene.rooms:
        dr.polygon([T(*p) for p in r.polygon], outline=(120, 120, 120, 160))
    hv = scene.hvac
    colours = {"supply": (190, 0, 190, 150), "return": (0, 150, 150, 150), "exhaust": (150, 90, 0, 150),
               "fresh_air": (60, 160, 60, 150)}
    for d in hv.ducts:
        poly = _duct_poly([tuple(p) for p in d.path], d.width, d.width_end)
        if poly.geom_type == "Polygon" and not poly.is_empty:
            dr.polygon([T(*p) for p in poly.exterior.coords], fill=colours[d.system], outline=colours[d.system][:3])
    unit_xy = {u.id: u.position for u in hv.units}
    for t in hv.terminals:
        col = (0, 90, 255) if t.kind.startswith("supply") else (0, 160, 0)
        poly = _rect(t.position[0], t.position[1], t.width, t.depth, t.rotation_deg)
        dr.polygon([T(*p) for p in poly.exterior.coords], outline=col, width=3)
        if t.unit_id in unit_xy:
            ux, uy = unit_xy[t.unit_id]
            dr.line([T(*t.position), T(ux, uy)], fill=col + (90,), width=1)
        if t.airflow_lps:
            dr.text(T(t.position[0] + 0.25, t.position[1] - 0.1), f"{t.airflow_lps:.0f}", fill=col)
    for u in hv.units:
        poly = _rect(u.position[0], u.position[1], u.size.width, u.size.depth, u.rotation_deg)
        dr.polygon([T(*p) for p in poly.exterior.coords], outline=(220, 0, 0), width=3, fill=(220, 0, 0, 40))
        dr.text(T(u.position[0] - 0.5, u.position[1] + 0.2), f"{u.tag} {u.airflow_lps or '?'}", fill=(200, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
