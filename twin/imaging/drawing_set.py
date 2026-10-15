"""Drawing sets: which sheets belong to which floor, so a whole building can be dropped in at once.

A set is every file the user dropped: PDFs (one or many pages), images and CSV sheets. Each sheet (a PDF page, an
image, a CSV) is sorted as in `twin.imaging.classify` (floor plan, HVAC drawing, reference, furniture list, sensor
sheet) and given the floor it shows, read from its title ("LEVEL 2 FLOOR PLAN", "2ND STOREY", "GROUND FLOOR",
"BASEMENT 1", ...) or, for a single-sheet file, from its name ("L2_plan.pdf").

A set is a *building* when its floor plans name at least two different floors. Each floor then gets the best plan
sheet for it (a vector PDF before an image), the HVAC drawings and CSV sheets that name the same floor, and every
reference image. Sheets that name no floor are reported, never guessed. Anything else is a single-floor set and goes
into one project as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from twin.imaging.classify import (HVAC_NAME, HVAC_WORDS, MAX_PAGES_SCANNED, NOT_PLAN_WORDS, PLAN_WORDS, _drawing_count,
                                   _page_text, csv_kind, image_kind, is_csv_text)

ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_FLOOR = r"(?:storey|story|floor|level)"
# (pattern, key from the match, label from the match); in text and file names
PATTERNS: list[tuple[re.Pattern, object, object]] = [
    (re.compile(r"\blower\s+ground\s+" + _FLOOR + r"?\b", re.I), lambda m: -0.5, lambda m: "Lower ground"),
    (re.compile(r"\bground\s+" + _FLOOR + r"\b", re.I), lambda m: 0.0, lambda m: "Ground floor"),
    (re.compile(r"\bbasement(?:\s*(?:level\s*)?(\d{1,2}))?\b", re.I),
     lambda m: -float(m.group(1) or 1), lambda m: f"Basement {int(m.group(1) or 1)}"),
    (re.compile(r"\blevel\s*0*(\d{1,3})\b", re.I), lambda m: float(m.group(1)), lambda m: f"Level {int(m.group(1))}"),
    (re.compile(r"\blvl\.?\s*0*(\d{1,3})\b", re.I), lambda m: float(m.group(1)), lambda m: f"Level {int(m.group(1))}"),
    (re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\s+" + _FLOOR + r"\b", re.I),
     lambda m: float(m.group(1)), lambda m: f"{_ordinal(int(m.group(1)))} {m.group(0).split()[-1].lower()}"),
    (re.compile(r"\b(" + "|".join(ORDINALS) + r")\s+" + _FLOOR + r"\b", re.I),
     lambda m: float(ORDINALS[m.group(1).lower()]), lambda m: f"{_ordinal(ORDINALS[m.group(1).lower()])} {m.group(0).split()[-1].lower()}"),
    (re.compile(r"\broof\s+(?:plan|level|floor|terrace)\b", re.I), lambda m: 900.0, lambda m: "Roof"),
]
# only in file names: short forms that mean something else inside a drawing (L1 = a lintel, F2 = a fan)
NAME_PATTERNS: list[tuple[re.Pattern, object, object]] = [
    (re.compile(r"(?:^|[^a-z0-9])l\s?0*(\d{1,2})(?=[^a-z0-9]|$)", re.I), lambda m: float(m.group(1)),
     lambda m: f"Level {int(m.group(1))}"),
    (re.compile(r"(?:^|[^a-z0-9])b(\d)(?=[^a-z0-9]|$)", re.I), lambda m: -float(m.group(1)),
     lambda m: f"Basement {m.group(1)}"),
    (re.compile(r"(?:^|[^a-z0-9])gf(?=[^a-z0-9]|$)", re.I), lambda m: 0.0, lambda m: "Ground floor"),
]


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _matches(text: str, patterns) -> list[tuple[float, str, int]]:
    out = []
    for rx, key, label in patterns:
        for m in rx.finditer(text):
            out.append((key(m), label(m), m.start()))
    return out


def level_from_text(text: str) -> tuple[float, str] | None:
    """The floor a sheet shows, from its text: a level named on a line with "plan" / "layout" wins (a title such as
    "LEVEL 2 FLOOR PLAN"), else the only level named. Several levels and no title line: unknown."""
    if not text:
        return None
    scores: dict[float, list] = {}
    for line in text.splitlines():
        found = _matches(line, PATTERNS)
        if not found:
            continue
        titled = re.search(r"\b(plan|layout|acmv|hvac)\b", line, re.I) is not None
        for key, label, _ in found:
            s = scores.setdefault(key, [0, label])
            s[0] += 5 if titled else 1
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])
    if len(ranked) > 1 and ranked[0][1][0] == ranked[1][1][0]:
        return None
    if len(ranked) > 1 and ranked[0][1][0] < 5:
        return None   # several levels mentioned, none in a title: a section or a key plan
    return ranked[0][0], ranked[0][1][1]


def level_from_name(name: str) -> tuple[float, str] | None:
    stem = re.sub(r"\.[a-z0-9]+$", "", name or "", flags=re.I).replace("_", " ").replace("-", " ")
    found = _matches(stem, PATTERNS) or _matches(stem, NAME_PATTERNS)
    keys = {k for k, _, _ in found}
    if len(keys) != 1:
        return None
    k, label, _ = found[0]
    return k, label


def building_name(names: list[str]) -> str:
    """'Riverside_L2_floor_plan.pdf' -> 'Riverside'."""
    for name in names:
        stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I).replace("_", " ").replace("-", " ")
        for rx, _, _ in PATTERNS + NAME_PATTERNS:
            stem = rx.sub(" ", stem)
        stem = re.sub(r"\b(floor|plans?|layouts?|drawings?|set|sheets?|architectural|arch|general arrangement|ga)\b", " ",
                      stem, flags=re.I)
        stem = re.sub(r"\s+", " ", stem).strip(" .,&+")
        if len(stem) >= 2:
            return stem[0].upper() + stem[1:]
    return "Building"


@dataclass
class Sheet:
    file: int                 # index in the dropped files
    name: str
    page: int                 # 0-based (0 for images and CSV)
    pages: int
    kind: str | None          # plan | hvac | reference | furniture_list | sensor_sheet | None (not used)
    why: str
    sniffed: str              # pdf | png | jpg | csv
    level: tuple[float, str] | None = None
    drawn: int = 0

    def public(self) -> dict:
        return {"file": self.file, "name": self.name, "page": self.page + 1, "pages": self.pages, "kind": self.kind,
                "why": self.why, "level": self.level[1] if self.level else None}


@dataclass
class DrawingSet:
    sheets: list[Sheet]
    floors: list[dict] = field(default_factory=list)   # [{key, label, plan, hvac[], sheets[]}], bottom first
    unassigned: list[Sheet] = field(default_factory=list)

    @property
    def is_building(self) -> bool:
        return len(self.floors) >= 2


def _pdf_sheets(i: int, name: str, data: bytes) -> list[Sheet]:
    import fitz
    out = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        n = doc.page_count
        for p in range(min(n, MAX_PAGES_SCANNED)):
            page = doc[p]
            text = _page_text(page)
            hv = sum(text.count(w) for w in HVAC_WORDS)
            pl = sum(text.count(w) for w in PLAN_WORDS)
            neg = sum(text.count(w) for w in NOT_PLAN_WORDS)
            drawn = _drawing_count(page)
            if (n == 1 and HVAC_NAME.search(name)) or (hv >= 3 and hv > pl):
                kind, why = "hvac", "an air-conditioning (HVAC / ACMV) drawing"
            elif pl and pl >= neg:
                kind, why = "plan", "a floor plan"
            elif n == 1 and not neg:
                kind, why = "plan", "a drawing"
            else:
                kind, why = None, "not a floor plan (section, elevation, schedule or cover sheet)"
            level = level_from_text(text) or (level_from_name(name) if n == 1 else None)
            out.append(Sheet(i, name, p, n, kind, why, "pdf", level, drawn))
    return out


def analyse(files: list[tuple[str, bytes]]) -> DrawingSet:
    """Sort every sheet of the dropped files and group the floor plans by the floor they show."""
    sheets: list[Sheet] = []
    for i, (name, data) in enumerate(files):
        if is_csv_text(data):
            kind, why = csv_kind(data)
            sheets.append(Sheet(i, name, 0, 1, kind, why, "csv", level_from_name(name)))
        elif data[:1024].lstrip()[:5] == b"%PDF-":
            try:
                sheets.extend(_pdf_sheets(i, name, data))
            except Exception as exc:  # noqa: BLE001 - reported, the upload itself says what is wrong
                sheets.append(Sheet(i, name, 0, 1, None, f"the PDF could not be read ({exc})", "pdf"))
        else:
            try:
                kind, why = image_kind(data, name)
            except Exception as exc:  # noqa: BLE001
                kind, why = None, f"not a PNG, JPG, PDF or CSV ({type(exc).__name__})"
            sheets.append(Sheet(i, name, 0, 1, kind, why, "png" if data[:4] == b"\x89PNG" else "jpg",
                                level_from_name(name)))

    out = DrawingSet(sheets)
    plans: dict[float, list[Sheet]] = {}
    for s in sheets:
        if s.kind == "plan" and s.level is not None:
            plans.setdefault(s.level[0], []).append(s)
    if len(plans) < 2:
        return out
    for key in sorted(plans):
        best = max(plans[key], key=lambda s: (s.sniffed == "pdf", s.drawn))
        floor = {"key": key, "label": best.level[1], "plan": best, "hvac": [], "sheets": []}
        for s in sheets:
            if s.kind == "hvac" and s.level and s.level[0] == key:
                floor["hvac"].append(s)
            elif s.kind in ("furniture_list", "sensor_sheet") and s.level and s.level[0] == key:
                floor["sheets"].append(s)
        out.floors.append(floor)
    used = {id(f["plan"]) for f in out.floors} | {id(s) for f in out.floors for s in f["hvac"] + f["sheets"]}
    for s in sheets:
        if id(s) in used or s.kind in (None, "reference"):
            continue
        if s.kind == "plan" and s.level is not None:
            continue   # a second plan of a floor that has one (an enlarged part, a raster copy)
        out.unassigned.append(s)
    return out
