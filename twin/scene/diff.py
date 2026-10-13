"""Scene version diffs and room-scoped updates.

Regenerating or redesigning one room must never silently change anything else. Every
room-scoped edit goes through ``apply_room_update``, which re-checks the result with
``assert_only_rooms_changed`` and raises ``ScopeViolation`` otherwise.
"""

from __future__ import annotations

from typing import Any

from twin.scene.model import FurnitureItem, Material, RoomFinishes, Scene

COLLECTIONS = ("levels", "rooms", "walls", "openings", "furniture", "materials", "open_questions")
SINGLETONS = ("project", "lighting")


class ScopeViolation(ValueError):
    pass


def diff_scenes(a: Scene, b: Scene) -> dict[str, Any]:
    """Per-collection added / removed / changed ids, plus changed singleton sections."""
    out: dict[str, Any] = {}
    da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
    for coll in COLLECTIONS:
        ia = {e["id"]: e for e in da[coll]}
        ib = {e["id"]: e for e in db[coll]}
        changed = {}
        for k in sorted(ia.keys() & ib.keys()):
            if ia[k] != ib[k]:
                changed[k] = sorted(f for f in set(ia[k]) | set(ib[k]) if ia[k].get(f) != ib[k].get(f))
        entry = {"added": sorted(ib.keys() - ia.keys()), "removed": sorted(ia.keys() - ib.keys()), "changed": changed}
        if entry["added"] or entry["removed"] or entry["changed"]:
            out[coll] = entry
    for key in SINGLETONS:
        if da[key] != db[key]:
            out[key] = sorted(f for f in set(da[key]) | set(db[key]) if da[key].get(f) != db[key].get(f))
    return out


def rooms_touched(a: Scene, b: Scene) -> set[str]:
    """Rooms whose own data or furniture differ between a and b."""
    touched: set[str] = set()
    fa = {f.id: f for f in a.furniture}
    fb = {f.id: f for f in b.furniture}
    for fid in fa.keys() | fb.keys():
        x, y = fa.get(fid), fb.get(fid)
        if x != y:
            touched.update(r.room_id for r in (x, y) if r is not None)
    ra = {r.id: r for r in a.rooms}
    rb = {r.id: r for r in b.rooms}
    for rid in ra.keys() | rb.keys():
        if ra.get(rid) != rb.get(rid):
            touched.add(rid)
    return touched


def assert_only_rooms_changed(a: Scene, b: Scene, allowed_rooms: set[str]) -> None:
    d = diff_scenes(a, b)
    problems: list[str] = []
    for coll in ("levels", "walls", "openings", "open_questions"):
        if coll in d:
            problems.append(f"{coll} changed: {d[coll]}")
    for key in SINGLETONS:
        if key in d:
            problems.append(f"{key} changed: {d[key]}")
    if "rooms" in d:
        r = d["rooms"]
        if r["added"] or r["removed"]:
            problems.append(f"rooms added/removed: {r}")
        for rid, fields in r["changed"].items():
            if rid not in allowed_rooms:
                problems.append(f"room {rid} changed ({fields})")
            elif set(fields) - {"finishes"}:
                problems.append(f"room {rid} geometry/identity changed ({fields})")
    outside = rooms_touched(a, b) - allowed_rooms
    if outside:
        problems.append(f"furniture or rooms outside the allowed scope changed: {sorted(outside)}")
    if "materials" in d:
        m = d["materials"]
        if m["removed"] or m["changed"]:
            # changing an existing material would restyle every room that uses it
            problems.append(f"existing materials changed/removed: {m}")
    if problems:
        raise ScopeViolation("; ".join(problems))


def apply_room_update(base: Scene, room_id: str, furniture: list[FurnitureItem],
                      finishes: RoomFinishes | None = None, new_materials: list[Material] | None = None,
                      note: str = "") -> Scene:
    """Replace one room's furniture (and optionally finishes); everything else is kept verbatim."""
    room = base.room(room_id)
    for f in furniture:
        if f.room_id != room_id:
            raise ScopeViolation(f"furniture {f.id} targets room {f.room_id}, not {room_id}")
    keep = [f for f in base.furniture if f.room_id != room_id]
    taken = {f.id for f in keep}
    renamed: list[FurnitureItem] = []
    counter = max([int(f.id[1:]) for f in base.furniture if f.id[1:].isdigit()] + [0])
    for f in furniture:
        fid = f.id
        if fid in taken or not fid:
            counter += 1
            while f"F{counter}" in taken:
                counter += 1
            fid = f"F{counter}"
        taken.add(fid)
        renamed.append(f.model_copy(update={"id": fid}))
    existing_mats = {m.id for m in base.materials}
    added = [m for m in (new_materials or []) if m.id not in existing_mats]
    rooms = [r.model_copy(update={"finishes": finishes}) if (r.id == room_id and finishes is not None) else r
             for r in base.rooms]
    out = base.model_copy(update={
        "furniture": keep + renamed, "rooms": rooms, "materials": base.materials + added,
        "provenance": base.provenance.model_copy(update={"note": note or f"room {room.name} updated"}),
    })
    out = Scene.model_validate(out.model_dump())
    # provenance is bookkeeping; compare everything else
    assert_only_rooms_changed(base, out.model_copy(update={"provenance": base.provenance}), {room_id})
    return out
