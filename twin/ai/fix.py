"""'Fix with AI': the user describes what is wrong with the model in plain words; the AI answers with a
small list of edit operations against the scene model, which are applied, validated and stored as a new
version for review. Nothing is regenerated until the user looks at the result.

The AI only ever proposes DATA edits (add / update / delete of scene entities). Every edit is checked by
the same pydantic schema and validator as manual edits; an edit set that does not validate is sent back
to the model once with the errors, then rejected.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from twin.ai.client import AIClient, AIError, AIResult, ContentPart
from twin.ai.prompts import catalog_table
from twin.imaging.preprocess import image_data_url
from twin.scene.model import Scene

COLLECTIONS = {
    "levels": ("levels",), "rooms": ("rooms",), "walls": ("walls",), "openings": ("openings",),
    "furniture": ("furniture",), "materials": ("materials",),
    "hvac_units": ("hvac", "units"), "hvac_terminals": ("hvac", "terminals"), "ducts": ("hvac", "ducts"),
    "sensors": ("sensors",),
}
MAX_OPERATIONS = 60

SYSTEM_FIX = """\
You correct a 3D digital-twin scene model when the user tells you what is wrong. The scene is the single
source of truth: metres, +x east, +y north, +z up, room polygons and walls on wall CENTRELINES, angles in
degrees counter-clockwise from +x. HVAC units and ducts sit in the ceiling void unless they have an
elevation; diffusers/grilles sit in the ceiling plane. Furniture rotation 0 = front faces -y. Sensors are
monitoring points: position = plan location, elevation = centre height above the floor (null = 1.5 m, or just
under the ceiling when mounting is "ceiling"), unit_id = the HVAC unit they monitor.

Answer ONLY with edit operations:
- op "update": data_json is a JSON object with ONLY the fields to change (nested objects such as finishes,
  size or certainty are merged one level deep). To move something give its new position / polygon / path.
- op "add": data_json is the complete new entity including a new unique "id" (follow the id style of the
  collection, e.g. R66, W78, O38, F36, SD84, RG58, D200, U_1FC-41, S12). Use existing ids for references.
- op "delete": data_json is "". Deleting a wall deletes its openings; deleting a room deletes its furniture;
  deleting a unit unlinks its terminals and ducts.
Rules: change as little as possible and only what the request needs; keep every other value exactly as it is;
never renumber ids; keep neighbouring rooms sharing identical corner coordinates (move shared corners in
every room and wall that uses them); keep doors on walls (opening offset = distance of its centre from the
wall start along the wall); furniture asset_id must come from the catalog. If the request is ambiguous or
needs information you do not have, make no guess: return no operations and ask in `questions`.
`summary` says in one or two plain sentences what you changed. `reason` (short) explains each operation.
"""

FIX_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "operations", "questions"],
    "properties": {
        "summary": {"type": "string"},
        "operations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "collection", "id", "data_json", "reason"],
            "properties": {
                "op": {"type": "string", "enum": ["add", "update", "delete"]},
                "collection": {"type": "string", "enum": list(COLLECTIONS)},
                "id": {"type": "string"},
                "data_json": {"type": "string"},
                "reason": {"type": "string"},
            }}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class FixOutcome:
    scene: Scene
    summary: str
    changes: list[dict]
    questions: list[str]
    results: list[AIResult] = field(default_factory=list)


def _round(obj, nd: int = 3):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    return obj


def compact_scene(scene: Scene) -> dict:
    """The scene as the model sees it: rounded, without long notes or default noise."""
    d = _round(scene.model_dump(mode="json"))
    for key in ("schema_version", "units", "coordinate_system", "provenance", "open_questions"):
        d.pop(key, None)

    def slim(el: dict) -> dict:
        c = el.get("certainty")
        if isinstance(c, dict):
            el["certainty"] = {"status": c["status"]}
        return el
    for path in COLLECTIONS.values():
        coll = d
        for p in path:
            coll = coll[p]
        for el in coll:
            slim(el)
    d["hvac"]["source"] = {k: v for k, v in d["hvac"]["source"].items() if k in ("file_name", "method")}
    return d


def _coll(data: dict, name: str) -> list:
    node = data
    for p in COLLECTIONS[name]:
        node = node[p]
    return node


def apply_operations(scene: Scene, ops: list[dict], prompt: str) -> tuple[dict, list[dict]]:
    """Apply edit operations to a copy of the scene dict; raises ValueError with readable messages."""
    data = copy.deepcopy(scene.model_dump(mode="json"))
    errors: list[str] = []
    changes: list[dict] = []
    note = f"AI fix: {prompt[:160]}"
    if len(ops) > MAX_OPERATIONS:
        raise ValueError(f"{len(ops)} operations proposed; at most {MAX_OPERATIONS} are allowed per request")
    for i, op in enumerate(ops):
        name, ident, kind = op.get("collection"), str(op.get("id", "")), op.get("op")
        if name not in COLLECTIONS:
            errors.append(f"operation {i}: unknown collection '{name}'")
            continue
        coll = _coll(data, name)
        idx = next((k for k, el in enumerate(coll) if el.get("id") == ident), None)
        payload: dict = {}
        if kind in ("add", "update"):
            try:
                payload = json.loads(op.get("data_json") or "{}")
            except json.JSONDecodeError as exc:
                errors.append(f"operation {i} ({kind} {name} {ident}): data_json is not valid JSON ({exc})")
                continue
            if not isinstance(payload, dict):
                errors.append(f"operation {i}: data_json must be a JSON object")
                continue
        if kind == "delete":
            if idx is None:
                errors.append(f"operation {i}: {name} '{ident}' does not exist")
                continue
            coll.pop(idx)
            _cascade_delete(data, name, ident)
        elif kind == "update":
            if idx is None:
                errors.append(f"operation {i}: {name} '{ident}' does not exist (use op 'add' for new entities)")
                continue
            el = coll[idx]
            for k, v in payload.items():
                if k == "id":
                    continue
                if isinstance(v, dict) and isinstance(el.get(k), dict):
                    el[k] = {**el[k], **v}
                else:
                    el[k] = v
            _mark(el, name, note)
        elif kind == "add":
            payload.setdefault("id", ident)
            if any(el.get("id") == payload["id"] for el in coll):
                errors.append(f"operation {i}: {name} id '{payload['id']}' already exists")
                continue
            if "level_id" not in payload and name not in ("levels", "materials") and data["levels"]:
                payload["level_id"] = data["levels"][0]["id"]
            coll.append(payload)
            _mark(payload, name, note)
        else:
            errors.append(f"operation {i}: unknown op '{kind}'")
            continue
        changes.append({"op": kind, "collection": name, "id": payload.get("id", ident), "reason": op.get("reason", "")[:300]})
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return data, changes


def _mark(el: dict, name: str, note: str) -> None:
    """AI-made values are recorded as accepted estimates: the user asked for them and reviews the version,
    and builds list them in the manifest."""
    if name == "materials":
        return
    c = el.get("certainty") or {}
    status = c.get("status", "confirmed")
    el["certainty"] = {**c, "status": "accepted_estimate" if status != "unresolved" else status,
                       "source": "ai", "note": note[:500]}


def _cascade_delete(data: dict, name: str, ident: str) -> None:
    if name == "walls":
        data["openings"] = [o for o in data["openings"] if o["wall_id"] != ident]
    elif name == "rooms":
        data["furniture"] = [f for f in data["furniture"] if f["room_id"] != ident]
        for o in data["openings"]:
            if o.get("opens_into") == ident:
                o["opens_into"] = None
        for coll in (data["hvac"]["units"], data["hvac"]["terminals"], data["sensors"]):
            for el in coll:
                if el.get("room_id") == ident:
                    el["room_id"] = None
    elif name == "hvac_units":
        for coll in (data["hvac"]["terminals"], data["hvac"]["ducts"], data["sensors"]):
            for el in coll:
                if el.get("unit_id") == ident:
                    el["unit_id"] = None


def _validation_messages(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        out = []
        for e in exc.errors()[:20]:
            loc = ".".join(str(p) for p in e.get("loc", ()))
            out.append(f"{loc or 'scene'}: {e.get('msg')}")
        return out
    return [str(exc)]


def selection_context(scene: Scene, selection: list[str]) -> str:
    """The entities the user clicked, spelled out (ids are 'room:R12', 'hvac_unit:U_1FC-23', ...)."""
    kinds = {"room": "rooms", "wall": "walls", "opening": "openings", "furniture": "furniture",
             "hvac_unit": "hvac_units", "hvac_terminal": "hvac_terminals", "duct": "ducts"}
    data = scene.model_dump(mode="json")
    out = []
    for sel in selection[:12]:
        kind, _, ident = sel.partition(":")
        if kind not in kinds:
            continue
        el = next((e for e in _coll(data, kinds[kind]) if e.get("id") == ident), None)
        if el is not None:
            out.append(f"{kinds[kind]} {ident}: {json.dumps(_round(el), separators=(',', ':'))[:1500]}")
    return "\n".join(out)


def fix_scene(client: AIClient, scene: Scene, prompt: str, selection: list[str] | None = None,
              plan_image: Path | None = None, raw_sink=None) -> FixOutcome:
    parts = [ContentPart("text", f"USER REQUEST: {prompt}")]
    sel = selection_context(scene, selection or [])
    if sel:
        parts.append(ContentPart("text", "THE USER SELECTED THESE ENTITIES (the request most likely refers to them):\n" + sel))
    parts.append(ContentPart("text", "SCENE MODEL (JSON):\n" + json.dumps(compact_scene(scene), separators=(",", ":"))))
    if re.search(r"furnit|sofa|table|chair|bed|desk|asset|cabinet|counter|shelf", prompt, re.I):
        parts.append(ContentPart("text", "FURNITURE CATALOG (only allowed asset_id values):\n" + catalog_table()))
    if plan_image is not None and plan_image.is_file():
        url, size = image_data_url(plan_image, 2048)
        parts.append(ContentPart("text", f"FLOOR PLAN DRAWING the scene was traced from ({size[0]}x{size[1]} px), for reference."))
        parts.append(ContentPart("image", image_url=url, detail="high"))
    results: list[AIResult] = []
    for attempt in range(2):
        res = client.structured(system=SYSTEM_FIX, parts=parts, schema_name="scene_fix", schema=FIX_SCHEMA,
                                max_output_tokens=16000)
        results.append(res)
        if raw_sink is not None:
            raw_sink({"attempt": attempt, "model": res.model, "request_id": res.request_id, "usage": res.usage, "data": res.data})
        ans = res.data
        ops = ans.get("operations") or []
        questions = [str(q)[:400] for q in ans.get("questions") or []][:10]
        if not ops:
            return FixOutcome(scene, str(ans.get("summary", ""))[:600], [], questions or
                              ["The AI made no change. Describe the problem more precisely (which room / unit, and what it should be)."], results)
        try:
            data, changes = apply_operations(scene, ops, prompt)
            fixed = Scene.model_validate(data)
            fixed = fixed.model_copy(update={"provenance": fixed.provenance.model_copy(update={
                "source": "ai", "ai_model": res.model, "ai_request_id": res.request_id,
                "note": f"AI fix: {prompt[:400]}"})})
            return FixOutcome(fixed, str(ans.get("summary", ""))[:600], changes, questions, results)
        except (ValueError, ValidationError) as exc:
            msgs = _validation_messages(exc)
            if attempt == 1:
                raise AIError("AI_FIX_INVALID", "the AI's edits did not produce a valid scene; nothing was changed",
                              detail=msgs) from exc
            parts = parts + [ContentPart("text", "YOUR PREVIOUS OPERATIONS WERE REJECTED:\n" + json.dumps(ops)[:6000] +
                                         "\nERRORS:\n" + "\n".join(msgs) +
                                         "\nReturn a corrected, complete list of operations.")]
    raise AIError("AI_FIX_INVALID", "no valid edit set")  # pragma: no cover
