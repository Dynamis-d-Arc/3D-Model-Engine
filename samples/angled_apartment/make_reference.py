"""Reference input set for Floor Plan Twin: an apartment with a slanted facade, a 45 degree corner and a curved bay.

    .venv/Scripts/python.exe samples/angled_apartment/make_reference.py

Everything is drawn from one exact design (below), so the true geometry is known. Writes next to this script:

    floor_plan.pdf / .png    architectural plan, vector A3 at 1:75: upload as the FLOOR PLAN (use the PDF)
    hvac_plan.pdf / .png     ACMV layout on the same structural grid: upload as the HVAC DRAWING (PDF only)
    coordination.png         plan + HVAC in blue + furniture numbers, for checking by eye
    furniture_list.csv       every furniture item: catalog id, size, position, rotation
    scene_truth.json         the exact layout as a scene (rooms, walls, openings, furniture) in the plan frame

The HVAC sheet follows what twin.hvac reads: grey architectural underlay, dash-dot structural grid exploded into
separate dashes, services in black, labels as single-stroke lettering (the importer reads them with OCR from the
line work), nested-square diffusers, grilles with one diagonal, fan coils with two marker boxes in a casing, ducts as
parallel lines with their size printed (SAD 400X250).
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pymupdf
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

OUT = Path(__file__).resolve().parent
SCALE = 75
PT_M = 72 / 0.0254 / SCALE              # sheet points per metre
PAGE_W, PAGE_H = 1190.55, 841.89        # A3 landscape
OX, OY = 5.0, 6.5                       # building origin (grid A/1) on the sheet, metres from the bottom-left corner
BLACK, GREY, DARK, BLUE = (0, 0, 0), (0.6, 0.6, 0.6), (0.22, 0.22, 0.22), (0.05, 0.35, 0.85)
T_EXT, T_INT = 0.25, 0.12
TITLE = "RIVERSIDE APARTMENT 3B"

# ============================================================================================
# the design (building frame: metres, +x east, +y north, origin = grid 1/A)
# ============================================================================================


def xw(y: float) -> float:
    """Centreline of the slanted west facade: 2.00 m east over 10.00 m north."""
    return 0.2 * y


BAY_A, BAY_B, BAY_SAG = (5.5, 0.0), (9.5, 0.0), 1.4
BAY_R = (((BAY_B[0] - BAY_A[0]) / 2) ** 2 + BAY_SAG ** 2) / (2 * BAY_SAG)
BAY_C = ((BAY_A[0] + BAY_B[0]) / 2, -BAY_SAG + BAY_R)


BAY_A0 = math.atan2(BAY_A[1] - BAY_C[1], BAY_A[0] - BAY_C[0])     # -160 degrees ...
BAY_A1 = math.atan2(BAY_B[1] - BAY_C[1], BAY_B[0] - BAY_C[0])     # ... to -20, through -90 (south)


def bay_arc(a0: float, a1: float, radius: float, n: int) -> list[tuple[float, float]]:
    return [(BAY_C[0] + radius * math.cos(a0 + (a1 - a0) * i / n), BAY_C[1] + radius * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


BAY = bay_arc(BAY_A0, BAY_A1, BAY_R, 6)      # 6 straight facets: the bay's walls in the scene
assert min(p[1] for p in BAY) < -1.39 and math.dist(BAY[0], BAY_A) < 1e-9, "the bay must run A -> B bulging south"
EXTERIOR = [(0.0, 0.0), *BAY, (12.0, 0.0), (14.0, 2.0), (14.0, 10.0), (2.0, 10.0)]
EXTERIOR_DRAWN = [(0.0, 0.0), *bay_arc(BAY_A0, BAY_A1, BAY_R, 32), (12.0, 0.0), (14.0, 2.0), (14.0, 10.0), (2.0, 10.0)]

INTERIOR_WALLS = {        # id: (start, end)
    "W_I1": ((4.0, 0.0), (4.0, 7.0)), "W_I2": ((xw(4.2), 4.2), (4.0, 4.2)), "W_I3": ((4.0, 5.8), (11.0, 5.8)),
    "W_I4": ((11.0, 0.0), (11.0, 5.8)), "W_I5": ((11.0, 4.0), (14.0, 4.0)), "W_I6": ((xw(7.0), 7.0), (14.0, 7.0)),
    "W_I7": ((5.0, 7.0), (5.0, 10.0)), "W_I8": ((7.4, 7.0), (7.4, 10.0)), "W_I9": ((12.0, 7.0), (12.0, 10.0)),
}

ROOMS = [   # id, name, type, polygon, label point
    ("R1", "KITCHEN", "kitchen", [(0, 0), (4.0, 0), (4.0, 4.2), (xw(4.2), 4.2)], (2.0, 1.3)),
    ("R2", "LAUNDRY", "laundry", [(xw(4.2), 4.2), (4.0, 4.2), (4.0, 7.0), (xw(7.0), 7.0)], (2.7, 5.55)),
    ("R3", "LIVING / DINING", "living", [(4.0, 0), *BAY, (11.0, 0), (11.0, 5.8), (4.0, 5.8)], (6.0, 2.0)),
    ("R4", "STUDY", "office", [(11.0, 0), (12.0, 0), (14.0, 2.0), (14.0, 4.0), (11.0, 4.0)], (12.75, 2.3)),
    ("R5", "ENTRY / HALL", "hallway", [(4.0, 5.8), (11.0, 5.8), (11.0, 4.0), (14.0, 4.0), (14.0, 7.0), (4.0, 7.0)], (9.0, 6.4)),
    ("R6", "BEDROOM 2", "bedroom", [(xw(7.0), 7.0), (5.0, 7.0), (5.0, 10.0), (2.0, 10.0)], (3.3, 8.2)),
    ("R7", "BATHROOM", "bathroom", [(5.0, 7.0), (7.4, 7.0), (7.4, 10.0), (5.0, 10.0)], (6.1, 8.55)),
    ("R8", "MASTER BEDROOM", "bedroom", [(7.4, 7.0), (12.0, 7.0), (12.0, 10.0), (7.4, 10.0)], (9.5, 7.38)),
    ("R9", "ENSUITE", "bathroom", [(12.0, 7.0), (14.0, 7.0), (14.0, 10.0), (12.0, 10.0)], (12.62, 8.5)),
]

# doors: id, hinge point, other jamb, room it swings into ; openings: id, a, b ; windows: id, a, b, sill, height
DOORS = [
    ("D1", (14.0, 5.7), (14.0, 4.7), "R5"), ("D2", (13.75, 4.0), (12.9, 4.0), "R4"), ("D3", (4.0, 6.8), (4.0, 6.0), "R2"),
    ("D4", (4.12, 7.0), (4.87, 7.0), "R6"), ("D5", (5.6, 7.0), (6.4, 7.0), "R7"), ("D6", (11.7, 7.0), (10.8, 7.0), "R8"),
    ("D7", (12.0, 8.1), (12.0, 7.3), "R9"),
]
CASED = [("O1", (4.0, 1.0), (4.0, 2.8)), ("O2", (5.2, 5.8), (7.6, 5.8))]
WINDOWS = [
    ("WN1", (xw(1.0), 1.0), (xw(2.8), 2.8), 1.0, 1.2), ("WN2", (xw(5.0), 5.0), (xw(5.9), 5.9), 1.2, 0.9),
    ("WN3", (4.3, 0.0), (5.2, 0.0), 0.9, 1.5), ("WN4", (9.8, 0.0), (10.7, 0.0), 0.9, 1.5),
    ("WN5", (12.4, 0.4), (13.6, 1.6), 0.9, 1.5), ("WN6", (14.0, 2.5), (14.0, 3.5), 0.9, 1.5),
    ("WN7", (2.7, 10.0), (4.3, 10.0), 0.9, 1.5), ("WN8", (xw(8.1), 8.1), (xw(9.1), 9.1), 0.9, 1.5),
    ("WN9", (5.8, 10.0), (6.6, 10.0), 1.5, 0.9), ("WN10", (8.4, 10.0), (10.6, 10.0), 0.9, 1.5),
    ("WN11", (12.6, 10.0), (13.4, 10.0), 1.5, 0.9),
]
BAY_PIER = 0.35        # solid wall at each end of the curved bay; glazed in between (sill 0.45, 1.85 high)

# furniture: id, room, catalog id, (w, d, h), centre, rotation (degrees, the item's width axis from +x, CCW), note
_DESK_N = (0.981, -0.196)                     # into bedroom 2, square to the slanted facade
_DESK_C = (xw(7.9) + 0.495 * _DESK_N[0], 7.9 + 0.495 * _DESK_N[1])
FURNITURE = [
    ("F01", "R1", "kitchen_counter", (2.4, 0.62, 0.92), (1.9, 0.44), 0, "sink + hob, against the south wall"),
    ("F02", "R1", "fridge", (0.7, 0.7, 1.85), (3.55, 0.48), 0, ""),
    ("F03", "R1", "kitchen_island", (1.6, 0.85, 0.92), (2.2, 2.3), 0, ""),
    ("F04", "R1", "bar_stool", (0.4, 0.4, 0.75), (1.8, 3.05), 0, ""),
    ("F05", "R1", "bar_stool", (0.4, 0.4, 0.75), (2.6, 3.05), 0, ""),
    ("F06", "R2", "washing_machine", (0.6, 0.6, 0.85), (1.55, 4.56), 0, "washer"),
    ("F07", "R2", "washing_machine", (0.6, 0.6, 0.85), (2.2, 4.56), 0, "dryer"),
    ("F08", "R2", "storage_cabinet", (0.9, 0.45, 1.9), (2.4, 6.7), 180, ""),
    ("F09", "R3", "sofa_3seat", (2.2, 0.95, 0.85), (8.3, 2.3), 90, "faces the TV"),
    ("F10", "R3", "coffee_table", (1.1, 0.6, 0.42), (9.45, 2.3), 90, ""),
    ("F11", "R3", "tv_unit", (1.8, 0.45, 1.3), (10.71, 2.3), 90, "against the study wall"),
    ("F12", "R3", "rug", (2.4, 1.7, 0.015), (9.4, 2.3), 90, ""),
    ("F13", "R3", "armchair", (0.85, 0.85, 0.85), (7.0, -0.5), 20, "in the curved bay, turned 20 degrees"),
    ("F14", "R3", "armchair", (0.85, 0.85, 0.85), (8.0, -0.5), -20, "in the curved bay, turned -20 degrees"),
    ("F15", "R3", "floor_lamp", (0.4, 0.4, 1.6), (7.5, -0.95), 0, "between the bay armchairs"),
    ("F16", "R3", "plant", (0.5, 0.5, 1.2), (6.2, -0.12), 0, ""),
    ("F17", "R3", "dining_table", (1.6, 0.9, 0.75), (6.0, 4.4), 0, "6 seats"),
    ("F18", "R3", "dining_chair", (0.45, 0.52, 0.88), (5.6, 3.72), 0, ""),
    ("F19", "R3", "dining_chair", (0.45, 0.52, 0.88), (6.4, 3.72), 0, ""),
    ("F20", "R3", "dining_chair", (0.45, 0.52, 0.88), (5.6, 5.08), 180, ""),
    ("F21", "R3", "dining_chair", (0.45, 0.52, 0.88), (6.4, 5.08), 180, ""),
    ("F22", "R3", "dining_chair", (0.45, 0.52, 0.88), (4.92, 4.4), 270, ""),
    ("F23", "R3", "dining_chair", (0.45, 0.52, 0.88), (7.08, 4.4), 90, ""),
    ("F24", "R3", "sideboard", (1.6, 0.45, 0.8), (4.29, 4.4), 270, "against the kitchen wall"),
    ("F25", "R4", "desk", (1.4, 0.7, 0.75), (11.85, 3.55), 0, ""),
    ("F26", "R4", "office_chair", (0.62, 0.62, 1.05), (11.85, 2.9), 0, ""),
    ("F27", "R4", "bookshelf", (1.0, 0.35, 2.0), (11.24, 1.0), 270, ""),
    ("F28", "R4", "plant", (0.5, 0.5, 1.2), (13.45, 2.15), 0, "by the 45 degree corner"),
    ("F29", "R5", "bench", (1.6, 0.4, 0.45), (12.1, 4.26), 0, "entry bench"),
    ("F30", "R5", "plant", (0.5, 0.5, 1.2), (13.6, 6.6), 0, ""),
    ("F31", "R6", "bed_single", (0.95, 2.05, 0.95), (3.15, 9.4), 90, "head to the west"),
    ("F32", "R6", "wardrobe", (1.8, 0.6, 2.1), (4.64, 8.95), 270, ""),
    ("F33", "R6", "desk", (1.4, 0.7, 0.75), _DESK_C, 78.69, "follows the slanted facade"),
    ("F34", "R6", "office_chair", (0.62, 0.62, 1.05), (_DESK_C[0] + 0.6 * _DESK_N[0], _DESK_C[1] + 0.6 * _DESK_N[1]),
     78.69, ""),
    ("F35", "R7", "bathtub", (1.7, 0.75, 0.58), (6.2, 9.5), 0, ""),
    ("F36", "R7", "toilet", (0.4, 0.68, 0.8), (7.0, 7.9), 90, ""),
    ("F37", "R7", "vanity", (0.8, 0.5, 1.8), (5.31, 8.3), 270, ""),
    ("F38", "R8", "bed_double", (1.65, 2.15, 1.05), (9.5, 8.8), 0, "head to the north wall"),
    ("F39", "R8", "nightstand", (0.5, 0.4, 0.55), (8.35, 9.65), 0, ""),
    ("F40", "R8", "nightstand", (0.5, 0.4, 0.55), (10.65, 9.65), 0, ""),
    ("F41", "R8", "wardrobe", (1.8, 0.6, 2.1), (7.76, 8.4), 270, ""),
    ("F42", "R8", "armchair", (0.85, 0.85, 0.85), (11.45, 9.35), 0, "reading corner"),
    ("F43", "R9", "shower", (0.9, 0.9, 2.0), (13.4, 9.4), 0, ""),
    ("F44", "R9", "toilet", (0.4, 0.68, 0.8), (13.53, 7.85), 270, ""),
    ("F45", "R9", "vanity", (0.8, 0.5, 1.8), (12.55, 9.62), 180, ""),
]
CATALOG_NAMES = {"kitchen_counter": "Kitchen counter run (sink + hob)", "fridge": "Fridge", "kitchen_island": "Kitchen island",
                 "bar_stool": "Bar stool", "washing_machine": "Washing machine", "storage_cabinet": "Tall storage cabinet",
                 "sofa_3seat": "3-seat sofa", "coffee_table": "Coffee table", "tv_unit": "TV unit with TV", "rug": "Rug",
                 "armchair": "Armchair", "floor_lamp": "Floor lamp", "plant": "Potted plant", "dining_table": "Dining table",
                 "dining_chair": "Dining chair", "sideboard": "Sideboard", "desk": "Desk", "office_chair": "Office chair",
                 "bookshelf": "Bookshelf", "bench": "Bench", "bed_single": "Single bed", "wardrobe": "Wardrobe",
                 "bathtub": "Bathtub", "toilet": "Toilet", "vanity": "Vanity with basin and mirror", "bed_double": "Double bed",
                 "nightstand": "Nightstand", "shower": "Shower enclosure"}

# HVAC (ceiling void): units, supply diffusers, return grilles, ducts (centreline, width m, size label)
UNITS = [   # tag, centre, airflow L/s, tag label position (the airflow is printed 0.4 m under the tag)
    ("1FC-01", (8.0, 6.4), 500, (5.9, 6.6)),
    ("1FC-02", (9.6, 9.2), 200, (11.2, 9.75)),
    ("1FC-03", (3.4, 9.2), 150, (4.95, 8.75)),
]
WALL_UNIT = ("AC-01", (11.16, 2.6), 100, (12.15, 2.85))       # wall split in the study, casing 0.20 x 0.9 m
DIFFUSERS = [   # id, centre, L/s, label centre, served by
    ("SD1", (2.4, 3.4), 100, (2.4, 4.05), "1FC-01"), ("SD2", (6.0, 4.4), 125, (6.0, 5.0), "1FC-01"),
    ("SD3", (8.0, 0.6), 125, (6.85, 0.45), "1FC-01"), ("SD4", (9.9, 3.4), 150, (9.9, 4.05), "1FC-01"),
    ("SD5", (7.9, 7.8), 100, (7.9, 8.45), "1FC-02"), ("SD6", (11.3, 7.8), 100, (11.3, 8.45), "1FC-02"),
    ("SD7", (3.4, 7.7), 150, (4.45, 7.7), "1FC-03"),
]
GRILLES = [("RG1", (9.6, 6.4), (1.0, 0.4), "1FC-01"), ("RG2", (8.3, 9.45), (0.6, 0.3), "1FC-02"),
           ("RG3", (2.4, 7.9), (0.6, 0.3), "1FC-03")]
DUCTS = [   # unit, centreline a -> b, width, metres trimmed off a / b where the run tees into a wider duct's side
    ("1FC-01", (8.0, 6.0), (8.0, 3.4), 0.40, 0.0, 0.15), ("1FC-01", (2.7, 3.4), (9.6, 3.4), 0.30, 0.0, 0.0),
    ("1FC-01", (8.0, 3.4), (8.0, 0.9), 0.30, 0.15, 0.0), ("1FC-01", (6.0, 3.4), (6.0, 4.1), 0.30, 0.15, 0.0),
    ("1FC-02", (9.6, 8.8), (9.6, 7.8), 0.25, 0.0, 0.125), ("1FC-02", (8.2, 7.8), (11.0, 7.8), 0.25, 0.0, 0.0),
    ("1FC-03", (3.4, 8.8), (3.4, 8.0), 0.25, 0.0, 0.0),
]
# size callouts sit clear of the duct lines: the importer takes strokes ending next to text for label leaders
DUCT_LABELS = [("SAD 400X250", (9.55, 4.9)), ("SAD 300X200", (5.0, 2.65)), ("SAD 250X200", (10.0, 7.15)),
               ("SAD 250X200", (2.0, 8.45))]
GRID_X = [("1", 0.0), ("2", 3.5), ("3", 7.0), ("4", 10.5), ("5", 14.0)]
GRID_Y = [("A", 0.0), ("B", 3.5), ("C", 7.0), ("D", 10.0)]
GRID_EXT = (-3.0, 17.0, -3.0, 13.0)      # x0, x1, y0, y1 the grid lines run over

# ============================================================================================
# geometry helpers
# ============================================================================================


def P(x: float, y: float) -> pymupdf.Point:
    return pymupdf.Point((x + OX) * PT_M, PAGE_H - (y + OY) * PT_M)


def local(c, rot_deg, u, v):
    r = math.radians(rot_deg)
    return (c[0] + u * math.cos(r) - v * math.sin(r), c[1] + u * math.sin(r) + v * math.cos(r))


def rect_pts(c, w, d, rot=0.0):
    return [local(c, rot, u, v) for u, v in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2), (-w / 2, -d / 2))]


def circle_pts(c, r, n=20):
    return [(c[0] + r * math.cos(2 * math.pi * i / n), c[1] + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def walls_polygon():
    ext = Polygon(EXTERIOR_DRAWN).exterior.buffer(T_EXT / 2, join_style=2, mitre_limit=6)
    inner = [LineString(ab).buffer(T_INT / 2, cap_style=3, join_style=2) for ab in INTERIOR_WALLS.values()]
    walls = unary_union([ext, *inner])
    cuts = [LineString([a, b]).buffer(0.2, cap_style=2) for _, a, b, *_ in DOORS + CASED + WINDOWS]
    cuts.append(LineString(bay_glazing(0.0)).buffer(0.2, cap_style=2))
    return walls.difference(unary_union(cuts))


def bay_glazing(offset: float) -> list[tuple[float, float]]:
    """The glazed middle of the bay arc (radius + offset), between the two solid piers."""
    a_pier = BAY_PIER / BAY_R
    return bay_arc(BAY_A0 + a_pier, BAY_A1 - a_pier, BAY_R + offset, 24)


# ============================================================================================
# single-stroke lettering (CAD style); the HVAC importer reads labels from line work with OCR
# ============================================================================================

def _arc(cx, cy, rx, ry, a0, a1, step=22.5):
    n = max(2, int(abs(a1 - a0) / step + 0.5))
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
            for i in range(n + 1)]


GLYPHS: dict[str, list[list[tuple[float, float]]]] = {     # 4 wide x 6 high, y up
    "0": [_arc(2, 3, 1.55, 3, 0, 360)],          # narrower than an O, or OCR reads 1FC-01 as 1FC-O1
    "1": [[(0.8, 4.6), (2.2, 6), (2.2, 0)]],
    "2": [_arc(2, 4.1, 2, 1.9, 160, -25) + [(0, 0), (4, 0)]],
    "3": [_arc(2, 4.55, 1.8, 1.45, 150, -90) + _arc(2, 1.55, 2, 1.55, 90, -150)],
    "4": [[(3, 0), (3, 6), (0, 1.6), (4.2, 1.6)]],
    "5": [[(3.8, 6), (0.5, 6), (0.3, 3.4)] + _arc(2, 1.95, 2, 1.95, 132, -145)],
    "6": [_arc(2, 3, 2, 3, 55, 192), _arc(2, 1.9, 2, 1.9, 0, 360)],
    "7": [[(0, 6), (4, 6), (1.4, 0)]],
    "8": [_arc(2, 4.55, 1.75, 1.45, 0, 360), _arc(2, 1.6, 2, 1.6, 0, 360)],
    "9": [_arc(2, 4.1, 2, 1.9, 0, 360), _arc(2, 3, 2, 3, 12, -125)],
    "A": [[(0, 0), (2, 6), (4, 0)], [(0.7, 2.1), (3.3, 2.1)]],
    "C": [_arc(2.2, 3, 2.2, 3, 42, 318)],
    "D": [[(1.6, 0), (0, 0), (0, 6), (1.6, 6)] + _arc(1.6, 3, 2.4, 3, 90, -90)],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3.1), (2.8, 3.1)]],
    "L": [[(0, 6), (0, 0), (3.6, 0)]],
    "S": [_arc(2, 4.5, 1.9, 1.5, 25, 270) + _arc(2, 1.5, 2, 1.5, 90, -155)],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "/": [[(0.5, -0.3), (3.5, 6.3)]],
    "-": [[(0.6, 2.9), (3.4, 2.9)]],
    " ": [],
}
ADVANCE = 5.6


def stroke_text(shape, text: str, centre, height: float, colour=BLACK, width_pt: float = 0.3) -> None:
    """Text centred on `centre` (building metres), cap height `height` metres, drawn as line strokes."""
    u = height / 6
    total = (len(text) * ADVANCE - (ADVANCE - 4)) * u
    x0, y0 = centre[0] - total / 2, centre[1] - height / 2
    for k, ch in enumerate(text.upper()):
        for line in GLYPHS[ch]:
            shape.draw_polyline([P(x0 + (k * ADVANCE + gx) * u, y0 + gy * u) for gx, gy in line])
    shape.finish(color=colour, width=width_pt, closePath=False)


# ============================================================================================
# drawing
# ============================================================================================

def polyline(shape, pts, colour, width=0.35, fill=None, close=False, dashes=None):
    shape.draw_polyline([P(*p) for p in pts])
    shape.finish(color=colour, width=width, fill=fill, closePath=close, dashes=dashes)


def text(page, s, at, size=7.0, colour=BLACK, font="helv", rotate=0, align="c"):
    """Real (selectable) text centred at a building-frame point."""
    p = P(*at)
    w = pymupdf.get_text_length(s, fontname=font, fontsize=size)
    if rotate == 90:
        page.insert_text(pymupdf.Point(p.x + size * 0.35, p.y + w / 2), s, fontsize=size, fontname=font, color=colour, rotate=90)
    else:
        dx = {"c": w / 2, "l": 0, "r": w}[align]
        page.insert_text(pymupdf.Point(p.x - dx, p.y + size * 0.35), s, fontsize=size, fontname=font, color=colour)


def draw_grid(page, colour):
    sh = page.new_shape()
    dash, gap, dot = 15 / PT_M, 3 / PT_M, 1.0 / PT_M        # dash-dot, every piece its own line (as CAD exports it)
    x0, x1, y0, y1 = GRID_EXT

    def dashed(a, b):
        L = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        t = 0.0
        while t < L:
            for piece in (dash, dot):
                s, e = t, min(L, t + piece)
                if e > s:
                    sh.draw_line(P(a[0] + ux * s, a[1] + uy * s), P(a[0] + ux * e, a[1] + uy * e))
                t = e + gap
                if t >= L:
                    break
    for _, x in GRID_X:
        dashed((x, y0), (x, y1))
    for _, y in GRID_Y:
        dashed((x0, y), (x1, y))
    sh.finish(color=colour, width=0.3)
    r = 0.45
    for name, x in GRID_X:
        for y in (y0 - r, y1 + r):
            polyline(sh, circle_pts((x, y), r), colour, 0.4)
    for name, y in GRID_Y:
        for x in (x0 - r, x1 + r):
            polyline(sh, circle_pts((x, y), r), colour, 0.4)
    sh.commit()
    for name, x in GRID_X:
        for y in (y0 - r, y1 + r):
            text(page, name, (x, y), 9, colour, "hebo")
    for name, y in GRID_Y:
        for x in (x0 - r, x1 + r):
            text(page, name, (x, y), 9, colour, "hebo")


def draw_walls(page, filled: bool, colour):
    sh = page.new_shape()
    walls = walls_polygon()
    polys = list(walls.geoms) if hasattr(walls, "geoms") else [walls]
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            sh.draw_polyline([P(*p) for p in ring.coords])
    if filled:
        sh.finish(color=BLACK, fill=DARK, width=0.5, closePath=True, even_odd=True)
    else:
        sh.finish(color=colour, width=0.5, closePath=True)
    # door leaves and swings, window glazing
    for _, h, j, _room in DOORS:
        wdir = ((j[0] - h[0]), (j[1] - h[1]))
        w = math.hypot(*wdir)
        d = (wdir[0] / w, wdir[1] / w)
        rc = next(r for r in ROOMS if r[0] == _room)
        mid = ((h[0] + j[0]) / 2, (h[1] + j[1]) / 2)
        n = (-d[1], d[0])
        if Polygon(rc[3]).distance(LineString([mid, (mid[0] + n[0] * 0.3, mid[1] + n[1] * 0.3)]).interpolate(1.0)) > 1e-6:
            n = (d[1], -d[0])
        sh.draw_polyline([P(*h), P(h[0] + n[0] * w, h[1] + n[1] * w)])
        sh.draw_polyline([P(h[0] + w * (math.cos(t) * d[0] + math.sin(t) * n[0]), h[1] + w * (math.cos(t) * d[1] + math.sin(t) * n[1]))
                          for t in [math.pi / 2 * i / 16 for i in range(17)]])
    sh.finish(color=colour if not filled else BLACK, width=0.35)
    for _, a, b, *_ in WINDOWS:
        L = math.dist(a, b)
        d = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        n = (-d[1], d[0])
        t = T_EXT / 2
        for off in (-t, -0.03, 0.03, t):
            sh.draw_line(P(a[0] + n[0] * off, a[1] + n[1] * off), P(b[0] + n[0] * off, b[1] + n[1] * off))
        for e in (a, b):
            sh.draw_line(P(e[0] - n[0] * t, e[1] - n[1] * t), P(e[0] + n[0] * t, e[1] + n[1] * t))
    for off in (-T_EXT / 2, -0.03, 0.03, T_EXT / 2):
        sh.draw_polyline([P(*p) for p in bay_glazing(off)])
    sh.finish(color=colour if not filled else BLACK, width=0.3)
    sh.commit()


def draw_furniture(page, colour, width=0.3):
    sh = page.new_shape()
    for fid, room, kind, (w, d, h), c, rot, note in FURNITURE:
        L = lambda u, v: local(c, rot, u, v)  # noqa: E731
        poly = lambda pts: sh.draw_polyline([P(*L(u, v)) for u, v in pts])  # noqa: E731
        box = lambda u0, v0, u1, v1: poly([(u0, v0), (u1, v0), (u1, v1), (u0, v1), (u0, v0)])  # noqa: E731
        circ = lambda u, v, r: poly([(u + r * math.cos(2 * math.pi * i / 16), v + r * math.sin(2 * math.pi * i / 16)) for i in range(17)])  # noqa: E731
        if kind in ("bar_stool", "floor_lamp", "plant", "office_chair"):
            circ(0, 0, w / 2)
            if kind == "plant":
                for i in range(6):
                    a = math.pi * i / 3
                    poly([(0, 0), (w * 0.42 * math.cos(a), w * 0.42 * math.sin(a))])
            elif kind == "floor_lamp":
                poly([(-w / 3, 0), (w / 3, 0)]), poly([(0, -w / 3), (0, w / 3)])
            elif kind == "office_chair":
                box(-w * 0.35, d * 0.3, w * 0.35, d * 0.45)
            continue
        box(-w / 2, -d / 2, w / 2, d / 2)
        if kind in ("sofa_3seat", "armchair"):
            box(-w / 2, d / 2 - 0.2, w / 2, d / 2)
            box(-w / 2, -d / 2, -w / 2 + 0.18, d / 2 - 0.2), box(w / 2 - 0.18, -d / 2, w / 2, d / 2 - 0.2)
            if kind == "sofa_3seat":
                for u in (-w / 6 + 0.03, w / 6 - 0.03):
                    poly([(u * 1.0, -d / 2), (u * 1.0, d / 2 - 0.2)])
        elif kind in ("bed_double", "bed_single"):
            n = 2 if kind == "bed_double" else 1
            pw = (w - 0.2) / n
            for i in range(n):
                u0 = -w / 2 + 0.1 + i * pw
                box(u0 + 0.03, d / 2 - 0.45, u0 + pw - 0.03, d / 2 - 0.1)
            poly([(-w / 2, d / 2 - 0.65), (w / 2, d / 2 - 0.65)])
            poly([(-w / 2, -d / 2 + 0.55), (w / 2, d / 2 - 0.65)])
        elif kind in ("wardrobe", "storage_cabinet"):
            poly([(-w / 2, -d / 2), (w / 2, d / 2)])
            poly([(-w / 2 + 0.05, 0), (w / 2 - 0.05, 0)])
        elif kind == "kitchen_counter":
            box(-0.75, -0.2, -0.35, 0.18), box(-0.3, -0.2, 0.1, 0.18)
            for u, v in ((0.55, -0.12), (0.85, -0.12), (0.55, 0.14), (0.85, 0.14)):
                circ(u, v, 0.09)
        elif kind == "fridge":
            poly([(-w / 2, -d / 2 + 0.08), (w / 2, -d / 2 + 0.08)])
        elif kind in ("kitchen_island", "coffee_table", "dining_table", "sideboard", "desk", "bench", "tv_unit"):
            if kind == "tv_unit":
                box(-0.7, -0.03, 0.7, 0.03)
            elif kind != "bench":
                box(-w / 2 + 0.05, -d / 2 + 0.05, w / 2 - 0.05, d / 2 - 0.05)
        elif kind == "dining_chair":
            poly([(-w / 2, d / 2 - 0.08), (w / 2, d / 2 - 0.08)])
        elif kind == "nightstand":
            circ(0, 0, 0.1)
        elif kind == "bookshelf":
            for v in (-d / 6, d / 6):
                poly([(-w / 2, v), (w / 2, v)])
        elif kind == "rug":
            box(-w / 2 + 0.08, -d / 2 + 0.08, w / 2 - 0.08, d / 2 - 0.08)
        elif kind == "toilet":
            box(-w / 2, d / 2 - 0.2, w / 2, d / 2)
            poly([(0.17 * math.cos(2 * math.pi * i / 16), -0.08 + 0.24 * math.sin(2 * math.pi * i / 16)) for i in range(17)])
        elif kind == "bathtub":
            box(-w / 2 + 0.08, -d / 2 + 0.08, w / 2 - 0.08, d / 2 - 0.08)
            circ(w / 2 - 0.25, 0, 0.04)
        elif kind == "shower":
            poly([(-w / 2, -d / 2), (w / 2, d / 2)]), poly([(-w / 2, d / 2), (w / 2, -d / 2)])
            circ(0, 0, 0.05)
        elif kind == "vanity":
            poly([(0.25 * math.cos(2 * math.pi * i / 16), 0.15 * math.sin(2 * math.pi * i / 16)) for i in range(17)])
        elif kind == "washing_machine":
            circ(0, 0, 0.2)
    sh.finish(color=colour, width=width, closePath=False)
    sh.commit()


def net_areas() -> dict[str, float]:
    walls = unary_union([Polygon(EXTERIOR_DRAWN).exterior.buffer(T_EXT / 2, join_style=2, mitre_limit=6),
                         *[LineString(ab).buffer(T_INT / 2, cap_style=3, join_style=2) for ab in INTERIOR_WALLS.values()]])
    return {rid: round(Polygon(poly).difference(walls).area, 1) for rid, _, _, poly, _ in ROOMS}


def draw_dimensions(page):
    sh = page.new_shape()

    def dim(a, b, off_dir, label, rotate=0):
        L = math.dist(a, b)
        d = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        sh.draw_line(P(*a), P(*b))
        for e in (a, b):
            sh.draw_line(P(e[0] - 0.12 * (d[0] - d[1]), e[1] - 0.12 * (d[1] + d[0])), P(e[0] + 0.12 * (d[0] - d[1]), e[1] + 0.12 * (d[1] + d[0])))
        mid = ((a[0] + b[0]) / 2 + off_dir[0] * 0.22, (a[1] + b[1]) / 2 + off_dir[1] * 0.22)
        text(page, label, mid, 6.5, BLACK, rotate=rotate)
    xs = [x for _, x in GRID_X]
    for x0, x1 in zip(xs, xs[1:]):
        dim((x0, -2.1), (x1, -2.1), (0, 1), f"{round((x1 - x0) * 1000):,}".replace(",", " "))
    dim((0, -2.7), (14, -2.7), (0, 1), "14 000")
    ys = [y for _, y in GRID_Y]
    for y0, y1 in zip(ys, ys[1:]):
        dim((15.4, y0), (15.4, y1), (-1, 0), f"{round((y1 - y0) * 1000):,}".replace(",", " "), rotate=90)
    dim((16.0, 0), (16.0, 10), (-1, 0), "10 000", rotate=90)
    dim((10.2, 0), (10.2, -BAY_SAG), (-1, 0), "", rotate=90)
    sh.draw_line(P(BAY_C[0] + 0.3, -BAY_SAG), P(10.35, -BAY_SAG))       # extension line from the bay's outermost point
    sh.finish(color=BLACK, width=0.3)
    sh.commit()
    text(page, "1 400", (10.55, -0.7), 6.5, rotate=90)


def draw_room_labels(page, colour, with_area=True):
    areas = net_areas()
    for rid, name, _, _, at in ROOMS:
        text(page, name, (at[0], at[1] + (0.14 if with_area else 0)), 7.0, colour, "hebo")
        if with_area:
            text(page, f"{areas[rid]:.1f} m²", (at[0], at[1] - 0.17), 6.5, colour)


def draw_notes(page):
    text(page, "CURVED BAY: R = %.2f m, 1.40 m DEEP, GLAZED" % BAY_R, (3.3, -0.75), 6.0)
    text(page, "45° CORNER 2.00 x 2.00", (13.75, 0.55), 5.5, rotate=0)
    text(page, "FACADE SLOPES 2.00 m OVER 10.00 m", (-1.1, 5.0), 6.0, rotate=90)


def title_block(page, sheet_title: str, number: str, notes: list[str], colour=BLACK):
    sh = page.new_shape()
    m = 14
    sh.draw_rect(pymupdf.Rect(m, m, PAGE_W - m, PAGE_H - m))
    x0 = PAGE_W - m - 255
    sh.draw_line(pymupdf.Point(x0, m), pymupdf.Point(x0, PAGE_H - m))
    for y in (m + 70, PAGE_H - m - 150, PAGE_H - m - 95, PAGE_H - m - 50):
        sh.draw_line(pymupdf.Point(x0, y), pymupdf.Point(PAGE_W - m, y))
    sh.finish(color=colour, width=0.8)
    sh.commit()
    put = lambda s, x, y, size=8, font="helv": page.insert_text(pymupdf.Point(x, y), s, fontsize=size, fontname=font, color=colour)  # noqa: E731
    put(TITLE, x0 + 12, m + 26, 13, "hebo")
    put("Reference drawing set - not for construction", x0 + 12, m + 44, 7.5)
    put("Angled facade, 45° corner, curved bay", x0 + 12, m + 58, 7.5)
    y = m + 92
    for line in notes:
        put(line, x0 + 12, y, 7)
        y += 11
    put(sheet_title, x0 + 12, PAGE_H - m - 128, 12, "hebo")
    put("LEVEL 1 (single floor)", x0 + 12, PAGE_H - m - 112, 8)
    put("SCALE 1:75 @ A3", x0 + 12, PAGE_H - m - 74, 11, "hebo")
    put("Dimensions in millimetres; grid 1-5 / A-D", x0 + 12, PAGE_H - m - 60, 7)
    put(f"DRAWING {number}", x0 + 12, PAGE_H - m - 30, 11, "hebo")
    put("REV A  2026-10-15", x0 + 160, PAGE_H - m - 30, 8)
    # north arrow + scale bar (0-5 m)
    cx, cy = x0 + 200, m + 105
    sh = page.new_shape()
    sh.draw_circle(pymupdf.Point(cx, cy), 16)
    sh.draw_polyline([pymupdf.Point(cx, cy - 22), pymupdf.Point(cx - 6, cy + 8), pymupdf.Point(cx, cy + 3), pymupdf.Point(cx + 6, cy + 8),
                      pymupdf.Point(cx, cy - 22)])
    bx, by = x0 + 12, PAGE_H - m - 165
    for i in range(5):
        sh.draw_rect(pymupdf.Rect(bx + i * PT_M, by - 4, bx + (i + 1) * PT_M, by))
    sh.finish(color=colour, width=0.6)
    sh.commit()
    put("N", cx - 3.5, cy - 25, 9, "hebo")
    for i in (0, 1, 5):
        put(f"{i}", bx + i * PT_M - 2, by + 9, 6.5)
    put("m", bx + 5 * PT_M + 8, by + 1, 6.5)


# ---- HVAC services ---------------------------------------------------------------------------

def draw_services(page, colour, lettering=True):
    sh = page.new_shape()
    # supply diffusers (4-way): squares 0.60 and 0.30 m joined at the corners. Plain nested squares would be read
    # by the importer's OCR as the character 回, and that text box would hide the duct ends around them
    for _, c, *_ in DIFFUSERS:
        for s in (0.6, 0.3):
            sh.draw_rect(pymupdf.Rect(P(c[0] - s / 2, c[1] + s / 2), P(c[0] + s / 2, c[1] - s / 2)))
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            sh.draw_line(P(c[0] + dx * 0.15, c[1] + dy * 0.15), P(c[0] + dx * 0.3, c[1] + dy * 0.3))
    # return grilles: rectangle + one diagonal
    for _, c, (w, d), _ in GRILLES:
        sh.draw_rect(pymupdf.Rect(P(c[0] - w / 2, c[1] + d / 2), P(c[0] + w / 2, c[1] - d / 2)))
        sh.draw_line(P(c[0] - w / 2, c[1] - d / 2), P(c[0] + w / 2, c[1] + d / 2))
    # ducted fan coils: casing 1.4 x 0.8 m + two concentric marker boxes 0.8 m apart on the long axis
    for _, c, _, _ in UNITS:
        for yy in (c[1] - 0.4, c[1] + 0.4):
            sh.draw_line(P(c[0] - 0.7, yy), P(c[0] + 0.7, yy))
        for xx in (c[0] - 0.7, c[0] + 0.7):
            sh.draw_line(P(xx, c[1] - 0.4), P(xx, c[1] + 0.4))
        for mx in (c[0] - 0.4, c[0] + 0.4):
            for s in (0.17, 0.13):
                sh.draw_rect(pymupdf.Rect(P(mx - s / 2, c[1] + s / 2), P(mx + s / 2, c[1] - s / 2)))
    # wall split: casing 0.20 x 0.9 m on the study wall (not 0.25: that would read as a 250 mm duct)
    _, c, _, _ = WALL_UNIT
    for p, q in (((-0.1, -0.45), (-0.1, 0.45)), ((0.1, -0.45), (0.1, 0.45)), ((-0.1, -0.45), (0.1, -0.45)),
                 ((0.1, 0.45), (-0.1, 0.45))):     # never a closed loop: PyMuPDF would report it as one rectangle
        sh.draw_line(P(c[0] + p[0], c[1] + p[1]), P(c[0] + q[0], c[1] + q[1]))
    # ducts: two separate side lines per run, as CAD exports them; a branch stops at the side of the duct it tees
    # into (that side stays continuous), so no side line shares an end point with another
    for _, a, b, w, cut_a, cut_b in DUCTS:
        L = math.dist(a, b)
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        n = (-u[1], u[0])
        a2, b2 = (a[0] + u[0] * cut_a, a[1] + u[1] * cut_a), (b[0] - u[0] * cut_b, b[1] - u[1] * cut_b)
        for s in (-w / 2, w / 2):
            sh.draw_line(P(a2[0] + n[0] * s, a2[1] + n[1] * s), P(b2[0] + n[0] * s, b2[1] + n[1] * s))
    sh.finish(color=colour, width=0.45)
    sh.commit()
    if not lettering:
        return
    sh = page.new_shape()
    h = 0.19     # 2.5 mm lettering at 1:75
    for tag, c, flow, at in UNITS + [WALL_UNIT]:
        stroke_text(sh, tag, at, h, colour)
        stroke_text(sh, f"{flow} L/S", (at[0], at[1] - 0.4), h, colour)
    for _, c, flow, at, _ in DIFFUSERS:
        stroke_text(sh, f"{flow} L/S", at, h, colour)
    for s, at in DUCT_LABELS:
        stroke_text(sh, s, at, h, colour)
    sh.commit()


def hvac_legend(page):
    x0, y0 = PAGE_W - 14 - 243, 14 + 300
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(x0, y0, x0 + 10, y0 + 10))
    sh.draw_rect(pymupdf.Rect(x0 + 2, y0 + 2, x0 + 8, y0 + 8))
    sh.draw_rect(pymupdf.Rect(x0, y0 + 20, x0 + 18, y0 + 27))
    sh.draw_line(pymupdf.Point(x0, y0 + 27), pymupdf.Point(x0 + 18, y0 + 20))
    sh.draw_line(pymupdf.Point(x0, y0 + 40), pymupdf.Point(x0 + 26, y0 + 40))
    sh.draw_line(pymupdf.Point(x0, y0 + 46), pymupdf.Point(x0 + 26, y0 + 46))
    sh.finish(color=GREY, width=0.5)
    sh.commit()
    for i, s in enumerate(["Supply air diffuser (airflow in L/s)", "Return air grille", "Supply air duct, size W x H mm (SAD)"]):
        page.insert_text(pymupdf.Point(x0 + 34, y0 + 8 + 20 * i), s, fontsize=7, fontname="helv")
    page.insert_text(pymupdf.Point(x0, y0 + 66), "1FC-xx  ducted fan coil unit in the ceiling void", fontsize=7, fontname="helv")
    page.insert_text(pymupdf.Point(x0, y0 + 78), "AC-xx  wall-mounted split unit (2.2 m)", fontsize=7, fontname="helv")


# ============================================================================================
# sheets and data files
# ============================================================================================

def floor_plan(doc):
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    draw_grid(page, GREY)
    draw_furniture(page, (0.35, 0.35, 0.35))
    draw_walls(page, filled=True, colour=BLACK)
    draw_dimensions(page)
    draw_room_labels(page, BLACK)
    draw_notes(page)
    a = net_areas()
    title_block(page, "FLOOR PLAN", "A-101", [
        f"Net floor area {sum(a.values()):.1f} m² (9 rooms)",
        "Walls: exterior 250 mm, interior 120 mm",
        "Ceiling height 2.70 m; doors 2.10 m",
        "Windows: sill 900 mm unless noted",
        "Bay glazing: sill 450 mm, 1.85 m high",
        "Furniture shown indicative; see furniture_list.csv",
    ])
    return page


def hvac_plan(doc):
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    draw_grid(page, GREY)
    draw_furniture(page, GREY, 0.25)
    draw_walls(page, filled=False, colour=GREY)
    draw_room_labels(page, GREY, with_area=False)
    draw_services(page, BLACK)
    title_block(page, "ACMV LAYOUT", "M-101", [
        "3 ducted fan coil units (1FC-01..03) in the",
        "ceiling void, 1 wall split (AC-01) in the study",
        "Design airflows in L/s; ducts W x H in mm",
        "Return air via ceiling grilles to the plenum",
        "Grids as architectural drawing A-101",
    ])
    hvac_legend(page)
    return page


def coordination(doc):
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    draw_grid(page, GREY)
    draw_furniture(page, (0.45, 0.45, 0.45))
    draw_walls(page, filled=True, colour=BLACK)
    draw_room_labels(page, BLACK)
    draw_services(page, BLUE)
    for fid, *_rest in FURNITURE:
        c = _rest[3]
        text(page, fid[1:], c, 5.0, (0.75, 0.1, 0.1), "hebo")
    title_block(page, "COORDINATION", "X-101", [
        "Floor plan + ACMV (blue) + furniture numbers",
        "(red, = F-numbers in furniture_list.csv)",
        "For checking by eye; upload A-101 and M-101",
    ])
    return page


def scene_truth() -> dict:
    """The exact layout in the app's plan frame (sheet metres from the bottom-left corner, as read from the PDF)."""
    S = lambda p: [round(p[0] + OX, 4), round(p[1] + OY, 4)]  # noqa: E731
    conf = {"status": "confirmed", "source": "user", "note": "reference design"}
    walls, openings = [], []
    ext = EXTERIOR + [EXTERIOR[0]]
    bay_ids = []
    for i, (a, b) in enumerate(zip(ext, ext[1:])):
        wid = f"W_E{i + 1:02d}"
        walls.append({"id": wid, "level_id": "L0", "start": S(a), "end": S(b), "thickness": T_EXT, "kind": "exterior", "certainty": conf})
        if a in BAY[:-1] and b in BAY[1:]:
            bay_ids.append((wid, a, b))
    for wid, (a, b) in INTERIOR_WALLS.items():
        walls.append({"id": wid, "level_id": "L0", "start": S(a), "end": S(b), "thickness": T_INT, "kind": "interior", "certainty": conf})

    def host(a, b):
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        best = None
        for w in walls:
            s, e = (w["start"][0] - OX, w["start"][1] - OY), (w["end"][0] - OX, w["end"][1] - OY)
            d = LineString([s, e]).distance(LineString([mid, mid]).centroid)
            if best is None or d < best[0]:
                best = (d, w, s, e)
        _, w, s, e = best
        return w["id"], round(LineString([s, e]).project(LineString([mid, mid]).centroid), 4)
    for did, h, j, room in DOORS:
        wid, off = host(h, j)
        w = next(x for x in walls if x["id"] == wid)
        s = (w["start"][0] - OX, w["start"][1] - OY)
        hinge = "start" if math.dist(h, s) < math.dist(j, s) else "end"
        openings.append({"id": did, "kind": "door", "wall_id": wid, "offset": off, "width": round(math.dist(h, j), 3), "height": 2.1,
                         "hinge_side": hinge, "opens_into": room, "certainty": conf})
    for oid, a, b in CASED:
        wid, off = host(a, b)
        openings.append({"id": oid, "kind": "opening", "wall_id": wid, "offset": off, "width": round(math.dist(a, b), 3),
                         "height": 2.4, "certainty": conf})
    for oid, a, b, sill, hgt in WINDOWS:
        wid, off = host(a, b)
        openings.append({"id": oid, "kind": "window", "wall_id": wid, "offset": off, "width": round(math.dist(a, b), 3),
                         "height": hgt, "sill_height": sill, "certainty": conf})
    for k, (wid, a, b) in enumerate(bay_ids, start=1):     # a window in every facet, clear of the facet joints
        L = math.dist(a, b)
        openings.append({"id": f"WB{k}", "kind": "window", "wall_id": wid, "offset": round(L / 2, 4), "width": round(L - 0.36, 3),
                         "height": 1.85, "sill_height": 0.45, "certainty": conf})
    rooms = [{"id": rid, "name": name.title(), "type": typ, "level_id": "L0", "polygon": [S(p) for p in poly], "certainty": conf}
             for rid, name, typ, poly, _ in ROOMS]
    furniture = [{"id": fid, "asset_id": kind, "room_id": room, "position": S(c), "rotation_deg": rot,
                  "size": {"width": w, "depth": d, "height": h}, "label": note or None, "certainty": conf}
                 for fid, room, kind, (w, d, h), c, rot, note in FURNITURE]
    return {"project": {"name": "Riverside Apartment 3B", "style": "modern",
                        "prompt": "Warm modern apartment: oak floors, white walls, a reading nook in the curved bay."},
            "levels": [{"id": "L0", "name": "Level 1", "wall_height": 2.7, "certainty": conf}],
            "rooms": rooms, "walls": walls, "openings": openings, "furniture": furniture,
            "provenance": {"source": "manual", "note": "samples/angled_apartment/make_reference.py (exact design)"}}


def furniture_csv(path: Path) -> None:
    rooms = {rid: name.title() for rid, name, *_ in ROOMS}
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "room", "item", "catalog_id", "width_m", "depth_m", "height_m", "x_m", "y_m", "rotation_deg",
                     "plan_x_m", "plan_y_m", "notes"])
        for fid, room, kind, (w, d, h), c, rot, note in FURNITURE:
            wr.writerow([fid, rooms[room], CATALOG_NAMES[kind], kind, w, d, h, round(c[0], 3), round(c[1], 3), rot,
                         round(c[0] + OX, 3), round(c[1] + OY, 3), note])


def main() -> None:
    sheets = {"floor_plan": floor_plan, "hvac_plan": hvac_plan, "coordination": coordination}
    for name, build in sheets.items():
        doc = pymupdf.open()
        page = build(doc)
        if name != "coordination":
            doc.save(OUT / f"{name}.pdf", garbage=3, deflate=True)
        page.get_pixmap(dpi=150).save(OUT / f"{name}.png")
        doc.close()
    furniture_csv(OUT / "furniture_list.csv")
    (OUT / "scene_truth.json").write_text(json.dumps(scene_truth(), indent=1), encoding="utf-8")
    a = net_areas()
    print(f"wrote {', '.join(sorted(p.name for p in OUT.iterdir() if p.suffix in ('.pdf', '.png', '.csv', '.json')))}")
    print(f"net area {sum(a.values()):.1f} m2: " + ", ".join(f"{r[1].title()} {a[r[0]]}" for r in ROOMS))


if __name__ == "__main__":
    main()
