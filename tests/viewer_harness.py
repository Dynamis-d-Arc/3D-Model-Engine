"""Drive the REAL packaged Unreal viewer against a REAL backend process (used by tests/test_unreal_viewer.py
and the private real-data test). Nothing here is mocked: the backend runs as `python -m twin.api` on its own
data directory and port, and the viewer is the packaged TwinWalkthrough.exe from `python -m twin.unreal.package`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from twin.config import PROJECT_ROOT

VIEWER_EXE = Path(os.environ.get("TWIN_VIEWER_EXE", PROJECT_ROOT / "dist" / "TwinViewer" / "Windows" / "TwinWalkthrough.exe"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    """`python -m twin.api` in a subprocess on a private data dir; stop() kills it."""

    def __init__(self, data_dir: Path, port: int | None = None) -> None:
        self.data_dir = data_dir
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = data_dir.parent / f"backend_{self.port}.log"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 60) -> "Backend":
        env = {**os.environ, "TWIN_DATA_DIR": str(self.data_dir), "TWIN_PORT": str(self.port), "TWIN_HOST": "127.0.0.1",
               "PYTHONIOENCODING": "utf-8"}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([sys.executable, "-m", "twin.api"], cwd=PROJECT_ROOT, env=env,
                                     stdout=open(self.log, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if httpx.get(f"{self.url}/api/viewer/v1/info", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"backend exited: {self.log.read_text(encoding='utf-8')[-2000:]}")
            time.sleep(0.5)
        raise RuntimeError("backend did not start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)

    def client(self) -> httpx.Client:
        # no keep-alive: uvicorn closes idle connections after 5 s
        return httpx.Client(base_url=self.url, timeout=60, headers={"Connection": "close"})

    def launch(self, pid: str, bid: str | None = None, all_projects: bool = False) -> dict:
        with self.client() as c:
            r = c.post(f"/api/projects/{pid}/viewer/launch", json={"build_id": bid, "all_projects": all_projects})
            r.raise_for_status()
            return r.json()


def run_viewer(launch: dict | None, out_dir: Path, *, extra: list[str] | None = None, timeout_s: float = 900,
               resolution: tuple[int, int] = (1600, 900)) -> tuple[int, dict | None]:
    """Run the packaged viewer with -TwinTour; returns (exit code, tour_report.json or None)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [str(VIEWER_EXE), "-windowed", f"-ResX={resolution[0]}", f"-ResY={resolution[1]}", "-TwinTour",
            f"-TwinTourOut={out_dir.as_posix()}", "-unattended", "-nosplash", "-NoSound", "-log",
            f"-abslog={(out_dir / 'viewer.log').as_posix()}"]
    if launch is not None:
        f = out_dir / "test.twinview"
        f.write_text(json.dumps(launch), encoding="utf-8")
        args.append(f"-TwinLaunch={f.as_posix()}")
    args += extra or []
    proc = subprocess.run(args, timeout=timeout_s)
    report = out_dir / "tour_report.json"
    return proc.returncode, (json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None)


def wait_job(c: httpx.Client, jid: str, timeout: float = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "failed", "needs_review"):
            return j
        time.sleep(3)
    raise TimeoutError(jid)
