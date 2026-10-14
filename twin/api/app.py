"""FastAPI backend: project management and orchestration.  Run:  python -m twin.api

All errors are JSON: {"error": {"code": ..., "message": ..., "detail": ...}}.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.background import BackgroundTask

from twin import __version__
from twin.ai.budget import BudgetGuard
from twin.ai.client import AIClient, AIError, AIResult
from twin.ai.interpret import interpret_plan, outcome_from_response, redesign_room
from twin.api.jobs import JobContext, JobManager
from twin.api.storage import NotFound, Storage, new_id
from twin.assets.catalog import load_catalog
from twin.blender.runner import blender_version
from twin.config import PROJECT_ROOT, Settings, get_settings, install_log_redaction
from twin.fixtures import load_sample_apartment
from twin.imaging.pdf_text import calibration_for, extract_pdf_text
from twin.imaging.preprocess import UploadRejected, normalize_upload
from twin.layers import run_layer_build
from twin.pipeline import run_build
from twin.scene import derive
from twin.scene.compile import scene_hash
from twin.scene.diff import change_summary, diff_scenes
from twin.scene.sensors import (KIND_COLORS, KIND_LABELS, KIND_UNITS, SHEET_TEMPLATE, TAG_PREFIX, geometry_key,
                                import_sheet, layer_changes)
from twin.scene.model import (Brief, Certainty, Level, NEEDS_REVIEW_STATUSES, ProjectMeta, Provenance, Room, Scene,
                              Wall, scene_json_schema)
from twin.scene.styles import BUILTIN_MATERIALS, STYLES
from twin.scene.validate import validate_scene

log = logging.getLogger("twin.api")
FRONTEND_DIR = PROJECT_ROOT / "frontend"


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(status_code=status, detail={"code": code, "message": message, "detail": detail})


def readable_validation(exc: ValidationError, limit: int = 25) -> list[str]:
    out = []
    for e in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
        out.append(f"{loc or 'scene'}: {e.get('msg')}")
    return out


# ---- request bodies ------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    brief: Brief = Field(default_factory=Brief)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    brief: Brief | None = None


class VersionCreate(BaseModel):
    scene: dict
    parent: int | None = None
    note: str = Field(default="", max_length=1000)
    source: Literal["edit", "manual", "derive"] = "edit"


class SceneBody(BaseModel):
    scene: dict
    brief: Brief | None = None


class DeriveBody(BaseModel):
    scene: dict
    exterior_thickness: float = Field(default=0.2, ge=0.05, le=1.0)
    interior_thickness: float = Field(default=0.1, ge=0.05, le=1.0)


class FitFurnitureBody(BaseModel):
    scene: dict
    furniture_id: str


class FitOverallBody(BaseModel):
    scene: dict
    width: float = Field(gt=0.5, le=500)
    depth: float = Field(gt=0.5, le=500)


class InterpretBody(BaseModel):
    plan_file_id: str | None = None
    reference_file_ids: list[str] = Field(default_factory=list, max_length=8)


class BuildBody(BaseModel):
    render_previews: bool = True
    reuse_previous: bool = True
    mode: Literal["full", "layer", "auto"] = Field(
        default="full", description="full = generate with Blender; layer = reuse a saved build whose building is "
                                    "identical and rewrite only its layers (sensors); auto = layer when possible")
    base_build: str | None = Field(default=None, description="layer/auto: the saved build to update (default: the "
                                                             "newest completed build with the same building)")


class SensorSheetBody(BaseModel):
    scene: dict
    text: str = Field(max_length=2_000_000, description="CSV placement sheet")


class RedesignBody(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    reference_file_ids: list[str] = Field(default_factory=list, max_length=4)


class FixBody(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    selection: list[str] = Field(default_factory=list, max_length=12, description="e.g. 'room:R12', 'hvac_unit:U_1FC-23'")
    include_plan: bool = True


class FurnitureBody(BaseModel):
    file_id: str | None = Field(default=None, description="vector PDF plan to read (default: the newest one)")
    keep_user_items: bool = True
    reuse_job_id: str | None = Field(default=None, description="merge the saved tile answers of an earlier job (no AI call)")
    retry_missing: bool = Field(default=False, description="with reuse_job_id: also send the tiles that job has no answer for")


class CalibrationBody(BaseModel):
    calibration: dict | None


def parse_scene(data: dict) -> Scene:
    try:
        return Scene.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "SCENE_INVALID", "The scene does not match the schema.", readable_validation(exc))


def blank_scene(name: str, brief: Brief) -> Scene:
    """One rectangular room from the brief's outer dimensions, as a starting point for manual tracing."""
    if not (brief.overall_width and brief.overall_depth):
        raise ApiError(400, "BRIEF_INCOMPLETE", "Enter the overall outer width and depth first.")
    t = 0.2
    w, d = round(brief.overall_width - t, 3), round(brief.overall_depth - t, 3)
    pts = [(0.0, 0.0), (w, 0.0), (w, d), (0.0, d)]
    walls = [Wall(id=f"W{i + 1}", level_id="L0", start=pts[i], end=pts[(i + 1) % 4], thickness=t, kind="exterior",
                  certainty=Certainty(status="confirmed", source="user", note="from brief outer dimensions"))
             for i in range(4)]
    return Scene(project=ProjectMeta(name=name, style=brief.style, prompt=brief.prompt),
                 levels=[Level(id="L0", name="Ground floor", wall_height=brief.wall_height)],
                 rooms=[Room(id="R1", name="Room 1", type="living", level_id="L0", polygon=pts)],
                 walls=walls, provenance=Provenance(source="manual", note="blank layout from brief"))


def create_app(settings: Settings | None = None, ai_transport=None) -> FastAPI:
    settings = settings or get_settings()
    install_log_redaction(settings)
    storage = Storage(settings.data_dir)
    jobs = JobManager(storage)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.shutdown()
        if getattr(app.state, "streamer", None) is not None:
            app.state.streamer.shutdown()   # streamed viewer processes + the signalling server

    app = FastAPI(title="Floor plan -> 3D digital twin", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.storage, app.state.jobs = settings, storage, jobs

    def budget(ctx: JobContext | None = None, purpose: str = "") -> BudgetGuard:
        return BudgetGuard(storage, settings.ai, project_id=ctx.project_id if ctx else None,
                           job_id=ctx.job_id if ctx else None, purpose=purpose)

    def ai_client(ctx: JobContext, purpose: str) -> AIClient:
        # every HTTP attempt is reserved against the spending cap and recorded with its cost
        return AIClient(settings.ai, transport=ai_transport, budget=budget(ctx, purpose))

    @app.exception_handler(HTTPException)
    async def http_error(_req: Request, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else \
            {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail), "detail": None}
        return JSONResponse({"error": body}, status_code=exc.status_code)

    @app.exception_handler(NotFound)
    async def not_found(_req: Request, exc: NotFound):
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": f"Not found: {exc.args[0]}", "detail": None}},
                            status_code=404)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def bad_request(_req: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()[:25]]
        return JSONResponse({"error": {"code": "REQUEST_INVALID", "message": "The request is invalid.", "detail": msgs}},
                            status_code=422)

    # ---- system ------------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        blender: dict = {"path": settings.blender_path, "available": False}
        if settings.blender_path and Path(settings.blender_path).is_file():
            try:
                blender.update(available=True, version=blender_version(settings.blender_path))
            except Exception as exc:  # noqa: BLE001
                blender["error"] = str(exc)
        else:
            blender["error"] = "Blender not found; set TWIN_BLENDER_PATH"
        ue_dir = settings.unreal_engine_dir
        unreal = {"engine_dir": ue_dir, "engine_found": bool(ue_dir and Path(ue_dir).is_dir()),
                  "project": str(settings.unreal_project), "project_found": settings.unreal_project.is_file(),
                  "module_built": (settings.unreal_project.parent / "Binaries" / "Win64" /
                                   f"UnrealEditor-{settings.unreal_project.stem}.dll").is_file()}
        return {"version": __version__, "ai": settings.ai.public(), "blender": blender, "unreal": unreal,
                "ai_usage_today": storage.ai_usage_today(), "ai_daily_limit": settings.ai.daily_request_limit,
                "ai_budget": budget().status(),
                "streaming": app.state.streamer.status() if getattr(app.state, "streamer", None) else None}

    @app.get("/api/catalog")
    def catalog() -> dict:
        cat = load_catalog()
        return {
            "catalog_version": cat.catalog_version,
            "assets": [a.model_dump() for a in cat.assets],
            "styles": {k: {"label": v["label"], "lighting": v["lighting"]} for k, v in STYLES.items()},
            "materials": [{"id": m.id, "name": m.name, "base_color": m.base_color, "pattern": m.pattern}
                          for m in BUILTIN_MATERIALS.values()],
            "room_types": list(Room.model_fields["type"].annotation.__args__),
            "sensor_kinds": [{"id": k, "label": v, "unit": KIND_UNITS[k], "color": KIND_COLORS[k], "prefix": TAG_PREFIX[k]}
                             for k, v in KIND_LABELS.items()],
        }

    @app.get("/api/schema/scene")
    def schema() -> dict:
        return scene_json_schema()

    # ---- projects ----------------------------------------------------------------------------
    @app.get("/api/projects")
    def projects() -> list[dict]:
        return storage.list_projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict:
        return storage.create_project(body.name, body.brief)

    @app.get("/api/projects/{pid}")
    def project(pid: str) -> dict:
        p = storage.get_project(pid)
        p["files"] = storage.list_files(pid)
        p["versions"] = storage.list_versions(pid)
        p["jobs"] = storage.list_jobs(pid, limit=15)
        p["builds"] = storage.list_builds(pid)
        return p

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch) -> dict:
        return storage.update_project(pid, name=body.name, brief=body.brief)

    # ---- uploads -----------------------------------------------------------------------------
    @app.post("/api/projects/{pid}/files", status_code=201)
    async def upload(pid: str, file: UploadFile = File(...), kind: Literal["plan", "reference", "hvac"] = Form(...),
                     page: int = Form(1)) -> dict:
        storage.get_project(pid)
        limit = settings.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise ApiError(413, "UPLOAD_TOO_LARGE", f"Files are limited to {settings.max_upload_mb} MB.")
        if not data:
            raise ApiError(400, "UPLOAD_EMPTY", "The uploaded file is empty.")
        fid = new_id("f")
        folder = storage.upload_dir(pid, fid)
        try:
            norm = normalize_upload(data, folder, page=max(0, page - 1))
        except UploadRejected as exc:
            raise ApiError(415, "UPLOAD_REJECTED", str(exc))
        (folder / f"original.{norm.kind}").write_bytes(data)
        record = storage.add_file(pid, fid, kind, file.filename or "upload", norm.kind, data, norm.width, norm.height,
                                  norm.pages, norm.page)
        if norm.kind == "pdf":
            # exact vector text + drawing scale: labels in metres for the AI, automatic underlay calibration
            try:
                info = extract_pdf_text(data, norm.page)
            except Exception as exc:  # noqa: BLE001 - a broken text layer must not block the upload
                log.warning("PDF text extraction failed for %s: %s", fid, exc)
                info = None
            if info is not None:
                (folder / "pdf_text.json").write_text(json.dumps(info), encoding="utf-8")
                cal = calibration_for(info, norm.width) if kind == "plan" else None
                if cal:
                    record = storage.set_calibration(pid, fid, cal)
                record["pdf_scale"] = info["scale"]
                record["pdf_labels"] = info["label_count"]
        return record

    @app.get("/api/projects/{pid}/files/{fid}/{variant}")
    def file_content(pid: str, fid: str, variant: Literal["original", "normalized", "thumb"]):
        meta = storage.get_file(pid, fid)
        folder = storage.upload_dir(pid, fid)
        path = folder / (f"original.{meta['sniffed']}" if variant == "original" else f"{variant}.png")
        if not path.is_file():
            raise ApiError(404, "FILE_MISSING", "The stored file is missing.")
        return FileResponse(path, filename=meta["original_name"] if variant == "original" else None)

    @app.put("/api/projects/{pid}/files/{fid}/calibration")
    def calibration(pid: str, fid: str, body: CalibrationBody) -> dict:
        return storage.set_calibration(pid, fid, body.calibration)

    # ---- scene versions ----------------------------------------------------------------------
    def store_version(pid: str, scene: Scene, source: str, note: str, parent: int | None) -> dict:
        report = validate_scene(scene, storage.brief(pid))
        changes = None
        if parent is not None:
            try:
                old = storage.load_scene(pid, parent)
                changes = {**change_summary(old, scene), "sensors": layer_changes(old, scene)}
            except NotFound:
                pass
        meta = storage.add_version(pid, scene, source=source, note=note, parent=parent,
                                   validation=report.model_dump(mode="json"), scene_hash=scene_hash(scene),
                                   changes=changes)
        return {**meta, "scene": scene.model_dump(mode="json")}

    @app.get("/api/projects/{pid}/versions/{number}/changes")
    def version_changes(pid: str, number: int, against: int | None = None) -> dict:
        """What changed from version `against` (default: the parent) to `number`: per-collection ids, counts,
        the sensor layer changes, and whether the building itself changed."""
        meta = storage.get_version_meta(pid, number)
        other = against if against is not None else meta["parent"]
        if other is None:
            raise ApiError(404, "NO_PARENT", f"v{number} has no parent version; pass ?against=<version>.")
        a, b = storage.load_scene(pid, other), storage.load_scene(pid, number)
        return {"from": other, "to": number, **change_summary(a, b), "sensors": layer_changes(a, b),
                "detail": diff_scenes(a, b)}

    @app.get("/api/projects/{pid}/versions")
    def versions(pid: str) -> list[dict]:
        storage.get_project(pid)
        return storage.list_versions(pid)

    @app.get("/api/projects/{pid}/versions/{number}")
    def version(pid: str, number: int) -> dict:
        meta = storage.get_version_meta(pid, number)
        return {**meta, "scene": storage.load_scene(pid, number).model_dump(mode="json")}

    @app.post("/api/projects/{pid}/versions", status_code=201)
    def create_version(pid: str, body: VersionCreate) -> dict:
        storage.get_project(pid)
        scene = parse_scene(body.scene)
        if body.parent is not None:
            storage.get_version_meta(pid, body.parent)
        return store_version(pid, scene, body.source, body.note or "edited in review", body.parent)

    @app.post("/api/projects/{pid}/versions/fixture", status_code=201)
    def from_fixture(pid: str) -> dict:
        p = storage.get_project(pid)
        scene = load_sample_apartment()
        scene = scene.model_copy(update={"project": scene.project.model_copy(update={"name": p["name"]})})
        return store_version(pid, scene, "fixture", "sample apartment fixture (known measurements)", None)

    @app.post("/api/projects/{pid}/versions/blank", status_code=201)
    def from_blank(pid: str) -> dict:
        p = storage.get_project(pid)
        return store_version(pid, blank_scene(p["name"], storage.brief(pid)), "manual",
                             "blank layout from the brief's outer dimensions", None)

    # ---- stateless scene tools ---------------------------------------------------------------
    @app.post("/api/scene/validate")
    def validate(body: SceneBody) -> dict:
        return validate_scene(parse_scene(body.scene), body.brief).model_dump(mode="json")

    @app.post("/api/scene/derive-walls")
    def derive_walls(body: DeriveBody) -> dict:
        scene, notes = derive.derive_walls_from_rooms(parse_scene(body.scene), body.exterior_thickness,
                                                      body.interior_thickness)
        return {"scene": scene.model_dump(mode="json"), "notes": notes}

    @app.post("/api/scene/fit-furniture")
    def fit_furniture(body: FitFurnitureBody) -> dict:
        try:
            scene, shift = derive.fit_furniture_into_room(parse_scene(body.scene), body.furniture_id)
        except (ValueError, StopIteration) as exc:
            raise ApiError(422, "CANNOT_FIT", str(exc) or "unknown furniture id")
        return {"scene": scene.model_dump(mode="json"), "shift_m": round(shift, 3)}

    @app.post("/api/scene/fit-overall")
    def fit_overall(body: FitOverallBody) -> dict:
        try:
            scene = derive.fit_to_overall(parse_scene(body.scene), body.width, body.depth)
        except ValueError as exc:
            raise ApiError(422, "CANNOT_FIT", str(exc))
        return {"scene": scene.model_dump(mode="json")}

    @app.post("/api/scene/accept-estimates")
    def accept_estimates(body: SceneBody) -> dict:
        """Mark every remaining estimated/extracted value as a knowingly accepted estimate."""
        data = parse_scene(body.scene).model_dump(mode="json")
        changed = 0
        groups = [data[c] for c in ("levels", "rooms", "walls", "openings", "furniture", "sensors")]
        groups += [data["hvac"][c] for c in ("units", "terminals", "ducts")]
        for coll in groups:
            for el in coll:
                if el["certainty"]["status"] in NEEDS_REVIEW_STATUSES:
                    el["certainty"]["status"] = "accepted_estimate"
                    changed += 1
        return {"scene": data, "changed": changed}

    @app.post("/api/scene/import-sensors")
    def import_sensors(body: SensorSheetBody) -> dict:
        """Merge a CSV sensor placement sheet into the scene (not saved: the editor shows the result as
        unsaved edits to check before saving a new version)."""
        scene, info = import_sheet(parse_scene(body.scene), body.text)
        return {"scene": scene.model_dump(mode="json"), **info}

    @app.get("/api/sensors/template.csv", include_in_schema=False)
    def sensor_template():
        return Response(SHEET_TEMPLATE, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="sensor_placement_template.csv"'})

    # ---- AI jobs -----------------------------------------------------------------------------
    def require_ai() -> None:
        if not settings.ai.configured:
            raise ApiError(503, "AI_NOT_CONFIGURED",
                           "AI interpretation is not configured yet. Set TWIN_AI_API_KEY and TWIN_AI_MODEL "
                           "(see .env.example), restart the server, and run `python -m twin.ai.probe`.")
        used = storage.ai_usage_today()["calls"]
        if used >= settings.ai.daily_request_limit:
            raise ApiError(429, "AI_LIMIT_REACHED",
                           f"The daily AI request limit ({settings.ai.daily_request_limit}) has been reached.")
        b = budget().status()
        if b["cap_usd"] is not None and not b["prices_configured"]:
            raise ApiError(503, "AI_BUDGET_PRICES_MISSING",
                           f"A ${b['cap_usd']:.2f} spending cap is set but the model prices are not "
                           "(TWIN_AI_PRICE_INPUT_PER_1M / TWIN_AI_PRICE_OUTPUT_PER_1M), so spend cannot be enforced.")
        if b["cap_usd"] is not None and b["remaining_usd"] <= 0:
            raise ApiError(402, "AI_BUDGET_EXCEEDED",
                           f"The AI spending cap of ${b['cap_usd']:.2f} has been reached "
                           f"(${b['spent_usd']:.4f} spent). Raise TWIN_AI_BUDGET_USD to continue.", b)

    def file_path(pid: str, fid: str) -> tuple[Path, dict]:
        meta = storage.get_file(pid, fid)
        return storage.upload_dir(pid, fid) / "normalized.png", meta

    @app.post("/api/projects/{pid}/interpret", status_code=202)
    def interpret(pid: str, body: InterpretBody) -> dict:
        p = storage.get_project(pid)
        require_ai()
        plan_path = plan_meta = None
        if body.plan_file_id:
            plan_path, plan_meta = file_path(pid, body.plan_file_id)
        refs = [file_path(pid, f)[0] for f in body.reference_file_ids]
        brief = storage.brief(pid)

        def run(ctx: JobContext) -> tuple[str, dict]:
            ctx.progress(0.1, "sending plan to the AI model")
            client = ai_client(ctx, "interpret")
            info = None
            if plan_meta:
                info = {"kind": plan_meta["sniffed"]}
                sheet_file = storage.upload_dir(pid, plan_meta["id"]) / "pdf_text.json"
                if sheet_file.is_file():
                    info["pdf_text"] = json.loads(sheet_file.read_text(encoding="utf-8"))
            raw_dir = storage.project_dir(pid) / "ai_responses"

            def keep(raw: dict) -> None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{ctx.job_id}.json").write_text(json.dumps(raw), encoding="utf-8")

            out = interpret_plan(client, brief, p["name"], plan_path, info, refs, raw_sink=keep)
            ctx.progress(0.9, "validating interpretation")
            v = store_version(pid, out.scene, "ai", f"AI interpretation ({out.result.model})", None)
            return "needs_review", {"version": v["number"], "summary": out.interpretation.get("summary", ""),
                                    "notes": out.notes, "usage": out.result.usage,
                                    "cost_usd": round(client.last_cost_usd or 0.0, 6),
                                    "open_questions": len(out.scene.open_questions),
                                    "can_generate": v["validation"]["can_generate"]}

        return jobs.submit(pid, "interpret", body.model_dump(), run)

    @app.post("/api/projects/{pid}/ai-responses/{job_id}/reprocess", status_code=201)
    def reprocess(pid: str, job_id: str) -> dict:
        """Re-validate and convert a saved AI answer (e.g. after a validator fix) without paying again."""
        p = storage.get_project(pid)
        raw_file = storage.project_dir(pid) / "ai_responses" / f"{job_id}.json"
        if not job_id.startswith("j_") or not raw_file.is_file():
            raise ApiError(404, "NOT_FOUND", f"no saved AI response for job {job_id}")
        raw = json.loads(raw_file.read_text(encoding="utf-8"))
        result = AIResult(raw["data"], "", raw.get("model") or "", raw.get("request_id"), raw.get("usage") or {})
        try:
            out = outcome_from_response(raw["data"], storage.brief(pid), p["name"], result, raw.get("notes"))
        except AIError as exc:
            raise ApiError(422, exc.code, str(exc), exc.detail)
        v = store_version(pid, out.scene, "ai", f"AI interpretation ({result.model}), reprocessed from {job_id}", None)
        return {"version": v["number"], "notes": out.notes, "can_generate": v["validation"]["can_generate"],
                "summary": out.interpretation.get("summary", "")}

    @app.post("/api/projects/{pid}/versions/{number}/rooms/{room_id}/redesign", status_code=202)
    def redesign(pid: str, number: int, room_id: str, body: RedesignBody) -> dict:
        scene = storage.load_scene(pid, number)
        if room_id not in {r.id for r in scene.rooms}:
            raise ApiError(404, "NOT_FOUND", f"room {room_id} not in version {number}")
        require_ai()
        refs = [file_path(pid, f)[0] for f in body.reference_file_ids]

        def run(ctx: JobContext) -> tuple[str, dict]:
            ctx.progress(0.1, f"asking the AI to redesign {scene.room(room_id).name}")
            client = ai_client(ctx, "redesign")
            updated, result, info = redesign_room(client, scene, room_id, body.prompt, refs)
            v = store_version(pid, updated, "ai_redesign", f"AI redesign of {scene.room(room_id).name}", number)
            return "needs_review", {"version": v["number"], "room_id": room_id, **info, "usage": result.usage,
                                    "cost_usd": round(client.last_cost_usd or 0.0, 6)}

        return jobs.submit(pid, "redesign", {"room_id": room_id, **body.model_dump()}, run, version=number)

    @app.post("/api/projects/{pid}/versions/{number}/fix", status_code=202)
    def fix(pid: str, number: int, body: FixBody) -> dict:
        """'Fix with AI': plain-language correction of the model -> edit operations -> new version to review."""
        scene = storage.load_scene(pid, number)
        require_ai()
        plan_img = None
        if body.include_plan:
            plans = [f for f in storage.list_files(pid) if f["kind"] == "plan"]
            if plans:
                plan_img = storage.upload_dir(pid, plans[-1]["id"]) / "normalized.png"

        def run(ctx: JobContext) -> tuple[str, dict]:
            ctx.progress(0.1, "asking the AI to fix the model")
            client = ai_client(ctx, "fix")
            raw_dir = storage.project_dir(pid) / "ai_responses"

            def keep(raw: dict) -> None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{ctx.job_id}_fix{raw['attempt']}.json").write_text(json.dumps(raw), encoding="utf-8")

            from twin.ai.fix import fix_scene
            out = fix_scene(client, scene, body.prompt, body.selection, plan_img, raw_sink=keep)
            usage = [r.usage for r in out.results]
            cost = round(client.last_cost_usd or 0.0, 6)
            if not out.changes:
                return "completed", {"version": None, "summary": out.summary, "questions": out.questions,
                                     "changes": [], "usage": usage, "cost_usd": cost}
            ctx.progress(0.9, "validating the fixed model")
            v = store_version(pid, out.scene, "ai_fix", f"AI fix: {body.prompt[:200]}", number)
            return "needs_review", {"version": v["number"], "summary": out.summary, "questions": out.questions,
                                    "changes": out.changes, "usage": usage, "cost_usd": cost,
                                    "can_generate": v["validation"]["can_generate"]}

        return jobs.submit(pid, "fix", body.model_dump(), run, version=number)

    @app.get("/api/projects/{pid}/versions/{number}/detect-furniture/estimate")
    def detect_furniture_estimate(pid: str, number: int) -> dict:
        """Tiles and expected cost of a furniture detection run (measured: ~8k input, ~5k output tokens/tile)."""
        from twin.ai.furniture import EXPECTED_TILE_TOKENS, plan_sheet, plan_tiles
        scene = storage.load_scene(pid, number)
        try:
            plan_sheet(storage, pid)
            tiles = len(plan_tiles(scene))
        except AIError as exc:
            return {"available": False, "reason": str(exc), "tiles": 0, "est_usd": None}
        ai = settings.ai
        est = None
        if ai.price_input_per_1m is not None and ai.price_output_per_1m is not None:
            ti, to = EXPECTED_TILE_TOKENS
            est = round(tiles * (ti * 1.25 * ai.price_input_per_1m + to * ai.price_output_per_1m) / 1e6, 2)
        return {"available": True, "tiles": tiles, "est_usd": est, "model": ai.model,
                "remaining_usd": budget().status().get("remaining_usd"), "furniture_now": len(scene.furniture)}

    @app.post("/api/projects/{pid}/versions/{number}/detect-furniture", status_code=202)
    def detect_furniture_job(pid: str, number: int, body: FurnitureBody) -> dict:
        """Detect every drawn furniture / equipment item on the vector plan (tiled AI pass) -> new version."""
        from twin.ai.furniture import FurnitureOutcome, detect_furniture, furniture_scene, merge_tiles, plan_sheet
        scene = storage.load_scene(pid, number)
        raw_dir = storage.project_dir(pid) / "ai_responses"
        saved = None
        if body.reuse_job_id:
            saved = raw_dir / f"{body.reuse_job_id}_tiles.json"
            if not body.reuse_job_id.startswith("j_") or not saved.is_file():
                raise ApiError(404, "NOT_FOUND", f"no saved furniture answers for job {body.reuse_job_id}")
        if saved is None or body.retry_missing:
            require_ai()
        try:
            pdf, sheet = plan_sheet(storage, pid, body.file_id)
        except AIError as exc:
            raise ApiError(409, exc.code, str(exc))

        def run(ctx: JobContext) -> tuple[str, dict]:
            room_ids = {r.id for r in scene.rooms}
            spent0 = budget().spent()
            previous = json.loads(saved.read_text(encoding="utf-8")) if saved is not None else None
            if previous is not None and not body.retry_missing:
                ctx.progress(0.3, "merging saved tile answers")
                out: FurnitureOutcome = merge_tiles(previous, room_ids)
            else:
                from dataclasses import replace as dc_replace
                ai = dc_replace(settings.ai, reasoning_effort=settings.ai.furniture_effort or settings.ai.reasoning_effort)
                client = AIClient(ai, transport=ai_transport, budget=BudgetGuard(
                    storage, ai, project_id=pid, job_id=ctx.job_id, purpose="furniture"))
                raw_dir.mkdir(parents=True, exist_ok=True)
                answers: list[dict] = list(previous or [])
                lock = threading.Lock()

                def keep(entry: dict) -> None:   # every paid tile answer is kept, even if a later tile fails
                    with lock:
                        answers.append(entry)
                        (raw_dir / f"{ctx.job_id}_tiles.json").write_text(json.dumps(answers), encoding="utf-8")

                ctx.progress(0.02, "rendering the plan in tiles")
                out = detect_furniture(client, scene, pdf, sheet, raw_sink=keep, previous=previous,
                                       progress=lambda f, stage: ctx.progress(0.03 + 0.9 * f, stage))
            ctx.progress(0.95, "placing the furniture")
            new_scene, info = furniture_scene(scene, out, keep_user_items=body.keep_user_items)
            cost = round(max(0.0, budget().spent() - spent0), 4)
            v = store_version(pid, new_scene, "ai_furniture",
                              f"AI furniture detection: {info['items']} items in {out.tiles or len(out.raw)} tiles", number)
            return "needs_review", {"version": v["number"], **info, "tiles": out.tiles or len(out.raw),
                                    "missing_tiles": out.missing_tiles, "notes": out.notes[:40], "cost_usd": cost,
                                    "can_generate": v["validation"]["can_generate"]}

        return jobs.submit(pid, "furniture", body.model_dump(), run, version=number)

    # ---- builds ------------------------------------------------------------------------------
    geometry_keys: dict[str, str | None] = {}   # build id -> geometry key of its scene (builds are immutable)
    geometry_lock = threading.Lock()

    def build_scene(pid: str, bid: str) -> Scene | None:
        path = storage.build_dir(pid, bid) / "scene.json"
        try:
            return Scene.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return None

    def build_geometry_key(pid: str, bid: str) -> str | None:
        with geometry_lock:
            if bid not in geometry_keys:
                s = build_scene(pid, bid)
                geometry_keys[bid] = geometry_key(s) if s is not None else None
            return geometry_keys[bid]

    def layer_base(pid: str, scene: Scene, preferred: str | None = None) -> dict | None:
        """Newest completed build with exactly this building (its model can take the new layers as is)."""
        key = geometry_key(scene)
        for b in storage.list_builds(pid):
            if b["status"] != "completed" or (preferred and b["id"] != preferred):
                continue
            if build_geometry_key(pid, b["id"]) == key:
                return b
        return None

    @app.get("/api/projects/{pid}/versions/{number}/build-options")
    def build_options(pid: str, number: int) -> dict:
        """Can this version update a saved model in place (only layers such as sensors changed), or does the
        building have to be generated again? Shown before the user starts a build."""
        scene = storage.load_scene(pid, number)
        meta = storage.get_version_meta(pid, number)
        base = layer_base(pid, scene)
        completed = [b for b in storage.list_builds(pid) if b["status"] == "completed"]
        out: dict[str, Any] = {"version": number, "can_generate": meta["validation"].get("can_generate"),
                               "saved_builds": len(completed)}
        brief_b = lambda b: {k: b.get(k) for k in ("id", "version", "kind", "finished_at")}  # noqa: E731
        if base is not None:
            old = build_scene(pid, base["id"])
            ch = layer_changes(old, scene)
            out["layer"] = {"possible": True, "base_build": brief_b(base), "changes": ch,
                            "up_to_date": ch["changed"] == 0 and base["version"] == number}
        else:
            reason = "No saved model yet: generate the building once with Blender."
            detail = None
            if completed:
                latest = completed[0]
                old = build_scene(pid, latest["id"])
                if old is not None:
                    detail = change_summary(old, scene)
                    parts = [f"{k} {', '.join(f'{n} {w}' for w, n in v.items() if n)}"
                             for k, v in detail["counts"].items() if k != "sensors" and any(v.values())]
                    parts += detail["sections"]
                    reason = (f"The building changed since the saved build {latest['id']} (v{latest['version']})"
                              + (f": {'; '.join(parts)}" if parts else "") + ". It has to be generated again.")
            out["layer"] = {"possible": False, "reason": reason, "changes_vs_latest": detail}
        return out

    def start_layer_build(pid: str, number: int, scene: Scene, base: dict) -> dict:
        job = jobs.create(pid, "layer_build", {"mode": "layer", "base_build": base["id"]}, number)
        bid = storage.create_build(pid, number, job["id"], base["id"], kind="layer")["id"]

        def run(ctx: JobContext) -> tuple[str, dict]:
            storage.update_build(bid, status="processing")
            try:
                man = run_layer_build(scene, storage.build_dir(pid, bid), build_id=bid,
                                      base_dir=storage.build_dir(pid, base["id"]), base_id=base["id"],
                                      base_version=base["version"], on_progress=ctx.progress)
            except Exception:
                storage.update_build(bid, status="failed")
                raise
            summary = {"kind": "layer", "base_build": base["id"], "base_version": base["version"],
                       "geometry_build": man["geometry_build"], "sensors": len(man["sensors"]),
                       "changes": man["layers"]["sensors"]["changes"], "timings": man["timings"],
                       "rooms": len(man["rooms"]), "furniture": len(man["furniture"]), "exports": len(man["exports"]),
                       "reused": len(man["exports"]), "previews": man["files"]["previews"]}
            storage.update_build(bid, status="completed", summary=summary)
            storage.update_project(pid, status="generated")
            return "completed", {"build_id": bid, **summary}

        jobs.start(job, run)
        return {**job, "build_id": bid, "build_kind": "layer"}

    @app.post("/api/projects/{pid}/versions/{number}/build", status_code=202)
    def build(pid: str, number: int, body: BuildBody) -> dict:
        scene = storage.load_scene(pid, number)
        report = validate_scene(scene, storage.brief(pid))
        if not report.can_generate:
            raise ApiError(409, "SCENE_NOT_READY", "Fix errors and confirm or accept estimated measurements first.",
                           [i.model_dump() for i in report.issues if i.severity in ("error", "review")][:50])
        if body.mode in ("layer", "auto"):
            base = layer_base(pid, scene, body.base_build)
            if base is not None:
                return start_layer_build(pid, number, scene, base)
            if body.mode == "layer":
                opts = build_options(pid, number)
                raise ApiError(409, "LAYER_UPDATE_NOT_POSSIBLE", opts["layer"]["reason"], opts["layer"])
        prev = storage.latest_completed_build(pid) if body.reuse_previous else None
        job = jobs.create(pid, "build", body.model_dump(), number)
        build_row = storage.create_build(pid, number, job["id"], prev["id"] if prev else None)
        bid = build_row["id"]

        def run(ctx: JobContext) -> tuple[str, dict]:
            storage.update_build(bid, status="processing")
            prev_dir = storage.build_dir(pid, prev["id"]) if prev else None
            try:
                res = run_build(scene, storage.build_dir(pid, bid), settings, build_id=bid,
                                previous_build=prev_dir, render=body.render_previews, on_progress=ctx.progress)
            except Exception:
                storage.update_build(bid, status="failed")
                raise
            summary = {"kind": "full", "timings": res.timings, "reused": len(res.reused),
                       "exports": len(res.manifest["exports"]), "rooms": len(res.manifest["rooms"]),
                       "furniture": len(res.manifest["furniture"]), "sensors": len(res.manifest["sensors"]),
                       "previews": res.manifest["files"]["previews"]}
            storage.update_build(bid, status="completed", summary=summary)
            storage.update_project(pid, status="generated")
            return "completed", {"build_id": bid, **summary}

        jobs.start(job, run)
        return {**job, "build_id": bid}

    @app.get("/api/projects/{pid}/builds/{bid}")
    def build_info(pid: str, bid: str) -> dict:
        b = storage.get_build(pid, bid)
        folder = storage.build_dir(pid, bid)
        man = folder / "manifest.json"
        b["manifest"] = None
        if b["status"] == "completed" and man.is_file():
            b["manifest"] = json.loads(man.read_text(encoding="utf-8"))
        b["files"] = sorted(
            [{"path": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size}
             for p in folder.rglob("*") if p.is_file() and not p.name.endswith(".tmp")],
            key=lambda f: f["path"]) if folder.is_dir() else []
        return b

    def safe_build_path(pid: str, bid: str, rel: str) -> Path:
        folder = storage.build_dir(pid, bid).resolve()
        path = (folder / rel).resolve()
        if folder not in path.parents or not path.is_file():
            raise ApiError(404, "FILE_MISSING", f"{rel} is not part of this build")
        return path

    @app.get("/api/projects/{pid}/builds/{bid}/files/{rel:path}")
    def build_file(pid: str, bid: str, rel: str, download: bool = False):
        path = safe_build_path(pid, bid, rel)
        return FileResponse(path, filename=path.name if download else None)

    @app.get("/api/projects/{pid}/builds/{bid}/bundle/{bundle}")
    def bundle(pid: str, bid: str, bundle: Literal["unreal", "all"]):
        b = storage.get_build(pid, bid)
        if b["status"] != "completed":
            raise ApiError(409, "BUILD_NOT_COMPLETED", "This build has not completed; there is nothing to download.")
        folder = storage.build_dir(pid, bid)
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(folder.rglob("*")):
                rel = p.relative_to(folder).as_posix()
                if not p.is_file() or rel.endswith((".tmp", ".log")):
                    continue
                if bundle == "unreal" and not (rel.startswith("export/unreal/") or rel in
                                               ("manifest.json", "plan.json", "scene.json", "export/fbx/scene_full.fbx")):
                    continue
                z.write(p, rel)
        tmp.close()
        return FileResponse(tmp.name, filename=f"{pid}_{bid}_{bundle}.zip", media_type="application/zip",
                            background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))

    # ---- jobs & usage --------------------------------------------------------------------------
    @app.get("/api/jobs/{jid}")
    def job(jid: str) -> dict:
        return storage.get_job(jid)

    @app.get("/api/projects/{pid}/jobs")
    def project_jobs(pid: str) -> list[dict]:
        storage.get_project(pid)
        return storage.list_jobs(pid)

    @app.get("/api/usage")
    def usage() -> dict:
        return {"today": storage.ai_usage_today(), "daily_limit": settings.ai.daily_request_limit,
                "budget": budget().status()}

    # ---- HVAC drawing import, live data, airflow model ----------------------------------------
    from twin.hvac.routes import register as register_hvac
    register_hvac(app, settings, storage, jobs, store_version, ApiError)

    # ---- read-only API of the packaged Unreal viewer (token-protected) -------------------------
    from twin.viewer.routes import register as register_viewer
    register_viewer(app, settings, storage)

    # ---- local Pixel Streaming test: the packaged viewer streamed into the web app (off by default) ------
    from twin.stream.routes import register as register_stream
    app.state.streamer = register_stream(app, settings, storage, ApiError)

    # ---- unreal (registered by twin.unreal.routes when available) ----------------------------
    try:
        from twin.unreal.routes import register as register_unreal
        register_unreal(app, settings, storage, jobs)
    except ImportError:
        pass

    # ---- frontend ----------------------------------------------------------------------------
    if FRONTEND_DIR.is_dir():
        three = FRONTEND_DIR / "node_modules" / "three"
        if three.is_dir():
            app.mount("/vendor/three", StaticFiles(directory=three), name="three")
        app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/app/")

    return app
