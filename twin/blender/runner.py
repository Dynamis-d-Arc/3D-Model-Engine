"""Run the Blender worker scripts headless and stream their progress."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
ProgressFn = Callable[[float, str, dict], None]


class BlenderNotConfigured(RuntimeError):
    pass


class BlenderError(RuntimeError):
    def __init__(self, message: str, log_tail: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail
        self.returncode = returncode


def require_blender(path: str | None) -> str:
    if not path or not Path(path).is_file():
        raise BlenderNotConfigured(
            "Blender was not found. Install Blender 4.2+ (5.2 tested) and set TWIN_BLENDER_PATH to blender.exe.")
    return path


@lru_cache(maxsize=4)
def blender_version(path: str) -> str:
    out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=120,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    first = next((ln for ln in out.stdout.splitlines() if ln.startswith("Blender")), "")
    if not first:
        raise BlenderError(f"could not read Blender version from {path}", out.stdout[-2000:] + out.stderr[-2000:])
    return first.strip()


def run_script(blender: str, script: str, args: list[str], *, log_path: Path, timeout_s: float = 1800,
               on_progress: ProgressFn | None = None) -> dict:
    """Run ``script`` from SCRIPTS_DIR in background Blender. Raises BlenderError on failure."""
    cmd = [blender, "--background", "--factory-startup", "--python-exit-code", "3",
           "--python", str(SCRIPTS_DIR / script), "--", *args]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque(maxlen=60)
    last: dict = {}
    start = time.time()
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                tail.append(line.rstrip())
                if line.startswith("@@TWIN "):
                    try:
                        last = json.loads(line[7:])
                    except json.JSONDecodeError:
                        continue
                    if on_progress:
                        on_progress(float(last.get("progress", 0.0)), str(last.get("stage", "")), last)
            rc = proc.wait()
        finally:
            timer.cancel()
        elapsed = time.time() - start
        log.write(f"# exit {rc} after {elapsed:.1f}s\n")
    if elapsed >= timeout_s:
        raise BlenderError(f"{script} timed out after {timeout_s:.0f}s", "\n".join(tail), rc)
    if rc != 0:
        raise BlenderError(f"{script} failed (exit code {rc})", "\n".join(tail), rc)
    if last.get("stage") not in ("done", "verified"):
        raise BlenderError(f"{script} ended without reporting completion", "\n".join(tail), rc)
    return {"elapsed_s": round(elapsed, 2), "last": last}
