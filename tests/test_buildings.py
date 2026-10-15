"""Drawing sets -> one project per floor, stacked; 'Make the whole building' runs the autopilot for every floor.

AI calls are MOCKED; Blender is switched off, so every floor stops with its layout ready (BLENDER_MISSING) and the
building run reports which floors still need something.
"""

import time

import httpx

from tests.ai_mock import Recorder, responses_payload, sample_interpretation
from tests.test_api import make_client, png_bytes, wait_job
from tests.test_autopilot import photo_bytes
from twin.imaging.drawing_set import analyse, building_name, level_from_name, level_from_text


def set_pdf() -> bytes:
    """Cover, two floor plans, an ACMV sheet for level 2 and a section that names every level."""
    import fitz
    doc = fitz.open()
    for lines in (["COVER SHEET", "DRAWING LIST"], ["LEVEL 1 FLOOR PLAN", "NORTH"], ["LEVEL 2 FLOOR PLAN", "NORTH"],
                  ["LEVEL 2 ACMV LAYOUT", "FCU-01 FCU-02 FCU-03", "SUPPLY DIFFUSER  DIFFUSER  DIFFUSER"],
                  ["SECTION A-A", "LEVEL 1", "LEVEL 2", "ROOF"]):
        page = doc.new_page(width=842, height=595)
        for i, text in enumerate(lines):
            page.insert_text((40, 40 + 16 * i), text)
        if "PLAN" in lines[0]:
            for k in range(20):
                page.draw_rect(fitz.Rect(100 + 10 * k, 100, 160 + 10 * k, 300))
    data = doc.tobytes()
    doc.close()
    return data


SET = [("Riverside_drawings.pdf", None), ("L3_plan.png", None), ("living_room.jpg", None),
       ("L2_furniture.csv", b"catalog_id,x,y\nfloor_lamp,1.0,1.0\n")]


def set_files():
    data = {"Riverside_drawings.pdf": set_pdf(), "L3_plan.png": png_bytes(), "living_room.jpg": photo_bytes()}
    return [(n, d if d is not None else data[n]) for n, d in SET]


def drop(c, files, **form):
    return c.post("/api/drawing-sets", data=form, files=[("files", (n, d)) for n, d in files])


# ---- reading floors --------------------------------------------------------------------------------

def test_levels_from_titles_and_names():
    assert level_from_text("LEVEL 02 FLOOR PLAN\nSCALE 1:100") == (2.0, "Level 2")
    assert level_from_text("2ND STOREY PLAN") == (2.0, "2nd storey")
    assert level_from_text("GROUND FLOOR PLAN") == (0.0, "Ground floor")
    assert level_from_text("BASEMENT 1 PLAN") == (-1.0, "Basement 1")
    assert level_from_text("ROOF PLAN") == (900.0, "Roof")
    assert level_from_text("SECTION A-A\nLEVEL 1\nLEVEL 2") is None        # names every level: no floor of its own
    assert level_from_text("FLOOR PLAN\nLEVEL 1 (single floor)") == (1.0, "Level 1")
    assert level_from_name("Riverside_L2_plan.pdf") == (2.0, "Level 2")
    assert level_from_name("GF.pdf") == (0.0, "Ground floor")
    assert level_from_name("floor_plan.pdf") is None and level_from_name("plan_L1_L2.pdf") is None
    assert building_name(["Riverside_L2_floor_plan.pdf"]) == "Riverside"


def test_a_drawing_set_is_split_into_floors():
    ds = analyse(set_files())
    assert ds.is_building
    assert [f["label"] for f in ds.floors] == ["Level 1", "Level 2", "Level 3"]
    f1, f2, f3 = ds.floors
    assert (f1["plan"].name, f1["plan"].page) == ("Riverside_drawings.pdf", 1)
    assert (f2["plan"].page, [h.page for h in f2["hvac"]]) == (2, [3])
    assert [s.name for s in f2["sheets"]] == ["L2_furniture.csv"]
    assert f3["plan"].name == "L3_plan.png"
    kinds = {(s.name, s.page): s.kind for s in ds.sheets}
    assert kinds[("Riverside_drawings.pdf", 0)] is None and kinds[("Riverside_drawings.pdf", 4)] is None   # cover, section
    assert kinds[("living_room.jpg", 0)] == "reference"


def test_the_reference_apartment_stays_one_floor():
    from pathlib import Path
    sample = Path(__file__).resolve().parents[1] / "samples" / "angled_apartment"
    ds = analyse([(n, (sample / n).read_bytes()) for n in ("floor_plan.pdf", "hvac_plan.pdf", "furniture_list.csv")])
    assert not ds.is_building


# ---- dropping a set on the start page --------------------------------------------------------------

def test_dropping_a_building_makes_floor_projects_and_a_stack(tmp_path):
    c = make_client(tmp_path, blender=False)
    r = drop(c, set_files())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["kind"] == "building" and out["unassigned"] == []
    assert [f["label"] for f in out["floors"]] == ["Level 1", "Level 2", "Level 3"]
    st = c.get(f"/api/stacks/{out['stack']['id']}").json()
    assert st["name"] == "Riverside" and [f["label"] for f in st["floors"]] == ["Level 1", "Level 2", "Level 3"]
    projects = {p["id"]: p for p in c.get("/api/projects").json()}
    f2 = c.get(f"/api/projects/{out['floors'][1]['project_id']}").json()
    assert projects[f2["id"]]["name"] == "Riverside · Level 2"
    files = {(f["kind"], f["page"]) for f in f2["files"]}
    assert files == {("plan", 2), ("hvac", 3), ("furniture_list", 0), ("reference", 0)}
    f3 = c.get(f"/api/projects/{out['floors'][2]['project_id']}").json()
    assert sorted(f["kind"] for f in f3["files"]) == ["plan", "reference"]


def test_dropping_one_floor_makes_one_project(tmp_path):
    c = make_client(tmp_path, blender=False)
    out = drop(c, [("Unit 3B plan.png", png_bytes()), ("ideas.jpg", photo_bytes())]).json()
    assert out["kind"] == "single" and out["project"]["name"] == "Unit 3B plan"
    assert sorted(f["kind"] for f in out["files"]) == ["plan", "reference"]
    assert c.get("/api/stacks").json() == []


# ---- making the whole building -------------------------------------------------------------------------

def test_making_the_building_runs_every_floor(tmp_path):
    rec = Recorder(*[httpx.Response(200, json=responses_payload(sample_interpretation())) for _ in range(3)])
    c = make_client(tmp_path, ai=True, recorder=rec, blender=False)
    out = drop(c, set_files()).json()
    sid = out["stack"]["id"]
    job = c.post(f"/api/stacks/{sid}/autopilot", json={"use_ai": False}).json()
    assert job["kind"] == "building" and job["project_id"] == sid
    assert c.post(f"/api/stacks/{sid}/autopilot", json={}).status_code == 409   # one run at a time
    done = wait_job(c, job["id"])
    r = done["result"]
    assert done["status"] == "needs_review" and r["stopped"]["code"] == "FLOORS_NEED_YOU"
    assert [f["status"] for f in r["floors"]] == ["waiting"] * 3 and all(f["code"] == "BLENDER_MISSING" for f in r["floors"])
    assert len(rec.requests) == 3                                                 # one reading per floor
    level2 = c.get(f"/api/projects/{out['floors'][1]['project_id']}/versions/{r['floors'][1]['version']}").json()
    assert [f["asset_id"] for f in level2["scene"]["furniture"]] == ["floor_lamp"]   # its own furniture list
    assert c.get(f"/api/stacks/{sid}/autopilot").json()["id"] == job["id"]


def test_built_floors_are_kept_and_a_floor_without_a_plan_waits(tmp_path, scene_dict):
    c = make_client(tmp_path, blender=False)
    a = c.post("/api/projects", json={"name": "A"}).json()["id"]
    b = c.post("/api/projects", json={"name": "B"}).json()["id"]
    v = c.post(f"/api/projects/{a}/versions", json={"scene": scene_dict}).json()["number"]
    storage = c.app.state.storage
    bid = storage.create_build(a, v, "j_fake", None)["id"]
    storage.update_build(bid, status="completed")
    sid = c.post("/api/stacks", json={"name": "Two", "floors": [{"project_id": a}, {"project_id": b}]}).json()["id"]
    done = wait_job(c, c.post(f"/api/stacks/{sid}/autopilot", json={}).json()["id"])
    f1, f2 = done["result"]["floors"]
    assert (f1["status"], f1["build_id"], f1["note"]) == ("done", bid, "built already")
    assert f2["status"] == "waiting" and f2["code"] == "NOTHING_TO_READ"
    assert done["result"]["built"] == 1


class SlowAI:
    """Every AI answer takes 1.5 s, so a run can be stopped while it reads the plans."""

    def transport(self) -> httpx.MockTransport:
        def answer(request):
            time.sleep(1.5)
            return httpx.Response(200, json=responses_payload(sample_interpretation()))
        return httpx.MockTransport(answer)


def test_stopping_the_building_stops_its_floors(tmp_path):
    c = make_client(tmp_path, ai=True, recorder=SlowAI(), blender=False)
    sid = drop(c, set_files()).json()["stack"]["id"]
    job = c.post(f"/api/stacks/{sid}/autopilot", json={"use_ai": False}).json()
    time.sleep(0.5)
    assert c.post(f"/api/jobs/{job['id']}/cancel").json()["stopping"] is True
    done = wait_job(c, job["id"])
    assert done["status"] == "needs_review" and done["result"]["stopped"]["code"] == "STOPPED"
    for f in done["result"]["floors"]:
        if f["job_id"]:
            child = wait_job(c, f["job_id"])
            assert child["status"] == "needs_review" and child["result"]["stopped"]["code"] == "STOPPED"
