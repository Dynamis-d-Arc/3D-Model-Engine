import copy
import json

import pytest

from twin.fixtures import SAMPLE_APARTMENT, SAMPLE_BRIEF, load_sample_apartment
from twin.scene.model import Scene


@pytest.fixture
def scene() -> Scene:
    return load_sample_apartment()


@pytest.fixture
def scene_dict() -> dict:
    return json.loads(SAMPLE_APARTMENT.read_text(encoding="utf-8"))


@pytest.fixture
def brief():
    return SAMPLE_BRIEF.model_copy(deep=True)


def mutate(scene_dict: dict, fn) -> Scene:
    d = copy.deepcopy(scene_dict)
    fn(d)
    return Scene.model_validate(d)
