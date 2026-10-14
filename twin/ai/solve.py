"""AI problem solver: the validation problems the free rules (twin.scene.autofix) could not fix go to a coding /
reasoning model (default gpt-5.3-codex, TWIN_AI_SOLVER_MODEL), which answers with the same small edit operations
as 'Fix with AI' (twin.ai.fix). The result is checked like any edit:

  1. the operations must apply and produce a valid scene (else: sent back once with the errors);
  2. the scene is validated again: problems the model was asked to solve should be gone and no new error may
     appear. New errors are sent back once; if they remain, the AI's edits are dropped (nothing is guessed in).

The model sees the problems, the entities they name, the whole scene (compact) and, when available, the floor
plan drawing, so it can decide what the drawing shows (an open-plan edge or a missing wall, where the door into
a room is). It never edits the user's own figures (brief, confirmed measurements) to make a warning go away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from twin.ai.client import AIClient, AIError, AIResult, ContentPart
from twin.ai.fix import FIX_SCHEMA, _validation_messages, apply_operations, compact_scene, selection_context
from twin.ai.prompts import catalog_table
from twin.imaging.preprocess import image_data_url
from twin.scene.autofix import issue_key
from twin.scene.model import Brief, Scene
from twin.scene.problems import AI_CODES, describe
from twin.scene.validate import Issue, ValidationReport, validate_scene

MAX_PROBLEMS = 40          # per request; the rest stay for the next run
MAX_OUTPUT_TOKENS = 16000

SYSTEM_SOLVE = """\
You fix validation problems in a 3D digital-twin scene model of a building floor. The scene is the single source
of truth: metres, +x east, +y north, room polygons and walls on wall CENTRELINES, angles in degrees CCW from +x.
Openings sit on walls (offset = distance of the opening centre from the wall start). Furniture rotation 0 = front
faces -y; furniture size null = catalog size.

You get a numbered list of PROBLEMS (code, message, entities), the entities involved, the scene JSON and, when
available, the floor plan drawing the scene was traced from. Solve each problem with the smallest edit that a
careful architect would make, following the drawing:
- W_ROOM_EDGE_NO_WALL: if the drawing shows the edge open (a lobby opening onto a corridor, a terrace edge, a
  counter line), set the room's "open_plan": true. If the drawing shows a wall there, add the wall (interior
  0.1 m between rooms, exterior thickness like the other outer walls on the outside).
- W_ROOM_UNREACHABLE / W_NO_ENTRANCE: add a door (kind "door", width 0.9, height 2.1, sill_height 0) on the wall
  between the room and the room it most plausibly opens to (the drawing's door arc if one is drawn, otherwise
  the corridor / circulation side), at least 0.15 m clear of joining walls; or an "opening" if the drawing
  shows the rooms open to each other. Doors open into the smaller room unless the drawing says otherwise.
- Overlaps of rooms / walls: correct the outline or remove the duplicate so neighbours share exact corners.
- Furniture problems: move, rotate, resize within its catalog size range, or swap asset_id for a better catalog
  item. Keep every item inside its room and clear of door swings.
- HVAC outlets not linked: link each to the unit its duct run reaches (or the nearest unit serving that room).
Answer ONLY with edit operations:
- op "update": data_json = a JSON object with ONLY the fields to change (nested objects are merged one level).
- op "add": data_json = the complete new entity with a new unique "id" in the collection's id style (W123, O45...).
- op "delete": data_json = "".
Rules: change nothing the problems do not need; never renumber ids; never change the brief or confirmed user
measurements to silence a warning; keep shared room corners identical in every room and wall that uses them.
If a problem needs information you do not have (the drawing does not show it), leave it and say why in
`questions`. `summary`: one or two plain sentences for a non-technical user. `reason` per operation: short,
plain, and starts with the problem number it solves, e.g. "#3 terrace had no door: added one from the spine".
"""


@dataclass
class SolveOutcome:
    scene: Scene
    summary: str
    changes: list[dict]
    questions: list[str]
    solved: list[dict]               # problems that are gone
    introduced: list[dict]           # new problems the accepted edits caused (warnings only; errors are rejected)
    rejected: str | None = None      # why the AI's edits were dropped, if they were
    results: list[AIResult] = field(default_factory=list)


def ai_problems(report: ValidationReport, codes: set[str] | None = None, targets: set[str] | None = None) -> list[Issue]:
    """Problems worth sending to the solver: errors and warnings it may fix, optionally filtered."""
    out = []
    for i in report.issues:
        if i.severity not in ("error", "warning") or i.code not in AI_CODES:
            continue
        if codes is not None and i.code not in codes:
            continue
        if targets is not None and not set(i.targets) & targets:
            continue
        out.append(i)
    return out[:MAX_PROBLEMS]


def _problem_text(problems: list[Issue]) -> str:
    lines = []
    for n, i in enumerate(problems, 1):
        lines.append(f"#{n} {i.code} ({describe(i.code)['title']}): {i.message} | entities: {', '.join(i.targets) or '-'}")
    return "\n".join(lines)


def _issue_dict(i: Issue) -> dict:
    return {"code": i.code, "severity": i.severity, "message": i.message, "targets": i.targets}


def solve_problems(client: AIClient, scene: Scene, problems: list[Issue], brief: Brief | None = None,
                   plan_image: Path | None = None, raw_sink=None) -> SolveOutcome:
    before = validate_scene(scene, brief)
    before_keys = {issue_key(i) for i in before.issues}
    asked = {issue_key(i) for i in problems}
    targets = sorted({t for i in problems for t in i.targets})
    parts = [ContentPart("text", "PROBLEMS TO SOLVE:\n" + _problem_text(problems))]
    sel = selection_context(scene, targets)
    if sel:
        parts.append(ContentPart("text", "ENTITIES INVOLVED:\n" + sel))
    parts.append(ContentPart("text", "SCENE MODEL (JSON):\n" + json.dumps(compact_scene(scene), separators=(",", ":"))))
    if any(i.code.startswith(("W_FURNITURE", "E_FURNITURE", "W_DOOR_SWING", "E_UNKNOWN_ASSET")) for i in problems):
        parts.append(ContentPart("text", "FURNITURE CATALOG (only allowed asset_id values):\n" + catalog_table()))
    if plan_image is not None and plan_image.is_file():
        url, size = image_data_url(plan_image, 2048)
        parts.append(ContentPart("text", f"FLOOR PLAN DRAWING the scene was traced from ({size[0]}x{size[1]} px)."))
        parts.append(ContentPart("image", image_url=url, detail="high"))
    results: list[AIResult] = []
    rejected: str | None = None
    for attempt in range(2):
        res = client.structured(system=SYSTEM_SOLVE, parts=parts, schema_name="scene_problem_fix", schema=FIX_SCHEMA,
                                max_output_tokens=MAX_OUTPUT_TOKENS)
        results.append(res)
        if raw_sink is not None:
            raw_sink({"attempt": attempt, "model": res.model, "request_id": res.request_id, "usage": res.usage, "data": res.data})
        ans = res.data
        ops = ans.get("operations") or []
        questions = [str(q)[:400] for q in ans.get("questions") or []][:10]
        summary = str(ans.get("summary", ""))[:600]
        if not ops:
            return SolveOutcome(scene, summary or "The AI found nothing it could safely change.", [], questions, [], [],
                                None, results)
        feedback: list[str]
        try:
            data, changes = apply_operations(scene, ops, "AI problem solver")
            fixed = Scene.model_validate(data)
        except (ValueError, ValidationError) as exc:
            feedback = _validation_messages(exc)
            rejected = "the AI's edits did not produce a valid model"
        else:
            after = validate_scene(fixed, brief)
            new_errors = [i for i in after.issues if i.severity == "error" and issue_key(i) not in before_keys]
            if not new_errors:
                after_keys = {issue_key(i) for i in after.issues}
                solved = [_issue_dict(i) for i in before.issues if issue_key(i) in asked and issue_key(i) not in after_keys]
                introduced = [_issue_dict(i) for i in after.issues
                              if i.severity in ("error", "warning") and issue_key(i) not in before_keys]
                fixed = fixed.model_copy(update={"provenance": fixed.provenance.model_copy(update={
                    "source": "ai", "ai_model": res.model, "ai_request_id": res.request_id,
                    "note": "AI problem solver"})})
                return SolveOutcome(fixed, summary, changes, questions, solved, introduced, None, results)
            feedback = [f"{i.code}: {i.message}" for i in new_errors[:20]]
            rejected = f"the AI's edits caused {len(new_errors)} new error(s): " + "; ".join(feedback[:3])
        if attempt == 0:
            parts = parts + [ContentPart("text", "YOUR PREVIOUS OPERATIONS WERE REJECTED:\n" + json.dumps(ops)[:6000] +
                                         "\nBECAUSE:\n" + "\n".join(feedback) +
                                         "\nReturn a corrected, complete list of operations (or none).")]
    return SolveOutcome(scene, "The AI's changes were not used: " + (rejected or "invalid"), [], [], [], [], rejected, results)


__all__ = ["AIError", "SolveOutcome", "ai_problems", "solve_problems", "SYSTEM_SOLVE"]
