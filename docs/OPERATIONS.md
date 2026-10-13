# Operations

## Running

* `python run_server.py` (or `python -m twin.api`) – binds `TWIN_HOST:TWIN_PORT` (default `127.0.0.1:8765`).
  Keep the default loopback binding: there are no user accounts yet.
* Data lives in `TWIN_DATA_DIR` (default `./data`, git-ignored). Back it up as a whole (SQLite + files).
* Generated caches: `assets_build/` (furniture library, textures) – safe to delete, rebuilt on demand.
* Logs: server stdout; per build `blender.log`; per Unreal job `unreal/*.log`.

## Security measures in place

* Secrets only from environment/.env (git-ignored), held as `SecretStr`, redacted from logs
  (`RedactSecrets` filter), never returned by `/api/health` (only `configured`, model, endpoint host).
* Uploads: size limit (`TWIN_MAX_UPLOAD_MB`), type by magic bytes (PNG/JPEG/PDF), Pillow decompression-bomb
  limit, stored under generated ids (never user file names).
* Downloads: build files resolved and checked to stay inside the build directory; ids are validated.
* AI requests use `store: false`; a daily request limit caps cost.
* Desktop viewer: the packaged app holds no secret. It gets a signed, read-only, project-scoped token that
  expires (`TWIN_VIEWER_TOKEN_TTL_H`, default 12 h); the signing secret (`TWIN_VIEWER_SECRET` or
  `data/viewer_secret.key`) never leaves the server and is redacted from logs, as are `tvw1.` tokens.
  `python -m twin.unreal.package` refuses to produce a package that contains a backend secret.
  `POST /api/projects/{p}/viewer/launch` is as open as the rest of the web API until accounts exist —
  keep the loopback binding or put the backend behind authentication before exposing it.
* Builds/jobs never delete user data; versions and builds are append-only.

## Roadmap to a service (stages 4–5, not implemented)

| Area | Next step |
|---|---|
| Geometry | multiple levels + stairs (schema already has `levels[]`), non-rectangular rooms in the editor's quick tools, wall-hung furniture, skirting/trims |
| Extraction | classical CV pre-pass (wall/line detection) to give the AI and the user snapping candidates; OCR of dimension strings; scale detection |
| Assets | larger catalog, glTF/FBX asset packs per style, user-uploaded assets with automatic normalisation |
| Accounts | auth (OIDC), per-user projects, roles; move from loopback to a reverse proxy with TLS |
| Storage | object storage for uploads/builds, Postgres instead of SQLite, signed download URLs |
| Jobs | external queue (e.g. Redis/RQ or Celery) and separate Blender/Unreal worker machines (GPU) |
| Limits & cost | per-user quotas, token budgets, cost dashboard from `ai_calls` |
| Unreal | packaged viewer done ([UNREAL_VIEWER.md](UNREAL_VIEWER.md)); next: Pixel Streaming (plan in that file), signed viewer releases, multi-level buildings |
