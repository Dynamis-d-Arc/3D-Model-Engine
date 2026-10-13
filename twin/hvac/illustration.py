"""Airflow illustration model: one zone per AC unit, not CFD.

The live view draws, per unit, a raymarched temperature cloud with a cooling jet, closed Catmull-Rom tracer
loops and faint path lines (frontend/js/airflow/illustration.js, school-flow.js). It only needs three
things per unit: where it is, whether
it blows, and how warm the air is. Walls and furniture do not block anything; the clouds are only clipped to
the floor outline.

    airflow_zones(scene) -> {"outline", "floors", "zones", ...}   (viewer frame: X = x, Z = -y, Y up, metres)

* zone box: +/-3 m around the unit, clamped to the floor outline's bounding box (0.15 m inset);
* outlet: the casing's discharge face, 8 cm clear of it, leaving along the casing axis that faces the
  unit's supply duct (ducted units) or its own room (exposed / wall units); a unit with neither leaves from
  the casing's underside and the renderer picks the direction (away from the nearest box edge);
* floors: the west/east split line (middle of the floor outline), unit count and total drawing airflow,
  which the thermal drivers need for the cooling drop.

    validate_thermal_results(data)   -- a MATLAB thermal results file (setpoint scenarios x floors x minutes)
"""

from __future__ import annotations

import math

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from twin.scene import geometry as g
from twin.scene.compile import hvac_placement
from twin.scene.model import Scene

ZONE_HALF = 3.0          # m, zone box half-size around a unit
OUTLINE_MAX_VERTICES = 64  # the cloud shader's polygon array
CLEAR = 0.08             # m, outlet clear of the louvre face


def floor_outline(scene: Scene, level_id: str) -> list[tuple[float, float]]:
    """Exterior ring of the level (rooms + walls), simplified to at most 64 vertices (plan frame)."""
    rooms = [r for r in scene.rooms if r.level_id == level_id]
    walls = g.walls_on_level(scene, level_id)
    shape = unary_union([g.room_polygon(r) for r in rooms] + list(g.wall_footprints(walls).values() if walls else []))
    if isinstance(shape, MultiPolygon):
        shape = max(shape.geoms, key=lambda p: p.area)
    poly = Polygon(shape.exterior)
    tol = 0.05
    simple = poly
    while len(simple.exterior.coords) - 1 > OUTLINE_MAX_VERTICES and tol < 5:
        simple = poly.simplify(tol, preserve_topology=True)
        tol *= 1.6
    return [(round(x, 3), round(y, 3)) for x, y in list(simple.exterior.coords)[:-1]]


def _unit_outlet(scene: Scene, u, place: dict, ceiling: float) -> dict:
    """Discharge face of the casing (plan frame) and the direction the air leaves, or the underside."""
    a = math.radians(u.rotation_deg)
    axes = [((math.cos(a), math.sin(a)), u.size.width / 2), ((-math.cos(a), -math.sin(a)), u.size.width / 2),
            ((-math.sin(a), math.cos(a)), u.size.depth / 2), ((math.sin(a), -math.cos(a)), u.size.depth / 2)]
    cx, cy = u.position
    target = None
    if u.kind == "fcu_ducted":
        # the supply plenum collar faces the supply duct: its nearest end, else the first supply diffuser
        ends = [p for d in scene.hvac.ducts if d.unit_id == u.id and d.system == "supply" for p in (d.path[0], d.path[-1])]
        if ends:
            target = min(ends, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
        else:
            sup = [t for t in scene.hvac.terminals if t.unit_id == u.id and t.kind.startswith("supply")]
            if sup:
                target = min((t.position for t in sup), key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
        if target is not None and math.hypot(target[0] - cx, target[1] - cy) < 0.05:
            target = None
    else:
        # exposed / wall units blow out of a long side into their own room (away from the wall)
        home = g.room_polygon(scene.room(u.room_id)) if u.room_id else None
        best = None
        for (dx, dy), half in axes[2:]:
            pts = [Point(cx + dx * s, cy + dy * s) for s in (0.6, 1.2, 1.8)]
            score = sum(1 for p in pts if home is not None and home.contains(p))
            if best is None or score > best[0]:
                best = (score, (cx + dx, cy + dy))
        target = best[1] if best else None
    z_mid = (place["z0"] + place["z1"]) / 2 if place else ceiling - 0.2
    if target is None:   # no discharge face known: leave from the casing's underside
        z = (place["z0"] if place else ceiling) - 0.05
        return {"o": [round(cx, 3), round(z, 3), round(-cy, 3)], "dir": None}
    vx, vy = target[0] - cx, target[1] - cy
    (dx, dy), half = max(axes, key=lambda ax: ax[0][0] * vx + ax[0][1] * vy)
    ox, oy = cx + dx * (half + CLEAR), cy + dy * (half + CLEAR)
    return {"o": [round(ox, 3), round(z_mid, 3), round(-oy, 3)], "dir": [round(dx, 4), 0.0, round(-dy, 4)]}


def airflow_zones(scene: Scene) -> dict:
    level = scene.levels[0]
    outline = floor_outline(scene, level.id)
    xs, ys = [p[0] for p in outline], [p[1] for p in outline]
    ceiling = level.wall_height
    place = hvac_placement(scene) if scene.hvac.units else {}
    # viewer frame: X = x, Z = -y
    min_x, max_x, min_z, max_z = min(xs), max(xs), -max(ys), -min(ys)
    poly = [[x, round(-y, 3)] for x, y in outline]
    zones = []
    units = [u for u in scene.hvac.units if u.level_id == level.id]
    for u in units:
        x, z = u.position[0], -u.position[1]
        b = [max(min_x + .15, x - ZONE_HALF), max(min_z + .15, z - ZONE_HALF),
             min(max_x - .15, x + ZONE_HALF), min(max_z - .15, z + ZONE_HALF)]
        out = _unit_outlet(scene, u, place.get(u.id), ceiling)
        zones.append({"key": u.id, "tag": u.tag, "unit_id": u.id, "floor": 1, "unitCount": 1,
                      "units": [[round(x, 3), 2.8, round(z, 3)]], "b": [round(v, 3) for v in b], "poly": poly,
                      "outlet": {**out, "ceiling": ceiling}, "airflowLs": u.airflow_lps or 240,
                      "room_id": u.room_id})
    split_x = (min_x + max_x) / 2
    return {
        "level_id": level.id, "ceiling": ceiling, "outline": poly,
        "bounds": [round(min_x, 3), round(min_z, 3), round(max_x, 3), round(max_z, 3)],
        "floors": [{"floor": 1, "splitX": round(split_x, 3), "unitCount": len(units),
                    "westUnits": sum(1 for u in units if u.position[0] < split_x),
                    "airflowLs": round(sum(u.airflow_lps or 240 for u in units), 1)}],
        "zones": zones,
        "rooms": [{"id": r.id, "name": r.name,
                   "bounds": [round(min(p[0] for p in r.polygon), 3), round(-max(p[1] for p in r.polygon), 3),
                              round(max(p[0] for p in r.polygon), 3), round(-min(p[1] for p in r.polygon), 3)]}
                  for r in scene.rooms if r.level_id == level.id],
    }


class ThermalResultsError(ValueError):
    pass


def validate_thermal_results(data: dict) -> dict:
    """A MATLAB thermal results file: engine MATLAB, setpoint scenarios, per floor a
    481-minute series of [west C, east C, AC kW, AC kWh so far, other kW]. Returns a short summary."""
    try:
        if (data.get("engine") or {}).get("name") != "MATLAB":
            raise ThermalResultsError("not a MATLAB results file (engine.name must be 'MATLAB')")
        scenarios = data["scenarios"]
        if not isinstance(scenarios, list) or not scenarios:
            raise ThermalResultsError("no scenarios")
        floors = set()
        for s in scenarios:
            float(s["setpoint"])
            for f in s["floors"]:
                series = f["series"]
                if len(series) < 2 or any(len(row) < 3 for row in series):
                    raise ThermalResultsError(f"floor {f.get('floor')}: series rows need [west, east, kW, ...]")
                floors.add(int(f["floor"]))
        for k in ("coolingKW", "inputKW"):
            float(data["options"][k])
        bfloors = {int(f["floor"]): f for f in data["building"]["floors"]}
        if not floors <= set(bfloors):
            raise ThermalResultsError("building.floors does not describe every floor of the scenarios")
    except ThermalResultsError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ThermalResultsError(f"malformed results file: {exc!r}") from exc
    return {"engine": data["engine"], "setpoints": [s["setpoint"] for s in scenarios], "floors": sorted(floors),
            "minutes": len(scenarios[0]["floors"][0]["series"]) - 1,
            "tags": {f: bfloors[f].get("tags", []) for f in sorted(floors)}}
