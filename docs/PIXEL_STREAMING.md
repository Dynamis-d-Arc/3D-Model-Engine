# Unreal walkthrough in the browser (Pixel Streaming)

The packaged Unreal viewer ([UNREAL_VIEWER.md](UNREAL_VIEWER.md)) can run on a GPU machine and stream its picture
to a web page with Unreal's **Pixel Streaming 2** plugin; keyboard and mouse go back the same way. The visitor needs
nothing but a browser. The three.js preview on the Generate tab is unchanged; streaming is an extra button.

**Status:** works as a **local test** — viewer, signalling server and browser on one PC, everything on
127.0.0.1. Nothing is hosted or reachable from the network; the hosting plan is at the end.

## What was verified (UE 5.8.3, Pixel Streaming 2, Windows 11, laptop RTX 4050)

* A 38-room floor (~1000 objects, 325k triangles, 19 sensors) streamed into the web app at 1600×900, H.264:
  building loaded in 1.3 s inside the streamed viewer, all 1027 objects passed the in-engine placement check,
  session ready 6–15 s after the click (cold start, two sessions at once = slower), first video ~1 s later.
* Walking (WASD), looking (mouse after a click on the picture, or the arrow keys), room list (M), room jump (1–9),
  selection, airflow overlay (V) and the help panel (H) work through the stream; Esc frees the mouse and does not
  quit the viewer.
* Mouse look needed two viewer fixes (both in `TwinPlayerController`): the turn is applied straight to the control
  rotation (`AddYawInput` after `Super::PlayerTick` was always discarded — this also affected the desktop viewer and
  is now covered by the tour's `look` check), and in streaming mode the mouse deltas are read by a Slate input
  pre-processor, because an off-screen viewport has no window to capture the mouse, so the input axes stay near zero.
  Both paths turn by the same mouse sensitivity setting (**[ / ]**, see UNREAL_VIEWER.md). A streamed delta is in
  streamer pixels, which the player scales by stream width / shown width, so a small player turns faster.
* Ending a session from the page stops the viewer process tree and deletes its token file.
* The signalling server listened on 127.0.0.1 only (player 8090, streamer 8888, SFU 8889).
* The desktop viewer tests (`tests/test_unreal_viewer.py`) pass against the streaming-enabled package.

## Pieces

| Piece | Where | Role |
|---|---|---|
| Streaming mode of the viewer | `TwinPlayerController` (`-TwinStreaming`, automatic with `-PixelStreamingConnectionURL`) | Esc never quits, no clipboard launch codes, no screenshots on the server; the session ends by itself after `-TwinIdleExit` s without input (default 600, exit 4), `-TwinMaxSession` s (default 3600, exit 5) or a load error lasting 60 s (exit 3); a countdown shows in the last minute |
| Pixel Streaming 2 plugin | `TwinWalkthrough.uproject` | video/audio encode (NVENC), WebRTC, browser input; idle unless `-PixelStreamingConnectionURL` is given |
| Signalling server ("Wilbur") | EpicGames/PixelStreamingInfrastructure, release `UE5.8-0.1.0` (not in this repo) | pairs browser and viewer, serves Epic's player page; started with `twin/stream/bind_loopback.cjs` so it listens on 127.0.0.1 only (it has no bind-address option) |
| Session launcher | `twin/stream/local.py`, `twin/stream/routes.py` | one viewer process per browser session, limits, state, clean stop |
| Web app | `frontend/js/generate.js` | **Open in browser (Unreal)** on a build → stream panel with *End session* / *Full screen* |

### Sessions API (local test)

| | |
|---|---|
| `GET /api/stream/status` | enabled / available / problems, active sessions, limits (also in `/api/health` as `streaming`) |
| `POST /api/projects/{p}/stream/sessions` `{"build_id"}` | starts a viewer for that completed build → `{id, state: starting, player_url, …}`; 429 `STREAM_BUSY` at the limit, 503 `STREAMING_NOT_AVAILABLE` |
| `GET /api/stream/sessions/{s}` | `starting` → `running` (viewer logged `[twin] ready`) → `ended` with `reason` |
| `DELETE /api/stream/sessions/{s}` (`POST …/end` for `sendBeacon`) | stops it now; the page also ends it when the tab closes |

### Security model (kept for hosting)

* The browser never receives a viewer token. Per session the backend mints one read-only token scoped to that
  one build, valid a little longer than the session limit, writes it to a file only the viewer reads
  (`-TwinTokenFile`, never on the command line) and deletes it when the session ends.
* The browser only gets the player URL with the session's streamer id.
* Sessions end by themselves (idle, maximum length, load error) and on request; the number of concurrent
  sessions is capped (`TWIN_STREAM_MAX_SESSIONS`, default 2).
* Like the rest of the web API there are no accounts yet, which is fine on 127.0.0.1 and is the first thing a
  hosted setup needs.

## Running the local test

1. Package the streaming-enabled viewer (separate folder, the desktop package stays as it is):
   `python -m twin.unreal.package --config Development --out dist/TwinViewerStreaming --skip-content`
2. Get the signalling server: release `UE5.8-0.1.0` of github.com/EpicGames/PixelStreamingInfrastructure, unpack it
   outside the repo, then in its root `npm install`, and build `Common` (`build:cjs` + `build:esm`), `Signalling`
   (`build:cjs`), `SignallingWebServer` (`build`), `Frontend/library` and `Frontend/ui-library` (`build:esm`) and
   `Frontend/implementations/typescript` (`build:dev` with `WEBPACK_OUTPUT_PATH=<SignallingWebServer>/www`).
   (Its own `platform_scripts/cmd/start.bat` does the same but downloads its own Node.js and looks up the public IP.)
3. Start the backend with `TWIN_STREAM_LOCAL=1` and `TWIN_STREAM_SIGNALLING_DIR=<…>/SignallingWebServer`.
   The backend starts the signalling server on first use and stops everything when it shuts down.
4. Generate tab → open a completed build → **Open in browser (Unreal)**. Click the picture to steer; Esc frees the
   mouse.

Viewer logs: `data/stream/sessions/<id>/viewer.log`; signalling: `data/stream/signalling.log`.

## Sensors, HVAC and live data

* In the stream today: the sensor markers (selectable, labelled), HVAC units coloured by live / simulated status,
  the airflow illustration, room and building menus — all drawn by the viewer.
* Not yet: sensor *readings* in the viewer (its `/api/viewer/v1/…/hvac/live` returns units only; adding sensors
  there plus a HUD line needs a C++ change and a repackage).
* Cheaper path for richer panels: show sensor / HVAC panels as normal web page elements beside the stream, from the
  APIs the Live tab already uses, and sync them with Pixel Streaming's data channel — browser → viewer
  `emitUIInteraction` (handled through the plugin's input handler `RegisterMessageHandler`), viewer → browser
  `SendPlayerMessage` / *Send Pixel Streaming 2 Response* (`addResponseEventListener` in the page): click a sensor
  in the panel to fly there, click an object in 3D to open its details in the panel.
* Future BMS data: unchanged — pushed to `POST /api/projects/{p}/hvac/telemetry`; the viewer and the web panels
  read the same endpoints.

## Hosting plan (not built)

1. **Accounts first**: the web app behind a login and TLS; starting a session costs GPU time, so it must be
   authenticated and capped per user / organisation (like the AI spending cap).
2. **GPU host(s)** running viewer instances + the signalling server + a TURN relay (coturn, short-lived credentials
   via `--turn_secret`); the streamer port never public.
3. **Session broker**: today's `LocalStreamer` generalised to remote hosts (start/stop instances, ports, health,
   queue, idle shutdown of the host itself).
4. **Signalling auth**: a one-time, short-lived session ticket checked on the player websocket (Wilbur's
   `verifyClient` / `authorizeStreamerId` hooks — it ships with no authentication by design) and bound to that
   session's streamer id. With a managed host that pre-starts instances, the page passes the ticket into the
   viewer over the data channel and the viewer exchanges it with the backend for its token, so the token still
   never reaches the browser.
5. **Packaging**: Shipping build for hosts; a Linux package removes the Windows licence cost but needs the Linux
   cross-compile toolchain and Vulkan testing. Lower quality settings may be needed on server GPUs (Lumen and
   virtual shadow maps are on; the laptop renders this floor at ~80 fps).

### Costs (researched 2026-10-14, on-demand list prices; sources below)

| Option | Price | Notes |
|---|---|---|
| AWS g4dn.xlarge (T4), Singapore | $0.736/h Linux, $0.920/h Windows | likely 2–3 light streams per GPU (to be measured) |
| Azure NV6ads A10 v5 (1/6 A10), Southeast Asia | $0.59/h Linux, $0.866/h Windows | smallest A10 slice |
| GCP g2-standard-4 (L4), Singapore | $0.872/h + $0.184/h Windows | L4 also encodes AV1 |
| AWS g5 (A10G) / g6 (L4) | not offered in Singapore | nearest: Tokyo / Malaysia |
| Internet egress (AWS / Azure / GCP, Singapore) | ~$0.12 per GB | a 10 Mbit/s stream ≈ 4.5 GB/h ≈ **$0.54 per viewer-hour**, as much as the GPU |
| Vagon Streams (managed) | $0.025/min T4 ($1.50/h), $0.036/min L4 | + $0.67/day per region; has Asia datacentres |
| Arcware Cloud (managed) | €10/month + €0.15/min | Munich / Los Angeles only |
| Eagle 3D Streaming (managed) | $29/month incl. 300 min, then $0.10/min | US/EU; says PS2 browser↔app messaging is not supported |
| AWS GameLift Streams (managed) | from $0.38/h (1/8 L4, Windows, us-east-1) | own streaming stack (not the Pixel Streaming plugin), no Singapore |

Rule of thumb for a pilot: a GPU VM started on demand and stopped when idle costs roughly **$1–1.5 per hour of
use** (GPU + Windows + egress for 1–2 viewers) and nothing while stopped; managed services trade a higher per-minute
price for no operations work. Sources: AWS price list (2026-10-12), prices.azure.com, gcloud-compute.com /
cloud.google.com, the vendors' pricing pages, Epic's Pixel Streaming 5.8 docs (stream tuning: 10–20 Mbit/s), the
PixelStreamingInfrastructure repository (UE5.8 branch, security guidelines).

## Known limitations of the local test

* One viewer process per session = one GPU encode; two sessions on a laptop GPU slow each other down.
* Epic's player page is used as is in an iframe (its overlay buttons hidden with `HideUI=true`); a hosted version
  would embed Epic's frontend library directly in the web app.
* No audio is needed by the walkthrough; the player starts muted.
* Headless browser tests cannot leave the locked mouse with Esc (a real browser can); the automated tests cover the
  session manager with a stand-in viewer process, the full stream was checked by hand with headless Edge.
