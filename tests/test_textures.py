"""Generated material textures: deterministic, seamless, stable cache keys."""

import numpy as np
import pytest

from twin.scene.model import Material
from twin.scene.styles import BUILTIN_MATERIALS
from twin.scene.textures import RGB_PATTERNS, ensure_texture, render_texture, texture_key, texture_size

NEW_PATTERNS = ["resin", "brushed", "panel", "perforated", "stack_tile", "endgrain", "chevron", "boards"]


def _mat(pattern: str, **kw) -> Material:
    return Material(id=f"t_{pattern}", name=pattern, base_color="#A86A36", pattern=pattern,
                    tile_size=kw.pop("tile_size", 0.6), **kw)


@pytest.mark.parametrize("pattern", NEW_PATTERNS)
def test_new_patterns_render_deterministically(pattern):
    m = _mat(pattern, palette=["#D9A05A", "#4A2A12"], joint_color="#222222")
    a, b = np.asarray(render_texture(m)), np.asarray(render_texture(m))
    assert a.shape == (texture_size(m), texture_size(m), 3)
    assert np.array_equal(a, b)
    assert a.std() > 1.0  # not a flat colour


@pytest.mark.parametrize("pattern", NEW_PATTERNS)
def test_new_patterns_tile_seamlessly(pattern):
    img = np.asarray(render_texture(_mat(pattern, palette=["#D9A05A", "#4A2A12"])), dtype=float)
    # the wrap-around step (last column -> first) must be no bigger than the steps inside the tile
    # (patterns with cell edges have their edge on the tile border, so compare with the largest step)
    for im in (img, img.transpose(1, 0, 2)):
        steps = np.abs(np.diff(im, axis=1)).mean(axis=(0, 2))
        wrap = np.abs(im[:, -1] - im[:, 0]).mean()
        assert wrap <= max(steps.max() * 1.05, 2 * np.median(steps) + 4), (wrap, steps.max())


def test_palette_colours_appear_in_multicolour_patterns():
    img = np.asarray(render_texture(_mat("stack_tile", palette=["#1060E0"])), dtype=float)
    blue = (img[..., 2] > img[..., 0] + 60).mean()
    assert 0.1 < blue < 0.95


def test_existing_material_keys_unchanged():
    # cache keys of materials that don't use palette/joint_color must not move (see assets_build/textures)
    assert texture_key(BUILTIN_MATERIALS["oak_planks"]) == "419c1d662141"
    m = BUILTIN_MATERIALS["oak_planks"]
    assert texture_key(m.model_copy(update={"palette": ["#000000"]})) != texture_key(m)


def test_rgb_patterns_cover_new_multicolour_kinds():
    assert {"panel", "perforated", "stack_tile", "endgrain", "chevron", "boards"} == set(RGB_PATTERNS)


def test_ensure_texture_writes_once(tmp_path):
    m = _mat("perforated", tile_size=1.2)
    p1 = ensure_texture(m, tmp_path)
    mtime = p1.stat().st_mtime_ns
    assert ensure_texture(m, tmp_path) == p1 and p1.stat().st_mtime_ns == mtime
