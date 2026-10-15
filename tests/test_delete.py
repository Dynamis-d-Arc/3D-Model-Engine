"""Deleting uploads and whole projects through the API (no Blender, no AI)."""

import os

import pytest

from tests.test_api import make_client, new_project, png_bytes
from twin.viewer.stacks import FloorSpec, StackSpec, StackStore


def upload(c, pid: str, kind: str = "plan", name: str = "plan.png") -> str:
    r = c.post(f"/api/projects/{pid}/files", data={"kind": kind}, files={"file": (name, png_bytes(), "image/png")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def trash_is_empty(c) -> bool:
    trash = c.app.state.settings.data_dir / "trash"
    return not trash.exists() or not any(trash.iterdir())


def test_delete_uploaded_file(tmp_path):
    c = make_client(tmp_path, blender=False)
    pid, other = new_project(c), new_project(c)
    keep = upload(c, pid)
    gone = upload(c, pid, "reference", "ref.png")
    folder = c.app.state.storage.upload_dir(pid, gone)
    assert folder.is_dir()

    r = c.delete(f"/api/projects/{pid}/files/{gone}")
    assert r.status_code == 200 and r.json() == {"deleted": gone}
    assert [f["id"] for f in c.get(f"/api/projects/{pid}").json()["files"]] == [keep]
    assert not folder.exists() and trash_is_empty(c)
    assert c.get(f"/api/projects/{pid}/files/{gone}/thumb").status_code == 404
    assert c.delete(f"/api/projects/{pid}/files/{gone}").status_code == 404
    # a file id only works inside its own project
    assert c.delete(f"/api/projects/{other}/files/{keep}").status_code == 404
    assert c.get(f"/api/projects/{pid}/files/{keep}/thumb").status_code == 200


def test_delete_project_removes_everything_but_ai_spend(tmp_path):
    c = make_client(tmp_path, blender=False)
    storage = c.app.state.storage
    pid, other = new_project(c), new_project(c)
    upload(c, pid)
    assert c.post(f"/api/projects/{pid}/versions/fixture").status_code == 201
    job = storage.create_job(pid, "build", {}, 1)
    storage.update_job(job["id"], status="completed")
    storage.create_build(pid, 1, job["id"], None)
    storage.record_ai_call(project_id=pid, job_id=None, model="m", api_style="responses", status="ok", cost_usd=0.5)
    folder = storage.project_dir(pid)
    assert folder.is_dir()

    r = c.delete(f"/api/projects/{pid}")
    assert r.status_code == 200 and r.json() == {"deleted": pid}
    assert c.get(f"/api/projects/{pid}").status_code == 404
    assert [p["id"] for p in c.get("/api/projects").json()] == [other]
    assert not folder.exists() and trash_is_empty(c)
    with storage.conn() as con:
        for table in ("files", "versions", "jobs", "builds"):
            assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (pid,)).fetchone()[0] == 0, table
    assert storage.ai_spent_usd() == pytest.approx(0.5)   # the spending cap still counts what was spent
    assert c.delete(f"/api/projects/{pid}").status_code == 404
    assert c.get(f"/api/projects/{other}").status_code == 200


def test_delete_refused_while_work_runs_or_project_is_a_floor(tmp_path):
    c = make_client(tmp_path, blender=False)
    storage = c.app.state.storage
    pid, other = new_project(c), new_project(c)
    fid = upload(c, pid)

    job = storage.create_job(pid, "hvac_import", {})   # queued
    for path in (f"/api/projects/{pid}/files/{fid}", f"/api/projects/{pid}"):
        r = c.delete(path)
        assert r.status_code == 409 and r.json()["error"]["code"] == "PROJECT_BUSY", r.text
        assert "hvac import" in r.json()["error"]["message"]
    storage.update_job(job["id"], status="failed")

    StackStore(c.app.state.settings.data_dir).create(
        StackSpec(name="Tower", floors=[FloorSpec(project_id=pid), FloorSpec(project_id=other)]))
    r = c.delete(f"/api/projects/{pid}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PROJECT_IN_BUILDING"
    assert "'Tower'" in r.json()["error"]["message"]
    assert c.get(f"/api/projects/{pid}").json()["files"][0]["id"] == fid
    # a floor's uploads can still be removed
    assert c.delete(f"/api/projects/{pid}/files/{fid}").status_code == 200


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks renaming its folder only on Windows")
def test_delete_project_with_an_open_file_changes_nothing(tmp_path):
    c = make_client(tmp_path, blender=False)
    storage = c.app.state.storage
    pid = new_project(c)
    fid = upload(c, pid)
    original = storage.upload_dir(pid, fid) / "original.png"
    with original.open("rb"):
        r = c.delete(f"/api/projects/{pid}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "FILES_IN_USE", r.text
        assert c.get(f"/api/projects/{pid}").json()["files"][0]["id"] == fid
        assert original.is_file()
        r = c.delete(f"/api/projects/{pid}/files/{fid}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "FILES_IN_USE"
    assert c.delete(f"/api/projects/{pid}").status_code == 200
    assert not storage.project_dir(pid).exists()
