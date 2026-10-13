"""Unit and coordinate conversions between the scene model, Blender, Unreal and plan images.

Scene / Blender: metres, right-handed, Z up, +X east, +Y north. Blender uses the scene
frame unchanged (scene unit scale 1.0), so ``scene_to_blender`` is the identity.

Unreal Engine 5: centimetres, LEFT-handed, Z up. The Blender FBX exporter + UE importer
map Blender (x, y, z) m to UE (100x, -100y, 100z) cm: the Y axis is mirrored, which turns
counter-clockwise rotations into clockwise ones, so yaw_ue = -rotation_deg.
These formulas are checked by the unit tests and by the UE import verification.

Plan images: pixels with the origin at the top-left and +y pointing down. A calibration
maps pixels to scene metres: x_m = (px - origin_px) * m_per_px,
y_m = (origin_py - py) * m_per_px (image y is flipped).
"""

from __future__ import annotations

from dataclasses import dataclass

M_TO_CM = 100.0


def scene_to_blender(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x, y, z)


def scene_to_unreal_location(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x * M_TO_CM, -y * M_TO_CM, z * M_TO_CM)


def unreal_to_scene_location(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x / M_TO_CM, -y / M_TO_CM, z / M_TO_CM)


def scene_to_unreal_yaw(rotation_deg: float) -> float:
    """Scene rotation (CCW about +Z, degrees) -> Unreal yaw (degrees)."""
    yaw = -rotation_deg
    yaw = (yaw + 180.0) % 360.0 - 180.0
    return 0.0 if yaw == -0.0 else yaw


def unreal_to_scene_yaw(yaw_deg: float) -> float:
    return (-yaw_deg) % 360.0


def scene_to_unreal_scale(sx: float, sy: float, sz: float) -> tuple[float, float, float]:
    """Mirroring Y flips the sign of both a direction and its axis, so scale magnitudes carry over."""
    return (sx, sy, sz)


@dataclass(frozen=True)
class PlanCalibration:
    """Maps plan image pixels to scene metres."""

    m_per_px: float
    origin_px: float  # pixel x of scene x = 0
    origin_py: float  # pixel y of scene y = 0

    def px_to_m(self, px: float, py: float) -> tuple[float, float]:
        return ((px - self.origin_px) * self.m_per_px, (self.origin_py - py) * self.m_per_px)

    def m_to_px(self, x: float, y: float) -> tuple[float, float]:
        return (self.origin_px + x / self.m_per_px, self.origin_py - y / self.m_per_px)

    @staticmethod
    def from_reference(p1_px: tuple[float, float], p2_px: tuple[float, float], length_m: float,
                       origin_px: tuple[float, float]) -> "PlanCalibration":
        """Calibrate from a reference line drawn on the image with a known real length."""
        dx, dy = p2_px[0] - p1_px[0], p2_px[1] - p1_px[1]
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= 0 or length_m <= 0:
            raise ValueError("reference line must have a positive length in pixels and metres")
        return PlanCalibration(length_m / dist, origin_px[0], origin_px[1])


def hex_to_linear_rgb(hex_color: str) -> tuple[float, float, float]:
    """sRGB hex -> linear RGB (what Blender's Principled BSDF base colour expects)."""
    h = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (out[0], out[1], out[2])
