"""Runtime configuration from environment variables (and an optional, git-ignored .env file).

Real environment variables always win over .env. The AI key is held as a SecretStr: it is
never logged, returned by the API, written into generated files, or sent to the frontend.
See .env.example for every setting.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, blank lines, optional quotes, `export ` prefix)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def _lookup(env: dict[str, str], file_vals: dict[str, str], *names: str, default: str | None = None) -> str | None:
    for n in names:
        if env.get(n):
            return env[n]
    for n in names:
        if file_vals.get(n):
            return file_vals[n]
    return default


def _float_or_none(value: str | None, name: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        v = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if v < 0:
        raise ValueError(f"{name} must not be negative")
    return v


def _detect_blender() -> str | None:
    candidates = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"), reverse=True)
    return candidates[0] if candidates else shutil.which("blender")


def _detect_unreal() -> str | None:
    candidates = sorted(glob.glob(r"C:\Program Files\Epic Games\UE_5.*"), reverse=True)
    return candidates[0] if candidates else None


@dataclass(frozen=True)
class AISettings:
    api_key: SecretStr | None
    base_url: str
    model: str | None
    api_style: str              # "responses" | "chat_completions"
    timeout_s: float
    max_retries: int
    reasoning_effort: str | None
    max_output_tokens: int
    daily_request_limit: int
    key_source: str | None = None   # name of the variable the key came from (never the value)
    budget_usd: float | None = None          # hard cap on total AI spend (see twin/ai/budget.py)
    price_input_per_1m: float | None = None  # USD per million input tokens for `model`
    price_output_per_1m: float | None = None  # USD per million output tokens (incl. reasoning) for `model`
    service_tier: str | None = "default"     # pinned so billing matches the configured (Standard) prices
    furniture_effort: str | None = "low"     # reasoning effort of the tiled furniture detection (cost driver)

    @property
    def configured(self) -> bool:
        return self.api_key is not None and bool(self.api_key.get_secret_value()) and bool(self.model)

    def public(self) -> dict:
        """Safe to show in the UI / logs: no secret material."""
        return {"configured": self.configured, "key_present": self.api_key is not None,
                "key_source": self.key_source, "model": self.model, "api_style": self.api_style,
                "endpoint_host": re.sub(r"^https?://([^/]+).*$", r"\1", self.base_url),
                "budget_usd": self.budget_usd, "price_input_per_1m": self.price_input_per_1m,
                "price_output_per_1m": self.price_output_per_1m}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    max_upload_mb: int
    blender_path: str | None
    unreal_engine_dir: str | None
    unreal_project: Path
    ai: AISettings
    render_previews: bool = True
    extra: dict = field(default_factory=dict)
    # desktop / streamed Unreal viewer (twin/viewer): token signing secret (None = generated per data dir),
    # token lifetime, and the backend URL written into launch files (None = the URL the request came in on)
    viewer_secret: SecretStr | None = None
    viewer_token_ttl_h: float = 12.0
    public_url: str | None = None


def load_settings(env: dict[str, str] | None = None, env_file: Path | None = ENV_FILE) -> Settings:
    env = dict(os.environ if env is None else env)
    fv = read_env_file(env_file) if env_file else {}
    key_name = next((n for n in ("TWIN_AI_API_KEY", "OPENAI_API_KEY") if env.get(n) or fv.get(n)), None)
    key = _lookup(env, fv, "TWIN_AI_API_KEY", "OPENAI_API_KEY")
    data_dir = Path(_lookup(env, fv, "TWIN_DATA_DIR", default=str(PROJECT_ROOT / "data")))
    ai = AISettings(
        api_key=SecretStr(key) if key else None,
        base_url=_lookup(env, fv, "TWIN_AI_BASE_URL", default="https://api.openai.com/v1").rstrip("/"),
        model=_lookup(env, fv, "TWIN_AI_MODEL"),
        api_style=_lookup(env, fv, "TWIN_AI_API_STYLE", default="responses"),
        timeout_s=float(_lookup(env, fv, "TWIN_AI_TIMEOUT_S", default="180")),
        max_retries=int(_lookup(env, fv, "TWIN_AI_MAX_RETRIES", default="3")),
        reasoning_effort=_lookup(env, fv, "TWIN_AI_REASONING_EFFORT"),
        max_output_tokens=int(_lookup(env, fv, "TWIN_AI_MAX_OUTPUT_TOKENS", default="20000")),
        daily_request_limit=int(_lookup(env, fv, "TWIN_AI_DAILY_REQUEST_LIMIT", default="200")),
        key_source=key_name,
        budget_usd=_float_or_none(_lookup(env, fv, "TWIN_AI_BUDGET_USD"), "TWIN_AI_BUDGET_USD"),
        price_input_per_1m=_float_or_none(_lookup(env, fv, "TWIN_AI_PRICE_INPUT_PER_1M"), "TWIN_AI_PRICE_INPUT_PER_1M"),
        price_output_per_1m=_float_or_none(_lookup(env, fv, "TWIN_AI_PRICE_OUTPUT_PER_1M"), "TWIN_AI_PRICE_OUTPUT_PER_1M"),
        service_tier=_lookup(env, fv, "TWIN_AI_SERVICE_TIER", default="default") or None,
        furniture_effort=_lookup(env, fv, "TWIN_AI_FURNITURE_EFFORT", default="low") or None,
    )
    if ai.api_style not in ("responses", "chat_completions"):
        raise ValueError("TWIN_AI_API_STYLE must be 'responses' or 'chat_completions'")
    return Settings(
        data_dir=data_dir.resolve(),
        host=_lookup(env, fv, "TWIN_HOST", default="127.0.0.1"),
        port=int(_lookup(env, fv, "TWIN_PORT", default="8765")),
        max_upload_mb=int(_lookup(env, fv, "TWIN_MAX_UPLOAD_MB", default="25")),
        blender_path=_lookup(env, fv, "TWIN_BLENDER_PATH") or _detect_blender(),
        unreal_engine_dir=_lookup(env, fv, "TWIN_UNREAL_ENGINE_DIR") or _detect_unreal(),
        unreal_project=Path(_lookup(env, fv, "TWIN_UNREAL_PROJECT",
                                    default=str(PROJECT_ROOT / "unreal" / "TwinWalkthrough" / "TwinWalkthrough.uproject"))),
        ai=ai,
        render_previews=_lookup(env, fv, "TWIN_RENDER_PREVIEWS", default="1") not in ("0", "false", "no"),
        viewer_secret=SecretStr(v) if (v := _lookup(env, fv, "TWIN_VIEWER_SECRET")) else None,
        viewer_token_ttl_h=float(_lookup(env, fv, "TWIN_VIEWER_TOKEN_TTL_H", default="12")),
        public_url=(_lookup(env, fv, "TWIN_PUBLIC_URL") or "").rstrip("/") or None,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


class RedactSecrets(logging.Filter):
    """Belt and braces: scrub the configured key and anything key-shaped from log records."""

    PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-\.]{8,}|tvw1\.[A-Za-z0-9_\-\.]{8,})")

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return self.PATTERN.sub("***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = self.scrub(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction(settings: Settings) -> None:
    secrets_ = [s.get_secret_value() for s in (settings.ai.api_key, settings.viewer_secret) if s is not None]
    f = RedactSecrets(secrets_)
    root = logging.getLogger()
    for h in root.handlers or []:
        h.addFilter(f)
    root.addFilter(f)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "twin"):
        logging.getLogger(name).addFilter(f)
