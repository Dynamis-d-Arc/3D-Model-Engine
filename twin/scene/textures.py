"""Deterministic, tileable material textures (PNG, sRGB).

Each patterned material gets one base-colour texture whose single tile covers
``material.tile_size`` metres; the Blender worker writes UVs in metres / tile_size,
so tiling survives FBX/glTF export (no node-based mapping needed in Unreal or three.js).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

from twin.scene.model import Material

TEXTURE_SIZE = 512
TEXTURE_VERSION = "1"


def _rng(material: Material) -> np.random.Generator:
    seed = int(hashlib.sha256(f"{material.id}:{material.pattern}".encode()).hexdigest()[:8], 16)
    return np.random.default_rng(seed)


def periodic_noise(n: int, cells: int, rng: np.random.Generator) -> np.ndarray:
    """Smooth value noise in [-1, 1] that wraps seamlessly at the image borders."""
    grid = rng.uniform(-1.0, 1.0, (cells, cells))
    coords = np.arange(n) * cells / n
    i0 = np.floor(coords).astype(int)
    t = coords - i0
    t = t * t * (3 - 2 * t)
    i1 = (i0 + 1) % cells
    a = grid[np.ix_(i0, i0)] * (1 - t)[None, :] + grid[np.ix_(i0, i1)] * t[None, :]
    b = grid[np.ix_(i1, i0)] * (1 - t)[None, :] + grid[np.ix_(i1, i1)] * t[None, :]
    return a * (1 - t)[:, None] + b * t[:, None]


def fbm(n: int, rng: np.random.Generator, base: int = 4, octaves: int = 4) -> np.ndarray:
    out = np.zeros((n, n))
    amp, total = 1.0, 0.0
    for o in range(octaves):
        out += amp * periodic_noise(n, base * 2 ** o, rng)
        total += amp
        amp *= 0.5
    return out / total


def _detail(material: Material, n: int) -> np.ndarray:
    """Grey-scale detail map in roughly [-1, 1] for the material's pattern."""
    rng = _rng(material)
    y, x = np.mgrid[0:n, 0:n] / n  # one tile = [0, 1)
    tile_m = material.tile_size
    p = material.pattern
    if p == "wood":
        planks = max(1, round(tile_m / 0.19))
        col = np.floor(x * planks).astype(int)
        tone = rng.uniform(-0.6, 0.6, planks)[col]
        lengths = max(1, round(tile_m / 1.2))
        offs = rng.uniform(0, 1, planks)[col]
        row_pos = (y * lengths + offs) % 1.0
        end_seam = (row_pos < 0.006).astype(float)
        side_seam = ((x * planks) % 1.0 < 0.012).astype(float)
        warp = fbm(n, rng, base=4, octaves=3) * 0.15
        grain = np.sin((x * planks * 7 + warp * 6 + offs * 10) * 2 * np.pi * 3) * 0.25
        streaks = fbm(n, rng, base=16, octaves=2) * 0.35
        return np.clip(0.55 * tone + grain + streaks - 1.6 * np.maximum(end_seam, side_seam), -1.5, 1.0)
    if p == "tile":
        count = max(1, round(tile_m / 0.6))
        tx, ty = (x * count) % 1.0, (y * count) % 1.0
        grout = ((tx < 0.012) | (ty < 0.012)).astype(float)
        idx = np.floor(x * count).astype(int) * 97 + np.floor(y * count).astype(int)
        tone = rng.uniform(-0.25, 0.25, count * 97 + count)[idx]
        return tone + fbm(n, rng, base=8, octaves=3) * 0.12 - 1.2 * grout
    if p == "brick":
        rows = max(2, round(tile_m / 0.075)) // 2 * 2
        per_row = max(1, round(tile_m / 0.225))
        r = np.floor(y * rows).astype(int)
        bx = (x * per_row + 0.5 * (r % 2)) % per_row
        bi = np.floor(bx).astype(int) + r * 101
        tone = rng.uniform(-0.5, 0.5, rows * 101 + per_row + 1)[bi]
        mortar = (((y * rows) % 1.0 < 0.14) | ((bx % 1.0) < 0.045)).astype(float)
        return tone + fbm(n, rng, base=16, octaves=3) * 0.3 + mortar * 1.4
    if p == "marble":
        turb = fbm(n, rng, base=2, octaves=4)
        veins = np.abs(np.sin((x + y * 0.5 + turb * 1.2) * 2 * np.pi))
        fine = np.abs(np.sin((x * 3 + y * 2 + fbm(n, rng, base=4, octaves=4) * 2.5) * 2 * np.pi))
        return -1.3 * (1 - veins) ** 18 - 0.5 * (1 - fine) ** 30 + fbm(n, rng, base=4, octaves=3) * 0.15
    if p == "stone":
        return fbm(n, rng, base=8, octaves=5) * 0.6 + (rng.uniform(0, 1, (n, n)) > 0.985) * 0.6
    if p == "concrete":
        return fbm(n, rng, base=4, octaves=5) * 0.5 + rng.normal(0, 0.08, (n, n)) \
            - (rng.uniform(0, 1, (n, n)) > 0.997) * 1.2
    if p == "fabric":
        threads = max(8, round(tile_m / 0.004)) // 4 * 4
        weave = np.sin(x * threads * np.pi * 2) * np.sin(y * threads * np.pi * 2)
        return weave * 0.25 + fbm(n, rng, base=8, octaves=3) * 0.2
    if p == "plaster":
        return fbm(n, rng, base=8, octaves=5) * 0.25
    if p == "resin":  # poured resin floor: soft clouding and a fine aggregate speckle
        return fbm(n, rng, base=2, octaves=5) * 0.7 + rng.normal(0, 0.12, (n, n))
    if p == "brushed":  # horizontal brush streaks
        rows = rng.normal(0, 1, n)
        rows = 0.25 * np.roll(rows, 1) + 0.5 * rows + 0.25 * np.roll(rows, -1)
        return rows[:, None] * (0.6 + 0.4 * periodic_noise(n, 4, rng)) * 0.8 + fbm(n, rng, base=2, octaves=3) * 0.4
    return np.zeros((n, n))


AMPLITUDE = {"wood": 0.22, "tile": 0.12, "brick": 0.28, "marble": 0.25, "stone": 0.18, "concrete": 0.16,
             "fabric": 0.18, "plaster": 0.06, "resin": 0.05, "brushed": 0.07}


# ---------------------------------------------------------------------------------------------
# multi-colour patterns (rendered straight to RGB in [0, 1]); array rows run along V (up a wall)
# ---------------------------------------------------------------------------------------------

def _rgb(hex_color: str) -> np.ndarray:
    return np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)], dtype=float) / 255.0


def _palette(material: Material, rng: np.random.Generator, count: int, base_weight: float = 0.4) -> np.ndarray:
    """``count`` colours drawn from base_color + palette (base_color gets ``base_weight``)."""
    cols = [_rgb(material.base_color)] + [_rgb(c) for c in material.palette]
    if len(cols) == 1:
        return np.repeat(cols[0][None, :], count, axis=0)
    w = np.array([base_weight] + [(1 - base_weight) / (len(cols) - 1)] * (len(cols) - 1))
    return np.array(cols)[rng.choice(len(cols), count, p=w)]


def _joint(material: Material, factor: float) -> np.ndarray:
    return _rgb(material.joint_color) if material.joint_color else _rgb(material.base_color) * factor


def _edge_mask(dist_m: np.ndarray, half_width_m: float, ppm: float) -> np.ndarray:
    """Anti-aliased coverage of a line whose centre is at distance 0."""
    return np.clip((half_width_m - dist_m) * ppm + 0.5, 0.0, 1.0)


def _panel(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """Insulated sandwich panel: smooth enamel with a V-groove joint every tile (vertical)."""
    ppm = n / m.tile_size
    x = (np.arange(n) / n)[None, :].repeat(n, 0)
    tone = 1 + 0.03 * fbm(n, rng, base=6, octaves=4) + rng.normal(0, 0.01, (n, n))
    rgb = _rgb(m.base_color)[None, None, :] * tone[:, :, None]
    d = np.minimum(x, 1 - x) * m.tile_size
    groove = _edge_mask(d, 0.003, ppm)[:, :, None]
    shoulder = np.clip(1 - np.abs(d - 0.006) * ppm / 2, 0, 1)[:, :, None]
    return (rgb * (1 + 0.05 * shoulder)) * (1 - groove) + _joint(m, 0.62) * groove


def _perforated(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """Perforated timber acoustic panel: 25 mm hole grid, horizontal veneer grain, joints every tile."""
    ppm = n / m.tile_size
    count = max(4, round(m.tile_size / 0.025))
    y, x = np.mgrid[0:n, 0:n] / n
    warp = fbm(n, rng, base=3, octaves=3)
    grain = np.sin(2 * np.pi * (y * max(8, round(m.tile_size * 30)) + 0.8 * warp))
    tone = 1 + 0.035 * grain + 0.06 * fbm(n, rng, base=4, octaves=4)
    rgb = _rgb(m.base_color)[None, None, :] * tone[:, :, None]
    cx, cy = (x * count) % 1.0 - 0.5, (y * count) % 1.0 - 0.5
    d = np.hypot(cx, cy) * m.tile_size / count  # metres from the hole centre
    hole = _edge_mask(d, 0.004, ppm)
    rim = np.clip(1 - np.abs(d - 0.0048) * ppm / 1.5, 0, 1) * 0.18
    rgb = rgb * (1 - rim)[:, :, None]
    rgb = rgb * (1 - hole[:, :, None]) + _joint(m, 0.22) * hole[:, :, None]
    seam = _edge_mask(np.minimum(np.minimum(x, 1 - x), np.minimum(y, 1 - y)) * m.tile_size, 0.0015, ppm)
    return rgb * (1 - seam[:, :, None]) + _joint(m, 0.4) * seam[:, :, None]


def _stack_tile(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """Hand-glazed 75 x 300 mm tiles, stack bond (vertical), with per-tile tone and pillowed edges."""
    ppm = n / m.tile_size
    cols, rows = max(1, round(m.tile_size / 0.075)), max(1, round(m.tile_size / 0.3))
    tw, th = m.tile_size / cols, m.tile_size / rows
    y, x = np.mgrid[0:n, 0:n] / n
    X, Y = x * cols, y * rows
    ci, ri = np.floor(X).astype(int), np.floor(Y).astype(int)
    fx, fy = X - ci, Y - ri
    idx = ri * cols + ci
    colour = _palette(m, rng, cols * rows)[idx]
    bright = rng.uniform(-0.08, 0.08, cols * rows)[idx]
    glaze = fbm(n, rng, base=max(cols, rows) * 2, octaves=3)
    edge = np.minimum(np.minimum(fx * tw, (1 - fx) * tw), np.minimum(fy * th, (1 - fy) * th))
    pillow = np.clip(edge / 0.008, 0, 1)
    rgb = colour * ((1 + bright + 0.07 * glaze) * (0.8 + 0.2 * pillow))[:, :, None]
    grout = _edge_mask(edge, 0.0015, ppm)[:, :, None]
    joint = _joint(m, 1.0) if m.joint_color else np.clip(_rgb(m.base_color) * 0.7 + 0.25, 0, 1)
    return rgb * (1 - grout) + joint * grout


def _endgrain(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """End-grain parquet mosaic: rows of random blocks, each with its own tone and growth rings."""
    ppm = n / m.tile_size

    def cuts(lo: float, hi: float) -> np.ndarray:  # block boundaries in pixels, summing to the tile
        sizes = []
        while sum(sizes) < m.tile_size:
            sizes.append(rng.uniform(lo, hi))
        edges = np.cumsum([0.0] + sizes) * (n / sum(sizes))
        return edges

    out = np.zeros((n, n, 3))
    row_edges = np.round(cuts(0.025, 0.05)).astype(int)
    xs = np.arange(n)
    for r0, r1 in zip(row_edges, row_edges[1:]):
        if r1 <= r0:
            continue
        edges = cuts(0.02, 0.065)
        shift = rng.uniform(0, n)
        pos = (xs - shift) % n
        bi = np.clip(np.searchsorted(edges, pos, side="right") - 1, 0, len(edges) - 2)
        nb = len(edges) - 1
        colour = _palette(m, rng, nb)
        bright = rng.uniform(-0.12, 0.12, nb)
        widths = np.diff(edges)
        ring_cx = widths * rng.uniform(-1.0, 2.0, nb)  # ring centre, block-local pixels (often outside)
        ring_cy = (r1 - r0) * rng.uniform(-1.0, 2.0, nb)
        spacing = rng.uniform(0.0015, 0.0035, nb) * ppm
        gx, gy = rng.uniform(-0.18, 0.18, nb), rng.uniform(-0.18, 0.18, nb)
        lx = pos - edges[bi]  # block-local x per column
        ly = np.arange(r1 - r0)[:, None]
        dist = np.hypot(lx[None, :] - ring_cx[bi][None, :], ly - ring_cy[bi][None, :])
        ring = np.sin(2 * np.pi * dist / spacing[bi][None, :])
        grad = gx[bi][None, :] * (lx / widths[bi] - 0.5)[None, :] + gy[bi][None, :] * (ly / (r1 - r0) - 0.5)
        tone = (1 + bright[bi])[None, :] * (1 + 0.16 * ring + grad)
        glue = ((lx < 1.0)[None, :] | (ly < 1.0)).astype(float)
        out[r0:r1] = colour[bi][None, :, :] * (tone * (1 - 0.35 * glue))[:, :, None]
    return out * (1 + 0.05 * fbm(n, rng, base=16, octaves=2))[:, :, None]


def _chevron(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """Geometric patchwork upholstery: diagonal bands of triangles in the palette, fine weave."""
    k = max(2, round(m.tile_size / 0.06))
    y, x = np.mgrid[0:n, 0:n] / n
    X, Y = x * k, y * k
    a, b = np.floor(X).astype(int), np.floor(Y).astype(int)
    tri = ((X - a) + (Y - b) > 1).astype(int)
    band = _palette(m, rng, k * 2, base_weight=0.45).reshape(k, 2, 3)
    cell = _palette(m, rng, k * k * 2, base_weight=0.3).reshape(k, k, 2, 3)
    swap = rng.uniform(0, 1, (k, k, 2)) < 0.15
    s = (a + b) % k
    colour = np.where(swap[a, b, tri][:, :, None], cell[a, b, tri], band[s, tri])
    striped = (rng.uniform(0, 1, (k, k, 2)) < 0.3)[a, b, tri]
    pin = np.where(striped, np.sin(2 * np.pi * (X - Y) * 4) * 0.07, 0.0)
    threads = max(8, round(m.tile_size / 0.004)) // 4 * 4
    weave = np.sin(x * threads * np.pi * 2) * np.sin(y * threads * np.pi * 2) * 0.05
    return colour * (1 + pin + weave + 0.04 * fbm(n, rng, base=8, octaves=2))[:, :, None]


def _boards(m: Material, n: int, rng: np.random.Generator) -> np.ndarray:
    """Vertical board panelling (about 300 mm boards) with grain along the boards."""
    ppm = n / m.tile_size
    planks = max(1, round(m.tile_size / 0.3))
    y, x = np.mgrid[0:n, 0:n] / n
    col = np.floor(x * planks).astype(int)
    colour = _palette(m, rng, planks)[col]
    bright = rng.uniform(-0.06, 0.06, planks)[col]
    offs = rng.uniform(0, 1, planks)[col]
    warp = fbm(n, rng, base=4, octaves=3)
    fine = np.sin(2 * np.pi * (x * planks * 30 + warp * 0.5 + offs * 7))
    cathedral = np.sin(2 * np.pi * (x * planks * 3 + warp * 0.35 + offs))
    streak = fbm(n, rng, base=32, octaves=2)
    streak = sum(np.roll(streak, s, axis=0) for s in range(0, n, max(1, n // 24))) / 24
    rgb = colour * (1 + bright + 0.025 * fine + 0.035 * cathedral ** 3 + 0.3 * streak)[:, :, None]
    d = np.minimum((x * planks) % 1.0, 1 - (x * planks) % 1.0) * (m.tile_size / planks)
    seam = _edge_mask(d, 0.0015, ppm)[:, :, None]
    return rgb * (1 - seam) + _joint(m, 0.35) * seam


RGB_PATTERNS = {"panel": _panel, "perforated": _perforated, "stack_tile": _stack_tile, "endgrain": _endgrain,
                "chevron": _chevron, "boards": _boards}
# fine-detail patterns need more pixels per tile
TEXTURE_SIZES = {"perforated": 1024, "stack_tile": 1024, "endgrain": 1024, "chevron": 1024, "boards": 1024}


def texture_key(material: Material) -> str:
    # unset colour extras are left out so materials that don't use them keep their existing keys
    data = f"{TEXTURE_VERSION}:{material.model_dump_json(exclude={'palette', 'joint_color'})}"
    if material.palette or material.joint_color:
        data += f":{material.palette}:{material.joint_color}"
    return hashlib.sha256(data.encode()).hexdigest()[:12]


def texture_size(material: Material) -> int:
    return TEXTURE_SIZES.get(material.pattern, TEXTURE_SIZE)


def render_texture(material: Material, size: int | None = None) -> Image.Image:
    size = size or texture_size(material)
    if material.pattern in RGB_PATTERNS:
        rgb = RGB_PATTERNS[material.pattern](material, size, _rng(material))
    else:
        base = _rgb(material.base_color)
        detail = _detail(material, size)
        amp = AMPLITUDE.get(material.pattern, 0.0)
        rgb = base[None, None, :] * (1.0 + amp * detail[:, :, None])
    rgb = np.clip(rgb, 0.0, 1.0)
    return Image.fromarray((rgb * 255 + 0.5).astype(np.uint8), "RGB")


def ensure_texture(material: Material, cache_dir: Path) -> Path | None:
    """Write (or reuse) the texture for a patterned material; None for plain colours."""
    if material.pattern == "none":
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"T_{material.id}_{texture_key(material)}.png"
    if not path.exists():
        tmp = path.with_suffix(".tmp.png")
        render_texture(material).save(tmp, optimize=True)
        tmp.replace(path)
    return path
