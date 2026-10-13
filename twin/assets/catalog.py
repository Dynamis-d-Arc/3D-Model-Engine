"""Furniture asset catalog (twin/assets/catalog.json).

The catalog is the contract between the AI (which may only choose these asset ids),
the geometry compiler (nominal sizes, scaling limits) and the Blender asset library
(which builds one mesh per id at exactly the nominal size).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CATALOG_PATH = Path(__file__).with_name("catalog.json")

# Non-uniform scale allowed relative to the nominal asset size before a warning is raised.
SCALE_RANGE = (0.6, 1.6)
# Same, for ``stretch`` assets (modular / box-like designs that tolerate any drawn size).
STRETCH_SCALE_RANGE = (0.25, 4.0)


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    category: str
    size: tuple[float, float, float]
    roles: list[str]
    room_types: list[str]
    placement: Literal["wall", "free", "floor_layer"]
    # words/phrases as printed on drawings (equipment schedules, plan labels) that this asset covers
    aliases: list[str] = Field(default_factory=list)
    # metres above the floor for mounted items (wall cabinets, hoods, countertop appliances)
    default_elevation: float = Field(default=0.0, ge=0.0, le=3.0)
    # modular / box-like design: may be stretched to any drawn size without looking wrong
    stretch: bool = False

    def scale_range(self) -> tuple[float, float]:
        return STRETCH_SCALE_RANGE if self.stretch else SCALE_RANGE


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_version: str
    conventions: str
    assets: list[Asset] = Field(min_length=1)

    def get(self, asset_id: str) -> Asset | None:
        return next((a for a in self.assets if a.id == asset_id), None)

    def ids(self) -> list[str]:
        return [a.id for a in self.assets]


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    return Catalog.model_validate(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
