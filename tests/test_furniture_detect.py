"""Tiled AI furniture detection (twin/ai/furniture.py): tiling, merge rules, scene conversion, and the whole
pass on a synthetic vector PDF with a fake model (no network)."""

from __future__ import annotations

import pytest

from twin.ai.client import AIResult
from twin.ai.furniture import (FurnitureOutcome, detect_furniture, furniture_scene, merge_tiles, plan_tiles,
                               tile_parts, catalog_for_furniture)
from twin.fixtures import load_sample_apartment
from twin.scene.model import Certainty, Scene


def _tile(n, core, items, view=None):
    view = view or (core[0] - 1, core[1] - 1, core[2] + 1, core[3] + 1)
    return {"tile": n, "core": core, "view": view, "rooms": [], "data": {"items": items, "notes": ""}}


def _it(a, x, y, w=0.5, d=0.5, r=0.0, room=None, label="", c=0.9, h=None):
    return {"a": a, "x": x, "y": y, "w": w, "d": d, "h": h, "r": r, "room": room, "l": label, "c": c}


def test_plan_tiles_cover_every_room():
    s = load_sample_apartment()
    tiles = plan_tiles(s, tile_m=5.0, margin_m=1.0)
    assert 2 <= len(tiles) <= 6
    for t in tiles:
        assert t.view == pytest.approx((t.core[0] - 1, t.core[1] - 1, t.core[2] + 1, t.core[3] + 1))
        assert t.rooms and all("polygon" in r for r in t.rooms)
    # every room centre falls in exactly one core
    from twin.scene import geometry as g
    for r in s.rooms:
        c = g.room_polygon(r).representative_point()
        hits = [t for t in tiles if t.core[0] <= c.x <= t.core[2] and t.core[1] <= c.y <= t.core[3]]
        assert len(hits) >= 1


def test_merge_keeps_core_items_and_drops_margin_duplicates_and_nonsense():
    rooms = {"R1", "R2"}
    raw = [
        _tile(1, (0.0, 0.0, 5.0, 5.0), [
            _it("dining_chair", 1.0, 1.0, room="R1"),
            _it("dining_chair", 5.5, 1.0),                      # centred in the margin: tile 2 reports it
            _it("work_table", 2.0, 2.0, w=40.0, d=0.7),         # implausible size
            _it("no_such_asset", 3.0, 3.0),                     # not in the catalog
            _it("hand_sink", 5.0, 4.0, label="Hand Wash RA-01", c=0.95, room="R9"),   # on the tile edge
        ]),
        _tile(2, (5.0, 0.0, 10.0, 5.0), [
            _it("dining_chair", 5.5, 1.0, r=450.0),
            _it("hand_sink", 5.02, 4.01, label="Hand Wash", c=0.7),                  # same sink from the neighbour
        ]),
    ]
    out = merge_tiles(raw, rooms)
    got = sorted((i["asset_id"], i["x"]) for i in out.items)
    assert got == [("dining_chair", 1.0), ("dining_chair", 5.5), ("hand_sink", 5.0)]
    sink = next(i for i in out.items if i["asset_id"] == "hand_sink")
    assert sink["label"] == "Hand Wash RA-01" and sink["room"] is None      # unknown room id dropped
    chair = next(i for i in out.items if i["x"] == 5.5)
    assert chair["rotation"] == pytest.approx(90.0)
    assert any("implausible size" in n for n in out.notes) and any("duplicate" in n for n in out.notes)


def test_furniture_scene_places_items_and_keeps_hand_placed_ones():
    s = load_sample_apartment()
    data = s.model_dump(mode="json")
    for f in data["furniture"]:   # the fixture's furniture counts as AI-placed, except one item placed by hand
        f["certainty"] = Certainty(status="estimated", source="ai").model_dump(mode="json")
    mine = data["furniture"][0]
    mine["certainty"] = Certainty(status="confirmed", source="user").model_dump(mode="json")
    s = Scene.model_validate(data)
    room = s.room(mine["room_id"])
    from twin.scene import geometry as g
    inside = g.room_polygon(room).representative_point()
    outcome = FurnitureOutcome(items=[
        {"asset_id": "wall_cabinet", "x": inside.x, "y": inside.y, "width": 1.6, "depth": 0.35, "height": None,
         "rotation": 90.0, "room": None, "label": "Wall Cabinet", "confidence": 0.9},
        {"asset_id": mine["asset_id"], "x": mine["position"][0], "y": mine["position"][1], "width": 0.5, "depth": 0.5,
         "height": None, "rotation": 0.0, "room": None, "label": "", "confidence": 0.9},          # the user's item
        {"asset_id": "waste_bin", "x": 500.0, "y": 500.0, "width": 0.45, "depth": 0.45, "height": None,
         "rotation": 0.0, "room": None, "label": "", "confidence": 0.9},                          # outside the building
    ])
    new, info = furniture_scene(s, outcome)
    assert info["items"] == 1 and info["kept_user_items"] == 1 and info["unplaced"] == 1
    assert len(new.furniture) == 2
    cab = next(f for f in new.furniture if f.asset_id == "wall_cabinet")
    assert cab.room_id == room.id and cab.elevation == pytest.approx(1.45)   # mounted height from the catalog
    assert cab.size.width == pytest.approx(1.6) and cab.label == "Wall Cabinet"
    assert cab.certainty.status == "extracted" and cab.certainty.source == "ai"
    assert int(cab.id[1:]) > max(int(f["id"][1:]) for f in data["furniture"])


def _vector_plan(tmp_path):
    """A small vector PDF at 1:100 whose plan frame matches the sample apartment (0..10 m x 0..8 m)."""
    import pymupdf
    k = 0.1 / 2.8346456693   # metres per point at 1:100 (1 pt = 0.3528 mm -> 35.28 mm at full scale)
    W = H = 12.0 / k
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    to_pt = lambda x, y: (x / k, H - y / k)  # noqa: E731
    page.draw_rect(pymupdf.Rect(*to_pt(1.0, 7.0), *to_pt(3.0, 6.3)), color=(0, 0, 0), width=1)
    page.insert_text(to_pt(1.2, 6.6), "WORK TABLE", fontsize=6)
    pdf = tmp_path / "plan.pdf"
    doc.save(pdf)
    sheet = {"page": 0, "page_w_pt": W, "page_h_pt": H, "scale": 100, "m_per_pt": k,
             "labels": [{"text": "WORK TABLE", "x_pt": 2.0 / k, "y_pt": H - 6.65 / k, "w_pt": 30, "h_pt": 6, "rotated": False}]}
    return pdf, sheet


class FakeClient:
    """Answers every tile with one work table at the drawn rectangle, if that point is in the tile's core."""

    def __init__(self):
        self.calls = []

    def structured(self, **kw):
        self.calls.append(kw)
        import re
        m = re.search(r"CORE AREA.*?x (-?\d+\.\d+)\.\.(-?\d+\.\d+), y (-?\d+\.\d+)\.\.(-?\d+\.\d+)", kw["parts"][0].text)
        xs, ys = (float(m[1]), float(m[2])), (float(m[3]), float(m[4]))
        items = []
        if xs[0] <= 2.0 <= xs[1] and ys[0] <= 6.65 <= ys[1]:
            items.append(_it("work_table", 2.0, 6.65, w=2.0, d=0.7, r=180.0, label="WORK TABLE", c=0.9))
        return AIResult({"items": items, "notes": ""}, "", "fake-model", f"req{len(self.calls)}",
                        {"input_tokens": 100, "output_tokens": 50})


def test_detect_furniture_end_to_end_with_a_fake_model(tmp_path):
    s = load_sample_apartment()
    pdf, sheet = _vector_plan(tmp_path)
    parts = tile_parts(pdf, sheet, plan_tiles(s, tile_m=5.0)[0], catalog_for_furniture())
    assert parts[-1].kind == "image" and parts[-1].image_url.startswith("data:image/png;base64,")
    assert "work_table" in parts[3].text and "prep table" in parts[3].text      # catalog with drawing words
    fake = FakeClient()
    kept = []
    out = detect_furniture(fake, s, pdf, sheet, tile_m=5.0, raw_sink=kept.append, parallel=2)
    assert out.tiles == len(fake.calls) == len(kept) >= 2
    assert [i["asset_id"] for i in out.items] == ["work_table"]       # reported once, by the tile that owns it
    assert any("WORK TABLE" in c["parts"][2].text for c in fake.calls)  # the exact PDF label reached the model
    new, info = furniture_scene(s, out, keep_user_items=False)
    assert info["items"] == 1 and new.furniture[0].size.width == pytest.approx(2.0)


def test_detect_furniture_resumes_only_the_missing_tiles(tmp_path):
    s = load_sample_apartment()
    pdf, sheet = _vector_plan(tmp_path)
    first = FakeClient()
    saved = []
    out = detect_furniture(first, s, pdf, sheet, tile_m=5.0, raw_sink=saved.append, parallel=1)
    lost = saved.pop(0)                                   # as if this tile had failed with a provider error
    again = FakeClient()
    out2 = detect_furniture(again, s, pdf, sheet, tile_m=5.0, previous=saved, parallel=1)
    assert len(again.calls) == 1 and f"TILE {lost['tile']}." in again.calls[0]["parts"][0].text
    assert out2.missing_tiles == [] and [i["asset_id"] for i in out2.items] == [i["asset_id"] for i in out.items]
    assert any("tile answers reused" in n for n in out2.notes)


def test_tiles_refused_by_parallel_budget_reservations_are_retried_one_by_one(tmp_path):
    from twin.ai.client import AIError
    s = load_sample_apartment()
    pdf, sheet = _vector_plan(tmp_path)

    class TightBudget(FakeClient):
        """Refuses the first attempt of every tile, as the cap does when parallel reservations add up."""
        def __init__(self):
            super().__init__()
            self.refused = set()

        def structured(self, **kw):
            tile = kw["parts"][0].text.split(".")[0]
            if tile not in self.refused:
                self.refused.add(tile)
                raise AIError("AI_BUDGET_EXCEEDED", "Spending cap reached (worst-case reservations)")
            return super().structured(**kw)

    fake = TightBudget()
    out = detect_furniture(fake, s, pdf, sheet, tile_m=5.0, parallel=3)
    assert out.missing_tiles == [] and [i["asset_id"] for i in out.items] == ["work_table"]
    assert not any("AI_BUDGET_EXCEEDED" in n for n in out.notes)
