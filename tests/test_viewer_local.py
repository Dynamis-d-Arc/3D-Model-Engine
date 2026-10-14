"""'Open on this PC' (twin.viewer.local): the backend starts the desktop viewer next to the browser.

AUTOMATED: the viewer process is replaced by a recorder; the real packaged viewer is exercised by
tests/test_unreal_viewer.py through the same command-line arguments.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_viewer import completed_build
from twin.api.app import create_app
from twin.config import load_settings
from twin.viewer import local as local_mod
from twin.viewer.tokens import load_secret, verify


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return type("P", (), {"pid": 4242})()


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = tmp_path / "viewer" / "Windows" / "TwinWalkthrough.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    rec = Recorder()
    monkeypatch.setattr(local_mod, "_spawn", rec)

    def make(**extra):
        settings = load_settings(env={"TWIN_DATA_DIR": str(tmp_path / "data"), "TWIN_BLENDER_PATH": "",
                                      "TWIN_DESKTOP_VIEWER_EXE": str(exe), **extra}, env_file=None)
        app = create_app(settings)
        here = TestClient(app, client=("127.0.0.1", 50123))
        remote = TestClient(app)  # client host "testclient": not this PC
        return here, remote, app.state.storage, settings
    return make, rec, exe


def _arg(args, name):
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"-{name}="))


def test_open_on_this_pc_starts_the_viewer_with_a_token_file(env, scene):
    make, rec, exe = env
    here, remote, storage, settings = make()
    pid, bid = completed_build(storage, scene)
    assert here.get("/api/health").json()["desktop_viewer"]["available"] is True
    r = here.post(f"/api/projects/{pid}/viewer/open-local", json={}).json()
    assert r["started"] is True and r["pid"] == 4242
    args, kwargs = rec.calls[-1]
    assert args[0] == str(exe) and _arg(args, "TwinProject") == pid and _arg(args, "TwinBuild") == bid
    assert _arg(args, "TwinServer").startswith("http://127.0.0.1:") and kwargs["cwd"] == str(exe.parent)
    # the token travels in a file, never on the command line; it is read-only and scoped to this project
    assert not any(a.startswith("-TwinToken=") for a in args)
    token = Path(_arg(args, "TwinTokenFile")).read_text(encoding="ascii")
    grant = verify(load_secret(None, settings.data_dir), token)
    assert grant.allows(pid) and not grant.allows("p_other")
    # a double click opens one viewer
    assert here.post(f"/api/projects/{pid}/viewer/open-local", json={}).json()["started"] is False and len(rec.calls) == 1


def test_open_on_this_pc_is_local_only(env, scene):
    make, rec, _ = env
    here, remote, storage, _ = make()
    pid, _ = completed_build(storage, scene)
    assert remote.post(f"/api/projects/{pid}/viewer/open-local", json={}).json()["error"]["code"] == "LOCAL_ONLY"
    proxied = here.post(f"/api/projects/{pid}/viewer/open-local", json={}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert proxied.status_code == 403
    deployed, _, storage2, _ = make(TWIN_PUBLIC_URL="https://twin.example.org")
    pid2, _ = completed_build(storage2, scene)
    assert deployed.get("/api/health").json()["desktop_viewer"]["available"] is False
    assert deployed.post(f"/api/projects/{pid2}/viewer/open-local", json={}).status_code == 503
    assert rec.calls == []


def test_open_on_this_pc_without_a_packaged_viewer(env, scene, tmp_path):
    make, rec, exe = env
    exe.unlink()
    here, _, storage, _ = make()
    pid, _ = completed_build(storage, scene)
    st = here.get("/api/health").json()["desktop_viewer"]
    assert st["available"] is False and "does not exist" in st["problem"]
    assert here.post(f"/api/projects/{pid}/viewer/open-local", json={}).json()["error"]["code"] == "DESKTOP_VIEWER_NOT_AVAILABLE"


def test_open_a_multi_floor_building_on_this_pc(env, scene):
    make, rec, _ = env
    here, _, storage, settings = make()
    p1, _ = completed_build(storage, scene)
    p2, _ = completed_build(storage, scene)
    st = here.post("/api/stacks", json={"name": "Tower", "floors": [{"project_id": p1}, {"project_id": p2}]}).json()
    r = here.post(f"/api/stacks/{st['id']}/viewer/open-local", json={}).json()
    assert r["started"] is True
    args, _ = rec.calls[-1]
    assert _arg(args, "TwinProject") == st["id"] and _arg(args, "TwinBuild") == st["plan"]["composite_id"]
    grant = verify(load_secret(None, settings.data_dir), Path(_arg(args, "TwinTokenFile")).read_text(encoding="ascii"))
    assert grant.allows(st["id"]) and grant.allows(p1) and grant.allows(p2)
