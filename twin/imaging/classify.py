"""What an upload is, so one drop zone takes every file: floor plan, HVAC drawing, reference photo or a CSV sheet.

The signals are cheap and explainable: the file name, the PDF's text layer, and for images how much of the picture
is white paper with thin dark lines (a drawing) versus colour (a photo). Every answer carries the reason, and the
user can change the kind on the thumbnail when it is wrong.

For multi-page PDFs the page is chosen the same way: the page whose title and text say "floor plan" (or "ACMV" for an
HVAC drawing) and which carries the most drawing, rather than page 1.
"""

from __future__ import annotations

import csv
import io
import re

from PIL import Image

from twin.scene.furniture_sheet import looks_like_furniture_sheet

HVAC_WORDS = ("acmv", "hvac", "air-con", "aircon", "air con", "air conditioning", "mechanical ventilation", "fcu",
              "fan coil", "diffuser", "ahu", "ductwork", "chilled water", "vrf", "vrv")
HVAC_NAME = re.compile(r"(^|[^a-z])(hvac|acmv|mech|mvac|aircon|air[-_ ]?con|ac[-_ ]?layout|m&e|mep|duct)", re.I)
PLAN_WORDS = ("floor plan", "layout plan", "general arrangement", "furniture layout", "unit layout", "storey plan",
              "level plan", "plan view")
NOT_PLAN_WORDS = ("elevation", "section", "schedule", "detail", "cover sheet", "drawing list", "legend", "index")
PHOTO_NAME = re.compile(r"(^|[^a-z])(photo|img|image|inspo|mood|ref|reference|interior|render|pinterest)", re.I)
PLAN_NAME = re.compile(r"(^|[^a-z])(plan|layout|floor|level|storey|apartment|unit|ga)([^a-z]|$)", re.I)
MAX_PAGES_SCANNED = 40


def is_csv_text(data: bytes) -> bool:
    try:
        head = data[:4096].decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    first = head.splitlines()[0] if head.strip() else ""
    return "," in first or ";" in first or "\t" in first


def csv_kind(data: bytes) -> tuple[str | None, str]:
    """furniture_list | sensor_sheet | None, and why."""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        header = next(csv.reader(io.StringIO(text)), [])
    except csv.Error:
        return None, "the CSV could not be read"
    cols = {c.strip().lower().replace(" ", "_") for c in header}
    if looks_like_furniture_sheet(header):
        return "furniture_list", "a furniture list (it has a catalog column)"
    if cols & {"tag", "sensor", "sensor_tag"} or ("kind" in cols and cols & {"bms_point", "bms", "elevation"}):
        return "sensor_sheet", "a sensor placement sheet (it has a tag column)"
    return None, ("not a furniture list (needs a catalog_id column) or a sensor sheet (needs a tag column); "
                  f"columns found: {', '.join(header[:12])}")


def _page_text(page) -> str:
    try:
        return page.get_text("text").lower()
    except Exception:  # noqa: BLE001 - a broken text layer only means no text signal
        return ""


def _drawing_count(page) -> int:
    try:
        return len(page.get_cdrawings())
    except Exception:  # noqa: BLE001
        try:
            return len(page.get_drawings())
        except Exception:  # noqa: BLE001
            return 0


def pick_pdf_page(data: bytes, want: str = "plan") -> tuple[int, str]:
    """0-based page of a multi-page PDF that is most likely the floor plan (want='plan') or HVAC drawing."""
    import fitz
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.page_count <= 1:
            return 0, "single page"
        best, best_score, why = 0, float("-inf"), ""
        for i in range(min(doc.page_count, MAX_PAGES_SCANNED)):
            page = doc[i]
            text = _page_text(page)
            hv = sum(text.count(w) for w in HVAC_WORDS)
            pl = sum(text.count(w) for w in PLAN_WORDS)
            neg = sum(text.count(w) for w in NOT_PLAN_WORDS)
            drawn = _drawing_count(page)
            score = min(drawn, 20000) / 2000.0          # how much is drawn on the page (0..10)
            score += 6 * min(pl, 3) - 2 * min(neg, 3)
            score += 4 * min(hv, 4) if want == "hvac" else -1.5 * min(hv, 4)
            if score > best_score:
                best, best_score = i, score
                why = (f"page {i + 1} of {doc.page_count}: " +
                       ", ".join(x for x in (f"says '{next(w for w in PLAN_WORDS if w in text)}'" if pl else "",
                                             "HVAC words" if want == "hvac" and hv else "",
                                             f"{drawn} drawn shapes") if x))
        return best, why


def pdf_kind(data: bytes, name: str, page: int) -> tuple[str, str]:
    """plan | hvac for a PDF upload, and why."""
    if HVAC_NAME.search(name or ""):
        return "hvac", f"the file name '{name}' says it is an HVAC drawing"
    import fitz
    with fitz.open(stream=data, filetype="pdf") as doc:
        text = _page_text(doc[min(page, doc.page_count - 1)])
    hv = sum(text.count(w) for w in HVAC_WORDS)
    pl = sum(text.count(w) for w in PLAN_WORDS)
    if hv >= 3 and hv > pl:
        return "hvac", "the drawing's text is about air-conditioning (ACMV / HVAC, FCU, diffusers)"
    return "plan", "a drawing" + (" titled as a floor plan" if pl else "")


def image_kind(data: bytes, name: str) -> tuple[str, str]:
    """plan | reference for a PNG/JPG upload: white paper with thin lines is a drawing, colour is a photo."""
    if HVAC_NAME.search(name or ""):
        return "hvac", f"the file name '{name}' says it is an HVAC drawing (only its vector PDF can be placed in 3D)"
    if PHOTO_NAME.search(name or "") and not PLAN_NAME.search(name or ""):
        return "reference", f"the file name '{name}' says it is a reference image"
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((256, 256))
    rgb, sat = img.tobytes(), img.convert("HSV").tobytes()[1::3]
    n = len(sat)
    white = sum(1 for i in range(0, len(rgb), 3) if min(rgb[i], rgb[i + 1], rgb[i + 2]) > 225) / n
    colourful = sum(1 for v in sat if v > 60) / n
    if white > 0.45 and colourful < 0.12:
        return "plan", f"a drawing ({white:.0%} white paper, little colour)"
    if PLAN_NAME.search(name or ""):
        return "plan", f"the file name '{name}' says it is a plan"
    return "reference", f"a photo or rendering ({colourful:.0%} coloured pixels)"
