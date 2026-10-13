"""Unit conversions and coordinate transforms."""

import math

import pytest

from twin.scene.transforms import (
    PlanCalibration, hex_to_linear_rgb, scene_to_blender, scene_to_unreal_location, scene_to_unreal_yaw,
    unreal_to_scene_location, unreal_to_scene_yaw,
)


def test_blender_is_identity():
    assert scene_to_blender(1.25, -3.5, 2.7) == (1.25, -3.5, 2.7)


@pytest.mark.parametrize("p, ue", [
    ((0, 0, 0), (0, 0, 0)),
    ((1.0, 0, 0), (100.0, 0, 0)),
    ((0, 1.0, 0), (0, -100.0, 0)),       # north (+Y) is -Y in Unreal's left-handed frame
    ((0, 0, 2.7), (0, 0, 270.0)),
    ((10.2, 8.2, -0.2), (1020.0, -820.0, -20.0)),
])
def test_scene_to_unreal_location(p, ue):
    assert scene_to_unreal_location(*p) == pytest.approx(ue)
    assert unreal_to_scene_location(*ue) == pytest.approx(p)


@pytest.mark.parametrize("rot, yaw", [(0, 0), (90, -90), (180, -180), (270, 90), (-90, 90), (45, -45), (360, 0)])
def test_scene_to_unreal_yaw(rot, yaw):
    assert scene_to_unreal_yaw(rot) == pytest.approx(yaw)
    assert unreal_to_scene_yaw(yaw) % 360 == pytest.approx(rot % 360)


def test_yaw_is_consistent_with_mirrored_axes():
    """Rotating +X by the scene angle then mirroring Y equals rotating UE +X by the UE yaw."""
    for rot in range(-360, 361, 15):
        a = math.radians(rot)
        sx, sy = math.cos(a), math.sin(a)
        ux, uy, _ = scene_to_unreal_location(sx, sy, 0)
        y = math.radians(scene_to_unreal_yaw(rot))
        assert (ux / 100, uy / 100) == pytest.approx((math.cos(y), math.sin(y)), abs=1e-9)


def test_plan_calibration_round_trip():
    cal = PlanCalibration.from_reference((100, 900), (1100, 900), 10.0, origin_px=(100, 900))
    assert cal.m_per_px == pytest.approx(0.01)
    assert cal.px_to_m(1100, 100) == pytest.approx((10.0, 8.0))   # image y grows downwards
    assert cal.m_to_px(10.0, 8.0) == pytest.approx((1100, 100))
    with pytest.raises(ValueError):
        PlanCalibration.from_reference((5, 5), (5, 5), 3.0, origin_px=(0, 0))


def test_hex_to_linear():
    assert hex_to_linear_rgb("#FFFFFF") == pytest.approx((1, 1, 1))
    assert hex_to_linear_rgb("#000000") == pytest.approx((0, 0, 0))
    assert hex_to_linear_rgb("#808080")[0] == pytest.approx(0.2158605, abs=1e-6)
