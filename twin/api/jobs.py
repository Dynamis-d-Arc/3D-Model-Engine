"""Background jobs with persisted status: queued -> processing -> needs_review | completed | failed.

Lengthy work (AI calls, Blender builds, Unreal imports) runs in small thread pools so the
API stays responsive. Each job function receives a JobContext for progress updates and
returns (final_status, result). Exceptions become a 'failed' job with a meaningful error.
"""

from __future__ import annotations

import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from twin.ai.client import AIError
from twin.api.storage import Storage
from twin.pipeline import BuildFailed

log = logging.getLogger("twin.jobs")

POOLS = {"ai": 2, "blender": 1, "unreal": 1, "hvac": 1}
KIND_POOL = {"interpret": "ai", "redesign": "ai", "fix": "ai", "furniture": "ai", "build": "blender", "unreal_import": "unreal",
             "hvac_import": "hvac"}


@dataclass
class JobContext:
    storage: Storage
    job_id: str
    project_id: str
    _last_write: float = 0.0
    _last_progress: float = -1.0

    def progress(self, fraction: float, stage: str) -> None:
        now = time.time()
        if fraction >= 1.0 or now - self._last_write > 0.4 or fraction - self._last_progress > 0.05:
            self.storage.update_job(self.job_id, progress=round(max(0.0, min(1.0, fraction)), 3), stage=stage[:200])
            self._last_write, self._last_progress = now, fraction


JobFn = Callable[[JobContext], tuple[str, dict]]


def error_payload(exc: BaseException) -> dict:
    if isinstance(exc, BuildFailed):
        return {"code": exc.code, "message": str(exc), "detail": exc.detail}
    if isinstance(exc, AIError):
        return {"code": exc.code, "message": str(exc), "detail": exc.detail}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.isupper() and type(exc).__name__.endswith(("Error", "Failed")):
        return {"code": code, "message": str(exc), "detail": getattr(exc, "detail", None)}
    if isinstance(exc, FileNotFoundError):
        return {"code": "FILE_MISSING", "message": str(exc)}
    return {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}",
            "detail": {"trace": traceback.format_exc()[-3000:]}}


class JobManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.pools = {name: ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"twin-{name}")
                      for name, n in POOLS.items()}
        interrupted = storage.fail_interrupted_jobs()
        if interrupted:
            log.warning("marked %d interrupted jobs as failed", interrupted)

    def create(self, project_id: str, kind: str, params: dict, version: int | None = None) -> dict:
        """Create the queued job row (so callers can link other records to it) without starting it."""
        return self.storage.create_job(project_id, kind, params, version)

    def start(self, job: dict, fn: JobFn, on_finish: Callable[[dict], None] | None = None) -> dict:
        self.pools[KIND_POOL[job["kind"]]].submit(self._run, job["id"], job["project_id"], fn, on_finish)
        return job

    def submit(self, project_id: str, kind: str, params: dict, fn: JobFn, version: int | None = None,
               on_finish: Callable[[dict], None] | None = None) -> dict:
        return self.start(self.create(project_id, kind, params, version), fn, on_finish)

    def _run(self, job_id: str, project_id: str, fn: JobFn, on_finish) -> None:
        s = self.storage
        s.update_job(job_id, status="processing", started_at=time.time(), stage="starting")
        ctx = JobContext(s, job_id, project_id)
        try:
            status, result = fn(ctx)
            if status not in ("completed", "needs_review"):
                raise ValueError(f"job function returned invalid status {status}")
            s.update_job(job_id, status=status, progress=1.0, stage=status, result=result, finished_at=time.time())
        except BaseException as exc:  # noqa: BLE001 - every failure must be recorded
            payload = error_payload(exc)
            log.warning("job %s failed: %s %s", job_id, payload["code"], payload["message"])
            s.update_job(job_id, status="failed", stage="failed", error=payload, finished_at=time.time())
        if on_finish:
            try:
                on_finish(s.get_job(job_id))
            except Exception:  # noqa: BLE001
                log.exception("on_finish hook failed for %s", job_id)

    def shutdown(self) -> None:
        for p in self.pools.values():
            p.shutdown(wait=False, cancel_futures=True)
