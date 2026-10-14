"""Sensors: monitoring points layered onto a generated twin.

Sensors never change the building: adding, moving or removing one leaves walls, HVAC and furniture exactly
as they were, so a saved build can be updated without regenerating it (``twin.layers``). This module

    * places sensors in 3D (absolute heights from the room, the ceiling and the mounting),
    * turns them into small marker prisms in the same plan format as architectural objects,
    * says whether two scenes have the same building geometry (``geometry_key``), and
    * imports placement sheets (CSV: one row per sensor).
"""

from __future__ import annotations

import csv
import io
import math
import re
from typing import Any

from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from twin.scene import geometry as g
from twin.scene.compile import _round, canonical_hash, hvac_ceiling, make_prism
from twin.scene.model import Certainty, Material, Scene, Sensor

KIND_LABELS = {"temperature": "Temperature sensor", "humidity": "Humidity sensor",
               "temp_humidity": "Temperature + humidity sensor", "co2": "CO2 sensor",
               "pressure": "Differential pressure sensor", "occupancy": "Occupancy sensor",
               "airflow": "Airflow sensor", "energy": "Energy meter", "other": "Sensor"}
KIND_UNITS = {"temperature": "°C", "humidity": "%RH", "temp_humidity": "°C", "co2": "ppm", "pressure": "Pa",
              "occupancy": "people", "airflow": "L/s", "energy": "kW", "other": ""}
TAG_PREFIX = {"temperature": "TS", "humidity": "HS", "temp_humidity": "THS", "co2": "CO2", "pressure": "DP",
              "occupancy": "OS", "airflow": "AF", "energy": "EM", "other": "S"}
KIND_COLORS = {"temperature": "#E4572E", "humidity": "#2E86DE", "temp_humidity": "#D9822B", "co2": "#2FA866",
               "pressure": "#16A3A3", "occupancy": "#8E5BD9", "airflow": "#3D7DB8", "energy": "#D4A017",
               "other": "#6F7782"}

# marker: an octagonal puck with a coloured cap facing into the room
MARKER_RADIUS = 0.06       # circumradius of the octagon, metres
MARKER_HEIGHT = 0.10
CAP_HEIGHT = 0.02
CEILING_GAP = 0.005        # a ceiling sensor hangs just below the ceiling face
DEFAULT_HEIGHT = 1.5       # wall / free-standing sensors without an elevation
MARKER_TOTAL = MARKER_HEIGHT + CAP_HEIGHT

SENSOR_MATERIALS: dict[str, Material] = {
    "sensor_body": Material(id="sensor_body", name="Sensor housing", base_color="#F2F2EE", roughness=0.4),
    **{f"sensor_{k}": Material(id=f"sensor_{k}", name=f"{KIND_LABELS[k]} cap", base_color=c, roughness=0.35,
                               emission=0.6) for k, c in KIND_COLORS.items()},
}


def sensor_label(s: Sensor, unit_tag: str | None = None) -> str:
    text = s.label or KIND_LABELS[s.kind]
    return f"{text} {s.tag}" + (f" · {unit_tag}" if unit_tag else "")


def resolve_room(scene: Scene, s: Sensor):
    """The sensor's room: the one it names, else the room it stands in (None outside every room)."""
    if s.room_id is not None:
        return scene.room(s.room_id)
    return g.room_at(scene, s.level_id, *s.position)


def sensor_placement(scene: Scene) -> dict[str, dict]:
    """Absolute marker extent of every sensor: {id: {z0, z1, centre, ceiling, room_id}}."""
    out: dict[str, dict] = {}
    for s in scene.sensors:
        level = scene.level(s.level_id)
        room = resolve_room(scene, s)
        floor = level.elevation + (room.floor_offset if room else 0.0)
        ceiling = hvac_ceiling(scene, level, *s.position)
        if s.mounting == "ceiling" and s.elevation is None:
            z1 = ceiling - CEILING_GAP
            z0 = z1 - MARKER_TOTAL
        else:
            centre = floor + (DEFAULT_HEIGHT if s.elevation is None else s.elevation)
            z0, z1 = centre - MARKER_TOTAL / 2, centre + MARKER_TOTAL / 2
        out[s.id] = {"z0": z0, "z1": z1, "centre": (z0 + z1) / 2, "ceiling": ceiling, "floor": floor,
                     "room_id": room.id if room else None}
    return out


def _octagon(cx: float, cy: float, r: float) -> Polygon:
    # rotated by 22.5 degrees so two edges are axis-aligned and the bounds are exact (+-r cos 22.5)
    return Polygon([(cx + r * math.cos(math.radians(22.5 + 45 * k)), cy + r * math.sin(math.radians(22.5 + 45 * k)))
                    for k in range(8)])


def compile_sensors(scene: Scene) -> tuple[list[dict], dict[str, dict]]:
    """Marker objects (named SENSOR_<id>) and the materials they use, in build-plan format."""
    place = sensor_placement(scene)
    units = {u.id: u for u in scene.hvac.units}
    rooms = {r.id: r for r in scene.rooms}
    used: set[str] = set()
    objects: list[dict] = []
    for s in scene.sensors:
        p = place[s.id]
        x, y = s.position
        body, cap = "sensor_body", f"sensor_{s.kind}"
        used.update((body, cap))
        cap_down = s.mounting == "ceiling"  # the coloured face looks into the room
        zb0, zb1 = (p["z0"] + CAP_HEIGHT, p["z1"]) if cap_down else (p["z0"], p["z1"] - CAP_HEIGHT)
        zc0, zc1 = (p["z0"], p["z0"] + CAP_HEIGHT) if cap_down else (p["z1"] - CAP_HEIGHT, p["z1"])
        prisms = [make_prism(_octagon(x, y, MARKER_RADIUS), zb0, zb1, body),
                  make_prism(_octagon(x, y, MARKER_RADIUS * 0.92), zc0, zc1, cap)]
        xs = [q[0] for pr in prisms for q in pr["rings"][0]]
        ys = [q[1] for pr in prisms for q in pr["rings"][0]]
        unit = units.get(s.unit_id) if s.unit_id else None
        room = rooms.get(p["room_id"]) if p["room_id"] else None
        obj = {"name": f"SENSOR_{s.id}", "kind": "sensor", "level_id": s.level_id, "collision": "none",
               "prisms": prisms,
               "expected_bounds": [[_round(min(xs)), _round(min(ys)), _round(p["z0"])],
                                   [_round(max(xs)), _round(max(ys)), _round(p["z1"])]],
               "location": [_round(x), _round(y), _round(p["centre"])],
               "sensor_id": s.id, "tag": s.tag, "sensor_kind": s.kind, "mounting": s.mounting,
               "room_id": room.id if room else None, "room_name": room.name if room else None,
               "unit_id": s.unit_id, "unit_tag": unit.tag if unit else None, "bms_point": s.bms_point or None,
               "height_m": _round(p["centre"] - p["floor"]), "label": sensor_label(s, unit.tag if unit else None),
               "status": s.certainty.status}
        objects.append(obj)
    mats = {m: SENSOR_MATERIALS[m].model_dump(mode="json") for m in sorted(used)}
    for obj in objects:
        deps = {k: mats[k] for k in sorted({pr["mat"] for pr in obj["prisms"]})}
        obj["hash"] = canonical_hash({"obj": {k: v for k, v in obj.items() if k != "hash"}, "mats": deps})
    return objects, mats


def geometry_key(scene: Scene) -> str:
    """Hash of everything that shapes the generated building. Two scenes with the same key produce the
    same Blender output, so one's build can be reused for the other with only the layers rewritten."""
    data = scene.model_dump(mode="json")
    for k in ("sensors", "provenance", "open_questions"):
        data.pop(k, None)
    return canonical_hash(data)


def layer_changes(old: Scene, new: Scene) -> dict[str, Any]:
    """Sensor changes from old to new: ids added, removed, moved (position/height/mounting) and edited."""
    a = {s.id: s.model_dump(mode="json") for s in old.sensors}
    b = {s.id: s.model_dump(mode="json") for s in new.sensors}
    moved, edited = [], []
    for k in sorted(a.keys() & b.keys()):
        if a[k] == b[k]:
            continue
        place = {"position", "elevation", "mounting", "level_id"}
        if any(a[k][f] != b[k][f] for f in place):
            moved.append(k)
        else:
            edited.append(k)
    tag = {**{k: v["tag"] for k, v in a.items()}, **{k: v["tag"] for k, v in b.items()}}
    out = {"added": sorted(b.keys() - a.keys()), "removed": sorted(a.keys() - b.keys()), "moved": moved,
           "edited": edited}
    out["tags"] = {k: tag[k] for ids in out.values() for k in ids}
    out["count"] = len(new.sensors)
    out["changed"] = sum(len(out[k]) for k in ("added", "removed", "moved", "edited"))
    return out


def relink_sensor_units(data: dict, old: Scene) -> None:
    """After the HVAC layer was replaced in scene dict ``data``: point every sensor at the new unit with the
    same tag as its old unit (unit ids may change on a re-import), or unlink it."""
    old_tag = {u.id: u.tag for u in old.hvac.units}
    new_by_tag = {u["tag"]: u["id"] for u in data["hvac"]["units"]}
    new_ids = set(new_by_tag.values())
    for s in data.get("sensors", []):
        if s.get("unit_id") and s["unit_id"] not in new_ids:
            s["unit_id"] = new_by_tag.get(old_tag.get(s["unit_id"], ""))


def next_sensor_id(scene: Scene, taken: set[str] | None = None) -> str:
    taken = taken if taken is not None else {s.id for s in scene.sensors}
    n = max([int(m.group(1)) for i in taken if (m := re.fullmatch(r"S(\d+)", i))] + [0]) + 1
    while f"S{n}" in taken:
        n += 1
    return f"S{n}"


def next_tag(scene: Scene, kind: str) -> str:
    prefix = TAG_PREFIX.get(kind, "S")
    tags = {s.tag for s in scene.sensors}
    nums = [int(m.group(1)) for t in tags if (m := re.fullmatch(rf"{re.escape(prefix)}-(\d+)", t))]
    n = max(nums + [0]) + 1
    while f"{prefix}-{n:02d}" in tags:
        n += 1
    return f"{prefix}-{n:02d}"


# ---- placement sheets ------------------------------------------------------------------------------

KIND_ALIASES = {
    "temperature": "temperature", "temp": "temperature", "t": "temperature", "ts": "temperature",
    "humidity": "humidity", "rh": "humidity", "hum": "humidity", "hs": "humidity",
    "temp_humidity": "temp_humidity", "th": "temp_humidity", "t/rh": "temp_humidity", "temp/rh": "temp_humidity",
    "temperature/humidity": "temp_humidity", "ths": "temp_humidity",
    "co2": "co2", "carbon dioxide": "co2", "iaq": "co2",
    "pressure": "pressure", "dp": "pressure", "differential pressure": "pressure",
    "occupancy": "occupancy", "pir": "occupancy", "people": "occupancy", "people counter": "occupancy",
    "airflow": "airflow", "flow": "airflow", "energy": "energy", "kwh": "energy", "meter": "energy",
    "power meter": "energy", "other": "other",
}
COLUMNS = {
    "tag": ("tag", "sensor", "sensor_tag", "name", "id"), "kind": ("kind", "type", "sensor_type"),
    "x": ("x", "x_m", "east"), "y": ("y", "y_m", "north"), "elevation": ("elevation", "height", "z", "height_m"),
    "mounting": ("mounting", "mount"), "room": ("room", "room_name", "room_id", "space"),
    "unit": ("unit", "hvac_unit", "fcu", "ahu", "unit_tag"), "bms_point": ("bms_point", "bms", "point", "point_name"),
    "label": ("label", "description", "notes"), "level": ("level", "floor", "level_id"),
}
MAX_SHEET_ROWS = 4000


def _num(v: str) -> float | None:
    v = (v or "").strip().replace(",", ".")
    if not v:
        return None
    return float(v)


def import_sheet(scene: Scene, text: str) -> tuple[Scene, dict]:
    """Merge a CSV placement sheet into the scene's sensors.

    One row per sensor. ``tag`` is required; ``x``/``y`` (plan metres) place it exactly, otherwise ``room``
    (name or id) puts it at the room centre as an estimate to move later. A row whose tag already exists
    updates that sensor. Returns the new scene and {"added", "updated", "problems"}; rows with problems are
    skipped, never guessed."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return scene, {"added": [], "updated": [], "problems": ["the sheet is empty"]}
    if len(rows) > MAX_SHEET_ROWS + 1:
        return scene, {"added": [], "updated": [], "problems": [f"the sheet has more than {MAX_SHEET_ROWS} rows"]}
    header = [c.strip().lower().replace(" ", "_") for c in rows[0]]
    col: dict[str, int] = {}
    for key, names in COLUMNS.items():
        for n in names:
            if n in header:
                col[key] = header.index(n)
                break
    if "tag" not in col:
        return scene, {"added": [], "updated": [], "problems": ["no 'tag' column (expected a header row such as "
                                                                "tag,kind,x,y,elevation,room,unit,bms_point)"]}
    rooms_by_name = {r.name.strip().lower(): r for r in scene.rooms} | {r.id.lower(): r for r in scene.rooms}
    units_by_key = {u.tag.strip().lower(): u for u in scene.hvac.units} | {u.id.lower(): u for u in scene.hvac.units}
    levels_by_key = {lv.id.lower(): lv for lv in scene.levels} | {lv.name.strip().lower(): lv for lv in scene.levels}
    sensors = {s.tag: s for s in scene.sensors}
    order = [s.tag for s in scene.sensors]
    taken = {s.id for s in scene.sensors}
    added, updated, problems = [], [], []
    seen: set[str] = set()

    for line_no, r in enumerate(rows[1:], start=2):
        cell = lambda k: (r[col[k]].strip() if k in col and col[k] < len(r) else "")  # noqa: E731
        tag = cell("tag")
        if not tag:
            problems.append(f"row {line_no}: no tag")
            continue
        if tag in seen:
            problems.append(f"row {line_no}: tag {tag} appears twice in the sheet")
            continue
        seen.add(tag)
        old = sensors.get(tag)
        try:
            kind_txt = cell("kind").lower()
            kind = KIND_ALIASES.get(kind_txt) if kind_txt else (old.kind if old else "temperature")
            if kind is None:
                raise ValueError(f"unknown sensor kind '{cell('kind')}'")
            x, y, elev = _num(cell("x")), _num(cell("y")), _num(cell("elevation"))
            if (x is None) != (y is None):
                raise ValueError("give both x and y, or neither")
            lv_txt = cell("level").lower()
            level = levels_by_key.get(lv_txt) if lv_txt else (scene.level(old.level_id) if old else scene.levels[0])
            if level is None:
                raise ValueError(f"unknown level '{cell('level')}'")
            room_txt = cell("room").lower()
            room = rooms_by_name.get(room_txt) if room_txt else None
            if room_txt and room is None:
                raise ValueError(f"unknown room '{cell('room')}'")
            unit_txt = cell("unit").lower()
            unit = units_by_key.get(unit_txt) if unit_txt else None
            if unit_txt and unit is None:
                raise ValueError(f"unknown HVAC unit '{cell('unit')}'")
            mounting = cell("mounting").lower() or (old.mounting if old else None)
            if mounting and mounting not in ("wall", "ceiling", "free"):
                raise ValueError(f"mounting must be wall, ceiling or free, not '{mounting}'")
            certainty = Certainty(status="confirmed", source="user", note="placement sheet")
            if x is not None:
                position = (x, y)
            elif room is not None and (old is None or old.room_id != room.id):
                rp = g.room_polygon(room).representative_point()
                position = (round(rp.x, 3), round(rp.y, 3))
                certainty = Certainty(status="estimated", source="user",
                                      note="placed at the room centre from the sheet; move it to its real spot")
                mounting = mounting or "ceiling"
            elif old is not None:
                position, certainty = old.position, old.certainty
            else:
                raise ValueError("no position: give x and y, or a room")
            if room is None:   # no room column value: the room the position falls in (or the old one)
                if x is not None or old is None:
                    room = g.room_at(scene, level.id, *position)
                elif old.room_id:
                    room = scene.room(old.room_id)
            if room is not None and room.level_id != level.id:
                raise ValueError(f"room '{room.name}' is not on level '{level.name}'")
            # empty cells keep what the sensor already has (or the defaults for a new one)
            data = {**(old.model_dump() if old else {}),
                    "id": old.id if old else next_sensor_id(scene, taken), "tag": tag, "kind": kind,
                    "level_id": level.id, "room_id": room.id if room else None, "position": position,
                    "mounting": mounting or "wall", "certainty": certainty.model_dump()}
            if elev is not None:
                data["elevation"] = elev
            if unit is not None:
                data["unit_id"] = unit.id
            if cell("bms_point"):
                data["bms_point"] = cell("bms_point")[:120]
            if cell("label"):
                data["label"] = cell("label")[:80]
            new = Sensor.model_validate(data)
        except ValidationError as exc:
            e = exc.errors()[0]
            problems.append(f"row {line_no} ({tag}): {'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
            continue
        except ValueError as exc:
            problems.append(f"row {line_no} ({tag}): {exc}")
            continue
        taken.add(new.id)
        sensors[tag] = new
        if old is None:
            order.append(tag)
            added.append(tag)
        elif old != new:
            updated.append(tag)
    out = Scene.model_validate({**scene.model_dump(), "sensors": [sensors[t].model_dump() for t in order]})
    return out, {"added": added, "updated": updated, "problems": problems[:100]}


SHEET_TEMPLATE = (
    "tag,kind,x,y,elevation,mounting,room,unit,bms_point,label\n"
    "TS-01,temperature,4.20,2.10,1.5,wall,,,AHU1.ZONE1.TEMP,\n"
    "CO2-01,co2,,,,ceiling,Living,,,placed at the room centre\n"
)


def check_sensors(scene: Scene, add) -> None:
    """Validation of the sensor layer (called by twin.scene.validate)."""
    if not scene.sensors:
        return
    place = sensor_placement(scene)
    units = {u.id: u for u in scene.hvac.units}
    by_tag: dict[str, list[str]] = {}
    by_point: dict[str, list[str]] = {}
    for s in scene.sensors:
        by_tag.setdefault(s.tag.strip().lower(), []).append(s.id)
        if s.bms_point.strip():
            by_point.setdefault(s.bms_point.strip().lower(), []).append(s.id)
        tgt = f"sensor:{s.id}"
        p = place[s.id]
        if s.room_id is not None:
            room = scene.room(s.room_id)
            if room.level_id != s.level_id:
                add("E_SENSOR_LEVEL", "error", f"Sensor {s.tag} is on another level than its room '{room.name}'.", tgt)
            elif not g.room_polygon(room).buffer(0.05).contains(Point(*s.position)):
                add("E_SENSOR_OUTSIDE_ROOM", "error", f"Sensor {s.tag} is placed outside its room '{room.name}'.",
                    tgt, f"room:{room.id}")
        elif p["room_id"] is None:
            add("W_SENSOR_OUTSIDE_ROOMS", "warning",
                f"Sensor {s.tag} is not inside any room (outdoor sensor, or misplaced?).", tgt)
        if p["z0"] < p["floor"] - 1e-6:
            add("E_SENSOR_BELOW_FLOOR", "error", f"Sensor {s.tag} sits below the floor.", tgt)
        if p["z1"] > p["ceiling"] + 1e-6:
            add("E_SENSOR_ABOVE_CEILING", "error",
                f"Sensor {s.tag} reaches {p['z1'] - p['floor']:.2f} m but the ceiling is at "
                f"{p['ceiling'] - p['floor']:.2f} m.", tgt)
        if s.unit_id is not None and units[s.unit_id].level_id != s.level_id:
            add("W_SENSOR_UNIT_LEVEL", "warning",
                f"Sensor {s.tag} monitors unit {units[s.unit_id].tag} on another level.", tgt)
    for ids in by_tag.values():
        if len(ids) > 1:
            add("E_SENSOR_DUPLICATE_TAG", "error",
                f"{len(ids)} sensors share the tag {next(s.tag for s in scene.sensors if s.id == ids[0])}.",
                *[f"sensor:{i}" for i in ids])
    for point, ids in by_point.items():
        if len(ids) > 1:
            add("W_SENSOR_DUPLICATE_POINT", "warning", f"{len(ids)} sensors read the same BMS point '{point}'.",
                *[f"sensor:{i}" for i in ids])
    pending = [s for s in scene.sensors if s.certainty.status in ("extracted", "estimated")]
    if pending:
        add("R_CONFIRM_SENSORS", "review",
            f"{len(pending)} sensor position(s) are estimates; move them to the real spot, then confirm or accept.",
            *[f"sensor:{s.id}" for s in pending[:50]])
    unresolved = [s for s in scene.sensors if s.certainty.status == "unresolved"]
    if unresolved:
        add("E_UNRESOLVED_MEASUREMENT", "error", f"{len(unresolved)} sensor(s) have no resolved position.",
            *[f"sensor:{s.id}" for s in unresolved[:50]])
    no_point = [s for s in scene.sensors if not s.bms_point.strip()]
    if no_point:
        add("I_SENSOR_NO_POINT", "info",
            f"{len(no_point)} sensor(s) have no BMS point yet; they show simulated readings until one is set.",
            *[f"sensor:{s.id}" for s in no_point[:50]])
