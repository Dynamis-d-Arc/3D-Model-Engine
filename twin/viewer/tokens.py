"""Viewer access tokens: short-lived, read-only, project-scoped, signed by the backend.

The packaged Unreal viewer (and later a Pixel Streaming instance) never holds an API key or a backend
secret. The backend mints a token for one person / session; the viewer sends it as
``Authorization: Bearer <token>`` to the read-only ``/api/viewer/v1`` routes and nothing else.

    tvw1.<base64url(payload json)>.<base64url(HMAC-SHA256(secret, "tvw1." + payload part))>

payload: {"v": 1, "prj": ["p_..."] | "*", "bld": "b_..." | null, "iat": unix, "exp": unix, "jti": random}

The signing secret comes from TWIN_VIEWER_SECRET; without it a random secret is generated once per data
directory (``viewer_secret.key``) so tokens survive a restart but never leave the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

PREFIX = "tvw1"
MAX_TTL_H = 24 * 30


class TokenError(ValueError):
    """Invalid, expired or out-of-scope token. ``code`` is safe to show to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ViewerGrant:
    projects: tuple[str, ...] | None  # None = every project
    build: str | None                 # None = every build of the granted projects
    expires_at: float
    token_id: str

    def allows(self, pid: str, bid: str | None = None) -> bool:
        if self.projects is not None and pid not in self.projects:
            return False
        return bid is None or self.build is None or bid == self.build


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_secret(configured: str | None, data_dir: Path) -> bytes:
    """The configured secret, else one generated for this data directory (created 0600 on first use)."""
    if configured:
        if len(configured) < 32:
            raise ValueError("TWIN_VIEWER_SECRET must be at least 32 characters")
        return configured.encode("utf-8")
    path = data_dir / "viewer_secret.key"
    if not path.is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(secrets.token_urlsafe(48))
        except FileExistsError:  # another worker created it first
            pass
    value = path.read_text(encoding="ascii").strip()
    if len(value) < 32:
        raise ValueError(f"{path} holds a secret shorter than 32 characters; delete it to regenerate")
    return value.encode("ascii")


def mint(secret: bytes, *, projects: list[str] | None, build: str | None = None, ttl_hours: float = 12.0,
         now: float | None = None) -> tuple[str, ViewerGrant]:
    if not 0 < ttl_hours <= MAX_TTL_H:
        raise ValueError(f"ttl_hours must be in (0, {MAX_TTL_H}]")
    if build is not None and (not projects or len(projects) != 1):
        raise ValueError("a build-scoped token must name exactly one project")
    now = time.time() if now is None else now
    payload = {"v": 1, "prj": sorted(projects) if projects is not None else "*", "bld": build,
               "iat": int(now), "exp": int(now + ttl_hours * 3600), "jti": secrets.token_hex(8)}
    body = f"{PREFIX}.{_b64(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", _grant(payload)


def _grant(payload: dict) -> ViewerGrant:
    prj = payload["prj"]
    return ViewerGrant(projects=None if prj == "*" else tuple(prj), build=payload.get("bld"),
                       expires_at=float(payload["exp"]), token_id=str(payload.get("jti", "")))


def verify(secret: bytes, token: str, now: float | None = None) -> ViewerGrant:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenError("TOKEN_MALFORMED", "not a viewer token")
    body = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _unb64(parts[2])
    except (ValueError, TypeError):
        raise TokenError("TOKEN_MALFORMED", "bad signature encoding") from None
    if not hmac.compare_digest(expected, given):
        raise TokenError("TOKEN_INVALID", "the token signature does not match (wrong server or tampered token)")
    try:
        payload = json.loads(_unb64(parts[1]))
        grant = _grant(payload)
    except (ValueError, KeyError, TypeError):
        raise TokenError("TOKEN_MALFORMED", "bad token payload") from None
    if payload.get("v") != 1:
        raise TokenError("TOKEN_UNSUPPORTED", "unsupported token version")
    if (time.time() if now is None else now) >= grant.expires_at:
        raise TokenError("TOKEN_EXPIRED", "the viewer token has expired; open the building again from the web app")
    return grant
