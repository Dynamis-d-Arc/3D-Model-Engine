"""API routes for the Unreal integration (registered by twin.api.app)."""

from __future__ import annotations

import json

from fastapi import FastAPI

from twin.api.jobs import JobContext, JobManager
from twin.api.storage import Storage
from twin.config import Settings
from twin.unreal.runner import UnrealError, launch, run_import, run_tour


def register(app: FastAPI, settings: Settings, storage: Storage, jobs: JobManager) -> None:
    from twin.api.app import ApiError

    def completed_build(pid: str, bid: str):
        b = storage.get_build(pid, bid)
        if b["status"] != "completed":
            raise ApiError(409, "BUILD_NOT_COMPLETED", "Only completed, verified builds can be imported into Unreal.")
        return b, storage.build_dir(pid, bid)

    @app.post("/api/projects/{pid}/builds/{bid}/unreal/import", status_code=202)
    def unreal_import(pid: str, bid: str, tour: bool = True) -> dict:
        _, folder = completed_build(pid, bid)

        def run(ctx: JobContext) -> tuple[str, dict]:
            ctx.progress(0.05, "importing into Unreal (headless editor)")
            try:
                rep = run_import(settings, folder, pid)
            except UnrealError as exc:
                raise _as_build_failed(exc) from exc
            result = {"map": rep["map"], "actors": rep["actors"], "static_meshes": rep["static_meshes"],
                      "imported": len(rep["imported"]), "reused": len(rep["reused"]),
                      "max_bounds_deviation_cm": rep["max_bounds_deviation_cm"], "warnings": rep["warnings"],
                      "import_seconds": rep["elapsed_s"]}
            if tour:
                ctx.progress(0.6, "running the walkthrough tour (navigation, collision, selection, screenshots)")
                try:
                    tr = run_tour(settings, rep["map"], folder / "unreal" / "tour")
                except UnrealError as exc:
                    raise _as_build_failed(exc) from exc
                result["tour"] = {k: tr.get(k) for k in ("navigation", "walk", "wall_collision", "selection", "avg_fps",
                                                         "twin_actors", "screenshots")}
                result["screenshot"] = "unreal/tour/overview.png"
            (folder / "unreal" / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
            return "completed", result

        return jobs.submit(pid, "unreal_import", {"build_id": bid, "tour": tour}, run)

    @app.post("/api/projects/{pid}/builds/{bid}/unreal/launch")
    def unreal_launch(pid: str, bid: str) -> dict:
        _, folder = completed_build(pid, bid)
        result = folder / "unreal" / "result.json"
        if not result.is_file():
            raise ApiError(409, "UNREAL_NOT_IMPORTED", "Import this build into Unreal first.")
        map_path = json.loads(result.read_text(encoding="utf-8"))["map"]
        try:
            pid_ = launch(settings, map_path)
        except UnrealError as exc:
            raise ApiError(409, exc.code, str(exc), exc.detail)
        return {"map": map_path, "process_id": pid_}


def _as_build_failed(exc: UnrealError):
    from twin.pipeline import BuildFailed
    return BuildFailed(exc.code, str(exc), exc.detail)
