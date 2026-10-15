"""Accepting what the AI estimated: the one-click "Accept all" in Review and the autopilot's accept step.

Both work on scene JSON in place and report what they changed, so the caller can list it (the autopilot keeps it as
the run's assumptions; the build manifest records every accepted estimate as well).
"""

from __future__ import annotations

from twin.scene.model import NEEDS_REVIEW_STATUSES

# scene collection -> the target prefix used by validation issues and the editor ("room:R2")
TARGETS = {"levels": "level", "rooms": "room", "walls": "wall", "openings": "opening", "furniture": "furniture",
           "sensors": "sensor"}
HVAC_TARGETS = {"units": "hvac_unit", "terminals": "hvac_terminal", "ducts": "duct"}


def accept_estimates(data: dict) -> list[str]:
    """Mark every extracted / estimated value as a knowingly accepted estimate. Returns the targets changed."""
    groups = [(data.get(c) or [], t) for c, t in TARGETS.items()]
    groups += [((data.get("hvac") or {}).get(c) or [], t) for c, t in HVAC_TARGETS.items()]
    changed = []
    for coll, target in groups:
        for el in coll:
            if (el.get("certainty") or {}).get("status") in NEEDS_REVIEW_STATUSES:
                el["certainty"]["status"] = "accepted_estimate"
                changed.append(f"{target}:{el['id']}")
    return changed


def accept_open_questions(data: dict, answer: str = "accepted as drawn") -> list[dict]:
    """Answer every open, non-blocking AI question "as drawn". Blocking questions are left for the user.
    Returns the questions answered."""
    done = []
    for q in data.get("open_questions") or []:
        if not q["resolved"] and q.get("severity") != "blocking":
            q["resolved"] = True
            q["answer"] = q.get("answer") or answer
            done.append({"id": q["id"], "target": q.get("target", ""), "question": q["question"]})
    return done
