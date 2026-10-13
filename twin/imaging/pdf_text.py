"""Deterministic extraction from vector PDF drawings: drawing scale and text labels with positions.

For a CAD-exported sheet this is exact information (no AI): the sheet scale printed in the title
block ("1:100") turns PDF points into real metres, and every text block (room names, areas,
dimension strings) gets a position in the PLAN FRAME:

    x_m = x_pt * m_per_pt,  y_m = (page_height_pt - y_pt) * m_per_pt,  m_per_pt = 0.0254 / 72 * scale

i.e. full-scale metres with the origin at the bottom-left corner of the sheet, x right, y up.
"""

from __future__ import annotations

import re
from collections import Counter

PT_TO_M_PAPER = 0.0254 / 72.0
SCALE_RE = re.compile(r"(?<![\d.])1\s*:\s*(\d{2,4})(?![\d.])")
COMMON_SCALES = {20, 25, 50, 75, 100, 125, 150, 200, 250, 500, 1000}


def detect_scale(text: str) -> tuple[int | None, str]:
    """Most frequent '1:N' with a common architectural N; returns (N, evidence)."""
    found = [int(n) for n in SCALE_RE.findall(text) if int(n) in COMMON_SCALES]
    if not found:
        return None, ""
    counts = Counter(found)
    n, k = counts.most_common(1)[0]
    others = sorted(set(found) - {n})
    note = f"'1:{n}' found {k}x on the sheet" + (f"; other scales also present: {', '.join(f'1:{o}' for o in others)}" if others else "")
    return n, note


def extract_pdf_text(data: bytes, page: int = 0, max_labels: int = 1500) -> dict:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pg = doc[page]
        w_pt, h_pt = pg.rect.width, pg.rect.height
        full = pg.get_text()
        scale, evidence = detect_scale(full)
        labels = []
        for block in pg.get_text("dict").get("blocks", []):
            lines = []
            rotated = False
            for line in block.get("lines", []):
                t = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if t:
                    lines.append(t)
                    rotated = rotated or abs(line.get("dir", (1, 0))[1]) > 0.5
            text = " | ".join(lines).replace("�", "°")
            if len(text.replace(" ", "")) < 2:
                continue  # single-letter tags (R, G, ...) carry no layout information
            x0, y0, x1, y1 = block["bbox"]
            labels.append({"text": text[:120], "x_pt": round((x0 + x1) / 2, 1), "y_pt": round((y0 + y1) / 2, 1),
                           "w_pt": round(x1 - x0, 1), "h_pt": round(y1 - y0, 1), "rotated": rotated})
    m_per_pt = PT_TO_M_PAPER * scale if scale else None
    return {"page": page, "page_w_pt": round(w_pt, 2), "page_h_pt": round(h_pt, 2), "scale": scale,
            "scale_evidence": evidence, "m_per_pt": m_per_pt, "label_count": len(labels),
            "labels": labels[:max_labels]}


def labels_in_metres(info: dict, region_m: tuple[float, float, float, float] | None = None) -> list[dict]:
    """Labels converted to the plan frame (metres), optionally only those inside (x0, y0, x1, y1)."""
    k = info["m_per_pt"]
    if not k:
        return []
    out = []
    for lab in info["labels"]:
        x, y = lab["x_pt"] * k, (info["page_h_pt"] - lab["y_pt"]) * k
        if region_m and not (region_m[0] <= x <= region_m[2] and region_m[1] <= y <= region_m[3]):
            continue
        out.append({"text": lab["text"], "x": round(x, 2), "y": round(y, 2)})
    return out


def calibration_for(info: dict, image_w_px: int) -> dict | None:
    """Plan-underlay calibration matching the plan frame for the normalised raster of this page."""
    if not info.get("m_per_pt"):
        return None
    sheet_w_m = info["page_w_pt"] * info["m_per_pt"]
    return {"m_per_px": sheet_w_m / image_w_px, "origin_px": 0.0,
            "origin_py": round(info["page_h_pt"] * (image_w_px / info["page_w_pt"]), 3),
            "source": f"PDF scale 1:{info['scale']} ({info['scale_evidence']})"}
