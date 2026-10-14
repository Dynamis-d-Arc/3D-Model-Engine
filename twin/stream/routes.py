"""Streaming sessions API (local Pixel Streaming test, twin.stream.local).

    GET    /api/stream/status                       availability, active sessions, limits
    POST   /api/projects/{pid}/stream/sessions      {"build_id": "b_..." (default: newest completed)} -> session
    GET    /api/stream/sessions/{sid}               state: starting | running | ended (+ reason)
    DELETE /api/stream/sessions/{sid}               end it now (POST .../end: the same, for sendBeacon)

Like the desktop viewer link, starting a session has the trust of the rest of the web API (no accounts yet);
a hosted setup must put it behind the login.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from twin.api.storage import Storage
from twin.config import Settings
from twin.stream.local import LocalStreamer, StreamError


class SessionBody(BaseModel):
    build_id: str | None = Field(default=None, description="completed build to stream (default: the newest one)")


def register(app: FastAPI, settings: Settings, storage: Storage, api_error) -> LocalStreamer:
    streamer = LocalStreamer(settings)

    def fail(exc: StreamError):
        return api_error(exc.status, exc.code, str(exc))

    @app.get("/api/stream/status")
    def stream_status() -> dict:
        return streamer.status()

    @app.post("/api/projects/{pid}/stream/sessions", status_code=201)
    def start_session(pid: str, body: SessionBody) -> dict:
        storage.get_project(pid)
        if body.build_id:
            build = storage.get_build(pid, body.build_id)
            if build["status"] != "completed":
                raise api_error(409, "BUILD_NOT_COMPLETED", "Only completed, verified builds can be streamed.")
        else:
            build = storage.latest_completed_build(pid)
            if build is None:
                raise api_error(409, "NO_COMPLETED_BUILD", "Generate the building first; there is no completed build.")
        try:
            return streamer.start(pid, build["id"], build.get("version"))
        except StreamError as exc:
            raise fail(exc)

    @app.get("/api/stream/sessions/{sid}")
    def session(sid: str) -> dict:
        try:
            return streamer.get(sid)
        except StreamError as exc:
            raise fail(exc)

    @app.delete("/api/stream/sessions/{sid}")
    def end_session(sid: str) -> dict:
        try:
            return streamer.stop(sid)
        except StreamError as exc:
            raise fail(exc)

    @app.post("/api/stream/sessions/{sid}/end")
    def end_session_beacon(sid: str) -> dict:
        """Same as DELETE, for navigator.sendBeacon when the tab closes (beacons can only POST)."""
        return end_session(sid)

    return streamer
