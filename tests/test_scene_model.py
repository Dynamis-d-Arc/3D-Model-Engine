"""Structural validation of the versioned scene model (automated, no external apps)."""

import pytest
from pydantic import ValidationError

from tests.conftest import mutate
from twin.scene.model import SCHEMA_VERSION, Scene, scene_json_schema


def test_fixture_loads(scene):
    assert scene.schema_version == SCHEMA_VERSION
    assert scene.units == "m"
    assert len(scene.rooms) == 5 and len(scene.walls) == 7 and len(scene.openings) == 11
    assert len(scene.furniture) == 28


def test_json_schema_is_published():
    schema = scene_json_schema()
    assert schema["title"] == "Scene"
    assert "rooms" in schema["properties"]


@pytest.mark.parametrize("mutation, message", [
    (lambda d: d["rooms"].append(dict(d["rooms"][0])), "duplicate room id"),
    (lambda d: d["openings"][0].update(wall_id="W99"), "unknown wall"),
    (lambda d: d["furniture"][0].update(room_id="R99"), "unknown room"),
    (lambda d: d["walls"][0].update(thickness=-0.1), "greater than or equal"),
    (lambda d: d["walls"][0].update(end=[0.0, 0.01]), "closer than 5 cm"),
    (lambda d: d["openings"][0].update(sill_height=0.5), "doors must start at floor level"),
    (lambda d: d["rooms"][0].update(polygon=[[0, 0], [1, 1]]), "at least 3"),
    (lambda d: d.update(units="ft"), "Input should be 'm'"),
    (lambda d: d["rooms"][0].update(colour="red"), "Extra inputs are not permitted"),
    (lambda d: d["levels"][0].update(wall_height=40), "less than or equal"),
    (lambda d: d["walls"][0].update(start=[float("nan"), 0]), "finite number"),
])
def test_structural_errors_rejected(scene_dict, mutation, message):
    with pytest.raises(ValidationError) as exc:
        mutate(scene_dict, mutation)
    assert message in str(exc.value)


def test_round_trip_is_lossless(scene):
    again = Scene.model_validate(scene.model_dump(mode="json"))
    assert again == scene
