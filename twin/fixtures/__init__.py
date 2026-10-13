"""Repeatable test fixtures with known measurements."""

from __future__ import annotations

import json
from pathlib import Path

from twin.scene.model import Brief, KnownRoom, Scene

FIXTURE_DIR = Path(__file__).parent
SAMPLE_APARTMENT = FIXTURE_DIR / "sample_apartment.json"

# Known measurements of the sample apartment (what a user would type into the brief).
SAMPLE_BRIEF = Brief(
    overall_width=10.2, overall_depth=8.2, wall_height=2.7, style="modern",
    prompt="Bright modern apartment, oak floors, grey sofa, white kitchen.",
    known_rooms=[
        KnownRoom(name="Living / Dining", width=6.25, depth=4.25, basis="clear"),
        KnownRoom(name="Kitchen", width=3.45, depth=4.25, basis="clear"),
        KnownRoom(name="Bedroom", width=3.85, depth=3.45, basis="clear"),
        KnownRoom(name="Bathroom", width=2.30, depth=3.45, basis="clear"),
        KnownRoom(name="Study", width=3.45, depth=3.45, basis="clear"),
    ],
)


def load_sample_apartment() -> Scene:
    return Scene.model_validate(json.loads(SAMPLE_APARTMENT.read_text(encoding="utf-8")))
