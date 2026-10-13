"""Helpers shared by the scripts that run INSIDE Blender (bpy, bmesh, mathutils only).

Imported by build_library.py / build_scene.py / verify_exports.py after they add this
directory to sys.path. Must not import anything from the ``twin`` package.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector

T0 = time.time()


def progress(fraction: float, stage: str, **extra) -> None:
    """Machine-readable progress line parsed by twin.blender.runner."""
    msg = {"progress": round(max(0.0, min(1.0, fraction)), 3), "stage": stage, "t": round(time.time() - T0, 2)}
    msg.update(extra)
    print("@@TWIN " + json.dumps(msg), flush=True)


def script_args() -> list[str]:
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"


# ------------------------------------------------------------------------------------------
# mesh building
# ------------------------------------------------------------------------------------------

class MeshBuilder:
    """Accumulates parts into one bmesh; each part carries a material slot index."""

    def __init__(self) -> None:
        self.bm = bmesh.new()

    def _tag(self, faces, mat: int) -> None:
        for f in faces:
            f.material_index = mat

    def box(self, x0, x1, y0, y1, z0, z1, mat: int, bevel: float = 0.0, rot_z: float = 0.0) -> None:
        if x1 - x0 <= 1e-6 or y1 - y0 <= 1e-6 or z1 - z0 <= 1e-6:
            raise ValueError(f"degenerate box {x0, x1, y0, y1, z0, z1}")
        res = bmesh.ops.create_cube(self.bm, size=1.0)
        verts = res["verts"]
        sx, sy, sz = x1 - x0, y1 - y0, z1 - z0
        cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
        m = Matrix.Translation((cx, cy, cz)) @ Matrix.Rotation(rot_z, 4, "Z") @ Matrix.Diagonal((sx, sy, sz, 1.0))
        bmesh.ops.transform(self.bm, matrix=m, verts=verts)
        self._tag(list({f for v in verts for f in v.link_faces}), mat)  # before bevel: bevel faces inherit it
        if bevel > 0:
            edges = list({e for v in verts for e in v.link_edges})
            out = bmesh.ops.bevel(self.bm, geom=verts + edges, offset=min(bevel, sx / 3, sy / 3, sz / 3),
                                  segments=2, profile=0.5, affect="EDGES", clamp_overlap=True, material=mat)
            self._tag(out.get("faces", []), mat)

    def cylinder(self, cx, cy, z0, z1, r0, mat: int, r1=None, segments: int = 24, axis: str = "Z") -> None:
        """Cylinder or cone. Segment count is a multiple of 4 so the bounds hit the exact radius."""
        r1 = r0 if r1 is None else r1
        res = bmesh.ops.create_cone(self.bm, cap_ends=True, cap_tris=False, segments=segments,
                                    radius1=r0, radius2=r1, depth=z1 - z0)
        verts = res["verts"]
        if axis == "Z":
            m = Matrix.Translation((cx, cy, (z0 + z1) / 2))
        elif axis == "Y":  # cx, cy are (x, z) of the axis; z0..z1 span along Y
            m = Matrix.Translation((cx, (z0 + z1) / 2, cy)) @ Matrix.Rotation(math.radians(90), 4, "X")
        else:
            raise ValueError(axis)
        bmesh.ops.transform(self.bm, matrix=m, verts=verts)
        self._tag(list({f for v in verts for f in v.link_faces}), mat)

    def sphere(self, cx, cy, cz, r, mat: int, scale=(1.0, 1.0, 1.0), segments: int = 16, rings: int = 8) -> None:
        res = bmesh.ops.create_uvsphere(self.bm, u_segments=segments, v_segments=rings, radius=r)
        verts = res["verts"]
        m = Matrix.Translation((cx, cy, cz)) @ Matrix.Diagonal((*scale, 1.0))
        bmesh.ops.transform(self.bm, matrix=m, verts=verts)
        self._tag(list({f for v in verts for f in v.link_faces}), mat)

    def prism(self, rings, tris, z0, z1, mat_for_face) -> None:
        """Extruded polygon from a build-plan prism. mat_for_face(normal) -> slot index."""
        flat = [tuple(p) for ring in rings for p in ring]
        bot = [self.bm.verts.new((x, y, z0)) for x, y in flat]
        top = [self.bm.verts.new((x, y, z1)) for x, y in flat]
        faces = []
        for i, j, k in tris:
            faces.append(self.bm.faces.new((top[i], top[j], top[k])))
            faces.append(self.bm.faces.new((bot[k], bot[j], bot[i])))
        start = 0
        for ring in rings:
            n = len(ring)
            for a in range(n):
                ia, ib = start + a, start + (a + 1) % n
                faces.append(self.bm.faces.new((bot[ia], bot[ib], top[ib], top[ia])))
            start += n
        for f in faces:
            f.normal_update()
            f.material_index = mat_for_face(f.normal)

    def to_mesh(self, name: str) -> bpy.types.Mesh:
        # parts stay separate closed shells: welding touching parts would create duplicate faces
        mesh = bpy.data.meshes.new(name)
        self.bm.to_mesh(mesh)
        self.bm.free()
        mesh.update()
        return mesh


def normalize_to_box(mesh: bpy.types.Mesh, w: float, d: float, h: float, max_correction: float = 0.03) -> list[float]:
    """Map the mesh bounds exactly onto [-w/2, w/2] x [-d/2, d/2] x [0, h].

    Asset designs are authored to fit the box already; this removes rounding drift and
    guarantees catalog dimensions. A larger correction means a design bug and raises.
    """
    co = [v.co.copy() for v in mesh.vertices]
    lo = [min(c[i] for c in co) for i in range(3)]
    hi = [max(c[i] for c in co) for i in range(3)]
    target_lo, target_hi = (-w / 2, -d / 2, 0.0), (w / 2, d / 2, h)
    factors = []
    for i in range(3):
        f = (target_hi[i] - target_lo[i]) / (hi[i] - lo[i])
        if abs(f - 1.0) > max_correction:
            raise ValueError(f"{mesh.name}: axis {'xyz'[i]} spans {hi[i] - lo[i]:.4f}, expected "
                             f"{target_hi[i] - target_lo[i]:.4f}")
        factors.append(f)
    for v in mesh.vertices:
        v.co = Vector([target_lo[i] + (v.co[i] - lo[i]) * factors[i] for i in range(3)])
    mesh.update()
    return factors


def box_uv(mesh: bpy.types.Mesh, scale_for_material=None, origin=(0.0, 0.0, 0.0)) -> None:
    """Box-projected UVs in metres (divided by the material's tile size when given)."""
    if not mesh.uv_layers:
        mesh.uv_layers.new(name="UVMap")
    uv = mesh.uv_layers.active.data
    verts = mesh.vertices
    for poly in mesh.polygons:
        n = poly.normal
        ax = max(range(3), key=lambda i: abs(n[i]))
        if ax == 2:
            a, b, sign = 0, 1, 1.0 if n[2] >= 0 else -1.0
        elif ax == 0:
            a, b, sign = 1, 2, 1.0 if n[0] >= 0 else -1.0
        else:
            a, b, sign = 0, 2, -1.0 if n[1] >= 0 else 1.0
        s = scale_for_material(poly.material_index) if scale_for_material else 1.0
        for li in poly.loop_indices:
            c = verts[mesh.loops[li].vertex_index].co
            uv[li].uv = (sign * (c[a] - origin[a]) / s, (c[b] - origin[b]) / s)


def set_smooth_by_angle(mesh: bpy.types.Mesh, angle_deg: float = 35.0) -> None:
    for p in mesh.polygons:
        p.use_smooth = True
    if hasattr(mesh, "set_sharp_from_angle"):
        mesh.set_sharp_from_angle(angle=math.radians(angle_deg))


def hex_to_linear(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (out[0], out[1], out[2])


def world_bounds(obj: bpy.types.Object) -> list[list[float]]:
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return [[min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]]


def mesh_world_bounds(obj: bpy.types.Object) -> list[list[float]]:
    """Exact bounds from vertices (bound_box of a rotated object is only conservative)."""
    mw = obj.matrix_world
    pts = [mw @ v.co for v in obj.data.vertices]
    return [[min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]]


def local_bounds(obj: bpy.types.Object) -> list[list[float]]:
    pts = [v.co for v in obj.data.vertices]
    return [[min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]]


def select_only(objs) -> None:
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


FBX_EXPORT_SETTINGS = dict(
    # Documented in docs/BLENDER.md. Metres in Blender -> centimetres in Unreal via FBX unit scale.
    use_selection=True, object_types={"MESH"}, apply_unit_scale=True, apply_scale_options="FBX_SCALE_UNITS",
    global_scale=1.0, axis_forward="-Z", axis_up="Y", bake_space_transform=False, use_mesh_modifiers=True,
    mesh_smooth_type="FACE", use_triangles=True, use_tspace=False, add_leaf_bones=False,
    path_mode="COPY", embed_textures=True, use_custom_props=True, batch_mode="OFF",
)


def export_fbx(path, objs) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    select_only(objs)
    bpy.ops.export_scene.fbx(filepath=str(path), **FBX_EXPORT_SETTINGS)
