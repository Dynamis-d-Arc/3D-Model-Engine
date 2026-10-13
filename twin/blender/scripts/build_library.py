"""Build the reusable furniture asset library (.blend) from the catalog.  Runs INSIDE Blender:

    blender --background --factory-startup --python build_library.py -- \
        --catalog catalog.json --out furniture_library.blend --report library_report.json [--preview sheet.png]

Every catalog asset becomes one mesh object ``ASSET_<id>`` whose bounds are exactly the
catalog size: origin at the footprint centre on the floor, front facing -Y. Material slots
are placeholders named ``ROLE_<role>`` in catalog role order; the scene builder swaps in
real materials per style. UVs are in metres.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy  # noqa: E402

from _twinlib import (MeshBuilder, box_uv, load_json, normalize_to_box, progress, reset_scene, save_json,  # noqa: E402
                      script_args, set_smooth_by_angle)


def legs(b, W, D, z0, z1, size, inset, mat, round_=False):
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx, cy = sx * (W / 2 - inset - size / 2), sy * (D / 2 - inset - size / 2)
            if round_:
                b.cylinder(cx, cy, z0, z1, size / 2, mat, segments=12)
            else:
                b.box(cx - size / 2, cx + size / 2, cy - size / 2, cy + size / 2, z0, z1, mat)


def split_x(x0, x1, n, gap):
    w = (x1 - x0 - gap * (n - 1)) / n
    return [(x0 + i * (w + gap), x0 + i * (w + gap) + w) for i in range(n)]


# ------------------------------------------------------------------------------------------
# asset designs: build inside [-W/2, W/2] x [-D/2, D/2] x [0, H]; front faces -Y
# ------------------------------------------------------------------------------------------

def sofa(b, W, D, H, R, seats=3, arm=0.16):
    legs(b, W, D, 0.0, 0.12, 0.04, 0.06, R["frame_metal"] if "frame_metal" in R else R["wood"])
    up = R["upholstery"]
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.12, 0.40, up, bevel=0.02)
    b.box(-W / 2, -W / 2 + arm, -D / 2, D / 2, 0.40, 0.64, up, bevel=0.03)
    b.box(W / 2 - arm, W / 2, -D / 2, D / 2, 0.40, 0.64, up, bevel=0.03)
    b.box(-W / 2 + arm - 0.001, W / 2 - arm + 0.001, D / 2 - 0.2, D / 2, 0.40, H, up, bevel=0.03)
    for x0, x1 in split_x(-W / 2 + arm, W / 2 - arm, seats, 0.01):
        b.box(x0, x1, -D / 2 + 0.02, D / 2 - 0.2, 0.40, 0.53, R["cushion"], bevel=0.04)
        b.box(x0, x1, D / 2 - 0.36, D / 2 - 0.2, 0.53, H - 0.05, R["cushion"], bevel=0.05)


def armchair(b, W, D, H, R):
    sofa(b, W, D, H, R, seats=1, arm=0.14)


def coffee_table(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, R["wood"], bevel=0.005)
    legs(b, W, D, 0.0, H - 0.04, 0.035, 0.04, R["metal_accent"])
    b.box(-W / 2 + 0.08, W / 2 - 0.08, -D / 2 + 0.08, D / 2 - 0.08, 0.10, 0.12, R["wood"])


def tv_unit(b, W, D, H, R):
    legs(b, W, D, 0.0, 0.08, 0.04, 0.05, R["wood"])
    b.box(-W / 2, W / 2, -D / 2 + 0.012, D / 2, 0.08, 0.50, R["lacquer"], bevel=0.004)
    for x0, x1 in split_x(-W / 2 + 0.01, W / 2 - 0.01, 3, 0.008):
        b.box(x0, x1, -D / 2, -D / 2 + 0.012, 0.10, 0.48, R["wood"])
    b.box(-0.12, 0.12, -0.08, 0.10, 0.50, 0.52, R["screen"])
    b.box(-0.03, 0.03, 0.0, 0.04, 0.52, 0.62, R["screen"])
    b.box(-0.65, 0.65, 0.0, 0.04, 0.62, H, R["screen"], bevel=0.004)


def dining_table(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, R["wood"], bevel=0.005)
    b.box(-W / 2 + 0.1, W / 2 - 0.1, -D / 2 + 0.1, D / 2 - 0.1, H - 0.13, H - 0.04, R["wood"])
    legs(b, W, D, 0.0, H - 0.04, 0.05, 0.06, R["metal_accent"])


def dining_chair(b, W, D, H, R):
    s = 0.035
    b.box(-W / 2, W / 2, -D / 2, D / 2 - 0.04, 0.42, 0.48, R["upholstery"], bevel=0.01)
    for sx in (-1, 1):
        cx = sx * (W / 2 - s / 2 - 0.01)
        b.box(cx - s / 2, cx + s / 2, -D / 2 + 0.01, -D / 2 + 0.01 + s, 0.0, 0.42, R["wood"])
        b.box(cx - s / 2, cx + s / 2, D / 2 - s, D / 2, 0.0, H, R["wood"])
    b.box(-W / 2 + 0.01 + s, W / 2 - 0.01 - s, D / 2 - 0.03, D / 2 - 0.005, 0.60, H - 0.02, R["wood"], bevel=0.005)


def bed(b, W, D, H, R, pillows=2):
    legs(b, W, D - 0.08, 0.0, 0.12, 0.06, 0.05, R["wood"])
    b.box(-W / 2, W / 2, -D / 2, D / 2 - 0.08, 0.12, 0.32, R["wood"], bevel=0.01)
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.03, D / 2 - 0.1, 0.32, 0.55, R["bedding"], bevel=0.04)
    b.box(-W / 2 + 0.01, W / 2 - 0.01, -D / 2 + 0.01, D / 2 * 0.35, 0.45, 0.60, R["bedding"], bevel=0.05)
    if pillows == 2:
        b.box(-W / 2 + 0.08, -0.04, D / 2 - 0.48, D / 2 - 0.12, 0.55, 0.70, R["cushion"], bevel=0.06)
        b.box(0.04, W / 2 - 0.08, D / 2 - 0.48, D / 2 - 0.12, 0.55, 0.70, R["cushion"], bevel=0.06)
    else:
        b.box(-W / 2 + 0.1, W / 2 - 0.1, D / 2 - 0.48, D / 2 - 0.12, 0.55, 0.70, R["cushion"], bevel=0.06)
    b.box(-W / 2, W / 2, D / 2 - 0.08, D / 2, 0.12, H, R["cushion"], bevel=0.02)


def bed_double(b, W, D, H, R):
    bed(b, W, D, H, R, pillows=2)


def bed_single(b, W, D, H, R):
    bed(b, W, D, H, R, pillows=1)


def nightstand(b, W, D, H, R):
    legs(b, W, D - 0.012, 0.0, 0.12, 0.025, 0.03, R["metal_accent"])
    b.box(-W / 2, W / 2, -D / 2 + 0.012, D / 2, 0.12, H, R["wood"], bevel=0.004)
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.006, -D / 2 + 0.012, 0.30, H - 0.03, R["wood"])
    b.box(-0.06, 0.06, -D / 2, -D / 2 + 0.006, H - 0.09, H - 0.075, R["metal_accent"])


def wardrobe(b, W, D, H, R):
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.05, D / 2 - 0.02, 0.0, 0.06, R["lacquer"])
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.06, H, R["lacquer"], bevel=0.004)
    doors = split_x(-W / 2 + 0.005, W / 2 - 0.005, 3, 0.006)
    for i, (x0, x1) in enumerate(doors):
        b.box(x0, x1, -D / 2 + 0.008, -D / 2 + 0.02, 0.08, H - 0.02, R["lacquer"])
        hx = x1 - 0.05 if i % 2 == 0 else x0 + 0.03
        b.box(hx, hx + 0.02, -D / 2, -D / 2 + 0.008, 0.90, 1.30, R["metal_accent"])


def desk(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.03, H, R["wood"], bevel=0.004)
    legs(b, W, D, 0.0, H - 0.03, 0.04, 0.03, R["frame_metal"])
    b.box(-W / 2 + 0.07, W / 2 - 0.07, D / 2 - 0.06, D / 2 - 0.03, H - 0.13, H - 0.09, R["frame_metal"])
    b.box(W / 2 - 0.45, W / 2 - 0.08, -D / 2 + 0.1, D / 2 - 0.08, H - 0.25, H - 0.03, R["wood"])


def office_chair(b, W, D, H, R):
    m = R["frame_metal"]
    b.box(-W / 2, W / 2, -0.025, 0.025, 0.04, 0.08, m)
    b.box(-0.025, 0.025, -D / 2, D / 2, 0.04, 0.08, m)
    for cx, cy in ((W / 2 - 0.02, 0), (-W / 2 + 0.02, 0), (0, D / 2 - 0.02), (0, -D / 2 + 0.02)):
        b.box(cx - 0.02, cx + 0.02, cy - 0.02, cy + 0.02, 0.0, 0.04, m)
    b.cylinder(0.0, 0.0, 0.08, 0.45, 0.025, m, segments=12)
    b.box(-0.25, 0.25, -0.26, 0.2, 0.45, 0.53, R["upholstery"], bevel=0.03)
    b.box(-0.025, 0.025, 0.2, 0.26, 0.47, 0.62, m)
    b.box(-0.23, 0.23, 0.24, D / 2, 0.58, H, R["upholstery"], bevel=0.03)


def bookshelf(b, W, D, H, R):
    t = 0.025
    b.box(-W / 2, -W / 2 + t, -D / 2, D / 2, 0.0, H, R["wood"])
    b.box(W / 2 - t, W / 2, -D / 2, D / 2, 0.0, H, R["wood"])
    shelves = 6
    zs = [i * (H - t) / (shelves - 1) for i in range(shelves)]
    for z in zs:
        b.box(-W / 2 + t, W / 2 - t, -D / 2, D / 2 - 0.01, z, z + t, R["wood"])
    b.box(-W / 2 + t, W / 2 - t, D / 2 - 0.01, D / 2, t, H - t, R["lacquer"])
    rnd = random.Random(7)
    for z in zs[:-2]:
        x = -W / 2 + t + 0.02
        while True:
            bw = rnd.uniform(0.02, 0.05)
            if x + bw > W / 2 - t - 0.02:
                break
            bh = rnd.uniform(0.17, min(0.27, (H - t) / (shelves - 1) - t - 0.03))
            bd = rnd.uniform(0.18, 0.24)
            b.box(x, x + bw, -D / 2 + 0.05, -D / 2 + 0.05 + bd, z + t, z + t + bh,
                  R["lacquer"] if rnd.random() < 0.5 else R["wood"])
            x += bw + (0.002 if rnd.random() < 0.85 else rnd.uniform(0.05, 0.12))


def sideboard(b, W, D, H, R):
    legs(b, W, D - 0.012, 0.0, 0.16, 0.03, 0.05, R["metal_accent"])
    b.box(-W / 2, W / 2, -D / 2 + 0.012, D / 2, 0.16, H, R["wood"], bevel=0.004)
    for x0, x1 in split_x(-W / 2 + 0.01, W / 2 - 0.01, 4, 0.006):
        b.box(x0, x1, -D / 2 + 0.004, -D / 2 + 0.012, 0.18, H - 0.02, R["wood"])
        b.box((x0 + x1) / 2 - 0.05, (x0 + x1) / 2 + 0.05, -D / 2, -D / 2 + 0.004, H - 0.08, H - 0.065, R["metal_accent"])


def kitchen_counter(b, W, D, H, R):
    lac, top = R["lacquer"], R["stone_top"]
    b.box(-W / 2, W / 2, -D / 2 + 0.07, D / 2 - 0.02, 0.0, 0.10, lac)
    b.box(-W / 2, W / 2, -D / 2 + 0.04, D / 2, 0.10, H - 0.04, lac)
    for x0, x1 in split_x(-W / 2 + 0.005, W / 2 - 0.005, 4, 0.006):
        b.box(x0, x1, -D / 2 + 0.022, -D / 2 + 0.04, 0.12, H - 0.06, lac)
        b.box((x0 + x1) / 2 - 0.15, (x0 + x1) / 2 + 0.15, -D / 2 + 0.01, -D / 2 + 0.022, H - 0.14, H - 0.12, R["metal_accent"])
    zt0, zt1 = H - 0.04, H
    sink = (-0.75, -0.25, -0.20, 0.15)
    hob = (0.35, 0.95, -0.22, 0.18)
    b.box(-W / 2, sink[0], -D / 2, D / 2, zt0, zt1, top)
    b.box(sink[0], sink[1], -D / 2, sink[2], zt0, zt1, top)
    b.box(sink[0], sink[1], sink[3], D / 2, zt0, zt1, top)
    b.box(sink[1], hob[0], -D / 2, D / 2, zt0, zt1, top)
    b.box(hob[0], hob[1], -D / 2, hob[2], zt0, zt1, top)
    b.box(hob[0], hob[1], hob[3], D / 2, zt0, zt1, top)
    b.box(hob[1], W / 2, -D / 2, D / 2, zt0, zt1, top)
    b.box(sink[0], sink[1], sink[2], sink[3], H - 0.22, H - 0.02, R["metal_accent"])
    b.box(hob[0], hob[1], hob[2], hob[3], H - 0.06, H - 0.005, R["appliance"])


def kitchen_island(b, W, D, H, R):
    b.box(-W / 2 + 0.05, W / 2 - 0.05, -D / 2 + 0.1, D / 2 - 0.1, 0.0, 0.10, R["lacquer"])
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.082, D / 2 - 0.07, 0.10, H - 0.04, R["lacquer"])
    for x0, x1 in split_x(-W / 2 + 0.035, W / 2 - 0.035, 3, 0.006):
        b.box(x0, x1, -D / 2 + 0.07, -D / 2 + 0.082, 0.12, H - 0.06, R["lacquer"])
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, R["stone_top"], bevel=0.004)


def fridge(b, W, D, H, R):
    legs(b, W, D - 0.03, 0.0, 0.02, 0.04, 0.04, R["metal_accent"])
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.02, H, R["appliance"], bevel=0.01)
    b.box(-W / 2 + 0.005, W / 2 - 0.005, -D / 2 + 0.012, -D / 2 + 0.03, 0.04, 1.20, R["appliance"], bevel=0.004)
    b.box(-W / 2 + 0.005, W / 2 - 0.005, -D / 2 + 0.012, -D / 2 + 0.03, 1.22, H - 0.01, R["appliance"], bevel=0.004)
    b.box(W / 2 - 0.08, W / 2 - 0.06, -D / 2, -D / 2 + 0.012, 0.95, 1.15, R["metal_accent"])
    b.box(W / 2 - 0.08, W / 2 - 0.06, -D / 2, -D / 2 + 0.012, 1.30, 1.50, R["metal_accent"])


def bar_stool(b, W, D, H, R):
    b.cylinder(0.0, 0.0, H - 0.04, H, W / 2, R["wood"], segments=24)
    for sx in (-1, 1):
        for sy in (-1, 1):
            b.box(sx * 0.12 - 0.0125, sx * 0.12 + 0.0125, sy * 0.12 - 0.0125, sy * 0.12 + 0.0125, 0.0, H - 0.04, R["frame_metal"])
    for sy in (-1, 1):
        b.box(-0.1075, 0.1075, sy * 0.12 - 0.008, sy * 0.12 + 0.008, 0.25, 0.27, R["frame_metal"])
        b.box(sy * 0.12 - 0.008, sy * 0.12 + 0.008, -0.1075, 0.1075, 0.25, 0.27, R["frame_metal"])


def toilet(b, W, D, H, R):
    c = R["ceramic"]
    b.box(-0.12, 0.12, -D / 2 + 0.16, D / 2 - 0.18, 0.0, 0.36, c, bevel=0.03)
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2, D / 2 - 0.18, 0.30, 0.40, c, bevel=0.04)
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.01, D / 2 - 0.19, 0.40, 0.43, c, bevel=0.01)
    b.box(-W / 2, W / 2, D / 2 - 0.18, D / 2, 0.40, H - 0.01, c, bevel=0.02)
    b.box(-0.03, 0.03, D / 2 - 0.12, D / 2 - 0.06, H - 0.01, H, R["metal_accent"])


def bathtub(b, W, D, H, R):
    c, r = R["ceramic"], 0.07
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.0, 0.10, c)
    b.box(-W / 2, -W / 2 + r, -D / 2, D / 2, 0.10, H, c, bevel=0.01)
    b.box(W / 2 - r, W / 2, -D / 2, D / 2, 0.10, H, c, bevel=0.01)
    b.box(-W / 2 + r, W / 2 - r, -D / 2, -D / 2 + r, 0.10, H, c, bevel=0.01)
    b.box(-W / 2 + r, W / 2 - r, D / 2 - r, D / 2, 0.10, H, c, bevel=0.01)
    b.box(W / 2 - r - 0.04, W / 2 - r, -0.02, 0.02, H - 0.16, H - 0.10, R["metal_accent"])


def shower(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.0, 0.06, R["ceramic"], bevel=0.005)
    b.box(-W / 2 + 0.02, W / 2, -D / 2, -D / 2 + 0.008, 0.06, H - 0.05, R["glass"])
    b.box(-W / 2, -W / 2 + 0.008, -D / 2 + 0.008, D / 2, 0.06, H - 0.05, R["glass"])
    b.box(-W / 2, W / 2, -D / 2, -D / 2 + 0.02, H - 0.05, H, R["metal_accent"])
    b.box(-W / 2, -W / 2 + 0.02, -D / 2 + 0.02, D / 2, H - 0.05, H, R["metal_accent"])
    b.box(-0.015, 0.015, D / 2 - 0.3, D / 2, H - 0.15, H - 0.12, R["metal_accent"])
    b.cylinder(0.0, D / 2 - 0.3, H - 0.2, H - 0.15, 0.1, R["metal_accent"], segments=16)
    b.box(-0.06, 0.06, D / 2 - 0.04, D / 2, 1.0, 1.12, R["metal_accent"])


def vanity(b, W, D, H, R):
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.06, D / 2 - 0.02, 0.0, 0.10, R["lacquer"])
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.10, 0.80, R["lacquer"])
    for x0, x1 in split_x(-W / 2 + 0.005, W / 2 - 0.005, 2, 0.006):
        b.box(x0, x1, -D / 2 + 0.018, -D / 2 + 0.03, 0.12, 0.78, R["lacquer"])
        b.box((x0 + x1) / 2 - 0.07, (x0 + x1) / 2 + 0.07, -D / 2 + 0.006, -D / 2 + 0.018, 0.70, 0.715, R["metal_accent"])
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.80, 0.86, R["ceramic"], bevel=0.01)
    b.cylinder(0.0, D / 2 - 0.08, 0.86, 1.02, 0.015, R["metal_accent"], segments=12)
    b.box(-0.012, 0.012, D / 2 - 0.2, D / 2 - 0.08, 0.99, 1.02, R["metal_accent"])
    b.box(-W / 2 + 0.05, W / 2 - 0.05, D / 2 - 0.02, D / 2, 1.0, H, R["mirror"])


def washing_machine(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.0, H, R["appliance"], bevel=0.01)
    b.cylinder(0.0, 0.40, -D / 2, -D / 2 + 0.02, 0.17, R["glass"], segments=24, axis="Y")
    b.box(-W / 2 + 0.03, -W / 2 + 0.25, -D / 2 + 0.01, -D / 2 + 0.02, H - 0.1, H - 0.03, R["glass"])


def floor_lamp(b, W, D, H, R):
    b.cylinder(0.0, 0.0, 0.0, 0.03, 0.15, R["frame_metal"], segments=24)
    b.cylinder(0.0, 0.0, 0.03, H - 0.3, 0.012, R["frame_metal"], segments=12)
    b.cylinder(0.0, 0.0, H - 0.38, H, W / 2, R["lamp_shade"], r1=W / 2 * 0.65, segments=24)


def plant(b, W, D, H, R):
    b.cylinder(0.0, 0.0, 0.0, 0.36, 0.14, R["pot"], r1=0.18, segments=24)
    b.cylinder(0.0, 0.0, 0.34, 0.75, 0.015, R["plant"], segments=8)
    b.sphere(0.0, 0.0, 0.82, W / 2, R["plant"], scale=(1.0, 1.0, 0.95))
    b.sphere(0.06, -0.04, 1.0, 0.16, R["plant"])
    b.sphere(-0.05, 0.05, H - 0.16, 0.16, R["plant"])


def rug(b, W, D, H, R):
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.0, H, R["rug"], bevel=0.004)


# ------------------------------------------------------------------------------------------
# commercial kitchen, F&B and back-of-house helpers
# ------------------------------------------------------------------------------------------

def hexa(b, pts, mat):
    """Convex hexahedron from 8 corners: the bottom 4 counter-clockwise seen from above, then the
    top 4 in the same order (sloped panels, trapezoid glass, canopy skirts)."""
    v = [b.bm.verts.new(p) for p in pts]
    quads = [(v[3], v[2], v[1], v[0]), (v[4], v[5], v[6], v[7])]
    for i in range(4):
        j = (i + 1) % 4
        quads.append((v[i], v[j], v[4 + j], v[4 + i]))
    for q in quads:
        b.bm.faces.new(q).material_index = mat


def tub(b, x0, x1, y0, y1, z0, z1, t, mat):
    """Open-top box (sink bowl, basket, basin) with wall thickness t."""
    b.box(x0, x1, y0, y1, z0, z0 + t, mat)
    b.box(x0, x1, y0, y0 + t, z0 + t, z1, mat)
    b.box(x0, x1, y1 - t, y1, z0 + t, z1, mat)
    b.box(x0, x0 + t, y0 + t, y1 - t, z0 + t, z1, mat)
    b.box(x1 - t, x1, y0 + t, y1 - t, z0 + t, z1, mat)


def slab_with_holes(b, x0, x1, y0, y1, z0, z1, holes, mat):
    """Slab with rectangular cut-outs (hx0, hx1, hy0, hy1); holes must not overlap along X."""
    x = x0
    for hx0, hx1, hy0, hy1 in sorted(holes):
        if hx0 - x > 1e-6:
            b.box(x, hx0, y0, y1, z0, z1, mat)
        if hy0 - y0 > 1e-6:
            b.box(hx0, hx1, y0, hy0, z0, z1, mat)
        if y1 - hy1 > 1e-6:
            b.box(hx0, hx1, hy1, y1, z0, z1, mat)
        x = hx1
    if x1 - x > 1e-6:
        b.box(x, x1, y0, y1, z0, z1, mat)


def knob(b, x, z, y0, depth, r, mat):
    """Control knob / dial on a front face: cylinder along +Y from y0."""
    b.cylinder(x, z, y0, y0 + depth, r, mat, segments=12, axis="Y")


def louvres(b, x0, x1, y0, y1, z0, z1, n, mat):
    pitch = (z1 - z0) / n
    for i in range(n):
        z = z0 + i * pitch + pitch * 0.3
        b.box(x0, x1, y0, y1, z, z + pitch * 0.4, mat)


def bullet_legs(b, W, D, h, mat, inset=0.03, r=0.02, yc=0.0):
    """Round stainless legs with wider adjustable feet, from the floor to h."""
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx, cy = sx * (W / 2 - inset - r), yc + sy * (D / 2 - inset - r)
            b.cylinder(cx, cy, 0.0, 0.03, r + 0.006, mat, r1=r, segments=12)
            b.cylinder(cx, cy, 0.03, h, r, mat, segments=12)


def cook_base(b, W, D, R, body_top, fascia_top, knobs):
    """Stainless cooking-line base: legs, body, control fascia with knobs, lower door with a bar handle."""
    s, f = R["stainless"], R["frame_metal"]
    bullet_legs(b, W, D - 0.03, 0.15, s, inset=0.035, yc=0.015)
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.15, body_top, s)
    b.box(-W / 2, W / 2, -D / 2 + 0.012, -D / 2 + 0.03, fascia_top - 0.13, fascia_top, s, bevel=0.003)
    for i in range(knobs):
        knob(b, -W / 2 + (i + 0.5) * W / knobs, fascia_top - 0.065, -D / 2, 0.012, 0.022, f)
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.022, -D / 2 + 0.03, 0.20, fascia_top - 0.16, s, bevel=0.002)
    b.box(-W / 2 + 0.06, W / 2 - 0.06, -D / 2 + 0.008, -D / 2 + 0.022, fascia_top - 0.21, fascia_top - 0.19, f)


# ------------------------------------------------------------------------------------------
# commercial kitchen designs
# ------------------------------------------------------------------------------------------

def work_table(b, W, D, H, R):
    s = R["stainless"]
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, s, bevel=0.004)
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.03, D / 2 - 0.03, H - 0.10, H - 0.04, s)
    b.box(-W / 2 + 0.035, W / 2 - 0.035, -D / 2 + 0.035, D / 2 - 0.035, 0.16, 0.19, s)
    bullet_legs(b, W, D, H - 0.10, s)


def kitchen_cabinet(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    bullet_legs(b, W, D - 0.02, 0.12, s, inset=0.04, yc=0.01)
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.12, H - 0.04, s)
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, s, bevel=0.004)
    for i, (x0, x1) in enumerate(split_x(-W / 2 + 0.01, W / 2 - 0.01, 2, 0.008)):
        b.box(x0, x1, -D / 2 + 0.008, -D / 2 + 0.02, 0.14, H - 0.06, s, bevel=0.002)
        hx = x1 - 0.06 if i == 0 else x0 + 0.04
        b.box(hx, hx + 0.02, -D / 2, -D / 2 + 0.008, H - 0.40, H - 0.16, m)


def wall_cabinet(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    b.box(-W / 2, W / 2, -D / 2 + 0.025, D / 2, 0.0, H, s, bevel=0.003)
    # two sliding doors on staggered tracks
    b.box(-W / 2 + 0.01, 0.03, -D / 2 + 0.014, -D / 2 + 0.024, 0.02, H - 0.02, s, bevel=0.002)
    b.box(-0.03, W / 2 - 0.01, -D / 2 + 0.004, -D / 2 + 0.014, 0.02, H - 0.02, s, bevel=0.002)
    b.box(-W / 2 + 0.05, -W / 2 + 0.07, -D / 2 + 0.008, -D / 2 + 0.014, H / 2 - 0.12, H / 2 + 0.12, m)
    b.box(W / 2 - 0.07, W / 2 - 0.05, -D / 2, -D / 2 + 0.004, H / 2 - 0.12, H / 2 + 0.12, m)


def undercounter_chiller(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    bullet_legs(b, W, D - 0.03, 0.12, s, inset=0.04, yc=0.015)
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.12, H - 0.04, s)
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, s, bevel=0.004)
    cx1 = -W / 2 + 0.36  # compressor bay with a louvred grille on the left
    b.box(-W / 2 + 0.01, cx1, -D / 2 + 0.02, -D / 2 + 0.03, 0.15, H - 0.07, s)
    louvres(b, -W / 2 + 0.04, cx1 - 0.03, -D / 2 + 0.012, -D / 2 + 0.02, 0.20, H - 0.12, 8, m)
    for x0, x1 in split_x(cx1 + 0.008, W / 2 - 0.01, 2, 0.008):
        b.box(x0, x1, -D / 2 + 0.012, -D / 2 + 0.03, 0.15, H - 0.07, s, bevel=0.003)
        b.box(x0 + 0.05, x1 - 0.05, -D / 2, -D / 2 + 0.012, H - 0.13, H - 0.105, m)


def upright_fridge(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    bullet_legs(b, W, D - 0.06, 0.10, s, inset=0.04, yc=0.03)
    b.box(-W / 2, W / 2, -D / 2 + 0.06, D / 2, 0.10, H - 0.20, s)
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, H - 0.20, H, s, bevel=0.005)  # compressor housing
    louvres(b, -W / 2 + 0.10, W / 2 - 0.10, -D / 2 + 0.01, -D / 2 + 0.02, H - 0.17, H - 0.03, 5, m)
    for i, (x0, x1) in enumerate(split_x(-W / 2 + 0.005, W / 2 - 0.005, 2, 0.008)):
        b.box(x0, x1, -D / 2 + 0.035, -D / 2 + 0.06, 0.12, H - 0.22, s, bevel=0.004)
        hx = x1 - 0.09 if i == 0 else x0 + 0.06
        b.box(hx, hx + 0.03, -D / 2, -D / 2 + 0.035, 0.85, 1.55, m, bevel=0.005)


def sink_unit(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    ts = H - 0.08  # worktop surface; the upstand and wall-mounted taps rise above it
    bw, gap = min(0.50, (W - 0.30) / 2), 0.08
    holes = [(-gap / 2 - bw, -gap / 2, -D / 2 + 0.08, D / 2 - 0.12), (gap / 2, gap / 2 + bw, -D / 2 + 0.08, D / 2 - 0.12)]
    slab_with_holes(b, -W / 2, W / 2, -D / 2, D / 2 - 0.03, ts - 0.04, ts, holes, s)
    b.box(-W / 2, W / 2, -D / 2, -D / 2 + 0.03, ts - 0.10, ts - 0.04, s)  # front apron
    b.box(-W / 2, W / 2, D / 2 - 0.03, D / 2, ts - 0.04, H, s)  # back upstand
    b.box(-W / 2 + 0.035, W / 2 - 0.035, -D / 2 + 0.035, D / 2 - 0.035, 0.16, 0.19, s)  # undershelf
    bullet_legs(b, W, D, ts - 0.04, s)
    for hx0, hx1, hy0, hy1 in holes:
        tub(b, hx0, hx1, hy0, hy1, ts - 0.30, ts, 0.012, s)
        cx, cy = (hx0 + hx1) / 2, (hy0 + hy1) / 2
        b.cylinder(cx, cy, ts - 0.288, ts - 0.282, 0.035, m, segments=16)  # drain
        b.box(cx - 0.06, cx + 0.06, D / 2 - 0.06, D / 2 - 0.03, H - 0.07, H - 0.02, m)  # mixer body
        b.cylinder(cx, H - 0.045, D / 2 - 0.26, D / 2 - 0.06, 0.012, m, segments=12, axis="Y")  # spout


def hand_sink(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    ts = H - 0.08
    b.box(-W / 2, W / 2, D / 2 - 0.03, D / 2, ts - 0.30, H, s)  # splash back plate
    b.box(-0.10, 0.10, D / 2 - 0.14, D / 2 - 0.03, 0.0, ts - 0.24, s)  # pedestal / waste cover
    tub(b, -W / 2, W / 2, -D / 2, D / 2 - 0.03, ts - 0.24, ts, 0.015, s)
    b.cylinder(0.0, -0.02, ts - 0.225, ts - 0.22, 0.03, m, segments=16)  # drain
    b.box(-0.07, 0.07, -D / 2 + 0.03, -D / 2 + 0.045, ts - 0.40, ts - 0.24, m)  # knee-valve paddle
    b.box(-0.03, 0.03, D / 2 - 0.06, D / 2 - 0.03, H - 0.07, H - 0.01, m)
    b.cylinder(0.0, H - 0.04, D / 2 - 0.22, D / 2 - 0.06, 0.012, m, segments=12, axis="Y")


def cooking_range(b, W, D, H, R):
    s, f = R["stainless"], R["frame_metal"]
    top = H - 0.06
    cook_base(b, W, D, R, top, top, 4)
    b.box(-W / 2, W / 2, -D / 2 + 0.012, D / 2, top, top + 0.015, s, bevel=0.002)  # hob plate
    b.box(-W / 2, W / 2, D / 2 - 0.05, D / 2, top + 0.015, H, s)  # back flue upstand
    y0, y1 = -D / 2 + 0.03, D / 2 - 0.05
    gx, gy = W / 2, (y1 - y0) / 2
    zb, zt, t = top + 0.015, H, 0.014
    half = min(gx, gy) / 2 - 0.02
    for i in range(2):
        for j in range(2):
            cx, cy = -W / 2 + (i + 0.5) * gx, y0 + (j + 0.5) * gy
            b.cylinder(cx, cy, zb, zb + 0.015, 0.075, f, segments=16)  # burner
            b.cylinder(cx, cy, zb + 0.015, zb + 0.025, 0.035, f, segments=12)  # cap
            b.box(cx - half, cx + half, cy - half, cy - half + t, zb, zt, f)  # pan-support grate
            b.box(cx - half, cx + half, cy + half - t, cy + half, zb, zt, f)
            b.box(cx - half, cx - half + t, cy - half + t, cy + half - t, zb, zt, f)
            b.box(cx + half - t, cx + half, cy - half + t, cy + half - t, zb, zt, f)
            b.box(cx - half + t, cx + half - t, cy - t / 2, cy + t / 2, zt - 0.012, zt, f)
            b.box(cx - t / 2, cx + t / 2, cy - half + t, cy + half - t, zt - 0.014, zt - 0.002, f)


def griddle(b, W, D, H, R):
    s, f = R["stainless"], R["frame_metal"]
    top = H - 0.07
    cook_base(b, W, D, R, top, top, 3)
    b.box(-W / 2, W / 2, -D / 2 + 0.012, D / 2, top, top + 0.012, s)  # deck
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.10, D / 2 - 0.06, top + 0.012, top + 0.04, f, bevel=0.003)
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.03, -D / 2 + 0.085, top + 0.012, top + 0.022, s)  # grease trough
    b.box(-W / 2, W / 2, D / 2 - 0.06, D / 2, top + 0.012, H, s)  # back splash
    b.box(-W / 2, -W / 2 + 0.025, -D / 2 + 0.10, D / 2 - 0.06, top + 0.012, H - 0.02, s)  # side splashes
    b.box(W / 2 - 0.025, W / 2, -D / 2 + 0.10, D / 2 - 0.06, top + 0.012, H - 0.02, s)


def deep_fryer(b, W, D, H, R):
    s, f = R["stainless"], R["frame_metal"]
    ts = H - 0.06  # deck surface
    vb = ts - 0.30  # vat bottom
    cook_base(b, W, D, R, vb, ts - 0.03, 2)
    hx0, hx1, hy0, hy1 = -W / 2 + 0.04, W / 2 - 0.04, -D / 2 + 0.10, D / 2 - 0.16
    slab_with_holes(b, -W / 2, W / 2, -D / 2 + 0.03, D / 2, vb, ts, [(hx0, hx1, hy0, hy1)], s)
    b.box(-W / 2, W / 2, D / 2 - 0.10, D / 2, ts, H, s)  # flue
    b.box(hx0, hx1, hy0, hy1, vb, ts - 0.13, f)  # oil
    by0, by1 = hy0 + 0.03, hy1 - 0.08
    for x0, x1 in split_x(hx0 + 0.015, hx1 - 0.015, 2, 0.02):
        tub(b, x0, x1, by0, by1, ts - 0.16, ts - 0.05, 0.006, f)  # basket
        cx = (x0 + x1) / 2
        b.box(cx - 0.008, cx + 0.008, by0 - 0.015, by0, ts - 0.08, ts + 0.025, f)
        b.box(cx - 0.008, cx + 0.008, -D / 2 + 0.01, by0 - 0.015, ts + 0.005, ts + 0.025, f)  # handle


def combi_oven(b, W, D, H, R):
    s, gl, sc = R["stainless"], R["glass"], R["screen"]
    st, yc = 0.70, 0.03
    for sx in (-1, 1):  # stand
        for sy in (-1, 1):
            cx, cy = sx * (W / 2 - 0.04), yc + sy * (D / 2 - 0.09)
            b.box(cx - 0.02, cx + 0.02, cy - 0.02, cy + 0.02, 0.0, st, s)
    b.box(-W / 2 + 0.01, W / 2 - 0.01, yc - (D / 2 - 0.07), yc + (D / 2 - 0.07), st - 0.05, st, s)
    b.box(-W / 2 + 0.02, W / 2 - 0.02, yc - (D / 2 - 0.07), yc + (D / 2 - 0.07), 0.12, 0.15, s)
    b.box(-W / 2, W / 2, -D / 2 + 0.06, D / 2, st, H, s, bevel=0.01)  # oven body
    dx0, dx1 = -W / 2 + 0.015, W / 2 - 0.22
    b.box(dx0, dx1, -D / 2 + 0.035, -D / 2 + 0.06, st + 0.03, H - 0.03, s, bevel=0.006)  # door
    b.box(dx0 + 0.05, dx1 - 0.10, -D / 2 + 0.03, -D / 2 + 0.035, st + 0.10, H - 0.12, sc)  # dark oven cavity
    b.box(dx0 + 0.04, dx1 - 0.09, -D / 2 + 0.02, -D / 2 + 0.03, st + 0.09, H - 0.11, gl)  # window
    b.box(dx1 - 0.07, dx1 - 0.035, -D / 2, -D / 2 + 0.035, st + 0.25, H - 0.25, s, bevel=0.006)  # handle
    px0, px1 = W / 2 - 0.20, W / 2 - 0.015
    b.box(px0, px1, -D / 2 + 0.045, -D / 2 + 0.06, st + 0.03, H - 0.03, s)  # control panel
    b.box(px0 + 0.025, px1 - 0.025, -D / 2 + 0.04, -D / 2 + 0.045, H - 0.36, H - 0.08, sc)  # touch screen
    knob(b, (px0 + px1) / 2, H - 0.48, -D / 2 + 0.025, 0.02, 0.035, s)


def charcoal_grill(b, W, D, H, R):
    s, f = R["stainless"], R["frame_metal"]
    ts = H - 0.025  # rim; the grate bars rest on it
    vb = ts - 0.22  # fire-pit floor
    cook_base(b, W, D, R, vb, ts - 0.03, 3)
    hx0, hx1, hy0, hy1 = -W / 2 + 0.06, W / 2 - 0.06, -D / 2 + 0.12, D / 2 - 0.10
    slab_with_holes(b, -W / 2, W / 2, -D / 2 + 0.03, D / 2, vb, ts, [(hx0, hx1, hy0, hy1)], s)
    b.box(hx0, hx1, hy0, hy1, vb, vb + 0.05, f)  # coal bed
    rnd = random.Random(11)
    for _ in range(14):
        b.sphere(rnd.uniform(hx0 + 0.05, hx1 - 0.05), rnd.uniform(hy0 + 0.05, hy1 - 0.05), vb + 0.055,
                 rnd.uniform(0.025, 0.04), f, scale=(1.0, 1.0, 0.6), segments=8, rings=4)
    x = hx0 + 0.02
    while x + 0.012 <= hx1 - 0.01:
        b.box(x, x + 0.012, hy0 - 0.02, hy1 + 0.02, ts, H, f)  # grate bars
        x += 0.045


def dishwasher(b, W, D, H, R):
    s, m = R["stainless"], R["metal_accent"]
    bullet_legs(b, W, D - 0.04, 0.12, s, inset=0.035, yc=0.02)
    b.box(-W / 2, W / 2, -D / 2 + 0.04, D / 2, 0.12, 0.82, s)  # wash-tank cabinet
    b.box(-W / 2 + 0.01, W / 2 - 0.01, -D / 2 + 0.03, -D / 2 + 0.04, 0.16, 0.66, s, bevel=0.002)
    b.box(-W / 2 + 0.01, W / 2 - 0.01, -D / 2 + 0.03, -D / 2 + 0.04, 0.68, 0.80, m)  # control strip
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.82, 0.86, s)  # tank rim
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.06, D / 2 - 0.02, 0.86, 1.36, s, bevel=0.01)  # hood
    for sx in (-1, 1):
        b.box(sx * (W / 2 - 0.10) - 0.015, sx * (W / 2 - 0.10) + 0.015, -D / 2 + 0.02, -D / 2 + 0.06, 1.02, 1.05, m)
    b.box(-W / 2 + 0.06, W / 2 - 0.06, -D / 2, -D / 2 + 0.02, 1.01, 1.06, m)  # hood lift handle
    b.box(-W / 2 + 0.08, W / 2 - 0.08, -D / 2 + 0.15, D / 2 - 0.06, 1.36, H, s, bevel=0.008)  # booster housing


def exhaust_hood(b, W, D, H, R):
    """Wall canopy hung at default_elevation: open underneath, vertical skirt, sloped front."""
    s, t, sk, inset = R["stainless"], 0.02, 0.30, 0.40
    b.box(-W / 2, W / 2, D / 2 - t, D / 2, 0.0, H, s)  # back
    b.box(-W / 2, W / 2, -D / 2, -D / 2 + t, 0.0, sk, s)  # front skirt
    hexa(b, [(-W / 2, -D / 2, sk), (W / 2, -D / 2, sk), (W / 2, -D / 2 + t, sk), (-W / 2, -D / 2 + t, sk),
             (-W / 2, -D / 2 + inset, H - t), (W / 2, -D / 2 + inset, H - t),
             (W / 2, -D / 2 + inset + t, H - t), (-W / 2, -D / 2 + inset + t, H - t)], s)  # sloped front
    for x0, x1 in ((-W / 2, -W / 2 + t), (W / 2 - t, W / 2)):
        b.box(x0, x1, -D / 2 + t, D / 2 - t, 0.0, sk, s)
        hexa(b, [(x0, -D / 2, sk), (x1, -D / 2, sk), (x1, D / 2 - t, sk), (x0, D / 2 - t, sk),
                 (x0, -D / 2 + inset, H - t), (x1, -D / 2 + inset, H - t), (x1, D / 2 - t, H - t),
                 (x0, D / 2 - t, H - t)], s)
    b.box(-W / 2, W / 2, -D / 2 + inset, D / 2, H - t, H, s)  # top
    yb = D / 2 - t  # sloped baffle-filter bank along the back, with a grease gutter below
    for x0, x1 in split_x(-W / 2 + t + 0.02, W / 2 - t - 0.02, max(2, round(W / 0.5)), 0.01):
        hexa(b, [(x0, yb - 0.03, 0.18), (x1, yb - 0.03, 0.18), (x1, yb, 0.18), (x0, yb, 0.18),
                 (x0, yb - 0.33, H - t), (x1, yb - 0.33, H - t), (x1, yb - 0.30, H - t), (x0, yb - 0.30, H - t)], s)
    b.box(-W / 2 + t, W / 2 - t, yb - 0.08, yb, 0.14, 0.18, s)
    b.box(-W / 2 + t, W / 2 - t, -D / 2 + t, -D / 2 + t + 0.04, 0.0, 0.03, s)  # front drip channel


def shelving_rack(b, W, D, H, R):
    s, r = R["stainless"], 0.0125
    for sx in (-1, 1):
        for sy in (-1, 1):
            b.cylinder(sx * (W / 2 - r), sy * (D / 2 - r), 0.0, H, r, s, segments=12)
    n, th = 5, 0.035
    for k in range(n):
        z = 0.15 + k * (H - 0.15 - th) / (n - 1)
        b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.004, -D / 2 + 0.016, z, z + th, s)
        b.box(-W / 2 + 0.02, W / 2 - 0.02, D / 2 - 0.016, D / 2 - 0.004, z, z + th, s)
        b.box(-W / 2 + 0.004, -W / 2 + 0.016, -D / 2 + 0.02, D / 2 - 0.02, z, z + th, s)
        b.box(W / 2 - 0.016, W / 2 - 0.004, -D / 2 + 0.02, D / 2 - 0.02, z, z + th, s)
        for x in (-W / 4, W / 4):
            b.box(x - 0.004, x + 0.004, -D / 2 + 0.016, D / 2 - 0.016, z, z + 0.025, s)
        for j in range(1, 8):
            y = -D / 2 + 0.016 + j * (D - 0.032) / 8
            b.box(-W / 2 + 0.016, W / 2 - 0.016, y - 0.003, y + 0.003, z + 0.025, z + 0.031, s)


def mobile_cart(b, W, D, H, R):
    s, f = R["stainless"], R["frame_metal"]
    wr = 0.05
    x1 = W / 2 - 0.07  # trays end here; the push handle is on the +X end
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx, cy = (-W / 2 + 0.07 if sx < 0 else x1 - 0.05), sy * (D / 2 - 0.07)
            b.cylinder(cx, wr, cy - 0.015, cy + 0.015, wr, f, segments=16, axis="Y")  # wheel
            for side in (-1, 1):
                b.box(cx - 0.03, cx + 0.03, cy + side * 0.024 - 0.004, cy + side * 0.024 + 0.004, wr, 0.10, f)
            b.box(cx - 0.03, cx + 0.03, cy - 0.028, cy + 0.028, 0.10, 0.13, f)  # castor plate
            b.cylinder(cx, cy, 0.13, H - 0.06, 0.014, s, segments=12)  # post
    for z0 in (0.22, H - 0.10):  # trays with raised lips
        b.box(-W / 2, x1, -D / 2, D / 2, z0, z0 + 0.025, s)
        b.box(-W / 2, x1, -D / 2, -D / 2 + 0.012, z0 + 0.025, z0 + 0.05, s)
        b.box(-W / 2, x1, D / 2 - 0.012, D / 2, z0 + 0.025, z0 + 0.05, s)
        b.box(-W / 2, -W / 2 + 0.012, -D / 2 + 0.012, D / 2 - 0.012, z0 + 0.025, z0 + 0.05, s)
        b.box(x1 - 0.012, x1, -D / 2 + 0.012, D / 2 - 0.012, z0 + 0.025, z0 + 0.05, s)
    for sy in (-1, 1):
        y = sy * (D / 2 - 0.10)
        b.box(x1, W / 2 - 0.012, y - 0.01, y + 0.01, H - 0.10, H - 0.08, f)  # handle bracket
        b.box(W / 2 - 0.03, W / 2 - 0.012, y - 0.01, y + 0.01, H - 0.08, H - 0.02, f)
    b.box(W / 2 - 0.035, W / 2, -D / 2 + 0.07, D / 2 - 0.07, H - 0.03, H, f)  # push handle


def floor_appliance(b, W, D, H, R):
    s, ap, sc = R["stainless"], R["appliance"], R["screen"]
    bullet_legs(b, W, D - 0.03, 0.08, s, inset=0.04, yc=0.015)
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.08, 0.80, s, bevel=0.006)  # storage bin / base
    b.box(-W / 2 + 0.06, W / 2 - 0.06, -D / 2 + 0.012, -D / 2 + 0.03, 0.40, 0.74, s, bevel=0.004)  # bin door
    b.box(-0.18, 0.18, -D / 2, -D / 2 + 0.012, 0.66, 0.69, ap)  # door handle
    b.box(-W / 2, W / 2, -D / 2 + 0.03, D / 2, 0.80, H, ap, bevel=0.01)  # machine head
    louvres(b, -W / 2 + 0.06, W / 2 - 0.06, -D / 2 + 0.02, -D / 2 + 0.03, 0.86, 1.12, 6, ap)
    b.box(-W / 2 + 0.06, W / 2 - 0.06, -D / 2 + 0.02, -D / 2 + 0.03, H - 0.22, H - 0.06, ap)  # control fascia
    b.box(-0.14, 0.14, -D / 2 + 0.012, -D / 2 + 0.02, H - 0.19, H - 0.09, sc)  # display


def countertop_appliance(b, W, D, H, R):
    """Generic bench-top machine (coffee machine silhouette): sits on a counter at default_elevation."""
    ap, m, sc = R["appliance"], R["metal_accent"], R["screen"]
    legs(b, W, D, 0.0, 0.015, 0.03, 0.03, m)
    b.box(-W / 2, W / 2, -D / 2 + 0.17, D / 2, 0.015, H - 0.12, ap, bevel=0.008)  # back tower
    b.box(-W / 2, W / 2, -D / 2 + 0.008, D / 2, H - 0.12, H, ap, bevel=0.008)  # head
    b.box(-W / 2 + 0.01, W / 2 - 0.01, -D / 2 + 0.02, -D / 2 + 0.17, 0.015, 0.05, ap)  # base under the drip tray
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.03, -D / 2 + 0.16, 0.05, 0.065, m)  # drip tray grate
    b.cylinder(0.0, -D / 2 + 0.09, H - 0.17, H - 0.12, 0.03, m, segments=16)  # outlet / group head
    b.box(-0.12, 0.12, -D / 2, -D / 2 + 0.008, H - 0.10, H - 0.03, sc)  # display


# ------------------------------------------------------------------------------------------
# F&B front of house and back of house designs
# ------------------------------------------------------------------------------------------

def waste_bin(b, W, D, H, R):
    lq, m = R["lacquer"], R["metal_accent"]
    r = W / 2
    b.cylinder(0.0, 0.0, 0.0, H - 0.08, r - 0.035, lq, r1=r - 0.012, segments=24)  # tapered body
    b.cylinder(0.0, 0.0, H - 0.08, H - 0.06, r, m, segments=24)  # rim
    b.cylinder(0.0, 0.0, H - 0.06, H, r - 0.005, lq, r1=r - 0.03, segments=24)  # lid
    b.box(-0.07, 0.07, -D / 2, -r + 0.06, 0.02, 0.05, m)  # pedal


def service_counter(b, W, D, H, R):
    lq, st, m = R["lacquer"], R["stone_top"], R["metal_accent"]
    b.box(-W / 2 + 0.04, W / 2 - 0.04, -D / 2 + 0.10, D / 2 - 0.02, 0.0, 0.10, m)  # recessed kick
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.06, D / 2, 0.10, H - 0.04, lq)
    for x0, x1 in split_x(-W / 2 + 0.02, W / 2 - 0.02, max(2, round(W / 0.4)), 0.01):
        b.box(x0, x1, -D / 2 + 0.045, -D / 2 + 0.06, 0.12, H - 0.07, lq, bevel=0.004)  # customer-side panels
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.04, H, st, bevel=0.004)  # top overhangs the customer side
    b.box(-W / 2 + 0.15, W / 2 - 0.15, -D / 2 + 0.005, -D / 2 + 0.03, 0.15, 0.18, m)  # foot rail
    for x in (-W / 2 + 0.25, 0.0, W / 2 - 0.25):
        b.box(x - 0.015, x + 0.015, -D / 2 + 0.03, -D / 2 + 0.045, 0.14, 0.19, m)


def display_case(b, W, D, H, R):
    s, gl = R["stainless"], R["glass"]
    dz, gt, fi = 0.85, 0.012, 0.30
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.06, D / 2 - 0.03, 0.0, 0.10, s)  # kick
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.10, dz, s)  # refrigerated base
    louvres(b, -W / 2 + 0.10, W / 2 - 0.10, -D / 2 + 0.01, -D / 2 + 0.02, 0.14, 0.30, 4, s)
    b.box(-W / 2, W / 2, -D / 2, D / 2, dz, dz + 0.03, s)  # display deck
    g0, g1 = dz + 0.03, H - 0.04
    hexa(b, [(-W / 2 + gt, -D / 2 + 0.02, g0), (W / 2 - gt, -D / 2 + 0.02, g0),
             (W / 2 - gt, -D / 2 + 0.02 + gt, g0), (-W / 2 + gt, -D / 2 + 0.02 + gt, g0),
             (-W / 2 + gt, -D / 2 + fi, g1), (W / 2 - gt, -D / 2 + fi, g1),
             (W / 2 - gt, -D / 2 + fi + gt, g1), (-W / 2 + gt, -D / 2 + fi + gt, g1)], gl)  # sloped front glass
    for x0, x1 in ((-W / 2, -W / 2 + gt), (W / 2 - gt, W / 2)):
        hexa(b, [(x0, -D / 2 + 0.02, g0), (x1, -D / 2 + 0.02, g0), (x1, D / 2, g0), (x0, D / 2, g0),
                 (x0, -D / 2 + fi, g1), (x1, -D / 2 + fi, g1), (x1, D / 2, g1), (x0, D / 2, g1)], gl)  # end glass
    b.box(-W / 2 + gt, W / 2 - gt, D / 2 - gt, D / 2, g0, g1, gl)  # rear sliding doors
    b.box(-W / 2, W / 2, -D / 2 + fi - 0.02, D / 2, g1, H, s, bevel=0.004)  # canopy
    b.box(-W / 2 + gt, W / 2 - gt, -D / 2 + 0.19, D / 2 - gt, g0 + 0.22, g0 + 0.232, gl)  # glass shelf
    for x0, x1 in split_x(-W / 2 + 0.05, W / 2 - 0.05, 3, 0.03):
        b.box(x0, x1, -D / 2 + 0.10, D / 2 - 0.06, g0, g0 + 0.02, s)  # display trays


def round_table(b, W, D, H, R):
    w, m = R["wood"], R["metal_accent"]
    b.cylinder(0.0, 0.0, H - 0.03, H, W / 2, w, segments=32)
    b.cylinder(0.0, 0.0, H - 0.06, H - 0.03, 0.15, m, segments=16)
    b.cylinder(0.0, 0.0, 0.03, H - 0.06, 0.035, m, segments=16)
    b.cylinder(0.0, 0.0, 0.0, 0.03, 0.24, m, r1=0.21, segments=24)


def communal_table(b, W, D, H, R):
    w, m = R["wood"], R["metal_accent"]
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.045, H, w, bevel=0.005)
    for sx in (-1, 1):  # trestles
        x = sx * (W / 2 - 0.35)
        b.box(x - 0.03, x + 0.03, -D / 2 + 0.08, D / 2 - 0.08, 0.0, 0.04, m)
        b.box(x - 0.03, x + 0.03, -D / 2 + 0.12, D / 2 - 0.12, H - 0.10, H - 0.045, m)
        b.box(x - 0.025, x + 0.025, -(D / 2 - 0.23), D / 2 - 0.23, 0.28, 0.32, m)
        for sy in (-1, 1):
            y = sy * (D / 2 - 0.20)
            b.box(x - 0.03, x + 0.03, y - 0.03, y + 0.03, 0.04, H - 0.10, m)
    b.box(-W / 2 + 0.375, W / 2 - 0.375, -0.025, 0.025, 0.28, 0.32, m)  # stretcher


def bench(b, W, D, H, R):
    w, m = R["wood"], R["metal_accent"]
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.045, H, w, bevel=0.005)
    for sx in (-1, 1):
        x = sx * (W / 2 - 0.12)
        b.box(x - 0.02, x + 0.02, -D / 2 + 0.04, D / 2 - 0.04, 0.0, 0.025, m)
        b.box(x - 0.02, x + 0.02, -D / 2 + 0.06, D / 2 - 0.06, H - 0.07, H - 0.045, m)
        for sy in (-1, 1):
            y = sy * (D / 2 - 0.08)
            b.box(x - 0.02, x + 0.02, y - 0.02, y + 0.02, 0.025, H - 0.07, m)


def locker(b, W, D, H, R):
    lq, m = R["lacquer"], R["metal_accent"]
    b.box(-W / 2 + 0.02, W / 2 - 0.02, -D / 2 + 0.05, D / 2 - 0.02, 0.0, 0.10, m)  # plinth
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.10, H, lq, bevel=0.003)
    zmid = (H + 0.10) / 2
    for x0, x1 in split_x(-W / 2 + 0.01, W / 2 - 0.01, 3, 0.008):
        for z0, z1 in ((0.12, zmid - 0.004), (zmid + 0.004, H - 0.02)):
            b.box(x0, x1, -D / 2 + 0.008, -D / 2 + 0.02, z0, z1, lq, bevel=0.002)
            for k in range(4):  # vent slots
                vz = z1 - 0.07 - k * 0.025
                b.box(x0 + 0.06, x1 - 0.06, -D / 2 + 0.005, -D / 2 + 0.008, vz, vz + 0.008, m)
            zm = (z0 + z1) / 2
            b.box(x1 - 0.05, x1 - 0.03, -D / 2, -D / 2 + 0.008, zm - 0.05, zm + 0.05, m)  # lock / handle


def storage_cabinet(b, W, D, H, R):
    lq, m = R["lacquer"], R["metal_accent"]
    b.box(-W / 2 + 0.03, W / 2 - 0.03, -D / 2 + 0.05, D / 2 - 0.02, 0.0, 0.08, lq)  # plinth
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.08, H, lq, bevel=0.003)
    for i, (x0, x1) in enumerate(split_x(-W / 2 + 0.005, W / 2 - 0.005, 2, 0.006)):
        b.box(x0, x1, -D / 2 + 0.008, -D / 2 + 0.02, 0.10, H - 0.02, lq, bevel=0.002)
        hx = x1 - 0.05 if i == 0 else x0 + 0.03
        b.box(hx, hx + 0.02, -D / 2, -D / 2 + 0.008, 0.95, 1.25, m)
    b.box(-0.012, 0.012, -D / 2, -D / 2 + 0.008, 1.32, 1.36, m)  # lock


def server_rack(b, W, D, H, R):
    f, sc = R["frame_metal"], R["screen"]
    legs(b, W, D, 0.0, 0.05, 0.05, 0.02, f)
    b.box(-W / 2, W / 2, -D / 2, D / 2, 0.05, 0.12, f)  # base frame
    b.box(-W / 2, W / 2, -D / 2, D / 2, H - 0.06, H, f)  # roof
    for x0, x1 in ((-W / 2, -W / 2 + 0.015), (W / 2 - 0.015, W / 2)):
        b.box(x0, x1, -D / 2 + 0.03, D / 2, 0.12, H - 0.06, f)  # side panels
    b.box(-W / 2 + 0.015, W / 2 - 0.015, D / 2 - 0.015, D / 2, 0.12, H - 0.06, f)  # rear door
    for x0, x1 in ((-W / 2, -W / 2 + 0.04), (W / 2 - 0.04, W / 2)):
        b.box(x0, x1, -D / 2, -D / 2 + 0.03, 0.12, H - 0.06, f)  # front frame
    for x0, x1 in ((-W / 2 + 0.05, -W / 2 + 0.07), (W / 2 - 0.07, W / 2 - 0.05)):
        b.box(x0, x1, -D / 2 + 0.06, -D / 2 + 0.08, 0.14, H - 0.08, f)  # 19-inch rails
    z = 0.16
    for u in (2, 1, 1, 4, 2, 1, 2, 1, 3, 1, 2, 4, 1, 2, 1, 3, 2, 1, 2, 4, 1, 2, 1, 2, 3, 1, 2, 1, 2):
        h = u * 0.0445
        if z + h > H - 0.10:
            break
        b.box(-W / 2 + 0.07, W / 2 - 0.07, -D / 2 + 0.055, D / 2 - 0.25, z + 0.003, z + h - 0.003, sc)  # equipment
        z += h


def urinal(b, W, D, H, R):
    """Wall-hung urinal shell, open at the front; the asset spans its own body and
    default_elevation lifts it to rim height."""
    c, m = R["ceramic"], R["metal_accent"]
    b.box(-W / 2 + 0.02, W / 2 - 0.02, D / 2 - 0.06, D / 2, 0.02, H - 0.08, c, bevel=0.03)  # back
    b.box(-W / 2, W / 2, -D / 2, D / 2 - 0.04, 0.0, 0.14, c, bevel=0.05)  # deep bottom lip / bowl
    zt = H - 0.10
    for x0, x1 in ((-W / 2 + 0.005, -W / 2 + 0.05), (W / 2 - 0.05, W / 2 - 0.005)):  # cheeks taper to the top
        hexa(b, [(x0, -D / 2 + 0.02, 0.06), (x1, -D / 2 + 0.02, 0.06), (x1, D / 2 - 0.04, 0.06), (x0, D / 2 - 0.04, 0.06),
                 (x0, -D / 2 + 0.14, zt), (x1, -D / 2 + 0.14, zt), (x1, D / 2 - 0.04, zt), (x0, D / 2 - 0.04, zt)], c)
    b.box(-W / 2 + 0.01, W / 2 - 0.01, -D / 2 + 0.13, D / 2 - 0.02, zt - 0.03, zt + 0.03, c, bevel=0.025)  # top cap
    b.cylinder(0.0, D / 2 - 0.04, zt, H, 0.015, m, segments=12)  # flush pipe
    b.box(-0.03, 0.03, D / 2 - 0.07, D / 2 - 0.01, H - 0.06, H - 0.02, m)  # flush valve


def equipment_box(b, W, D, H, R):
    s = R["stainless"]
    bullet_legs(b, W, D - 0.02, 0.08, s, inset=0.04, yc=0.01)
    b.box(-W / 2, W / 2, -D / 2 + 0.02, D / 2, 0.08, H, s, bevel=0.01)
    b.box(-W / 2 + 0.04, W / 2 - 0.04, -D / 2 + 0.008, -D / 2 + 0.02, 0.12, H - 0.22, s, bevel=0.003)  # access panel
    louvres(b, -W / 2 + 0.10, W / 2 - 0.10, -D / 2 + 0.003, -D / 2 + 0.008, 0.16, 0.40, 6, s)
    b.box(-W / 2 + 0.04, W / 2 - 0.04, -D / 2 + 0.008, -D / 2 + 0.02, H - 0.18, H - 0.05, s, bevel=0.003)  # controls
    for x in (-W / 2 + 0.12, -W / 2 + 0.22, W / 2 - 0.12):
        knob(b, x, H - 0.115, -D / 2, 0.008, 0.025, s)


DESIGNS = {
    "sofa_3seat": sofa, "armchair": armchair, "coffee_table": coffee_table, "tv_unit": tv_unit,
    "dining_table": dining_table, "dining_chair": dining_chair, "bed_double": bed_double, "bed_single": bed_single,
    "nightstand": nightstand, "wardrobe": wardrobe, "desk": desk, "office_chair": office_chair,
    "bookshelf": bookshelf, "sideboard": sideboard, "kitchen_counter": kitchen_counter,
    "kitchen_island": kitchen_island, "fridge": fridge, "bar_stool": bar_stool, "toilet": toilet,
    "bathtub": bathtub, "shower": shower, "vanity": vanity, "washing_machine": washing_machine,
    "floor_lamp": floor_lamp, "plant": plant, "rug": rug,
    # commercial kitchen / F&B / back of house
    "work_table": work_table, "kitchen_cabinet": kitchen_cabinet, "wall_cabinet": wall_cabinet,
    "undercounter_chiller": undercounter_chiller, "upright_fridge": upright_fridge, "sink_unit": sink_unit,
    "hand_sink": hand_sink, "cooking_range": cooking_range, "griddle": griddle, "deep_fryer": deep_fryer,
    "combi_oven": combi_oven, "charcoal_grill": charcoal_grill, "dishwasher": dishwasher,
    "exhaust_hood": exhaust_hood, "shelving_rack": shelving_rack, "mobile_cart": mobile_cart,
    "floor_appliance": floor_appliance, "countertop_appliance": countertop_appliance, "waste_bin": waste_bin,
    "service_counter": service_counter, "display_case": display_case, "round_table": round_table,
    "communal_table": communal_table, "bench": bench, "locker": locker, "storage_cabinet": storage_cabinet,
    "server_rack": server_rack, "urinal": urinal, "equipment_box": equipment_box,
}

PREVIEW_COLORS = {
    "upholstery": (0.35, 0.36, 0.40), "cushion": (0.80, 0.75, 0.66), "wood": (0.55, 0.36, 0.20),
    "frame_metal": (0.05, 0.05, 0.05), "metal_accent": (0.08, 0.08, 0.08), "ceramic": (0.9, 0.9, 0.9),
    "stone_top": (0.8, 0.78, 0.75), "glass": (0.6, 0.75, 0.8), "appliance": (0.6, 0.62, 0.64),
    "screen": (0.02, 0.02, 0.02), "plant": (0.1, 0.35, 0.08), "pot": (0.6, 0.25, 0.12), "rug": (0.6, 0.55, 0.45),
    "lamp_shade": (0.9, 0.85, 0.75), "bedding": (0.92, 0.92, 0.9), "lacquer": (0.85, 0.85, 0.83),
    "mirror": (0.8, 0.85, 0.88), "stainless": (0.72, 0.74, 0.76), "preview_label": (0.9, 0.9, 0.85),
}


def role_material(role: str) -> bpy.types.Material:
    name = f"ROLE_{role}"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = (*PREVIEW_COLORS.get(role, (0.7, 0.7, 0.7)), 1.0)
    bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.node_tree else None
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = mat.diffuse_color
    return mat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--preview")
    args = ap.parse_args(script_args())

    reset_scene()
    catalog = load_json(args.catalog)
    lib = bpy.data.collections.new("LIBRARY")
    bpy.context.scene.collection.children.link(lib)
    report = {"blender": bpy.app.version_string, "catalog_version": catalog["catalog_version"], "assets": {}}
    missing = [a["id"] for a in catalog["assets"] if a["id"] not in DESIGNS]
    if missing:
        raise SystemExit(f"no design for catalog assets: {missing}")
    n = len(catalog["assets"])
    for i, asset in enumerate(catalog["assets"]):
        W, D, H = asset["size"]
        roles = {r: k for k, r in enumerate(asset["roles"])}
        b = MeshBuilder()
        DESIGNS[asset["id"]](b, W, D, H, roles)
        mesh = b.to_mesh(f"MESH_{asset['id']}")
        factors = normalize_to_box(mesh, W, D, H)
        for r in asset["roles"]:
            mesh.materials.append(role_material(r))
        used = sorted({p.material_index for p in mesh.polygons})
        if used != list(range(len(asset["roles"]))):
            raise SystemExit(f"{asset['id']}: roles {asset['roles']} but faces use slots {used}")
        box_uv(mesh)
        set_smooth_by_angle(mesh, 35.0)
        obj = bpy.data.objects.new(f"ASSET_{asset['id']}", mesh)
        obj["twin_asset_id"] = asset["id"]
        obj["twin_size"] = list(asset["size"])
        lib.objects.link(obj)
        co = [v.co for v in mesh.vertices]
        dims = [max(c[k] for c in co) - min(c[k] for c in co) for k in range(3)]
        report["assets"][asset["id"]] = {
            "object": obj.name, "dims": [round(x, 6) for x in dims], "expected": asset["size"],
            "correction": [round(f, 5) for f in factors], "triangles": sum(len(p.vertices) - 2 for p in mesh.polygons),
            "roles": asset["roles"],
        }
        progress((i + 1) / n * 0.8, "assets", asset=asset["id"])

    for a in report["assets"].values():
        if any(abs(x - y) > 1e-4 for x, y in zip(a["dims"], a["expected"])):
            raise SystemExit(f"asset {a['object']} dims {a['dims']} != {a['expected']}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.out).resolve()), compress=True)
    progress(0.9, "saved")
    if args.preview:
        render_preview(lib, args.preview, [a["id"] for a in catalog["assets"]])
    save_json(args.report, report)
    progress(1.0, "done")


def render_preview(lib, path: str, order: list[str]) -> None:
    """Contact sheet of every asset in catalog order, labelled with its id (for humans; not used
    by the pipeline). Runs after the library is saved, so labels/camera never reach the .blend."""
    from mathutils import Vector
    scene = bpy.context.scene
    rank = {f"ASSET_{a}": k for k, a in enumerate(order)}
    objs = sorted(lib.objects, key=lambda o: (rank.get(o.name, len(rank)), o.name))
    cols, sx, sy = 8, 4.2, 4.0
    rows = (len(objs) + cols - 1) // cols
    elev = math.radians(55.0)  # camera elevation above the horizon
    label_mat = role_material("preview_label")
    for i, o in enumerate(objs):
        x, y = (i % cols) * sx, -(i // cols) * sy
        o.location = (x, y, 0.0)
        txt = bpy.data.curves.new(f"PreviewLabel_{o.name}", "FONT")
        txt.body = o.get("twin_asset_id", o.name)
        txt.size, txt.align_x = 0.30, "CENTER"
        txt.materials.append(label_mat)
        lbl = bpy.data.objects.new(txt.name, txt)
        lbl.location = (x, y - o["twin_size"][1] / 2 - 0.55, 0.0)
        lbl.rotation_euler = (math.pi / 2 - elev, 0.0, 0.0)  # face the camera
        scene.collection.objects.link(lbl)
    sun = bpy.data.objects.new("PreviewSun", bpy.data.lights.new("PreviewSun", "SUN"))
    sun.data.energy = 4.0
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    fill = bpy.data.objects.new("PreviewFill", bpy.data.lights.new("PreviewFill", "SUN"))
    fill.data.energy = 1.2
    fill.rotation_euler = (math.radians(60), 0, math.radians(-140))
    scene.collection.objects.link(fill)
    cam = bpy.data.objects.new("PreviewCam", bpy.data.cameras.new("PreviewCam"))
    scene.collection.objects.link(cam)
    width = (cols - 1) * sx + 4.6
    depth = (rows - 1) * sy + 3.0
    center = Vector(((cols - 1) * sx / 2, -(rows - 1) * sy / 2 - 0.3, 0.7))
    cam.location = center + 60.0 * Vector((0.0, -math.cos(elev), math.sin(elev)))
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    res_x, res_y = 2600, 1800
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = max(width, (depth * math.sin(elev) + 2.4 * math.cos(elev)) * res_x / res_y)
    cam.data.clip_end = 200.0
    scene.camera = cam
    world = bpy.data.worlds.new("PreviewWorld")
    world.color = (0.62, 0.64, 0.67)
    scene.world = world
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = res_x, res_y
    scene.render.filepath = str(Path(path).resolve())
    bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
