"""Plain-language descriptions of the validation codes, for the Review checklist.

Every issue code the validator can raise has a short title (the group heading in the UI), one sentence on why it
matters, and how it gets fixed:

    rule     a free rule fixes it (twin.scene.autofix)
    ai       needs judgement: the AI problem solver (twin.ai.solve) can propose a fix, or the user edits it
    decide   the user chooses between deterministic options (e.g. open plan vs add the wall)
    accept   the user confirms values the AI estimated (one click accepts them all)
    answer   the AI asked a question
    manual   only the user knows (a measurement, the brief); click the item and edit it
    info     no action needed
"""

from __future__ import annotations

from typing import Literal

FixKind = Literal["rule", "ai", "decide", "accept", "answer", "manual", "info"]

PROBLEMS: dict[str, tuple[str, str, FixKind]] = {
    # furniture
    "W_FURNITURE_SCALE": ("Furniture stretched or squashed",
                          "Items far from their normal size look distorted in 3D; a long run becomes a row of items.", "rule"),
    "W_FURNITURE_CROSSES_BOUNDARY": ("Furniture poking into walls", "Part of the item is inside a wall or outside its room.", "rule"),
    "E_FURNITURE_OUTSIDE_ROOM": ("Furniture outside its room", "The item stands outside the room it belongs to.", "rule"),
    "W_DOOR_SWING_BLOCKED": ("Furniture blocking a door", "The door would hit the item when it opens.", "rule"),
    "W_FURNITURE_OVERLAP": ("Furniture overlapping", "Two items stand in the same place.", "rule"),
    "W_FURNITURE_TOO_TALL": ("Furniture through the ceiling", "The item is taller than the room.", "rule"),
    "E_UNKNOWN_ASSET": ("Unknown furniture type", "The item uses a furniture type that is not in the catalog.", "ai"),
    # doors and windows
    "E_OPENING_AT_JUNCTION": ("Doors or windows cutting into a corner",
                              "An opening overlaps the end of a joining wall, so the wall cannot be built around it.", "rule"),
    "E_OPENING_OVERLAP": ("Doors or windows overlapping", "Two openings on one wall overlap or nearly touch.", "rule"),
    "E_OPENING_OUTSIDE_WALL": ("Doors or windows past the end of a wall", "The opening is longer than the wall it is on.", "rule"),
    "E_OPENING_TOO_TALL": ("Doors or windows taller than the wall", "The opening reaches above the wall top.", "rule"),
    "W_DOOR_OPENS_INTO": ("Doors opening into the wrong room", "The door is set to swing into a room it does not touch.", "ai"),
    # walls and rooms
    "W_WALL_OVERSHOOT": ("Walls sticking out past a corner", "A wall runs a little past the wall it meets.", "rule"),
    "W_WALL_SHORT": ("Very short walls", "A wall shorter than 30 cm is usually a drawing mistake.", "ai"),
    "E_WALL_OVERLAP": ("Walls on top of each other", "Two walls run along the same line.", "ai"),
    "W_ROOM_EDGE_NO_WALL": ("Room edges without a wall",
                            "Part of the room outline has no wall: either it is open plan, or a wall is missing.", "decide"),
    "W_ROOM_UNREACHABLE": ("Rooms with no way in", "No door or opening leads into the room.", "ai"),
    "W_NO_ENTRANCE": ("No entrance", "No door connects the floor to the outside.", "ai"),
    "E_ROOM_OVERLAP": ("Rooms overlapping", "Two room outlines cover the same floor.", "ai"),
    "E_ROOM_POLYGON_INVALID": ("Room outline crosses itself", "The room's outline is twisted or repeats a corner.", "ai"),
    "E_ROOM_TOO_SMALL": ("Rooms too small", "A room smaller than 0.5 m² is usually a drawing mistake.", "ai"),
    # checks against what the user entered
    "W_OVERALL_MISMATCH": ("Size differs from your brief", "The building's outer size differs from the size you entered.", "manual"),
    "W_ROOM_DIM_MISMATCH": ("Room sizes differ from your brief", "A room measures differently from the size you entered.", "manual"),
    "W_KNOWN_ROOM_MISSING": ("Rooms from your brief are missing", "A room you listed is not in the layout.", "manual"),
    # review gate
    "R_CONFIRM_MEASUREMENT": ("Sizes estimated by the AI",
                              "The AI read or estimated these sizes. Check the ones that matter, then accept the rest.", "accept"),
    "R_CONFIRM_HVAC": ("HVAC items read from the drawing", "Check the air-conditioning layer, then accept it.", "accept"),
    "R_CONFIRM_SENSORS": ("Sensors to confirm", "Sensor positions were imported or estimated; check and accept them.", "accept"),
    "Q_OPEN_QUESTION": ("Questions from the AI", "The AI was unsure about these; answer them or accept the drawing as is.", "answer"),
    "E_UNRESOLVED_MEASUREMENT": ("Missing measurements", "There is no usable value; enter it before building.", "manual"),
    # HVAC
    "W_HVAC_TERMINAL_UNLINKED": ("Air outlets not linked to a unit",
                                 "These diffusers or grilles have no unit, so they get no air in the simulation.", "ai"),
    "W_HVAC_OUTSIDE_ROOMS": ("HVAC items outside the building", "These units or outlets are not inside any room.", "ai"),
    "W_HVAC_AIRFLOW_BALANCE": ("Airflow does not add up",
                               "A unit's outlets supply more or less air than the unit; check the design figures.", "manual"),
    # sensors
    "E_SENSOR_OUTSIDE_ROOM": ("Sensors outside a room", "A sensor is placed outside every room.", "manual"),
    "E_SENSOR_LEVEL": ("Sensors on a missing floor", "A sensor refers to a floor that does not exist.", "manual"),
    "E_SENSOR_DUPLICATE_TAG": ("Sensor tags used twice", "Two sensors have the same tag.", "manual"),
    "E_SENSOR_BELOW_FLOOR": ("Sensors below the floor", "A sensor is mounted below floor level.", "manual"),
    "E_SENSOR_ABOVE_CEILING": ("Sensors above the ceiling", "A sensor is mounted above the ceiling.", "manual"),
    "W_SENSOR_UNIT_LEVEL": ("Sensors linked to a unit on another floor", "Check which unit the sensor monitors.", "manual"),
    "W_SENSOR_OUTSIDE_ROOMS": ("Sensors outside the rooms", "Some sensors are not inside any room.", "manual"),
    "W_SENSOR_DUPLICATE_POINT": ("Sensors in the same spot", "Two sensors share one position.", "manual"),
    # notes
    "I_WALL_FREE_END": ("Wall ends not joined to another wall", "Usually fine (a wall stub or a free-standing wall).", "info"),
    "I_WALL_CROSSING": ("Walls crossing", "Four-way wall junctions.", "info"),
    "I_WINDOW_FLOOR_LEVEL": ("Floor-to-ceiling windows", "Windows that start at the floor.", "info"),
    "I_SENSOR_NO_POINT": ("Sensors without a position", "Listed sensors that are not placed yet.", "info"),
}

# what the AI problem solver may be asked to fix (judgement on the drawing; never the user's own figures)
AI_CODES: frozenset[str] = frozenset(c for c, (_, _, kind) in PROBLEMS.items() if kind in ("ai", "decide", "rule"))


def describe(code: str) -> dict:
    title, why, fix = PROBLEMS.get(code, (code.replace("_", " ").title(), "", "manual"))
    return {"title": title, "why": why, "fix": fix}


def catalog() -> dict[str, dict]:
    return {code: describe(code) for code in PROBLEMS}
