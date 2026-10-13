"""Furniture catalog contract: every asset has a Blender design, valid roles, sane mounting heights,
and the validator treats stretchable / stacked (mounted) items sensibly."""

import ast
import json
from pathlib import Path
from typing import get_args

import pytest

from tests.conftest import mutate
from twin.assets.catalog import CATALOG_PATH, SCALE_RANGE, STRETCH_SCALE_RANGE, load_catalog
from twin.blender.runner import SCRIPTS_DIR
from twin.scene.model import FurnitureItem, MaterialRole, Room
from twin.scene.styles import BUILTIN_MATERIALS, STYLES
from twin.scene.validate import validate_scene

CAT = load_catalog()
LIB_SRC = (SCRIPTS_DIR / "build_library.py").read_text(encoding="utf-8")
LIB_AST = ast.parse(LIB_SRC)
FUNCS = {n.name: n for n in LIB_AST.body if isinstance(n, ast.FunctionDef)}


def _designs() -> dict[str, str]:
    """DESIGNS = {asset_id: function_name} from build_library.py (bpy is not importable here)."""
    for node in LIB_AST.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "DESIGNS" for t in node.targets):
            return {k.value: v.id for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("DESIGNS not found in build_library.py")


def _roles_used(fn_name: str, seen: set[str] | None = None) -> set[str]:
    """String keys of R[...] in a design function, following helpers it hands R to."""
    seen = set() if seen is None else seen
    if fn_name in seen or fn_name not in FUNCS:
        return set()
    seen.add(fn_name)
    used = set()
    for node in ast.walk(FUNCS[fn_name]):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "R"
                and isinstance(node.slice, ast.Constant)):
            used.add(node.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and any(isinstance(a, ast.Name) and a.id == "R" for a in node.args)):
            used |= _roles_used(node.func.id, seen)
    return used


def test_catalog_ids_unique_and_one_asset_per_line():
    ids = CAT.ids()
    assert len(ids) == len(set(ids)) >= 55
    lines = [ln for ln in CATALOG_PATH.read_text(encoding="utf-8").splitlines() if '"id":' in ln]
    assert len(lines) == len(ids)
    assert json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["catalog_version"] == CAT.catalog_version


def test_every_asset_has_a_design_using_all_its_roles():
    designs = _designs()
    assert set(designs) == set(CAT.ids())
    for a in CAT.assets:
        fn = designs[a.id]
        assert fn in FUNCS, f"{a.id}: design function {fn} missing"
        missing = set(a.roles) - _roles_used(fn)
        assert not missing, f"{a.id}: roles {sorted(missing)} never used by {fn}()"


def test_roles_are_valid_and_mapped_by_every_style():
    valid = set(get_args(MaterialRole))
    for a in CAT.assets:
        assert a.roles and len(a.roles) == len(set(a.roles)), a.id
        assert set(a.roles) <= valid, (a.id, set(a.roles) - valid)
    for name, style in STYLES.items():
        assert set(style["roles"]) == valid, name
    assert STYLES["modern"]["roles"]["stainless"] == "stainless_steel"
    steel = BUILTIN_MATERIALS["stainless_steel"]
    assert steel.pattern == "brushed" and steel.metallic >= 0.8


def test_room_types_placement_and_sizes():
    valid_rooms = set(get_args(Room.model_fields["type"].annotation))
    for a in CAT.assets:
        assert a.room_types and set(a.room_types) <= valid_rooms, (a.id, set(a.room_types) - valid_rooms)
        assert all(0.01 <= s <= 4.0 for s in a.size), a.id
        assert a.default_elevation + a.size[2] <= 3.0, f"{a.id} reaches above 3 m when mounted"
        assert a.aliases, f"{a.id} has no aliases"
        assert all(al.strip() and al == al.strip() for al in a.aliases), a.id


def test_mounted_assets_have_mounting_heights():
    mounted = {a.id: a.default_elevation for a in CAT.assets if a.default_elevation > 0}
    assert mounted == {"wall_cabinet": 1.45, "exhaust_hood": 2.0, "countertop_appliance": 0.9, "urinal": 0.45}


def test_commercial_kitchen_coverage():
    expected = {"work_table", "kitchen_cabinet", "wall_cabinet", "undercounter_chiller", "upright_fridge", "sink_unit",
                "hand_sink", "cooking_range", "griddle", "deep_fryer", "combi_oven", "charcoal_grill", "dishwasher",
                "exhaust_hood", "shelving_rack", "mobile_cart", "floor_appliance", "countertop_appliance",
                "waste_bin", "service_counter", "display_case", "round_table", "communal_table", "bench", "locker",
                "storage_cabinet", "server_rack", "urinal", "equipment_box"}
    assert expected <= set(CAT.ids())
    assert all("stainless" in CAT.get(i).roles for i in ("work_table", "cooking_range", "exhaust_hood", "sink_unit"))


def test_furniture_item_elevation_defaults_from_catalog():
    hood = FurnitureItem(id="F1", asset_id="exhaust_hood", room_id="R1", position=(1.0, 1.0))
    assert hood.elevation == pytest.approx(2.0)
    floor = FurnitureItem(id="F2", asset_id="exhaust_hood", room_id="R1", position=(1.0, 1.0), elevation=0.0)
    assert floor.elevation == 0.0  # an explicit value always wins (saved scenes)
    sofa = FurnitureItem(id="F3", asset_id="sofa_3seat", room_id="R1", position=(1.0, 1.0))
    assert sofa.elevation == 0.0
    assert FurnitureItem.model_validate(hood.model_dump()).elevation == pytest.approx(2.0)


def _kitchen(d):
    """Replace the sample kitchen's furniture with a small commercial line-up."""
    d["furniture"] = [f for f in d["furniture"] if f["room_id"] != "R2"] + [
        {"id": "K1", "asset_id": "cooking_range", "room_id": "R2", "position": [7.0, 7.4]},
        {"id": "K2", "asset_id": "exhaust_hood", "room_id": "R2", "position": [7.7, 7.3]},  # over the range
        {"id": "K3", "asset_id": "work_table", "room_id": "R2", "position": [8.4, 4.5],
         "size": {"width": 3.0, "depth": 0.7, "height": 0.9}},  # stretched 2x: fine for a stretch asset
        {"id": "K4", "asset_id": "countertop_appliance", "room_id": "R2", "position": [8.0, 4.5]},  # on the table
        {"id": "K5", "asset_id": "hand_sink", "room_id": "R2", "position": [7.2, 5.6],
         "size": {"width": 1.2, "depth": 0.4, "height": 0.9}},  # 2.7x, not a stretch asset
        {"id": "K6", "asset_id": "mobile_cart", "room_id": "R2", "position": [9.4, 6.3]},
        {"id": "K7", "asset_id": "waste_bin", "room_id": "R2", "position": [9.6, 6.0]},  # overlaps the cart
    ]


def test_validation_of_stretch_and_stacked_items(scene_dict):
    s = mutate(scene_dict, _kitchen)
    assert next(f for f in s.furniture if f.id == "K2").elevation == pytest.approx(2.0)  # hood mounting height
    issues = validate_scene(s).issues
    by_code: dict[str, set[str]] = {}
    for i in issues:
        by_code.setdefault(i.code, set()).update(t for t in i.targets if t.startswith("furniture:K"))
    assert by_code.get("W_FURNITURE_SCALE") == {"furniture:K5"}
    assert by_code.get("W_FURNITURE_OVERLAP") == {"furniture:K6", "furniture:K7"}
    assert not by_code.get("W_FURNITURE_TOO_TALL")
    assert not by_code.get("E_FURNITURE_OUTSIDE_ROOM") and not by_code.get("W_FURNITURE_CROSSES_BOUNDARY")


def test_stretch_scale_limits():
    a = CAT.get("work_table")
    assert a.stretch and a.scale_range() == STRETCH_SCALE_RANGE
    assert not CAT.get("hand_sink").stretch and CAT.get("hand_sink").scale_range() == SCALE_RANGE


def test_library_report_matches_catalog_when_present():
    """If a library was built for the current catalog, its report must list every asset at exact size."""
    from twin.pipeline import LIBRARY_DIR, library_key
    report = LIBRARY_DIR / f"furniture_library_{library_key()}.json"
    if not report.is_file():
        pytest.skip("library not built for this catalog yet (built on the first generation)")
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    assert set(data["assets"]) == set(CAT.ids())
    for aid, a in data["assets"].items():
        assert a["dims"] == pytest.approx(list(CAT.get(aid).size), abs=1e-4), aid
