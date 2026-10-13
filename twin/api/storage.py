"""Project storage: SQLite for metadata, the filesystem for uploads, scene versions and builds.

Layout under TWIN_DATA_DIR:
    twin.sqlite
    projects/<project_id>/uploads/<file_id>/{original.<ext>, normalized.png, thumb.png}
    projects/<project_id>/versions/v0001.json ...   (immutable once written)
    projects/<project_id>/builds/<build_id>/...      (one directory per build, never reused)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from twin.scene.model import Brief, Scene

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, brief_json TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
    original_name TEXT NOT NULL, sniffed TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    width INTEGER, height INTEGER, pages INTEGER, page INTEGER, calibration_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    project_id TEXT NOT NULL REFERENCES projects(id), number INTEGER NOT NULL, source TEXT NOT NULL,
    note TEXT NOT NULL, parent INTEGER, scene_hash TEXT NOT NULL, validation_json TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY (project_id, number)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL,
    result_json TEXT, error_json TEXT, version INTEGER, created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL, job_id TEXT NOT NULL,
    status TEXT NOT NULL, previous_build TEXT, created_at REAL NOT NULL, finished_at REAL, summary_json TEXT
);
CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, job_id TEXT, model TEXT, api_style TEXT,
    status TEXT NOT NULL, error_code TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
    latency_s REAL, created_at REAL NOT NULL, cost_usd REAL, purpose TEXT
);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created_at);
CREATE INDEX IF NOT EXISTS builds_project ON builds(project_id, created_at);
"""

JOB_STATUSES = ("queued", "processing", "needs_review", "completed", "failed")


class NotFound(KeyError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)
        self.db_path = self.root / "twin.sqlite"
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(ai_calls)").fetchall()}
            for col, typ in (("cost_usd", "REAL"), ("purpose", "TEXT")):  # migrate older databases
                if col not in cols:
                    c.execute(f"ALTER TABLE ai_calls ADD COLUMN {col} {typ}")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # ---- paths ----------------------------------------------------------------------------
    def project_dir(self, pid: str) -> Path:
        if not pid.startswith("p_") or not pid[2:].isalnum():
            raise NotFound(pid)
        return self.root / "projects" / pid

    def upload_dir(self, pid: str, fid: str) -> Path:
        return self.project_dir(pid) / "uploads" / fid

    def version_path(self, pid: str, number: int) -> Path:
        return self.project_dir(pid) / "versions" / f"v{number:04d}.json"

    def build_dir(self, pid: str, bid: str) -> Path:
        if not bid.startswith("b_") or not bid[2:].replace("_", "").isalnum():
            raise NotFound(bid)
        return self.project_dir(pid) / "builds" / bid

    # ---- projects -------------------------------------------------------------------------
    def create_project(self, name: str, brief: Brief) -> dict:
        pid = new_id("p")
        now = time.time()
        with self.conn() as c:
            c.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                      (pid, name, brief.model_dump_json(), "draft", now, now))
        self.project_dir(pid).mkdir(parents=True, exist_ok=True)
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise NotFound(pid)
        d = dict(row)
        d["brief"] = json.loads(d.pop("brief_json"))
        return d

    def list_projects(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id, name, status, created_at, updated_at FROM projects ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_project(self, pid: str, *, name: str | None = None, brief: Brief | None = None,
                       status: str | None = None) -> dict:
        self.get_project(pid)
        sets, args = ["updated_at=?"], [time.time()]
        if name is not None:
            sets.append("name=?"), args.append(name)
        if brief is not None:
            sets.append("brief_json=?"), args.append(brief.model_dump_json())
        if status is not None:
            sets.append("status=?"), args.append(status)
        with self.conn() as c:
            c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*args, pid))
        return self.get_project(pid)

    def brief(self, pid: str) -> Brief:
        return Brief.model_validate(self.get_project(pid)["brief"])

    # ---- files ------------------------------------------------------------------------------
    def add_file(self, pid: str, fid: str, kind: str, original_name: str, sniffed: str, data: bytes,
                 width: int, height: int, pages: int, page: int) -> dict:
        with self.conn() as c:
            c.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (fid, pid, kind, original_name[:200], sniffed, len(data), hashlib.sha256(data).hexdigest(),
                       width, height, pages, page, None, time.time()))
        self.update_project(pid)
        return self.get_file(pid, fid)

    def get_file(self, pid: str, fid: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM files WHERE id=? AND project_id=?", (fid, pid)).fetchone()
        if row is None:
            raise NotFound(fid)
        d = dict(row)
        raw = d.pop("calibration_json")
        d["calibration"] = json.loads(raw) if raw else None
        return d

    def list_files(self, pid: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id FROM files WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
        return [self.get_file(pid, r["id"]) for r in rows]

    def set_calibration(self, pid: str, fid: str, calibration: dict | None) -> dict:
        self.get_file(pid, fid)
        with self.conn() as c:
            c.execute("UPDATE files SET calibration_json=? WHERE id=?",
                      (json.dumps(calibration) if calibration else None, fid))
        return self.get_file(pid, fid)

    # ---- versions (immutable) ----------------------------------------------------------------
    def add_version(self, pid: str, scene: Scene, *, source: str, note: str, parent: int | None,
                    validation: dict, scene_hash: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT MAX(number) AS n FROM versions WHERE project_id=?", (pid,)).fetchone()
            number = (row["n"] or 0) + 1
            path = self.version_path(pid, number)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():  # never overwrite an existing version file
                raise RuntimeError(f"version file {path.name} already exists")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(scene.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(path)
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                      (pid, number, source, note[:1000], parent, scene_hash, json.dumps(validation), time.time()))
            c.execute("UPDATE projects SET updated_at=?, status=? WHERE id=?",
                      (time.time(), "ready" if validation.get("can_generate") else "needs_review", pid))
        return self.get_version_meta(pid, number)

    def get_version_meta(self, pid: str, number: int) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM versions WHERE project_id=? AND number=?", (pid, number)).fetchone()
        if row is None:
            raise NotFound(f"{pid} v{number}")
        d = dict(row)
        d["validation"] = json.loads(d.pop("validation_json"))
        return d

    def list_versions(self, pid: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT project_id, number, source, note, parent, scene_hash, created_at, validation_json "
                             "FROM versions WHERE project_id=? ORDER BY number DESC", (pid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            v = json.loads(d.pop("validation_json"))
            d["can_generate"], d["counts"] = v.get("can_generate"), v.get("counts", {})
            out.append(d)
        return out

    def load_scene(self, pid: str, number: int) -> Scene:
        path = self.version_path(pid, number)
        if not path.is_file():
            raise NotFound(f"{pid} v{number}")
        return Scene.model_validate_json(path.read_text(encoding="utf-8"))

    def latest_version(self, pid: str) -> int | None:
        with self.conn() as c:
            row = c.execute("SELECT MAX(number) AS n FROM versions WHERE project_id=?", (pid,)).fetchone()
        return row["n"]

    # ---- jobs -------------------------------------------------------------------------------
    def create_job(self, pid: str, kind: str, params: dict, version: int | None = None) -> dict:
        jid = new_id("j")
        with self.conn() as c:
            c.execute("INSERT INTO jobs (id, project_id, kind, status, params_json, version, created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (jid, pid, kind, "queued", json.dumps(params), version, time.time()))
        return self.get_job(jid)

    def update_job(self, jid: str, **fields: Any) -> None:
        cols, args = [], []
        for k, v in fields.items():
            if k in ("result", "error", "params"):
                k, v = f"{k}_json", json.dumps(v) if v is not None else None
            if k == "status" and v not in JOB_STATUSES:
                raise ValueError(v)
            cols.append(f"{k}=?")
            args.append(v)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", (*args, jid))

    def get_job(self, jid: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise NotFound(jid)
        d = dict(row)
        for k in ("params", "result", "error"):
            raw = d.pop(f"{k}_json")
            d[k] = json.loads(raw) if raw else None
        return d

    def list_jobs(self, pid: str, limit: int = 30) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (pid, limit)).fetchall()
        return [self.get_job(r["id"]) for r in rows]

    def fail_interrupted_jobs(self) -> int:
        err = json.dumps({"code": "INTERRUPTED", "message": "The server stopped while this job was running. "
                                                            "Start it again."})
        with self.conn() as c:
            cur = c.execute("UPDATE jobs SET status='failed', error_json=?, finished_at=? "
                            "WHERE status IN ('queued','processing')", (err, time.time()))
            c.execute("UPDATE builds SET status='failed', finished_at=? WHERE status IN ('queued','processing')",
                      (time.time(),))
            return cur.rowcount

    # ---- builds -----------------------------------------------------------------------------
    def create_build(self, pid: str, version: int, job_id: str, previous: str | None) -> dict:
        bid = f"b_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        with self.conn() as c:
            c.execute("INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?)",
                      (bid, pid, version, job_id, "queued", previous, time.time(), None, None))
        return self.get_build(pid, bid)

    def update_build(self, bid: str, *, status: str, summary: dict | None = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE builds SET status=?, finished_at=?, summary_json=COALESCE(?, summary_json) WHERE id=?",
                      (status, time.time() if status in ("completed", "failed") else None,
                       json.dumps(summary) if summary is not None else None, bid))

    def get_build(self, pid: str, bid: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM builds WHERE id=? AND project_id=?", (bid, pid)).fetchone()
        if row is None:
            raise NotFound(bid)
        d = dict(row)
        d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
        return d

    def list_builds(self, pid: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id FROM builds WHERE project_id=? ORDER BY created_at DESC", (pid,)).fetchall()
        return [self.get_build(pid, r["id"]) for r in rows]

    def latest_completed_build(self, pid: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT id FROM builds WHERE project_id=? AND status='completed' "
                            "ORDER BY finished_at DESC LIMIT 1", (pid,)).fetchone()
        return self.get_build(pid, row["id"]) if row else None

    # ---- AI usage -------------------------------------------------------------------------
    def record_ai_call(self, *, project_id: str | None, job_id: str | None, model: str | None, api_style: str,
                       status: str, error_code: str | None = None, usage: dict | None = None,
                       latency_s: float | None = None, cost_usd: float | None = None, purpose: str = "") -> None:
        u = usage or {}
        inp = u.get("input_tokens", u.get("prompt_tokens"))
        out = u.get("output_tokens", u.get("completion_tokens"))
        with self.conn() as c:
            c.execute("INSERT INTO ai_calls (project_id, job_id, model, api_style, status, error_code, input_tokens, "
                      "output_tokens, total_tokens, latency_s, created_at, cost_usd, purpose) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (project_id, job_id, model, api_style, status, error_code, inp, out, u.get("total_tokens"),
                       latency_s, time.time(), cost_usd, purpose[:60]))

    def ai_spent_usd(self) -> float:
        """Total recorded AI spend (all projects, all time) - the basis of the spending cap."""
        with self.conn() as c:
            row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_calls").fetchone()
        return float(row["s"])

    def ai_usage_today(self) -> dict:
        start = time.time() - (time.time() % 86400)  # UTC midnight
        with self.conn() as c:
            row = c.execute("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
                            "SUM(CASE WHEN status='ok' THEN 0 ELSE 1 END) AS failed, "
                            "COALESCE(SUM(cost_usd),0) AS cost_usd "
                            "FROM ai_calls WHERE created_at >= ?", (start,)).fetchone()
        return {k: (row[k] or 0) for k in ("calls", "input_tokens", "output_tokens", "failed", "cost_usd")}
