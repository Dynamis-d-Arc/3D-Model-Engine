"""Local Pixel Streaming sessions (twin/stream): off by default, loopback-only signalling, one build-scoped token
per session that never leaves the server, limits and clean ends. The viewer process is a stand-in here; the real
packaged viewer streaming into a browser is the manual check described in docs/PIXEL_STREAMING.md."""

import json
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_api import make_client, new_project
from tests.test_sensors import fabricate_full_build
from twin.stream import local
from twin.viewer.tokens import load_secret, verify

BIND = Path(local.__file__).with_name("bind_loopback.cjs")


def test_streaming_is_off_by_default(tmp_path):
    c = make_client(tmp_path, blender=False)
    st = c.get("/api/stream/status").json()
    assert st["enabled"] is False and st["available"] is False
    assert c.get("/api/health").json()["streaming"]["enabled"] is False
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions/fixture")
    fabricate_full_build(c, pid, 1)
    r = c.post(f"/api/projects/{pid}/stream/sessions", json={})
    assert r.status_code == 503 and r.json()["error"]["code"] == "STREAMING_NOT_AVAILABLE"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_signalling_preload_binds_loopback_only():
    js = ("const net=require('net'),http=require('http');const out=[];const done=()=>{if(out.length===3){"
          "console.log(JSON.stringify(out));process.exit(0)}};"
          "const a=net.createServer();a.listen(0,()=>{out.push(a.address().address);done()});"
          "const b=http.createServer();b.listen(0,undefined,()=>{out.push(b.address().address);done()});"
          "const d=net.createServer();d.listen({port:0},()=>{out.push(d.address().address);done()});")
    r = subprocess.run(["node", "--require", str(BIND), "-e", js], capture_output=True, text=True, timeout=30)
    assert json.loads(r.stdout.strip()) == ["127.0.0.1"] * 3, r.stderr


class FakeProc:
    """Stands in for the viewer process: running until it 'exits' with a code."""
    n = 0

    def __init__(self, args, **_kw):
        FakeProc.n += 1
        self.args, self.pid, self.code = args, 40000 + FakeProc.n, None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


@pytest.fixture
def streaming_client(tmp_path, monkeypatch):
    exe = tmp_path / "TwinWalkthrough.exe"
    exe.write_bytes(b"MZ")
    sig = tmp_path / "SignallingWebServer"
    (sig / "dist").mkdir(parents=True)
    (sig / "dist" / "index.js").write_text("// stand-in", encoding="utf-8")
    c = make_client(tmp_path, blender=False, extra={"TWIN_STREAM_LOCAL": "1", "TWIN_STREAM_VIEWER_EXE": str(exe),
                                                    "TWIN_STREAM_SIGNALLING_DIR": str(sig), "TWIN_STREAM_NODE": "node",
                                                    "TWIN_STREAM_MAX_SESSIONS": "1"})
    streamer = c.app.state.streamer
    started = []
    monkeypatch.setattr(local.subprocess, "Popen", lambda args, **kw: started.append(FakeProc(args)) or started[-1])
    monkeypatch.setattr(streamer, "ensure_signalling", lambda: None)
    monkeypatch.setattr(local, "kill_tree", lambda proc: setattr(proc, "code", 1))
    return c, streamer, started


def test_session_lifecycle_and_token_handling(streaming_client):
    c, streamer, started = streaming_client
    assert c.get("/api/stream/status").json()["available"] is True
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions/fixture")
    bid = fabricate_full_build(c, pid, 1)

    s = c.post(f"/api/projects/{pid}/stream/sessions", json={}).json()
    assert s["state"] == "starting" and s["build_id"] == bid
    assert s["player_url"].startswith("http://127.0.0.1:8090/player.html?StreamerId=" + s["id"])
    assert "tvw1." not in json.dumps(s)                                            # the browser never gets a token
    args = started[0].args
    assert "-TwinStreaming" in args and f"-PixelStreamingID={s['id']}" in args
    assert "-PixelStreamingConnectionURL=ws://127.0.0.1:8888" in args and "-RenderOffscreen" in args
    assert not any("tvw1." in a for a in args)                                     # not on the command line either
    token_file = Path(next(a for a in args if a.startswith("-TwinTokenFile=")).split("=", 1)[1])
    settings = c.app.state.settings
    grant = verify(load_secret(None, settings.data_dir), token_file.read_text(encoding="utf-8"))
    assert grant.projects == (pid,) and grant.build == bid                         # this build only, read-only
    assert grant.expires_at - time.time() <= settings.stream.max_session_s + 300          # lives as long as a session

    # one session at a time (TWIN_STREAM_MAX_SESSIONS=1)
    r = c.post(f"/api/projects/{pid}/stream/sessions", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "STREAM_BUSY"

    # the viewer reports ready in its log, later exits after no input -> ended, token gone
    (token_file.parent / "viewer.log").write_text("... [twin] ready: b_x, 66 actors", encoding="utf-8")
    assert c.get(f"/api/stream/sessions/{s['id']}").json()["state"] == "running"
    started[0].code = 4
    ended = c.get(f"/api/stream/sessions/{s['id']}").json()
    assert ended["state"] == "ended" and ended["reason"] == "ended after no input" and not token_file.exists()

    # a new session can start now; stopping it ends it and removes its token
    s2 = c.post(f"/api/projects/{pid}/stream/sessions", json={"build_id": bid}).json()
    tf2 = Path(next(a for a in started[1].args if a.startswith("-TwinTokenFile=")).split("=", 1)[1])
    assert tf2.is_file()
    stopped = c.delete(f"/api/stream/sessions/{s2['id']}").json()
    assert stopped["state"] == "ended" and stopped["reason"] == "stopped" and not tf2.exists()
    assert c.post(f"/api/stream/sessions/{s2['id']}/end").json()["state"] == "ended"
    assert c.get("/api/stream/sessions/snope").status_code == 404


def test_session_needs_a_completed_build(streaming_client):
    c, _streamer, _started = streaming_client
    pid = new_project(c)
    c.post(f"/api/projects/{pid}/versions/fixture")
    r = c.post(f"/api/projects/{pid}/stream/sessions", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_COMPLETED_BUILD"


def test_problems_are_reported(tmp_path):
    from twin.config import load_settings
    s = load_settings(env={"TWIN_DATA_DIR": str(tmp_path), "TWIN_STREAM_LOCAL": "1",
                           "TWIN_STREAM_VIEWER_EXE": str(tmp_path / "missing.exe")}, env_file=None)
    probs = replace(s.stream, node=None).problems()
    assert any("not packaged" in p for p in probs) and any("signalling server" in p for p in probs)
    assert any("Node.js" in p for p in probs)
