"""Read-only API for the packaged Unreal viewer (and, later, Pixel Streaming instances).

    GET  /api/viewer/v1/info                                         no auth: API version, features
    GET  /api/viewer/v1/buildings                                    buildings + completed builds the token may open
    GET  /api/viewer/v1/buildings/{pid}/builds/{bid}/manifest        viewer manifest (Unreal cm, metadata, pre-flight)
    GET  /api/viewer/v1/buildings/{pid}/builds/{bid}/model.glb       the Blender glTF export of the build
    GET  /api/viewer/v1/buildings/{pid}/builds/{bid}/hvac/live       HVAC readings per unit: live BMS or simulated, labelled
    GET  /api/viewer/v1/buildings/{pid}/builds/{bid}/hvac/airflow    airflow illustration geometry + MATLAB thermal state

Every route except /info needs ``Authorization: Bearer <viewer token>`` (twin.viewer.tokens). Tokens are
minted by the web app's side of the backend:

    POST /api/projects/{pid}/viewer/launch   {"build_id": "...", "ttl_hours": 12}  -> token + launch file

AI interpretation, Blender generation, storage and authentication stay on the backend; the viewer only
reads finished builds. Errors use the backend's JSON error shape {"error": {"code", "message", "detail"}}.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from twin import __version__
from twin.api.storage import NotFound, Storage
from twin.config import Settings
from twin.viewer.bundle import MODEL_REL, BundleError, airflow_ue, viewer_manifest
from twin.viewer.tokens import MAX_TTL_H, TokenError, ViewerGrant, load_secret, mint, verify

API_VERSION = 1
PREFIX = "/api/viewer/v1"
LAUNCH_FORMAT = "twin-viewer-launch"


class LaunchBody(BaseModel):
    build_id: str | None = Field(default=None, description="open this build (default: the newest completed build)")
    ttl_hours: float | None = Field(default=None, gt=0, le=MAX_TTL_H)
    all_projects: bool = Field(default=False, description="let the viewer switch to any project's builds")


def thermal_state(results: dict, unit_tags: set[str], setpoint_c: float | None, minute: int | None, split_x_cm: float | None) -> dict:
    """The MATLAB thermal model's zone temperatures for this level at one minute of the simulated run."""
    floors = results["building"]["floors"]
    floor = max(floors, key=lambda f: len(unit_tags & {t.upper() for t in f.get("tags", [])}))
    matched = len(unit_tags & {t.upper() for t in floor.get("tags", [])})
    if matched == 0:
        return {"available": False, "reason": "none of this level's unit tags appear in the MATLAB results"}
    scenarios = results["scenarios"]
    want = 24.0 if setpoint_c is None else setpoint_c
    sc = min(scenarios, key=lambda s: abs(float(s["setpoint"]) - want))
    series = next(f["series"] for f in sc["floors"] if int(f["floor"]) == int(floor["floor"]))
    last = len(series) - 1
    if minute is None:  # follow the clock: minute 0 = 08:00 local time of the run, clamped to the run
        now = datetime.now()
        minute = (now.hour - 8) * 60 + now.minute
    m = max(0, min(last, int(minute)))
    row = series[m]
    return {"available": True, "source": "matlab", "measured": False,
            "label": "MATLAB thermal model (simulation, not measured)",
            "engine": results.get("engine", {}), "method": results.get("method", ""),
            "floor": floor["floor"], "matched_tags": matched, "setpoint_c": float(sc["setpoint"]),
            "setpoints": [s["setpoint"] for s in scenarios], "minute": m, "minutes": last,
            "west_c": round(row[0], 2), "east_c": round(row[1], 2), "ac_kw": round(row[2], 2),
            "split_x_cm": split_x_cm}


def register(app: FastAPI, settings: Settings, storage: Storage) -> None:
    from twin.api.app import ApiError

    secret = load_secret(settings.viewer_secret.get_secret_value() if settings.viewer_secret else None, settings.data_dir)
    airflow_cache: dict[tuple[str, int], dict] = {}

    def grant_for(authorization: str | None) -> ViewerGrant:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "TOKEN_MISSING", "Send the viewer token as 'Authorization: Bearer <token>'.")
        try:
            return verify(secret, authorization[7:])
        except TokenError as exc:
            raise ApiError(401, exc.code, str(exc))

    def viewable_build(grant: ViewerGrant, pid: str, bid: str) -> tuple[dict, dict, Path]:
        if not grant.allows(pid, bid):
            raise ApiError(403, "OUT_OF_SCOPE", "This viewer token does not give access to that building.")
        try:
            project = storage.get_project(pid)
            build = storage.get_build(pid, bid)
        except NotFound:
            raise ApiError(404, "NOT_FOUND", "No such building or build.")
        if build["status"] != "completed":
            raise ApiError(409, "BUILD_NOT_COMPLETED", f"Build {bid} is {build['status']}; only verified builds can be viewed.")
        return project, build, storage.build_dir(pid, bid)

    def manifest_for(project: dict, build: dict, folder: Path) -> dict:
        try:
            return viewer_manifest(folder, project["id"], project["name"], build["version"])
        except BundleError as exc:
            raise ApiError(409, exc.code, str(exc), exc.detail)

    @app.get(f"{PREFIX}/info")
    def info() -> dict:
        return {"api": "twin-viewer", "api_version": API_VERSION, "server_version": __version__,
                "auth": "bearer viewer token (POST /api/projects/{pid}/viewer/launch)",
                "features": ["buildings", "manifest", "model.glb", "hvac/live", "hvac/airflow"]}

    @app.get(f"{PREFIX}/buildings")
    def buildings(authorization: str | None = Header(default=None)) -> dict:
        grant = grant_for(authorization)
        out = []
        for p in storage.list_projects():
            if not grant.allows(p["id"]):
                continue
            builds = [b for b in storage.list_builds(p["id"]) if b["status"] == "completed" and grant.allows(p["id"], b["id"])]
            builds = [b for b in builds if (storage.build_dir(p["id"], b["id"]) / MODEL_REL).is_file()]
            if not builds:
                continue
            out.append({"project_id": p["id"], "name": p["name"], "updated_at": p["updated_at"],
                        "builds": [{"build_id": b["id"], "version": b["version"], "finished_at": b["finished_at"],
                                    "rooms": (b["summary"] or {}).get("rooms"), "furniture": (b["summary"] or {}).get("furniture"),
                                    "model_bytes": (storage.build_dir(p["id"], b["id"]) / MODEL_REL).stat().st_size}
                                   for b in builds]})
        return {"buildings": out, "token_expires_at": grant.expires_at}

    @app.get(f"{PREFIX}/buildings/{{pid}}/builds/{{bid}}/manifest")
    def manifest(pid: str, bid: str, authorization: str | None = Header(default=None)) -> dict:
        project, build, folder = viewable_build(grant_for(authorization), pid, bid)
        return manifest_for(project, build, folder)

    @app.get(f"{PREFIX}/buildings/{{pid}}/builds/{{bid}}/model.glb")
    def model(pid: str, bid: str, authorization: str | None = Header(default=None)):
        _, _, folder = viewable_build(grant_for(authorization), pid, bid)
        path = folder / MODEL_REL
        if not path.is_file():
            raise ApiError(409, "MODEL_MISSING", "This build has no glTF model.")
        return FileResponse(path, media_type="model/gltf-binary", headers={"Cache-Control": "private, max-age=3600"})

    @app.get(f"{PREFIX}/buildings/{{pid}}/builds/{{bid}}/hvac/live")
    def hvac_live(pid: str, bid: str, authorization: str | None = Header(default=None)) -> dict:
        """Latest reading per HVAC unit of this build. ``measured`` is true only for real BMS data."""
        project, build, folder = viewable_build(grant_for(authorization), pid, bid)
        units = manifest_for(project, build, folder)["hvac"]["units"]
        now = time.time()
        base = {"server_time": now, "build_units": len(units)}
        if not units:
            return {**base, "source": "unavailable", "measured": False, "label": "No HVAC layer in this build",
                    "units": [], "matching": {"with_reading": 0, "missing": [], "unknown": []}}
        live = app.state.telemetry.live(pid)
        if live is not None:
            readings, site, newest = live
            source, label, measured = "live", "Live BMS data (measured)", True
        else:
            try:
                domain = app.state.hvac_domain_for(pid, build["version"])
            except Exception as exc:  # noqa: BLE001 - surfaced to the viewer as unavailable data
                return {**base, "source": "unavailable", "measured": False, "label": "HVAC data unavailable",
                        "reason": getattr(exc, "detail", None) or str(exc), "units": [],
                        "matching": {"with_reading": 0, "missing": [u["id"] for u in units], "unknown": []}}
            from twin.hvac.telemetry import simulate
            readings, site = simulate(domain, now)
            newest = site["ts"]
            source, label, measured = "simulated", "Simulated BMS (demonstration data, not measured)", False
        ids = {u["id"] for u in units}
        rows = []
        for u in units:
            r = readings.get(u["id"])
            if r is not None:
                rows.append({**{k: r.get(k) for k in ("on", "mode", "setpoint_c", "supply_air_c", "return_air_c", "room_temp_c",
                                                      "fan_speed", "airflow_lps", "alarm", "ts")},
                             "unit_id": u["id"], "tag": u["tag"], "object": u["object"]})
        return {**base, "source": source, "measured": measured, "label": label, "ts": newest,
                "age_s": round(max(0.0, now - newest), 1), "site": site or {}, "units": rows,
                "matching": {"with_reading": len(rows), "missing": sorted(ids - set(readings))[:100],
                             "unknown": sorted(set(readings) - ids)[:100]}}

    @app.get(f"{PREFIX}/buildings/{{pid}}/builds/{{bid}}/hvac/airflow")
    def hvac_airflow(pid: str, bid: str, minute: int | None = None, setpoint_c: float | None = None,
                     authorization: str | None = Header(default=None)) -> dict:
        """Airflow illustration zones (Unreal cm) and, when saved, the MATLAB thermal model's zone temperatures."""
        project, build, folder = viewable_build(grant_for(authorization), pid, bid)
        key = (pid, build["version"])
        if key not in airflow_cache:
            scene = storage.load_scene(pid, build["version"])
            if not scene.hvac.units:
                raise ApiError(409, "HVAC_MISSING", "This build has no HVAC units, so there is no airflow to show.")
            airflow_cache[key] = airflow_ue(scene)
        out = dict(airflow_cache[key])
        thermal_path = storage.project_dir(pid) / "thermal" / "results.json"
        if thermal_path.is_file():
            try:
                tags = {z["tag"].upper() for z in out["zones"]}
                xs = [p[0] for p in out["outline_cm"]]
                split = round((min(xs) + max(xs)) / 2, 1) if xs else None
                out["thermal"] = thermal_state(json.loads(thermal_path.read_text(encoding="utf-8")), tags, setpoint_c,
                                               minute, split)
            except (KeyError, TypeError, ValueError, StopIteration) as exc:
                out["thermal"] = {"available": False, "reason": f"MATLAB results unreadable: {exc}"}
        else:
            out["thermal"] = {"available": False, "reason": "No MATLAB thermal results saved for this project."}
        return out

    # ---- launch (web-app side: mints a token for the desktop viewer) ---------------------------
    @app.post("/api/projects/{pid}/viewer/launch")
    def launch(pid: str, body: LaunchBody, request: Request) -> dict:
        project = storage.get_project(pid)
        if body.build_id:
            build = storage.get_build(pid, body.build_id)
            if build["status"] != "completed":
                raise ApiError(409, "BUILD_NOT_COMPLETED", "Only completed, verified builds can be opened in the viewer.")
        else:
            build = storage.latest_completed_build(pid)
            if build is None:
                raise ApiError(409, "NO_COMPLETED_BUILD", "Generate the building first; there is no completed build to view.")
        manifest_for(project, build, storage.build_dir(pid, build["id"]))  # pre-flight before handing out a link
        ttl = body.ttl_hours or settings.viewer_token_ttl_h
        token, grant = mint(secret, projects=None if body.all_projects else [pid], ttl_hours=ttl)
        server = settings.public_url or str(request.base_url).rstrip("/")
        launch_doc = {"format": LAUNCH_FORMAT, "version": 1, "server": server, "project_id": pid, "build_id": build["id"],
                      "project_name": project["name"], "token": token, "expires_at": math.floor(grant.expires_at)}
        return {"launch": launch_doc, "file_name": f"{project['name'][:60]}.twinview".replace("/", "-").replace("\\", "-"),
                "command": f'TwinWalkthrough.exe -TwinLaunch="<path to the .twinview file>"',
                "expires_at": grant.expires_at}
