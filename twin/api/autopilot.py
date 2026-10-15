"""Autopilot: one click from uploaded drawings to a built, verified 3D model.

The steps are the jobs a user would otherwise start one by one. They run in order as child jobs, so each still shows
up in Activity and saves its own version; nothing is hidden or overwritten:

    layout          the AI reads the plan (skipped when the project already has a layout)
    furniture_list  an uploaded furniture list (CSV) becomes the furniture
    hvac            the HVAC drawing is placed on the layout (when it has none yet)
    sensor_sheet    an uploaded sensor placement sheet (CSV) places the sensors
    furniture       the drawn furniture is read from a vector PDF plan (when it fits the cost limit)
    fix             free rules, then the AI problem solver
    accept          estimated sizes accepted, non-blocking AI questions answered "as drawn"
    build           Blender builds and verifies the model (or the saved model is updated in place)

Review moves from before the build to after it: the finished model comes with the list of what was assumed (and the
build manifest records every accepted estimate, as for a manual "Accept all"). With ``review_first`` the run stops
after "fix" so the layout is checked before anything is accepted or built. A run stops early only for what it cannot
decide: a blocking AI question, errors no fix could solve, a missing tool, a failed required step, or the user.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from twin.api.jobs import JobContext, JobManager
from twin.api.storage import Storage
from twin.config import Settings
from twin.scene.furniture_sheet import import_furniture_sheet
from twin.scene.model import Scene
from twin.scene.review import accept_estimates, accept_open_questions
from twin.scene.sensors import import_sheet

LABELS = {"layout": "Read the plan", "furniture_list": "Place the furniture list", "hvac": "Place the HVAC drawing",
          "sensor_sheet": "Place the sensors",
          "furniture": "Read the drawn furniture", "fix": "Fix problems", "accept": "Accept the estimates",
          "build": "Build the 3D model"}
POLL_S = 0.5


class AutopilotBody(BaseModel):
    review_first: bool = Field(default=False, description="stop after fixing problems: the user checks the layout "
                                                          "before anything is accepted or built")
    furniture: Literal["auto", "detect", "skip"] = Field(
        default="auto", description="read the drawn furniture from a vector PDF plan: auto = when the estimate is "
                                    "within furniture_max_usd, detect = always, skip = never")
    furniture_max_usd: float = Field(default=1.0, ge=0, le=50)
    use_ai: bool = Field(default=True, description="send what the free rules cannot fix to the AI problem solver")
    fresh: bool = Field(default=False, description="read the plan again even when the project has a layout")
    from_version: int | None = Field(default=None, description="start from this version (default: the newest)")
    plan_file_id: str | None = None
    reference_file_ids: list[str] | None = Field(default=None, max_length=8, description="None = every reference image")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class AutopilotFailed(Exception):
    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code, self.detail = code, detail


class Stopped(Exception):
    """The user pressed Stop."""


@dataclass
class Actions:
    """What the autopilot can start: the same functions the API routes use (each submits its own job)."""
    interpret: Callable[[str, str | None, list[str]], dict]
    detect_furniture: Callable[[str, int], dict]
    furniture_estimate: Callable[[str, int], dict]
    hvac_import: Callable[[str, int, str], dict]
    solve: Callable[[str, int, bool], dict]
    build: Callable[[str, int], dict]
    store_version: Callable[..., dict]
    require_ai: Callable[[], None]
    ai_spent: Callable[[], float]
    blender_ok: Callable[[], bool]


def api_message(exc: HTTPException) -> tuple[str, str]:
    d = exc.detail if isinstance(exc.detail, dict) else {}
    return d.get("code") or f"HTTP_{exc.status_code}", d.get("message") or str(exc.detail)


def vector_plan(storage: Storage, pid: str) -> bool:
    for f in storage.list_files(pid):
        info = storage.upload_dir(pid, f["id"]) / "pdf_text.json"
        if f["kind"] == "plan" and f["sniffed"] == "pdf" and info.is_file():
            try:
                if json.loads(info.read_text(encoding="utf-8")).get("m_per_pt"):
                    return True
            except ValueError:
                pass
    return False


def register(app: FastAPI, settings: Settings, storage: Storage, jobs: JobManager, actions: Actions,
             api_error: type) -> None:
    cancelled: set[str] = set()
    lock = threading.Lock()

    def plan_steps(pid: str, body: AutopilotBody) -> dict:
        """The steps a run would take, with the reason for each one it leaves out (shown before starting)."""
        p = storage.get_project(pid)
        files = storage.list_files(pid)
        versions = storage.list_versions(pid)
        kinds = {k: [f for f in files if f["kind"] == k] for k in ("plan", "hvac", "furniture_list", "sensor_sheet")}
        need_layout = body.fresh or not versions
        ai_on = settings.ai.configured
        vector = vector_plan(storage, pid)
        steps: list[dict] = []
        blocked = None

        def add(key: str, run: bool, note: str = "") -> None:
            steps.append({"key": key, "label": LABELS[key], "run": run, "note": note})

        if need_layout:
            if not kinds["plan"] and not p["brief"].get("prompt"):
                blocked = "Add the floor plan (PNG, JPG or PDF) first."
            elif not ai_on:
                blocked = "Reading a plan needs the AI, which is not set up on this server."
            add("layout", True, "the AI reads the plan, your sizes and references")
        sheet_used = any(v["source"] == "furniture_list" for v in versions)
        if kinds["furniture_list"] and (need_layout or not sheet_used):
            add("furniture_list", True, kinds["furniture_list"][-1]["original_name"])
        if kinds["hvac"] and (need_layout or not any(v["source"] == "hvac" for v in versions)):   # once; later: Import
            add("hvac", vector, kinds["hvac"][-1]["original_name"] if vector else
                "needs the floor plan as a vector PDF with a printed scale")
        if kinds["sensor_sheet"] and (need_layout or not any(v["source"] == "sensor_sheet" for v in versions)):
            add("sensor_sheet", True, kinds["sensor_sheet"][-1]["original_name"])
        if need_layout and not kinds["furniture_list"]:
            if body.furniture == "skip":
                add("furniture", False, "switched off")
            elif not vector:
                add("furniture", False, "only for vector PDF plans; the AI's furniture is kept")
            elif not ai_on:
                add("furniture", False, "needs the AI")
            else:
                add("furniture", True, "if it costs no more than $%.2f" % body.furniture_max_usd
                    if body.furniture == "auto" else "")
        add("fix", True, "free rules" + (", then the AI for the rest" if body.use_ai and ai_on else ""))
        if not body.review_first:
            add("accept", True, "every estimate is listed for you to check afterwards")
            add("build", actions.blender_ok(), "" if actions.blender_ok() else "Blender is not installed: the run "
                                                                                "stops with the layout ready")
        else:
            steps.append({"key": "review", "label": "You check the layout", "run": True,
                          "note": "then build it, or press Finish automatically"})
        cap = settings.ai.budget_usd
        return {"steps": steps, "blocked": blocked, "ai_configured": ai_on, "blender": actions.blender_ok(),
                "budget_remaining_usd": None if cap is None else round(max(0.0, cap - actions.ai_spent()), 2)}

    class Run:
        def __init__(self, ctx: JobContext, pid: str, body: AutopilotBody, steps: list[dict]) -> None:
            self.ctx, self.pid, self.body = ctx, pid, body
            self.steps = [{**s, "status": "pending" if s["run"] else "skipped"} for s in steps if s["key"] != "review"]
            self.todo = [s for s in self.steps if s["status"] == "pending"]
            self.number: int | None = None
            self.out: dict[str, Any] = {"steps": self.steps, "version": None, "build_id": None, "assumptions": [],
                                        "questions": [], "remaining": [], "stopped": None, "cost_usd": 0.0}
            self.spent0 = actions.ai_spent()

        def step(self, key: str) -> dict | None:
            return next((s for s in self.steps if s["key"] == key), None)

        def publish(self) -> None:
            self.out["version"] = self.number
            self.out["cost_usd"] = round(max(0.0, actions.ai_spent() - self.spent0), 4)
            storage.update_job(self.ctx.job_id, result=self.out)

        def mark(self, key: str, status: str, **kw: Any) -> None:
            s = self.step(key)
            if s is not None:
                s.update(status=status, **kw)
                self.publish()

        def check(self) -> None:
            with lock:
                if self.ctx.job_id in cancelled:
                    raise Stopped()

        def frac(self, key: str, inner: float = 0.0) -> float:
            idx = next((i for i, s in enumerate(self.todo) if s["key"] == key), 0)
            return (idx + max(0.0, min(1.0, inner))) / max(1, len(self.todo))

        def wait(self, key: str, job: dict) -> dict:
            """Follow a child job to its end, mirroring its progress into this run."""
            self.mark(key, "running", job_id=job["id"])
            label = LABELS[key]
            while True:
                self.check()
                j = storage.get_job(job["id"])
                stage = j.get("stage") or j["status"]
                self.ctx.progress(self.frac(key, j.get("progress") or 0.0), f"{label}: {stage}")
                if j["status"] in ("completed", "needs_review", "failed"):
                    return j
                time.sleep(POLL_S)

        def start(self, key: str, fn: Callable[[], dict]) -> dict | None:
            """Start a child job; an API refusal skips the step with its reason (returns None)."""
            self.check()
            self.ctx.progress(self.frac(key), LABELS[key])
            try:
                return fn()
            except HTTPException as exc:
                code, msg = api_message(exc)
                self.mark(key, "skipped", note=msg, code=code)
                return None

    def run_autopilot(ctx: JobContext, pid: str, body: AutopilotBody, plan: dict, start: int | None) -> tuple[str, dict]:
        r = Run(ctx, pid, body, plan["steps"])
        r.number = start
        r.publish()
        try:
            return go(r)
        except Stopped:
            for s in r.steps:
                if s["status"] in ("pending", "running"):
                    s["status"] = "stopped"
            r.out["stopped"] = {"code": "STOPPED", "message": "Stopped by you. Everything finished so far is saved."}
            r.publish()
            return "needs_review", r.out
        finally:
            with lock:
                cancelled.discard(ctx.job_id)

    def stop_for_user(r: Run, code: str, message: str) -> tuple[str, dict]:
        for s in r.steps:
            if s["status"] == "pending":
                s["status"] = "waiting"
        r.out["stopped"] = {"code": code, "message": message}
        r.publish()
        return "needs_review", r.out

    def go(r: Run) -> tuple[str, dict]:
        pid, body = r.pid, r.body
        files = storage.list_files(pid)
        newest = lambda kind: next((f for f in reversed(files) if f["kind"] == kind), None)  # noqa: E731

        # 1 layout
        if r.step("layout"):
            plan_file = body.plan_file_id or (newest("plan") or {}).get("id")
            refs = body.reference_file_ids if body.reference_file_ids is not None else \
                [f["id"] for f in files if f["kind"] == "reference"][:8]
            job = r.start("layout", lambda: actions.interpret(pid, plan_file, refs))
            if job is None:
                s = r.step("layout")
                raise AutopilotFailed(s.get("code") or "AUTOPILOT_LAYOUT", s.get("note") or "the plan could not be read",
                                      {"steps": r.steps})
            j = r.wait("layout", job)
            if j["status"] == "failed":
                r.mark("layout", "failed", note=(j.get("error") or {}).get("message", "failed"))
                err = j.get("error") or {}
                raise AutopilotFailed(err.get("code") or "AUTOPILOT_LAYOUT", err.get("message") or "the AI could not "
                                      "read the plan", {"steps": r.steps, "job_id": j["id"]})
            r.number = j["result"]["version"]
            n_rooms = len(storage.load_scene(pid, r.number).rooms)
            r.mark("layout", "done", version=r.number, note=f"{n_rooms} rooms read")

        # 2 furniture list (the user's own furniture: replaces what the AI suggested)
        if r.step("furniture_list") and r.step("furniture_list")["status"] == "pending":
            r.check()
            f = newest("furniture_list")
            text = (storage.upload_dir(pid, f["id"]) / "original.csv").read_text(encoding="utf-8-sig", errors="replace")
            scene = storage.load_scene(pid, r.number)
            new, info = import_furniture_sheet(scene, text, replace=True)
            note = f"{plural(info['added'], 'item')} from {f['original_name']}" + \
                (f"; {plural(len(info['problems']), 'row')} skipped" if info["problems"] else "")
            if info["added"] and len(info["problems"]) > info["added"]:
                # most rows do not fit this layout (other coordinates?): keep the AI's furniture rather than a few items
                note = (f"not used: {len(info['problems'])} of {info['added'] + len(info['problems'])} rows did not fit "
                        "this layout (check the list's coordinates); the AI's furniture is kept")
                info["added"] = 0
            if info["added"]:
                v = actions.store_version(pid, new, "furniture_list", f"Furniture list {f['original_name']}: "
                                          f"{info['added']} items", r.number)
                r.number = v["number"]
            r.mark("furniture_list", "done" if info["added"] else "skipped", version=r.number, note=note,
                   problems=info["problems"][:20])

        # 3 HVAC drawing
        if r.step("hvac") and r.step("hvac")["status"] == "pending":
            scene = storage.load_scene(pid, r.number)
            if scene.hvac.units:
                r.mark("hvac", "skipped", note="the layout already has its HVAC layer")
            else:
                f = newest("hvac")
                job = r.start("hvac", lambda: actions.hvac_import(pid, r.number, f["id"]))
                if job is not None:
                    j = r.wait("hvac", job)
                    if j["status"] == "failed":
                        r.mark("hvac", "skipped", note=(j.get("error") or {}).get("message", "the import failed"),
                               job_id=j["id"])
                    else:
                        c = j["result"].get("counts") or {}
                        r.number = j["result"]["version"]
                        if not c.get("units") and not c.get("supply_diffusers"):
                            r.mark("hvac", "skipped", version=r.number, note="nothing could be placed on this layout "
                                   "(does the drawing belong to this floor?)")
                        else:
                            r.mark("hvac", "done", version=r.number, note=f"{c.get('units', 0)} units, "
                                   f"{c.get('supply_diffusers', 0)} diffusers, {c.get('ducts', 0)} ducts")

        # 3b sensor placement sheet (after the HVAC layer: sensors link to their unit by tag)
        if r.step("sensor_sheet") and r.step("sensor_sheet")["status"] == "pending":
            r.check()
            f = newest("sensor_sheet")
            text = (storage.upload_dir(pid, f["id"]) / "original.csv").read_text(encoding="utf-8-sig", errors="replace")
            new, info = import_sheet(storage.load_scene(pid, r.number), text)
            placed = len(info["added"]) + len(info["updated"])
            if placed:
                v = actions.store_version(pid, new, "sensor_sheet", f"Sensors from {f['original_name']}: "
                                          f"{len(info['added'])} added, {len(info['updated'])} updated", r.number)
                r.number = v["number"]
            r.mark("sensor_sheet", "done" if placed else "skipped", version=r.number,
                   note=f"{plural(placed, 'sensor')} from {f['original_name']}" +
                   (f"; {plural(len(info['problems']), 'row')} skipped" if info["problems"] else ""), problems=info["problems"][:20])

        # 4 drawn furniture (vector PDF): only within the cost limit, and only when it finds something
        if r.step("furniture") and r.step("furniture")["status"] == "pending":
            r.check()
            est = actions.furniture_estimate(pid, r.number)
            usd = est.get("est_usd")
            if not est.get("available"):
                r.mark("furniture", "skipped", note=est.get("reason") or "not available")
            elif body.furniture == "auto" and (usd is None or usd > body.furniture_max_usd):
                r.mark("furniture", "skipped", note="would cost about $%.2f (limit $%.2f); the AI's furniture is kept"
                       % (usd or 0, body.furniture_max_usd) if usd is not None else "its cost cannot be estimated")
            else:
                job = r.start("furniture", lambda: actions.detect_furniture(pid, r.number))
                if job is not None:
                    j = r.wait("furniture", job)
                    res = j.get("result") or {}
                    if j["status"] == "failed":
                        r.mark("furniture", "skipped", note=(j.get("error") or {}).get("message", "failed"), job_id=j["id"])
                    elif not res.get("items"):
                        r.mark("furniture", "skipped", note="no drawn furniture found; the AI's furniture is kept")
                    else:
                        r.number = res["version"]
                        r.mark("furniture", "done", version=r.number, note=f"{res['items']} items read from the drawing")

        # 5 fix problems
        use_ai = body.use_ai and settings.ai.configured
        job = r.start("fix", lambda: actions.solve(pid, r.number, use_ai))
        if job is not None:
            j = r.wait("fix", job)
            res = j.get("result") or {}
            if j["status"] == "failed":
                r.mark("fix", "skipped", note=(j.get("error") or {}).get("message", "failed"), job_id=j["id"])
            else:
                if res.get("version"):
                    r.number = res["version"]
                ai = res.get("ai") or {}
                parts = [f"{len(res.get('rule_fixes') or [])} fixed for free"]
                if ai.get("changes"):
                    parts.append(f"{len(ai.get('solved') or [])} solved by {ai.get('model')}")
                if ai.get("error"):
                    parts.append(f"AI step skipped: {ai['error'].get('message', '')}")
                r.mark("fix", "done", version=r.number, note=", ".join(parts),
                       questions=ai.get("questions") or [])

        if body.review_first:
            meta = storage.get_version_meta(pid, r.number)
            r.out["remaining"] = [i for i in meta["validation"]["issues"] if i["severity"] != "info"][:40]
            return stop_for_user(r, "REVIEW_FIRST", f"Layout v{r.number} is ready for you to check. Build it when you "
                                                    "are happy, or press Finish automatically.")

        # 6 accept estimates + non-blocking questions
        r.check()
        r.ctx.progress(r.frac("accept"), LABELS["accept"])
        data = storage.load_scene(pid, r.number).model_dump(mode="json")
        accepted = accept_estimates(data)
        answered = accept_open_questions(data, "accepted as drawn (autopilot)")
        if accepted or answered:
            v = actions.store_version(pid, Scene.model_validate(data), "autopilot",
                                      f"Autopilot: accepted {len(accepted)} estimated values and answered "
                                      f"{len(answered)} AI questions as drawn", r.number)
            r.number = v["number"]
        r.out["assumptions"] = accepted[:2000]
        r.out["questions"] = answered[:200]
        r.mark("accept", "done", version=r.number,
               note=f"{plural(len(accepted), 'estimate')}, {plural(len(answered), 'question')}" if accepted or answered
               else "nothing to accept")

        meta = storage.get_version_meta(pid, r.number)
        val = meta["validation"]
        blocking = [i for i in val["issues"] if i["severity"] in ("error", "review")]
        r.out["remaining"] = [i for i in val["issues"] if i["severity"] != "info"][:40]
        if not val["can_generate"]:
            qs = [i for i in blocking if i["code"] == "Q_OPEN_QUESTION"]
            msg = (f"{len(qs)} question{'s' if len(qs) != 1 else ''} from the AI need{'s' if len(qs) == 1 else ''} "
                   "your answer" if qs and len(qs) == len(blocking) else
                   f"{len(blocking)} problem{'s' if len(blocking) != 1 else ''} could not be fixed automatically")
            return stop_for_user(r, "NEEDS_YOU", f"{msg} (layout v{r.number}). Answer or fix "
                                                 f"{'them' if len(blocking) != 1 else 'it'} in Review, then press "
                                                 "Finish automatically.")

        # 7 build
        if r.step("build")["status"] == "skipped":
            r.out["stopped"] = {"code": "BLENDER_MISSING",
                                "message": f"Layout v{r.number} is ready, but Blender is not installed, so the 3D model "
                                           "could not be built. Install Blender (or set TWIN_BLENDER_PATH) and press Build."}
            r.publish()
            return "needs_review", r.out
        job = r.start("build", lambda: actions.build(pid, r.number))
        if job is None:
            s = r.step("build")
            raise AutopilotFailed(s.get("code") or "AUTOPILOT_BUILD", s.get("note") or "the build could not start",
                                  {"steps": r.steps, "version": r.number})
        j = r.wait("build", job)
        if j["status"] == "failed":
            err = j.get("error") or {}
            r.mark("build", "failed", note=err.get("message", "failed"), job_id=j["id"])
            raise AutopilotFailed(err.get("code") or "AUTOPILOT_BUILD",
                                  f"Layout v{r.number} is ready, but the 3D build failed: {err.get('message', '')}",
                                  {"steps": r.steps, "version": r.number, "job_id": j["id"]})
        res = j.get("result") or {}
        r.out["build_id"] = res.get("build_id") or job.get("build_id")
        r.mark("build", "done", version=r.number, build_id=r.out["build_id"],
               note="saved model updated in place" if res.get("kind") == "layer" else
               f"{res.get('rooms', 0)} rooms, {res.get('furniture', 0)} furniture items, verified")
        r.publish()
        return "completed", r.out

    @app.get("/api/projects/{pid}/autopilot/plan")
    def autopilot_plan(pid: str, review_first: bool = False, furniture: Literal["auto", "detect", "skip"] = "auto",
                       fresh: bool = False) -> dict:
        return plan_steps(pid, AutopilotBody(review_first=review_first, furniture=furniture, fresh=fresh))

    @app.post("/api/projects/{pid}/autopilot", status_code=202)
    def autopilot(pid: str, body: AutopilotBody) -> dict:
        """'Make the 3D model for me': every step from the uploaded drawings to a verified build, as one job."""
        storage.get_project(pid)
        running = [j for j in storage.active_jobs(pid) if j["kind"] == "autopilot"]
        if running:
            raise api_error(409, "AUTOPILOT_RUNNING", "The autopilot is already working on this project.",
                            [j["id"] for j in running])
        plan = plan_steps(pid, body)
        if plan["blocked"]:
            code = "AI_NOT_CONFIGURED" if "AI" in plan["blocked"] else "NOTHING_TO_READ"
            raise api_error(409 if code == "NOTHING_TO_READ" else 503, code, plan["blocked"])
        if any(s["key"] == "layout" for s in plan["steps"]):
            actions.require_ai()
        start = None
        if not any(s["key"] == "layout" for s in plan["steps"]):
            start = body.from_version or storage.latest_version(pid)
            storage.get_version_meta(pid, start)
        return jobs.submit(pid, "autopilot", body.model_dump(),
                           lambda ctx: run_autopilot(ctx, pid, body, plan, start), version=start)

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str) -> dict:
        """Stop an autopilot run after the step it is on (that step's own job finishes and is kept)."""
        j = storage.get_job(jid)
        if j["kind"] != "autopilot":
            raise api_error(409, "NOT_STOPPABLE", "Only an autopilot run can be stopped.")
        if j["status"] in ("queued", "processing"):
            with lock:
                cancelled.add(jid)
        return {"stopping": j["status"] in ("queued", "processing")}
