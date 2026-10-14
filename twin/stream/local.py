"""Local Pixel Streaming test: the packaged Unreal viewer shown inside the web app, everything on this PC.

A *session* is one ``TwinWalkthrough.exe`` started off-screen for one browser tab. It loads one build from
this backend (the read-only viewer API) and streams it through Epic's signalling server ("Wilbur",
PixelStreamingInfrastructure) to the web player. Every port listens on 127.0.0.1 only: the signalling server is
started with ``bind_loopback.cjs``, the viewer is told to connect to 127.0.0.1. Nothing is reachable from the
network, so this is a proof on one machine, not a hosted service (see docs/PIXEL_STREAMING.md).

The security model is the one a hosted setup keeps:
* the browser never receives a viewer token: one build-scoped, read-only token is minted per session with a
  lifetime just over the session limit, written to a file only the viewer process reads, deleted at the end;
* the browser only gets the player URL with the session's streamer id;
* sessions end by themselves (the viewer exits after no input / the maximum length / a lasting load error) and
  on request; at most ``max_sessions`` run at once.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from twin.config import Settings, StreamSettings
from twin.viewer.tokens import load_secret, mint

log = logging.getLogger("twin.stream")
BIND_LOOPBACK = Path(__file__).with_name("bind_loopback.cjs")
EXIT_REASONS = {0: "closed", 3: "the building could not be loaded", 4: "ended after no input",
                5: "maximum session length reached", -1: "stopped"}


class StreamError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started (the viewer has nothing to save)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


@dataclass
class Session:
    id: str
    project_id: str
    build_id: str
    version: int | None
    proc: subprocess.Popen
    folder: Path
    started_at: float = field(default_factory=time.time)
    ready_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None

    @property
    def log_file(self) -> Path:
        return self.folder / "viewer.log"

    @property
    def token_file(self) -> Path:
        return self.folder / "token.txt"


class LocalStreamer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg: StreamSettings = settings.stream
        self.root = settings.data_dir / "stream"
        self.secret = load_secret(settings.viewer_secret.get_secret_value() if settings.viewer_secret else None,
                                  settings.data_dir)
        self.sessions: dict[str, Session] = {}
        self.signalling: subprocess.Popen | None = None
        self.lock = threading.Lock()

    # ---- availability -------------------------------------------------------------------------
    def status(self) -> dict:
        cfg = self.cfg
        problems = cfg.problems() if cfg.enabled else ["local streaming is off (set TWIN_STREAM_LOCAL=1)"]
        return {"enabled": cfg.enabled, "available": cfg.enabled and not problems, "problems": problems,
                "mode": "local", "max_sessions": cfg.max_sessions, "active": self._active(),
                "signalling": port_open(cfg.player_port), "idle_exit_s": cfg.idle_exit_s,
                "max_session_s": cfg.max_session_s}

    def _active(self) -> int:
        return sum(1 for s in self.sessions.values() if s.proc.poll() is None)

    # ---- signalling server ------------------------------------------------------------------------
    def ensure_signalling(self) -> None:
        cfg = self.cfg
        if port_open(cfg.player_port) and port_open(cfg.streamer_port):
            return   # already running (ours, or started by hand with bind_loopback.cjs)
        if self.signalling is not None and self.signalling.poll() is None:
            kill_tree(self.signalling)
        self.root.mkdir(parents=True, exist_ok=True)
        out = open(self.root / "signalling.log", "ab")
        sd = cfg.signalling_dir
        cmd = [cfg.node, "--require", str(BIND_LOOPBACK), str(sd / "dist" / "index.js"), "--serve",
               "--http_root", str(sd / "www"), "--player_port", str(cfg.player_port),
               "--streamer_port", str(cfg.streamer_port), "--sfu_port", str(cfg.streamer_port + 1),
               "--log_folder", str(self.root / "signalling_logs"), "--log_config", "false"]
        self.signalling = subprocess.Popen(cmd, cwd=str(sd), stdout=out, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        end = time.time() + 15
        while time.time() < end:
            if port_open(cfg.player_port) and port_open(cfg.streamer_port):
                log.info("signalling server listening on 127.0.0.1:%s (players) / %s (streamers)",
                         cfg.player_port, cfg.streamer_port)
                return
            if self.signalling.poll() is not None:
                break
            time.sleep(0.3)
        raise StreamError(503, "SIGNALLING_FAILED", "The signalling server did not start; see data/stream/signalling.log.")

    # ---- sessions ------------------------------------------------------------------------------
    def start(self, project_id: str, build_id: str, version: int | None) -> dict:
        st = self.status()
        if not st["available"]:
            raise StreamError(503, "STREAMING_NOT_AVAILABLE", "; ".join(st["problems"]))
        with self.lock:
            self.refresh()
            if self._active() >= self.cfg.max_sessions:
                raise StreamError(429, "STREAM_BUSY", f"All {self.cfg.max_sessions} streaming sessions are in use; "
                                                     "end one or try again later.")
            self.ensure_signalling()
            sid = f"s{secrets.token_hex(5)}"
            folder = self.root / "sessions" / sid
            folder.mkdir(parents=True, exist_ok=True)
            # one build only, read-only, expires just after the session limit
            token, _grant = mint(self.secret, projects=[project_id], build=build_id,
                                 ttl_hours=(self.cfg.max_session_s + 300) / 3600.0)
            (folder / "token.txt").write_text(token, encoding="utf-8")
            cfg = self.cfg
            args = [str(cfg.viewer_exe), f"-TwinServer=http://127.0.0.1:{self.settings.port}",
                    f"-TwinProject={project_id}", f"-TwinBuild={build_id}", f"-TwinTokenFile={folder / 'token.txt'}",
                    "-TwinStreaming", f"-TwinIdleExit={cfg.idle_exit_s}", f"-TwinMaxSession={cfg.max_session_s}",
                    f"-PixelStreamingConnectionURL=ws://127.0.0.1:{cfg.streamer_port}", f"-PixelStreamingID={sid}",
                    "-RenderOffscreen", f"-ResX={cfg.res_x}", f"-ResY={cfg.res_y}", "-ForceRes", "-Unattended",
                    f"-abslog={folder / 'viewer.log'}"]
            proc = subprocess.Popen(args, cwd=str(cfg.viewer_exe.parent), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.sessions[sid] = Session(sid, project_id, build_id, version, proc, folder)
            log.info("stream session %s: %s / %s (pid %s)", sid, project_id, build_id, proc.pid)
            return self.describe(self.sessions[sid])

    def player_url(self, s: Session) -> str:
        # Epic's player page without its overlay buttons (the viewer draws its own HUD); locked mouse, because the
        # viewer looks around with raw mouse movement
        return (f"http://127.0.0.1:{self.cfg.player_port}/player.html?StreamerId={s.id}&AutoConnect=true"
                "&AutoPlayVideo=true&StartVideoMuted=true&HoveringMouse=false&MatchViewportRes=false&HideUI=true")

    def refresh(self) -> None:
        for s in self.sessions.values():
            self._update(s)

    def _update(self, s: Session) -> None:
        if s.ended_at is None:
            code = s.proc.poll()
            if code is not None:
                s.ended_at, s.exit_code = time.time(), code
                s.token_file.unlink(missing_ok=True)
                log.info("stream session %s ended (exit %s)", s.id, code)
        if s.ready_at is None and s.log_file.is_file():
            try:
                tail = s.log_file.read_text(encoding="utf-8", errors="replace")[-200_000:]
            except OSError:
                tail = ""
            if "[twin] ready:" in tail:
                s.ready_at = time.time()

    def describe(self, s: Session) -> dict:
        self._update(s)
        state = "ended" if s.ended_at else ("running" if s.ready_at else "starting")
        out = {"id": s.id, "project_id": s.project_id, "build_id": s.build_id, "version": s.version, "state": state,
               "started_at": s.started_at, "ready_s": round(s.ready_at - s.started_at, 1) if s.ready_at else None,
               "player_url": self.player_url(s), "idle_exit_s": self.cfg.idle_exit_s,
               "max_session_s": self.cfg.max_session_s}
        if s.ended_at:
            out.update(exit_code=s.exit_code, reason=EXIT_REASONS.get(s.exit_code, f"exit code {s.exit_code}"))
        return out

    def get(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            raise StreamError(404, "NOT_FOUND", f"no streaming session {sid}")
        return self.describe(s)

    def stop(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            raise StreamError(404, "NOT_FOUND", f"no streaming session {sid}")
        if s.proc.poll() is None:
            kill_tree(s.proc)
        self._update(s)
        if s.exit_code not in (0, 3, 4, 5):
            s.exit_code = -1
        s.token_file.unlink(missing_ok=True)
        return self.describe(s)

    def shutdown(self) -> None:
        for sid in list(self.sessions):
            try:
                self.stop(sid)
            except Exception:  # noqa: BLE001
                log.exception("stopping stream session %s", sid)
        if self.signalling is not None and self.signalling.poll() is None:
            kill_tree(self.signalling)
