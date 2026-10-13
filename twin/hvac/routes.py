"""HVAC endpoints: import an HVAC drawing into a scene version, review files, live data and the
airflow simulation domain (registered by twin.api.app)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Header, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from twin.api.jobs import JobContext, JobManager
from twin.api.storage import Storage
from twin.config import PROJECT_ROOT, Settings
from twin.hvac.airflow import DOMAIN_VERSION, build_domain, focus_rooms
from twin.hvac.illustration import ThermalResultsError, airflow_zones, validate_thermal_results
from twin.hvac.importer import HvacImportError, render_overlay
from twin.hvac.telemetry import TelemetryBody, TelemetryStore, simulate
from twin.scene.model import Scene


class HvacImportBody(BaseModel):
    file_id: str
    plan_file_id: str | None = None
    manual: dict[str, float] | None = Field(default=None, description="dx_pt, dy_pt, scale: skip automatic alignment")


IMPORT_TIMEOUT_S = 1200


def plan_sheet_for(storage: Storage, pid: str, plan_file_id: str | None) -> tuple[dict, dict]:
    """Spec of the vector floor plan the scene was traced from (paths + scale), and its file record."""
    files = [f for f in storage.list_files(pid) if f["kind"] == "plan"]
    if plan_file_id:
        files = [f for f in files if f["id"] == plan_file_id]
    for f in reversed(files):
        folder = storage.upload_dir(pid, f["id"])
        info_file = folder / "pdf_text.json"
        if f["sniffed"] == "pdf" and info_file.is_file():
            info = json.loads(info_file.read_text(encoding="utf-8"))
            if info.get("m_per_pt"):
                return {"pdf": str(folder / "original.pdf"), "page_h_pt": info["page_h_pt"], "m_per_pt": info["m_per_pt"],
                        "cache_dir": str(folder), "scale": info.get("scale")}, f
    raise HvacImportError("HVAC_PLAN_NOT_VECTOR",
                          "Automatic HVAC alignment needs the floor plan as a vector PDF with a drawing scale "
                          "(the scene's plan frame). Upload the floor plan PDF first.")


def run_import_process(spec: Path, ctx: JobContext) -> dict:
    """twin.hvac.worker in a child process; relays its progress lines; raises HvacImportError on failure."""
    proc = subprocess.Popen([sys.executable, "-m", "twin.hvac.worker", str(spec)], cwd=str(PROJECT_ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("{") and '"progress"' in line:
            try:
                m = json.loads(line)
                ctx.progress(float(m["progress"]), str(m["stage"]))
                continue
            except (ValueError, KeyError):
                pass
        if line:
            tail = (tail + [line])[-20:]
    try:
        proc.wait(timeout=IMPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise HvacImportError("HVAC_IMPORT_TIMEOUT", f"The HVAC import took longer than {IMPORT_TIMEOUT_S} s.")
    out = json.loads(spec.with_name("result.json").read_text(encoding="utf-8")) if spec.with_name("result.json").is_file() else None
    if out is None:
        raise HvacImportError("HVAC_IMPORT_CRASHED", f"The HVAC import process stopped (exit code {proc.returncode}).",
                              detail="\n".join(tail))
    if not out.get("ok"):
        raise HvacImportError(out.get("code") or "HVAC_IMPORT_FAILED", out.get("message", "failed"), detail=out.get("detail"))
    return out


def register(app: FastAPI, settings: Settings, storage: Storage, jobs: JobManager,
             store_version: Callable[..., dict], api_error: type) -> None:

    def imports_dir(pid: str, job_id: str) -> Path:
        return storage.project_dir(pid) / "hvac_imports" / job_id

    @app.post("/api/projects/{pid}/versions/{number}/hvac", status_code=202)
    def hvac_import(pid: str, number: int, body: HvacImportBody) -> dict:
        scene = storage.load_scene(pid, number)
        meta = storage.get_file(pid, body.file_id)
        if meta["sniffed"] != "pdf":
            raise api_error(415, "HVAC_NOT_PDF", "The HVAC drawing must be a vector PDF exported from CAD.")
        try:
            plan, plan_meta = plan_sheet_for(storage, pid, body.plan_file_id)
        except HvacImportError as exc:
            if body.manual is None:
                raise api_error(409, exc.code, str(exc))
            plan, plan_meta = None, None
        hvac_pdf = storage.upload_dir(pid, body.file_id) / "original.pdf"

        def run(ctx: JobContext) -> tuple[str, dict]:
            out_dir = imports_dir(pid, ctx.job_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "scene_in.json").write_text(scene.model_dump_json(), encoding="utf-8")
            spec = {"hvac_pdf": str(hvac_pdf), "scene": str(out_dir / "scene_in.json"), "out": str(out_dir / "result.json"),
                    "cache_dir": str(storage.upload_dir(pid, body.file_id)), "file_id": body.file_id,
                    "file_name": meta["original_name"], "manual": body.manual, "plan": plan}
            (out_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            res = run_import_process(out_dir / "spec.json", ctx)
            updated = scene.model_copy(update={"hvac": res["hvac"]})
            updated = Scene.model_validate(updated.model_dump(mode="json"))
            (out_dir / "report.json").write_text(json.dumps(res["report"], indent=1), encoding="utf-8")
            (out_dir / "scene_in.json").unlink(missing_ok=True)
            if plan_meta:
                render_overlay(updated, out_dir / "overlay.png", storage.upload_dir(pid, plan_meta["id"]) / "normalized.png",
                               plan_meta.get("calibration"))
            report = res["report"]
            c = report["counts"]
            v = store_version(pid, updated, "hvac", f"HVAC from {meta['original_name']}: {c['units']} units, "
                              f"{c['supply_diffusers']} diffusers, {c['return_grilles']} grilles, {c['ducts']} ducts", number)
            return "needs_review", {"version": v["number"], "counts": c, "issues": report["issues"][:40],
                                    "registration": report["registration"], "timings": report["timings"],
                                    "report": f"/api/projects/{pid}/hvac-imports/{ctx.job_id}/report.json",
                                    "overlay": f"/api/projects/{pid}/hvac-imports/{ctx.job_id}/overlay.png" if plan_meta else None}

        return jobs.submit(pid, "hvac_import", body.model_dump(), run, version=number)

    @app.get("/api/projects/{pid}/hvac-imports/{job_id}/{name}")
    def hvac_import_file(pid: str, job_id: str, name: str):
        if name not in ("report.json", "overlay.png") or not job_id.startswith("j_"):
            raise api_error(404, "NOT_FOUND", "unknown file")
        path = imports_dir(pid, job_id) / name
        if not path.is_file():
            raise api_error(404, "FILE_MISSING", f"{name} is not available for this import")
        return FileResponse(path)

    @app.get("/api/projects/{pid}/versions/{number}/hvac-overlay")
    def hvac_overlay(pid: str, number: int):
        """Plan + HVAC review image for any version (cached per version; versions are immutable)."""
        scene = storage.load_scene(pid, number)
        out = storage.project_dir(pid) / "hvac_overlays" / f"v{number:04d}.png"
        if not out.is_file():
            try:
                _, plan_meta = plan_sheet_for(storage, pid, None)
                img = storage.upload_dir(pid, plan_meta["id"]) / "normalized.png"
                render_overlay(scene, out, img, plan_meta.get("calibration"))
            except HvacImportError:
                render_overlay(scene, out)
        return FileResponse(out)

    # ---- live data + airflow simulation domain -------------------------------------------------
    telemetry = TelemetryStore()
    app.state.telemetry = telemetry
    domains: dict[tuple[str, int, str | None], dict] = {}

    def domain_for(pid: str, number: int, room: str | None = None) -> dict:
        key = (pid, number, room)
        if key not in domains:
            cache = storage.project_dir(pid) / "airflow" / (f"v{number:04d}.json" if not room else f"v{number:04d}_{room}.json")
            d = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else None
            if not d or d.get("domain_version") != DOMAIN_VERSION:
                scene = storage.load_scene(pid, number)
                if not scene.hvac.units:
                    raise api_error(409, "HVAC_MISSING", f"Version {number} has no HVAC units; import an HVAC drawing first.")
                if room and not any(r.id == room for r in scene.rooms):
                    raise api_error(404, "ROOM_NOT_FOUND", f"Version {number} has no room '{room}'.")
                d = build_domain(scene, room_id=room)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(d), encoding="utf-8")
            domains[key] = d
        return domains[key]

    app.state.hvac_domain_for = domain_for  # shared with the viewer API (simulated BMS of a version)

    @app.get("/api/projects/{pid}/versions/{number}/airflow-domain")
    def airflow_domain(pid: str, number: int, room: str | None = None) -> Response:
        """The voxel domain of the whole level, or of one room in detail (`room`)."""
        d = domain_for(pid, number, room or None)
        return Response(json.dumps(d), media_type="application/json")

    @app.get("/api/projects/{pid}/versions/{number}/airflow-rooms")
    def airflow_rooms(pid: str, number: int) -> list[dict]:
        """Rooms with supply outlets: candidates for a detailed room simulation."""
        return focus_rooms(storage.load_scene(pid, number))

    # ---- airflow illustration (one zone per unit, driven by thermal data) ----------------------
    zone_cache: dict[tuple[str, int], dict] = {}

    @app.get("/api/projects/{pid}/versions/{number}/airflow-zones")
    def airflow_zone_model(pid: str, number: int) -> dict:
        """Per-unit zones (box, outlet, floor outline) for the airflow illustration, viewer frame."""
        key = (pid, number)
        if key not in zone_cache:   # versions are immutable
            scene = storage.load_scene(pid, number)
            if not scene.hvac.units:
                raise api_error(409, "HVAC_MISSING", f"Version {number} has no HVAC units; import an HVAC drawing first.")
            zone_cache[key] = airflow_zones(scene)
        return zone_cache[key]

    def thermal_file(pid: str) -> Path:
        return storage.project_dir(pid) / "thermal" / "results.json"

    @app.get("/api/projects/{pid}/hvac/thermal-results")
    def thermal_results(pid: str) -> Response:
        """The saved MATLAB thermal results (west/east zone temperatures over the day) that drive the clouds."""
        storage.get_project(pid)
        f = thermal_file(pid)
        if not f.is_file():
            raise api_error(404, "NO_THERMAL_RESULTS", "No MATLAB thermal results saved for this project.")
        return Response(f.read_bytes(), media_type="application/json")

    @app.put("/api/projects/{pid}/hvac/thermal-results")
    async def put_thermal_results(pid: str, file: UploadFile = File(...)) -> dict:
        """Upload a MATLAB thermal results JSON (format: twin.hvac.illustration.validate_thermal_results)."""
        storage.get_project(pid)
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise api_error(413, "TOO_LARGE", "results file over 20 MB")
        try:
            data = json.loads(raw)
            summary = validate_thermal_results(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise api_error(422, "BAD_JSON", f"not JSON: {exc}")
        except ThermalResultsError as exc:
            raise api_error(422, "BAD_THERMAL_RESULTS", str(exc))
        f = thermal_file(pid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
        return {"saved": True, "file_name": file.filename, **summary}

    @app.delete("/api/projects/{pid}/hvac/thermal-results")
    def delete_thermal_results(pid: str) -> dict:
        f = thermal_file(pid)
        if f.is_file():
            f.unlink()
        return {"deleted": True}

    @app.get("/api/projects/{pid}/hvac/live")
    def hvac_live(pid: str, version: int, simulated: bool = False) -> dict:
        """Latest HVAC readings: real data when it is fresh (and not overridden), else the simulated BMS."""
        d = domain_for(pid, version)
        live = None if simulated else telemetry.live(pid)
        if live is not None:
            units, site, newest = live
            known = {u["id"] for u in d["units"]}
            return {"source": "live", "ts": newest, "site": site or {},
                    "units": {k: v for k, v in units.items() if k in known}}
        units, site = simulate(d)
        return {"source": "simulated", "ts": site["ts"], "site": site, "units": units}

    @app.post("/api/projects/{pid}/hvac/telemetry")
    def hvac_ingest(pid: str, body: TelemetryBody, x_twin_token: str | None = Header(default=None)) -> dict:
        """Push real BMS readings (units by tag or id). Set TWIN_HVAC_INGEST_TOKEN to require a token."""
        token = os.environ.get("TWIN_HVAC_INGEST_TOKEN")
        if token and x_twin_token != token:
            raise api_error(401, "BAD_TOKEN", "missing or wrong X-Twin-Token header")
        versions = storage.list_versions(pid)
        tags: dict[str, str] = {}
        for v in versions:  # newest first: map tags of the newest version that has HVAC
            sc = storage.load_scene(pid, v["number"])
            if sc.hvac.units:
                tags = {u.tag.upper(): u.id for u in sc.hvac.units} | {u.id.upper(): u.id for u in sc.hvac.units}
                break
        if not tags:
            raise api_error(409, "HVAC_MISSING", "No version of this project has HVAC units yet.")
        return telemetry.ingest(pid, body, lambda name: tags.get(name.strip().upper()))

    @app.get("/api/projects/{pid}/hvac/history")
    def hvac_history(pid: str, limit: int = 120) -> list[dict]:
        return telemetry.history(pid, max(1, min(720, limit)))

    hvac_counts: dict[tuple[str, int], dict] = {}

    @app.get("/api/projects/{pid}/hvac/versions")
    def hvac_versions(pid: str) -> list[dict]:
        """Versions that contain an HVAC layer (newest first), for the live view."""
        out = []
        for v in storage.list_versions(pid):
            key = (pid, v["number"])
            if key not in hvac_counts:  # versions are immutable: count once
                hv = storage.load_scene(pid, v["number"]).hvac
                hvac_counts[key] = {"units": len(hv.units), "terminals": len(hv.terminals), "ducts": len(hv.ducts)}
            if hvac_counts[key]["units"]:
                out.append({"number": v["number"], "note": v["note"], "can_generate": v["can_generate"], **hvac_counts[key]})
        return out
