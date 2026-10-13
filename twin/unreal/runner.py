"""Drive Unreal Engine 5 from the backend: compile the template, import builds, run the scripted tour.

All long operations run as subprocesses with timeouts; logs are kept next to the build.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from twin.config import Settings

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UnrealError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class UnrealPaths:
    engine_dir: Path
    editor: Path
    editor_cmd: Path
    build_bat: Path
    project: Path

    @property
    def module_dll(self) -> Path:
        return self.project.parent / "Binaries" / "Win64" / f"UnrealEditor-{self.project.stem}.dll"


def unreal_paths(settings: Settings) -> UnrealPaths:
    if not settings.unreal_engine_dir or not Path(settings.unreal_engine_dir).is_dir():
        raise UnrealError("UNREAL_NOT_FOUND", "Unreal Engine was not found; set TWIN_UNREAL_ENGINE_DIR (e.g. C:\\Program Files\\Epic Games\\UE_5.8).")
    eng = Path(settings.unreal_engine_dir)
    paths = UnrealPaths(eng, eng / "Engine/Binaries/Win64/UnrealEditor.exe", eng / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
                        eng / "Engine/Build/BatchFiles/Build.bat", settings.unreal_project)
    if not paths.editor_cmd.is_file():
        raise UnrealError("UNREAL_NOT_FOUND", f"UnrealEditor-Cmd.exe not found under {eng}")
    if not paths.project.is_file():
        raise UnrealError("UNREAL_PROJECT_MISSING", f"Walkthrough project not found: {paths.project}")
    return paths


def _run(cmd: list[str], log_path: Path, timeout_s: float, cwd: Path | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=cwd, creationflags=NO_WINDOW)
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        try:
            rc = proc.wait()
        finally:
            timer.cancel()
        log.write(f"# exit {rc}\n")
    return rc


def tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def compile_template(settings: Settings, log_path: Path, timeout_s: float = 1800) -> None:
    p = unreal_paths(settings)
    rc = _run(["cmd", "/c", str(p.build_bat), f"{p.project.stem}Editor", "Win64", "Development",
               f"-Project={p.project}", "-WaitMutex", "-NoHotReloadFromIDE"], log_path, timeout_s)
    if rc != 0 or not p.module_dll.is_file():
        text = tail(log_path, 60)
        hint = ""
        if "NetFxSDK" in text:
            hint = (" The .NET Framework 4.6+ SDK is missing: install the '.NET Framework 4.8.1 Developer Pack' "
                    "(or the matching Visual Studio Build Tools component) and retry.")
        raise UnrealError("UNREAL_COMPILE_FAILED", f"Compiling the walkthrough template failed (exit {rc}).{hint}",
                          {"log_tail": text})


def require_module(settings: Settings) -> UnrealPaths:
    p = unreal_paths(settings)
    if not p.module_dll.is_file():
        raise UnrealError("UNREAL_MODULE_NOT_BUILT",
                          "The walkthrough template's C++ module is not compiled yet. Run `python -m twin.unreal.setup` "
                          "(see docs/UNREAL.md).", {"expected": str(p.module_dll)})
    return p


def run_import(settings: Settings, build_dir: Path, project_id: str, timeout_s: float = 2400) -> dict:
    """Import a verified build (manifest.json) into the template project; returns the import report."""
    p = require_module(settings)
    out = build_dir / "unreal"
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "ue_import_report.json"
    report_path.unlink(missing_ok=True)
    args_path = out / "ue_import_args.json"
    args_path.write_text(json.dumps({"manifest": str(build_dir / "manifest.json"), "build_dir": str(build_dir),
                                     "project_id": project_id, "report": str(report_path)}), encoding="utf-8")
    log_path = out / "ue_import.log"
    script = (SCRIPTS_DIR / "import_twin.py").as_posix()
    t0 = time.time()
    rc = _run([str(p.editor_cmd), str(p.project), "-run=pythonscript", f"-script={script} {args_path.as_posix()}",
               "-unattended", "-nosplash", "-nop4", "-nullrhi", "-stdout", "-FullStdOutLogOutput"], log_path, timeout_s)
    if not report_path.is_file():
        raise UnrealError("UNREAL_IMPORT_FAILED", f"The Unreal import did not produce a report (exit {rc}).",
                          {"log_tail": tail(log_path)})
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["elapsed_s"] = round(time.time() - t0, 1)
    if report.get("status") == "error":
        raise UnrealError("UNREAL_IMPORT_FAILED", report.get("error", "import failed"),
                          {"trace": report.get("trace", ""), "log_tail": tail(log_path)})
    if report.get("status") != "ok":
        raise UnrealError("UNREAL_BOUNDS_MISMATCH", "Imported actors do not match the scene model in Unreal.",
                          {"failures": report.get("bounds_failures", [])[:20]})
    return report


def run_tour(settings: Settings, map_path: str, out_dir: Path, timeout_s: float = 900,
             resolution: tuple[int, int] = (1600, 900)) -> dict:
    """Play the map with -TwinTour (navigation / collision / selection checks + screenshots)."""
    p = require_module(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "tour_report.json"
    report_path.unlink(missing_ok=True)
    log_path = out_dir / "ue_tour.log"
    rc = _run([str(p.editor), str(p.project), map_path, "-game", "-windowed", f"-ResX={resolution[0]}",
               f"-ResY={resolution[1]}", "-TwinTour", f"-TwinTourOut={out_dir.as_posix()}", "-unattended", "-nosplash",
               "-nop4", "-NoSound", "-log", f"-abslog={(out_dir / 'ue_game.log').as_posix()}"], log_path, timeout_s)
    if not report_path.is_file():
        raise UnrealError("UNREAL_TOUR_FAILED", f"The walkthrough tour did not finish (exit {rc}).",
                          {"log_tail": tail(out_dir / "ue_game.log", 60)})
    return json.loads(report_path.read_text(encoding="utf-8"))


def launch(settings: Settings, map_path: str) -> int:
    """Open the walkthrough for the user (non-blocking). Returns the process id."""
    p = require_module(settings)
    proc = subprocess.Popen([str(p.editor), str(p.project), map_path, "-game", "-windowed", "-ResX=1600", "-ResY=900",
                             "-nosplash"], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return proc.pid
