"""Open the packaged desktop viewer on this PC straight from the web app ("Open on this PC").

For a backend and a browser on the same computer only: the routes refuse requests that do not come from
127.0.0.1 / ::1, requests forwarded by a proxy, and deployments with TWIN_PUBLIC_URL set, so a backend that is
reachable from the network never starts programs for remote visitors (they use the download link instead).

The viewer receives a short-lived read-only viewer token through a file (never on the command line, where other
users of the PC could read it); the file is deleted once the viewer has had time to read it.

The viewer used: TWIN_DESKTOP_VIEWER_EXE, else the Shipping package, else the Development package
(``python -m twin.unreal.package [--config Shipping --out dist/TwinViewerShipping]``).
"""

from __future__ import annotations

import secrets
import subprocess
import threading
import time
from pathlib import Path

from twin.config import PROJECT_ROOT, Settings
from twin.viewer.tokens import mint

CANDIDATES = (PROJECT_ROOT / "dist" / "TwinViewerShipping" / "Windows" / "TwinWalkthrough.exe",
              PROJECT_ROOT / "dist" / "TwinViewer" / "Windows" / "TwinWalkthrough.exe")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TOKEN_FILE_LIFETIME_S = 120     # the viewer reads its token file within seconds of starting
REPEAT_GUARD_S = 3.0            # a double click opens one viewer, not two


def _spawn(args: list[str], **kwargs) -> subprocess.Popen:
    """Starts the viewer (a seam for tests)."""
    return subprocess.Popen(args, **kwargs)


class LocalViewerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


class LocalViewerLauncher:
    def __init__(self, settings: Settings, secret: bytes) -> None:
        self.settings = settings
        self.secret = secret
        self.folder = settings.data_dir / "viewer_local"
        self._lock = threading.Lock()
        self._last: tuple[str, float] | None = None

    def viewer_exe(self) -> Path | None:
        if self.settings.desktop_viewer_exe:
            p = Path(self.settings.desktop_viewer_exe)
            return p if p.is_file() else None
        return next((p for p in CANDIDATES if p.is_file()), None)

    def status(self) -> dict:
        exe = self.viewer_exe()
        problem = None if exe else ("the desktop viewer is not packaged (python -m twin.unreal.package)"
                                    if not self.settings.desktop_viewer_exe else
                                    f"TWIN_DESKTOP_VIEWER_EXE does not exist: {self.settings.desktop_viewer_exe}")
        if self.settings.public_url:
            problem = "not available on a deployed server (TWIN_PUBLIC_URL is set): use the download link"
        return {"available": exe is not None and not self.settings.public_url, "exe": exe.name if exe else None,
                "package": exe.parent.parent.name if exe else None, "problem": problem}

    @staticmethod
    def check_request(client_host: str | None, headers) -> None:
        """Only the person sitting at this PC may start a program on it."""
        if client_host not in LOOPBACK:
            raise LocalViewerError(403, "LOCAL_ONLY", "Opening the viewer on this PC only works from a browser on the same "
                                                      "computer as the server; download the launch file instead.")
        if any(h in headers for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
            raise LocalViewerError(403, "LOCAL_ONLY", "This request came through a proxy; download the launch file instead.")

    def open(self, server_url: str, project_id: str, build_id: str, projects: list[str]) -> dict:
        st = self.status()
        if not st["available"]:
            raise LocalViewerError(503, "DESKTOP_VIEWER_NOT_AVAILABLE", st["problem"] or "the desktop viewer is not available")
        exe = self.viewer_exe()
        with self._lock:
            key, now = f"{project_id}/{build_id}", time.monotonic()
            if self._last and self._last[0] == key and now - self._last[1] < REPEAT_GUARD_S:
                return {"started": False, "reason": "already opening", "viewer": exe.parent.parent.name}
            self._last = (key, now)
        token, grant = mint(self.secret, projects=projects, ttl_hours=self.settings.viewer_token_ttl_h)
        self.folder.mkdir(parents=True, exist_ok=True)
        token_file = self.folder / f"{secrets.token_hex(8)}.token"
        token_file.write_text(token, encoding="ascii")
        args = [str(exe), "-windowed", "-ResX=1600", "-ResY=900", f"-TwinServer={server_url}", f"-TwinProject={project_id}",
                f"-TwinBuild={build_id}", f"-TwinTokenFile={token_file}"]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = _spawn(args, cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except OSError as exc:
            token_file.unlink(missing_ok=True)
            raise LocalViewerError(500, "DESKTOP_VIEWER_FAILED", f"The viewer could not be started: {exc}")
        timer = threading.Timer(TOKEN_FILE_LIFETIME_S, lambda: token_file.unlink(missing_ok=True))
        timer.daemon = True
        timer.start()
        return {"started": True, "pid": proc.pid, "viewer": exe.parent.parent.name, "expires_at": grant.expires_at}
