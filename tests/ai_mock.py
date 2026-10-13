"""MOCK data for AI tests: a hand-built answer in the AI schema describing the sample apartment.

This is NOT model output; it is what a correct interpretation of the fixture plan would look
like, used to test parsing, validation and conversion without network access.
"""

from __future__ import annotations

import copy
import json

import httpx

from twin.fixtures import load_sample_apartment
from twin.scene import geometry as g


def sample_interpretation() -> dict:
    s = load_sample_apartment()
    keys = {"R1": "living", "R2": "kitchen", "R3": "bedroom", "R4": "bath", "R5": "study"}
    rooms = [{"key": keys[r.id], "name": r.name, "type": r.type,
              "polygon": [{"x": x, "y": y} for x, y in r.polygon], "dimension_source": "plan_label",
              "confidence": 0.9, "notes": ""} for r in s.rooms]
    walls = [{"key": w.id.lower(), "start": {"x": w.start[0], "y": w.start[1]}, "end": {"x": w.end[0], "y": w.end[1]},
              "thickness_m": w.thickness, "exterior": w.kind == "exterior", "source": "scaled_from_drawing",
              "confidence": 0.8} for w in s.walls]
    openings = []
    for o in s.openings:
        cx, cy = g.wall_frame(s.wall(o.wall_id)).to_world(o.offset)
        openings.append({"key": o.id.lower(), "kind": o.kind, "wall_key": o.wall_id.lower(),
                         "center": {"x": round(cx, 4), "y": round(cy, 4)}, "width_m": o.width, "height_m": o.height,
                         "sill_height_m": o.sill_height, "hinge_side": o.hinge_side,
                         "opens_into_room_key": keys.get(o.opens_into) if o.opens_into else None,
                         "source": "plan_label", "confidence": 0.85})
    furniture = [{"key": f.id.lower(), "asset_id": f.asset_id, "room_key": keys[f.room_id],
                  "center": {"x": f.position[0], "y": f.position[1]}, "rotation_deg": f.rotation_deg,
                  "width_m": None, "depth_m": None, "height_m": None, "source": "ai_suggested", "confidence": 0.7}
                 for f in s.furniture]
    return {
        "plan_readable": True, "unreadable_reason": None, "detected_units": "m",
        "scale_basis": "overall dimension labels 10.2 m x 8.2 m", "wall_height_m": None,
        "wall_height_source": "estimated", "rooms": rooms, "walls": walls, "openings": openings,
        "furniture": furniture,
        "surface_materials": [{"target": "floor", "room_key": None, "description": "light oak", "base_color_hex": "#C49A6C",
                               "roughness": 0.5, "metallic": 0.0, "pattern": "wood"},
                              {"target": "floor", "room_key": "bath", "description": "grey tile",
                               "base_color_hex": "#8E8E8C", "roughness": 0.3, "metallic": 0.0, "pattern": "tile"}],
        "furniture_materials": [{"role": "upholstery", "description": "grey wool", "base_color_hex": "#7C7D80",
                                 "roughness": 0.9, "metallic": 0.0, "pattern": "fabric"}],
        "lighting_mood": "neutral",
        "uncertainties": [{"target": "room:kitchen", "question": "Is the kitchen window 1.4 m wide?", "severity": "info"}],
        "summary": "Two-bedroom-style apartment with living/dining, kitchen, bedroom, bathroom and study.",
    }


def responses_payload(data: dict | str, *, status: str = "completed", refusal: str | None = None,
                      incomplete_reason: str | None = None) -> dict:
    text = data if isinstance(data, str) else json.dumps(data)
    content = [{"type": "refusal", "refusal": refusal}] if refusal else [{"type": "output_text", "text": text}]
    out = {"id": "resp_mock123", "object": "response", "status": status,
           "output": [{"type": "reasoning", "summary": []}, {"type": "message", "role": "assistant", "content": content}],
           "usage": {"input_tokens": 1200, "output_tokens": 3400, "total_tokens": 4600}}
    if incomplete_reason:
        out["incomplete_details"] = {"reason": incomplete_reason}
    return out


def chat_payload(data: dict | str, finish_reason: str = "stop") -> dict:
    text = data if isinstance(data, str) else json.dumps(data)
    return {"id": "chatcmpl_mock", "choices": [{"index": 0, "finish_reason": finish_reason,
                                                "message": {"role": "assistant", "content": text, "refusal": None}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 3000, "total_tokens": 4000}}


class Recorder:
    """httpx MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else httpx.Response(500, json={"error": {"message": "no more mocks"}})
        if isinstance(item, Exception):
            raise item
        return item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def mutated(fn) -> dict:
    d = copy.deepcopy(sample_interpretation())
    fn(d)
    return d
