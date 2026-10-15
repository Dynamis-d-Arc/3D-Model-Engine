// @ts-check
/** Generate & preview: start builds, follow progress, preview the GLB, download verified outputs. */
import { api, get, post } from "./api.js";
import { refreshProject, switchTab, watchJob } from "./app.js";
import { state, on, emit } from "./state.js";
import { h, mount, toast, showError, badge, fmtBytes, fmtTime, fmtWhen, fmtM, ask, confirmDialog, jobProgress } from "./ui.js";
import { autopilotSummary } from "./autopilot.js";

let root;
let viewer = null;
let activeJob = null;
let buildCache = {};
/** streamed Unreal walkthrough (Pixel Streaming): {session, buildId, timer, shown} and the panel it is drawn in */
let stream = null;
let streamEl = null;

export function renderGenerate(el) {
  root = el;
  on("project", draw);
  on("tab", (t) => { if (t === "generate") draw(); });
}

const SOURCE_NAMES = { ai: "AI reading", ai_fix: "AI change", ai_furniture: "AI furniture", ai_redesign: "AI redesign",
  auto_fix: "auto-fixed", fixture: "sample", hvac: "HVAC import", hvac_import: "HVAC import", manual: "drawn by hand",
  edit: "edited", derive: "walls rebuilt", autopilot: "autopilot", furniture_list: "furniture list", sensor_sheet: "sensor sheet" };

function versionOptions() {
  return state.project.versions.map((v) => h("option", { value: v.number, selected: v.number === state.version },
    `v${v.number} — ${SOURCE_NAMES[v.source] || v.source.replace(/_/g, " ")} · ${v.can_generate ? "ready to build" : "needs review"}`));
}

async function startBuild(number, render, reuse, mode = "full") {
  try {
    const job = await post(`/api/projects/${state.project.id}/versions/${number}/build`, { render_previews: render, reuse_previous: reuse, mode });
    activeJob = { ...job, layer: job.build_kind === "layer" };
    state.buildId = job.build_id;
    await refreshProject();
    const final = await watchJob(job.id, (j) => { activeJob = { ...j, build_id: job.build_id, layer: job.build_kind === "layer" }; drawProgress(); });
    activeJob = null;
    delete buildCache[job.build_id];
    await refreshProject();
    if (final.status === "completed") {
      toast(final.result.kind === "layer"
        ? `Saved model updated in ${final.result.timings?.total_s ?? "?"} s: ${final.result.sensors} sensors on build ${final.result.base_build.slice(2)}'s building. Nothing was regenerated.`
        : "3D generation completed and verified.", "ok", 8000);
    } else showError(final.error || { message: "failed" }, job.build_kind === "layer" ? "Update failed" : "Generation failed");
  } catch (e) {
    if (e.code === "SCENE_NOT_READY") {
      showError(e, "Not ready");
      switchTab("review");
    } else showError(e, "Could not start generation");
  }
}

let progressEl = null;
function drawProgress() {
  if (!progressEl) return;
  const j = activeJob;
  if (!j) return mount(progressEl);
  mount(progressEl, h("div.card", null, jobProgress(j, { title: j.layer ? "Updating the saved model" : "Generating" })));
}

async function loadBuild(bid) {
  if (!buildCache[bid]) buildCache[bid] = await get(`/api/projects/${state.project.id}/builds/${bid}`);
  return buildCache[bid];
}

async function draw() {
  if (!root || !state.project || state.activeTab !== "generate") return;
  const p = state.project;
  if (viewer) { viewer.dispose(); viewer = null; }
  if (!p.versions.length) {
    return mount(root, h("div.card", null, h("h2", null, "Nothing to generate yet"), h("p", null, "Create and review a layout first."),
      h("button.btn", { onclick: () => switchTab("brief") }, "← Brief")));
  }
  const verSel = h("select", { "aria-label": "Layout version" }, versionOptions());
  const renderCb = h("input", { type: "checkbox", checked: true });
  const reuseCb = h("input", { type: "checkbox", checked: true });
  const vmeta = () => p.versions.find((v) => v.number === Number(verSel.value));
  const status = h("div");
  const rebuild = h("div");
  const build = (mode) => startBuild(Number(verSel.value), mode === "layer" ? false : renderCb.checked, mode === "layer" || reuseCb.checked, mode);
  const fullBtn = (primary) => h("button.btn" + (primary ? ".primary" : ".small"), { disabled: !!activeJob || !vmeta().can_generate,
    onclick: () => build("full") }, primary ? "Build 3D model" : "Rebuild everything (Blender)");
  // where the chosen version stands, and the one thing to do about it
  const panel = (kind, icon, head, sub, ...buttons) => h(`div.notice.build-status${kind ? "." + kind : ""}`, null,
    h("div.bs-head", null, h("span.bs-icon", null, icon), head), sub ? h("div.bs-sub", null, sub) : null,
    buttons.filter(Boolean).length ? h("div.row.bs-actions", null, buttons) : null);
  const updReady = async () => {
    const v = vmeta();
    mount(rebuild);
    if (!v.can_generate) {
      return mount(status, panel("warn", "!", `v${v.number} needs review first`, "It has problems or measurements to confirm.",
        h("button.btn.small", { onclick: () => { state.version = v.number; switchTab("review"); } }, "Review it →")));
    }
    mount(status, panel("", "▸", `v${v.number} is ready to build`, "Blender builds the 3D model and checks every export.", fullBtn(true)));
    let opts;
    try { opts = await get(`/api/projects/${p.id}/versions/${v.number}/build-options`); } catch { return; }
    if (Number(verSel.value) !== v.number) return;
    const L = opts.layer;
    if (L.possible && L.up_to_date) {
      const b = p.builds.find((x) => x.id === L.base_build.id) || L.base_build;
      const info = h("div");
      const when = fmtWhen(b.finished_at || b.created_at).replace(/^(Today|Yesterday)/, (w) => w.toLowerCase());
      mount(status, h("div", null, panel("ok", "✓", `v${v.number} is built`, `Built ${when}`,
        b.id !== state.buildId ? h("button.btn.small", { onclick: () => { state.buildId = b.id; draw(); } }, "Show it") : null,
        unrealButton(b, info, "button.btn.primary.small")), info));
      mount(rebuild, fullBtn(false));
    } else if (L.possible) {
      const c = L.changes;
      const what = [c.added.length && `${c.added.length} added`, c.moved.length && `${c.moved.length} moved`, c.edited.length && `${c.edited.length} edited`,
        c.removed.length && `${c.removed.length} removed`].filter(Boolean).join(", ") || "no sensor changes";
      mount(status, panel("ok", "↻", "Only the sensors changed",
        `Since the saved model (v${L.base_build.version}): ${what}. The building is reused as it is, so this takes seconds, no Blender.`,
        h("button.btn.primary.small", { disabled: !!activeJob, onclick: () => build("layer") }, "Update saved model")));
      mount(rebuild, fullBtn(false));
    } else if (opts.saved_builds) {
      mount(status, panel("", "▸", `v${v.number} is ready to build`, L.reason, fullBtn(true)));
    }
  };
  verSel.addEventListener("change", updReady);
  updReady();
  progressEl = h("div");
  const controls = h("div.card", null, h("h2", null, "3D model"),
    h("label.field", null, "Layout version", verSel),
    status,
    h("details.build-opts", null, h("summary", null, "Build options"),
      h("label.small", null, renderCb, " Render preview images"),
      h("label.small", null, reuseCb, " Reuse unchanged exports from the last build (faster)"),
      rebuild));

  const builds = p.builds;
  const list = h("div.card", null, h("h2", null, "Builds"),
    builds.length ? h("ul.build-list", null, builds.map((b) => h("li.build-item" + (b.id === state.buildId ? ".current" : ""),
      { onclick: () => { state.buildId = b.id; draw(); }, title: `Build ${b.id}` + (b.kind === "layer" ? ` — sensors updated on build ${b.previous_build}` : "") },
      h("span.bi-ver", null, `v${b.version}`),
      h("span.bi-main", null, h("span", null, b.kind === "layer" ? "Sensor update" : "Blender build"),
        b.status !== "completed" ? badge(b.status) : null, h("span.bi-id", null, `#${b.id.slice(-6)}`)),
      h("span.bi-when", null, b.id === state.buildId ? h("span.bi-showing", null, "Showing") : fmtWhen(b.finished_at || b.created_at)))))
      : h("p.muted", null, "No builds yet."));

  const detail = h("div");
  mount(root, progressEl, h("div.gen-layout", null, h("div", null, detail), h("div.side", null, controls, list)));
  drawProgress();
  // a build that finished while this project was open (e.g. by a building run) shows instead of "No build yet"
  if (!state.buildId || !p.builds.some((b) => b.id === state.buildId)) state.buildId = p.builds.find((b) => b.status === "completed")?.id ?? null;
  if (state.buildId) await drawBuild(detail, state.buildId);
  else mount(detail, h("div.card", null, h("h2", null, "No build yet"),
    h("p.muted", null, "Generate a validated version to get the Blender project, the Unreal export and an interactive 3D preview here.")));
}

async function drawBuild(el, bid) {
  let b;
  try { b = await loadBuild(bid); } catch (e) { return showError(e, "Loading build"); }
  const p = state.project;
  const base = `/api/projects/${p.id}/builds/${bid}`;
  if (b.status !== "completed") {
    let job = null;
    try { job = await get(`/api/jobs/${b.job_id}`); } catch { /* ignore */ }
    delete buildCache[bid];
    return mount(el, h("div.card", null, h("h2", null, `Build ${bid.slice(2)} · v${b.version}`), badge(b.status),
      b.status === "failed" && job?.error ? h("div.notice.error", { style: { marginTop: "10px" } }, h("b", null, job.error.code), ": ", job.error.message,
        job.error.detail ? h("pre.detail", null, JSON.stringify(job.error.detail, null, 1)) : null)
        : h("p.muted", null, b.status === "failed" ? "The build failed." : "This build is still running; no outputs are available until it completes and passes verification.")));
  }
  const m = b.manifest;
  const picked = h("div.picked.hidden");
  const viewWrap = h("div.viewer", null, picked);
  // a few rooms: one button each; large plans: a compact picker so the 3D view stays visible
  const roomBtns = m.rooms.length <= 8
    ? m.rooms.map((r) => h("button.btn.small", { onclick: () => viewer?.roomView(r) }, r.name))
    : [h("select", { "aria-label": "Jump to room", onchange: (e) => {
        const r = m.rooms.find((x) => x.id === e.target.value);
        if (r) viewer?.roomView(r);
      } }, h("option", { value: "" }, `Go to room… (${m.rooms.length})`),
      [...m.rooms].sort((a, b) => a.name.localeCompare(b.name)).map((r) => h("option", { value: r.id }, r.name)))];
  const ceilCb = h("input", { type: "checkbox", onchange: (e) => viewer?.setCeilings(e.target.checked) });
  const pinCb = h("input", { type: "checkbox", checked: true, onchange: (e) => viewer?.setPins(e.target.checked) });
  const overlay = h("div.overlay", null, h("button.btn.small", { onclick: () => viewer?.overview() }, "Overview"), ...roomBtns,
    h("label.btn.small", null, ceilCb, " ceilings"),
    (b.manifest.sensors || []).length ? h("label.btn.small", { title: "Sensor pins stay visible through walls" }, pinCb, " sensor pins") : null);
  viewWrap.append(overlay);

  const files = b.files;
  const fileSize = (rel) => files.find((f) => f.path === rel)?.bytes;
  const dl = (label, rel) => fileSize(rel) ? h("tr", null, h("td", null, label), h("td.small.muted", null, fmtBytes(fileSize(rel))),
    h("td", null, h("a.btn.small", { href: `${base}/files/${rel}?download=true` }, "Download"))) : null;
  const previews = m.files.previews || [];
  const v = m.verification;
  const accepted = m.accepted_estimates || [];
  const sensors = m.sensors || [];
  const isLayer = m.build_kind === "layer";
  const lc = m.layers?.sensors?.changes;
  const layerLine = isLayer ? h("div.notice.ok", { style: { margin: "0 0 8px" } },
    h("b", null, "Sensor update of a saved model. "),
    `The building comes from Blender build ${m.geometry_build.slice(2)}` + (m.base_build !== m.geometry_build ? ` (via ${m.base_build.slice(2)})` : "") +
    ` and was reused byte for byte; ${sensors.length} sensors were written on top` +
    (lc ? ` (${[lc.added.length && `${lc.added.length} added`, lc.moved.length && `${lc.moved.length} moved`, lc.edited.length && `${lc.edited.length} edited`,
      lc.removed.length && `${lc.removed.length} removed`].filter(Boolean).join(", ") || "no change"})` : "") +
    ` in ${m.timings?.total_s ?? "?"} s, placement checked within ${v.layer_max_deviation_cm != null ? (v.layer_max_deviation_cm * 10).toFixed(3) : "?"} mm.`) : null;
  const st = state.health?.streaming;
  streamEl = h("div");
  const unrealInfo = h("div");
  const made = autopilotSummary(b);   // lists the accepted estimates itself
  mount(el, streamEl, made,
    h("div.card", null, h("div.row", null, h("h2", { style: { margin: 0 } }, `Build ${bid.slice(2)} · scene v${b.version}`), badge("completed"),
      isLayer ? h("span.kind-tag", null, "sensor update") : null,
      h("span.spacer"),
      unrealButton(b, unrealInfo, "button.btn.small.primary"),
      st?.enabled ? h("button.btn.small.primary", { id: "streamBtn", disabled: !st.available || (stream && stream.session.state !== "ended"),
        title: st.available ? "Walk through this build in Unreal Engine, streamed into this page (Pixel Streaming)" : st.problems.join("; "),
        onclick: () => startStream(b) }, "Open in browser (Unreal)") : null,
      h("span.small.muted", null, `${m.blender} · ${fmtTime(b.finished_at)}`)),
      unrealInfo,
      h("div.small", { style: { margin: "8px 0" } }, `${m.rooms.length} rooms ·${m.objects.length} architectural objects · ${m.furniture.length} furniture items · ${sensors.length} sensors · `,
        `${v.files_checked} exported files re-imported and checked, max deviation ${(v.max_reimport_delta_m * 1000).toFixed(3)} mm (tolerance ${v.tolerance_m * 1000} mm)`,
        m.reused_exports.length ? ` · ${m.reused_exports.length} unchanged exports reused` : ""),
      layerLine,
      accepted.length && !made ? h("details.notice.warn", null, h("summary", null, `Built with ${accepted.length} accepted estimate(s): ` +
        Object.entries(accepted.reduce((a, t) => { const k = t.split(":")[0]; a[k] = (a[k] || 0) + 1; return a; }, {}))
          .map(([k, n]) => `${n} ${k === "furniture" ? "furniture items" : k + (n > 1 ? "s" : "")}`).join(", ")), h("div.small", { style: { marginTop: "6px" } }, accepted.join(", "))) : null,
      viewWrap),
    sensors.length ? sensorCard(sensors) : null,
    previews.length ? h("div.card", null, h("h2", null, isLayer ? "Blender renders of the saved building (without the sensor layer)" : "Blender renders"), h("div.gallery", null, previews.map((rel) => h("figure", null,
      h("a", { href: `${base}/files/${rel}`, target: "_blank" }, h("img", { src: `${base}/files/${rel}`, alt: rel, loading: "lazy" })),
      h("figcaption", null, rel.replace("preview/", "").replace(".png", "").replace("room_", "room ")))))) : null,
    h("div.card", null, h("h2", null, "Downloads"),
      h("table.simple", null, h("tbody", null,
        dl("Blender project (.blend)", "scene.blend"),
        h("tr", null, h("td", null, "Unreal bundle (per-object FBX + manifest)"), h("td.small.muted", null, "zip"),
          h("td", null, h("a.btn.small", { href: `${base}/bundle/unreal` }, "Download"))),
        dl("Whole scene FBX", "export/fbx/scene_full.fbx"),
        dl("Web model (GLB)", "export/web/scene.glb"),
        dl("Building only, without layers (GLB)", "export/web/geometry.glb"),
        sensors.length ? dl("Sensor layer (JSON)", "sensors.json") : null,
        dl("Unreal import manifest", "manifest.json"),
        dl("Scene model (source of truth)", "scene.json"),
        dl("Verification report", "verify_report.json"),
        dl("Layer check report", "layer_report.json"),
        h("tr", null, h("td", null, "Everything"), h("td.small.muted", null, "zip"), h("td", null, h("a.btn.small", { href: `${base}/bundle/all` }, "Download")))))),
    h("div.card", null, h("h2", null, "Rooms"), h("table.simple", null,
      h("thead", null, h("tr", null, h("th", null, "Room"), h("th", null, "Clear size m"), h("th", null, "Area m²"), h("th", null, "Floor"), h("th"))),
      h("tbody", null, m.rooms.map((r) => h("tr", null, h("td", null, r.name), h("td", null, `${fmtM(r.dims_clear[0])} × ${fmtM(r.dims_clear[1])}`),
        h("td", null, fmtM(r.area_net, 1)), h("td.small", null, r.floor_material),
        h("td", null, h("button.btn.small", { onclick: () => { state.version = b.version; switchTab("review"); } }, "Edit"))))))),
    unrealCard(b));
  if (stream) { stream.shown = null; drawStream(); }
  try {
    const { Viewer } = await import("./viewer.js");
    viewer = new Viewer(viewWrap, (info) => {
      if (!info) return picked.classList.add("hidden");
      picked.classList.remove("hidden");
      if (info.sensor_id) {
        return mount(picked, h("b", null, info.label), h("div.mono", null, info.name),
          h("div", null, `Room: ${info.room_name || "outside every room"} · height ${fmtM(info.height_m)} m (${info.mounting})`),
          h("div", null, `Monitors: ${info.unit_tag || "no unit"}`), h("div", null, `BMS point: ${info.bms_point || "not set (simulated readings)"}`),
          h("button.btn.small", { style: { marginTop: "6px" }, onclick: () => { state.version = b.version; switchTab("review"); emit("select", `sensor:${info.sensor_id}`); } }, "Edit in Review"));
      }
      mount(picked, h("b", null, info.label || info.name), h("div.mono", null, info.name),
        info.room_name ? h("div", null, `Room: ${info.room_name}`) : null,
        info.size_m ? h("div", null, `Size: ${info.size_m.map((x) => fmtM(x)).join(" × ")} m`) : null,
        info.asset_id ? h("div", null, `Asset: ${info.asset_id}`) : null,
        info.kind ? h("div", null, `Kind: ${info.kind}`) : null,
        info.label && !info.asset_id ? h("div", null, info.label) : null,
        state.health.ai.configured ? h("button.btn.small", { style: { marginTop: "6px" }, onclick: () => fixPicked(b.version, info) }, "Fix this with AI…") : null);
    });
    await viewer.load(`${base}/files/export/web/scene.glb`, m,
      Object.fromEntries((state.catalog.sensor_kinds || []).map((k) => [k.id, k.color])));
  } catch (e) {
    mount(viewWrap, h("div.notice.error", { style: { margin: "20px" } }, "3D preview unavailable: ", String(e.message || e),
      " (run `npm install` in frontend/ to provide three.js)"));
  }
}

// ---------------------------------------------------------------------------------------------
// Unreal walkthrough in the browser (Pixel Streaming). The backend starts the packaged viewer for this build
// and hands back only the player page URL; the viewer's read-only token never reaches the browser.
// ---------------------------------------------------------------------------------------------
async function startStream(b) {
  try {
    const s = await post(`/api/projects/${state.project.id}/stream/sessions`, { build_id: b.id });
    if (stream?.timer) clearInterval(stream.timer);
    stream = { session: s, buildId: b.id, timer: null, shown: null };
    stream.timer = setInterval(pollStream, 2000);
    drawStream();
    streamEl?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { showError(e, "Browser walkthrough"); }
}

async function pollStream() {
  if (!stream) return;
  try { stream.session = await get(`/api/stream/sessions/${stream.session.id}`); } catch (e) { return; }
  if (stream.session.state === "ended") { clearInterval(stream.timer); stream.timer = null; }
  drawStream();
}

async function endStream() {
  if (!stream) return;
  try { stream.session = await api(`/api/stream/sessions/${stream.session.id}`, { method: "DELETE" }); } catch (e) { showError(e, "Ending the session"); }
  if (stream.timer) clearInterval(stream.timer);
  stream.timer = null;
  drawStream();
}

window.addEventListener("pagehide", () => {   // closing the tab ends the session instead of waiting for the idle limit
  if (stream && stream.session.state !== "ended") navigator.sendBeacon(`/api/stream/sessions/${stream.session.id}/end`);
});

/** The panel above the build (redrawn only when the session state changes, so the player is not reloaded). */
function drawStream() {
  if (!streamEl || !stream) return;
  const s = stream.session;
  if (stream.shown === s.state && streamEl.childElementCount) return;
  stream.shown = s.state;
  const btn = /** @type {HTMLButtonElement|null} */ (document.getElementById("streamBtn"));
  if (btn) btn.disabled = s.state !== "ended";   // one session per tab
  const mins = Math.round((s.idle_exit_s || 600) / 60);
  let body;
  if (s.state === "starting") {
    body = h("div.stream-frame.wait", null, h("div", null, h("b", null, "Starting the Unreal viewer…"),
      h("div.small", null, `Loading build ${s.build_id.slice(2)} on this PC (a few seconds).`)));
  } else if (s.state === "running") {
    const frame = h("iframe", { src: s.player_url, allow: "autoplay; fullscreen", title: "Unreal walkthrough" });
    body = h("div", null, h("div.stream-frame", null, frame),
      h("div.row", { style: { marginTop: "6px" } },
        h("span.small.muted", null, `Click the picture to steer: WASD move, mouse look, M rooms, V airflow, H help. Esc frees the mouse. `,
          `The session ends after ${mins} min without input.`), h("span.spacer"),
        h("button.btn.small", { onclick: () => frame.requestFullscreen?.() }, "Full screen")));
  } else {
    body = h("div.notice", null, `Session ended: ${s.reason || "closed"}.`, " ",
      h("button.btn.small", { onclick: () => startStream({ id: s.build_id }) }, "Start again"));
  }
  mount(streamEl, h("div.card", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "Unreal walkthrough in the browser"), badge(s.state === "running" ? "live" : s.state, s.state === "running" ? "ok" : "info"),
      h("span.small.muted", null, `streamed from this PC · build ${s.build_id.slice(2)}${s.ready_s != null ? ` · ready in ${s.ready_s} s` : ""}`),
      h("span.spacer"), s.state !== "ended" ? h("button.btn.small.danger", { onclick: endStream }, "End session") : null),
    h("div", { style: { marginTop: "8px" } }, body)));
}

/** The sensor layer of a build: one row per sensor, "Show" flies the 3D view to it. */
function sensorCard(sensors) {
  const kinds = Object.fromEntries((state.catalog.sensor_kinds || []).map((k) => [k.id, k]));
  return h("div.card", null, h("h2", null, `Sensors (${sensors.length})`),
    h("div", { style: { maxHeight: "260px", overflow: "auto" } }, h("table.simple", null,
      h("thead", null, h("tr", null, h("th", null, "Tag"), h("th", null, "Type"), h("th", null, "Room"), h("th", null, "Height m"),
        h("th", null, "Unit"), h("th", null, "BMS point"), h("th"))),
      h("tbody", null, sensors.map((s) => h("tr", null,
        h("td", null, h("span.sensor-dot", { style: { background: kinds[s.kind]?.color, marginRight: "6px" } }), h("b", null, s.tag)),
        h("td.small", null, kinds[s.kind]?.label || s.kind), h("td.small", null, s.room_name || "–"), h("td.small", null, fmtM(s.height_m)),
        h("td.small", null, s.unit_tag || "–"), h("td.small.mono", null, s.bms_point || "–"),
        h("td", null, h("button.btn.small", { onclick: () => viewer?.focusSensor(s) }, "Show"))))))));
}

/** Selection id for the fix-with-AI request from a picked 3D object (manifest metadata). */
function pickedTarget(info) {
  if (info.sensor_id) return `sensor:${info.sensor_id}`;
  if (info.hvac_id) return `${{ hvac_unit: "hvac_unit", hvac_duct: "duct", hvac_diffuser: "hvac_terminal", hvac_grille: "hvac_terminal" }[info.kind] || "hvac_unit"}:${info.hvac_id}`;
  if (info.furniture_id) return `furniture:${info.furniture_id}`;
  if (info.opening_id) return `opening:${info.opening_id}`;
  if (info.wall_id) return `wall:${info.wall_id}`;
  if (info.room_id) return `room:${info.room_id}`;
  return null;
}

async function fixPicked(version, info) {
  const target = pickedTarget(info);
  const v = await ask("Fix with AI", [{ name: "prompt", label: `What is wrong${target ? ` with ${target}` : ""}?`, type: "textarea", value: "" }], "Fix");
  if (!v || !v.prompt) return;
  try {
    const job = await post(`/api/projects/${state.project.id}/versions/${version}/fix`, { prompt: v.prompt, selection: target ? [target] : [] });
    toast("Fix started; the new version opens in Review when it is ready.", "info", 6000);
    const final = await watchJob(job.id);
    await refreshProject();
    if (final.status === "needs_review" && final.result.version) {
      state.version = final.result.version;
      toast(`Fixed: ${final.result.summary}. Review it, then generate again.`, "ok", 9000);
      switchTab("review");
      emit("version", final.result.version);
    } else if (final.status === "failed") showError(final.error || { message: "failed" }, "Fix with AI");
    else toast(final.result?.questions?.[0] || final.result?.summary || "No change was made.", "info", 9000);
  } catch (e) { showError(e, "Fix with AI"); }
}

/** Hand a viewer launch document to the user: a .twinview download or the clipboard. */
async function deliverLaunch(info, r, copy, what) {
  const text = JSON.stringify(r.launch, null, 1);
  if (copy) {
    await navigator.clipboard.writeText(text);
    toast("Launch code copied. In the desktop viewer press Ctrl+V.", "ok", 7000);
  } else {
    const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "application/json" })), download: r.file_name });
    document.body.append(a);
    a.click();
    a.remove();
  }
  mount(info, h("div.notice.ok", null,
    `Read-only link for ${what}, valid until ${new Date(r.expires_at * 1000).toLocaleString()}. `,
    copy ? "Paste it into the viewer with Ctrl+V." : h("span", null, "Open it with ", h("code", null, `"Open Twin Viewer.cmd" "${r.file_name}"`), ".")));
}

/** "View in Unreal Engine": the backend starts the packaged viewer on this computer (browser + server on the same PC). */
async function openHere(info, url, body, what) {
  try {
    const r = await post(url, body);
    if (!r.started) return toast("The viewer is already opening.", "info", 4000);
    toast(`Opening ${what} in Unreal Engine… (a few seconds)`, "ok", 6000);
    mount(info, h("div.notice.ok", null, `The Unreal viewer (${r.viewer}) is starting on this PC with ${what}. `,
      "H shows the controls; Esc quits."));
  } catch (e) { showError(e, "View in Unreal Engine"); }
}

/** One click: this build opens in the packaged Unreal viewer on this PC. Greyed out (with the reason) until it is set up. */
function unrealButton(b, info, tag = "button.btn.primary") {
  const dv = state.health?.desktop_viewer;
  return h(tag, { disabled: !dv?.available,
    title: dv?.available ? "Walk through this build in Unreal Engine on this PC" : `Not set up yet: ${dv?.problem || "no packaged viewer"}`,
    onclick: () => openHere(info, `/api/projects/${state.project.id}/viewer/open-local`, { build_id: b.id }, "this build") },
  "View in Unreal Engine");
}

/** Desktop viewer: a short-lived read-only link the packaged Unreal app opens (no editor needed anywhere). */
function viewerSection(b) {
  const info = h("div");
  const here = !!state.health?.desktop_viewer?.available;
  const launch = async (copy) => {
    try {
      deliverLaunch(info, await post(`/api/projects/${state.project.id}/viewer/launch`, { build_id: b.id }), copy, "this project");
    } catch (e) { showError(e, "Desktop viewer"); }
  };
  return h("div", null,
    h("h3", null, "Desktop viewer"),
    h("p.small.muted", null, "Walk through this build in the packaged Unreal viewer: first-person navigation, room list, ",
      "furniture and HVAC details with live data, airflow illustration. The viewing PC needs no Unreal Editor or Blender; ",
      "the link holds a short-lived read-only token, never an API key."),
    h("div.row", null,
      here ? unrealButton(b, info) : null,
      h(here ? "button.btn" : "button.btn.primary", { onclick: () => launch(false) }, here ? "Download launch file" : "Open in desktop viewer"),
      h("button.btn", { onclick: () => launch(true) }, "Copy launch code")),
    here ? null : h("p.small.muted", null, "“View in Unreal Engine” on this PC needs the viewer packaged once (developer machine with UE 5.8): ",
      h("code", null, ".venv/Scripts/python.exe -m twin.unreal.package --config Shipping --out dist/TwinViewerShipping")),
    info,
    stacksSection());
}

const MAX_FLOORS = 6;

/** Multi-floor buildings: this project stacked with other floor projects, opened as one building in the viewer. */
function stacksSection() {
  const here = !!state.health?.desktop_viewer?.available;
  const box = h("div", null, h("p.small.muted", null, "Loading multi-floor buildings…"));
  const info = h("div");
  const refresh = async () => {
    try {
      const stacks = await get(`/api/stacks?project_id=${encodeURIComponent(state.project.id)}`);
      mount(box, stacks.length ? stacks.map(stackRow) : h("p.small.muted", null, "This floor is not part of a multi-floor building yet."));
    } catch (e) { mount(box, h("div.notice.warn", null, `Multi-floor buildings: ${e.message}`)); }
  };
  const launch = async (st, copy) => {
    try {
      const r = await post(`/api/stacks/${st.id}/viewer/launch`, {});
      deliverLaunch(info, r, copy, `${st.name} (${r.floors.length} floors)`);
    } catch (e) { showError(e, "Multi-floor building"); }
  };
  const edit = async (st) => {
    const projects = state.projects;
    const opts = [["", "—"], ...projects.map((p) => [p.id, p.name])];
    const floors = st ? st.floors : [{ project_id: state.project.id }];
    const given = st && st.floors.length > 1 && st.floors.every((f, i) => f.elevation_m != null);
    const step = given ? st.floors[1].elevation_m - st.floors[0].elevation_m : null;
    const fields = [{ name: "name", label: "Building name", value: st ? st.name : state.project.name }];
    for (let i = 0; i < MAX_FLOORS; i++) {
      fields.push({ name: `f${i}`, label: i === 0 ? "Floor 1 (lowest)" : `Floor ${i + 1}`, options: opts, value: floors[i]?.project_id || "" });
    }
    fields.push({ name: "step", label: "Floor-to-floor height in metres (blank = automatic: walls or ceiling-void ducts + slab)",
      type: "number", value: step ?? "" });
    const v = await ask(st ? "Edit multi-floor building" : "New multi-floor building", fields, st ? "Save" : "Create");
    if (!v) return;
    const chosen = [];
    for (let i = 0; i < MAX_FLOORS; i++) if (v[`f${i}`]) chosen.push(v[`f${i}`]);
    if (chosen.length < 2) { toast("Choose at least two floors.", "warn", 5000); return; }
    const old = st ? st.floors : [];
    const body = { name: v.name || "Building", floors: chosen.map((pid, i) => {
      const prev = old.find((f) => f.project_id === pid) || {};
      return { project_id: pid, label: prev.label ?? null, offset_m: prev.offset_m ?? null,
        elevation_m: v.step != null ? Math.round(i * v.step * 1000) / 1000 : null };
    }) };
    try {
      const saved = st ? await api(`/api/stacks/${st.id}`, { method: "PUT", json: body }) : await post("/api/stacks", body);
      toast(saved.plan_error ? saved.plan_error.message : `${saved.name}: ${saved.floors.length} floors stacked.`, saved.plan_error ? "warn" : "ok", 7000);
      refresh();
    } catch (e) { showError(e, "Multi-floor building"); }
  };
  const remove = async (st) => {
    if (!(await confirmDialog("Delete multi-floor building", `Delete "${st.name}"? Its floors (projects and builds) are kept.`, "Delete"))) return;
    try { await api(`/api/stacks/${st.id}`, { method: "DELETE" }); refresh(); } catch (e) { showError(e, "Multi-floor building"); }
  };
  const stackRow = (st) => {
    const rows = st.plan ? st.plan.floors.map((f) => h("li", null,
      h("b", null, f.label), ` ${f.project_name}, v${f.version} · floor at ${fmtM(f.elevation_m)} m`,
      f.elevation_source === "stacked" ? h("span.muted", null, " (automatic)") : null,
      f.alignment && f.alignment.method === "structural grid"
        ? h("span.muted", null, ` · aligned on the structural grid (${f.alignment.matched.join(" + ")} lines)`)
        : f.alignment && f.alignment.method !== "reference floor" ? h("span.muted", null, ` · placement: ${f.alignment.method}`) : null))
      : st.floors.map((f, i) => h("li", null, `Floor ${i + 1}: ${st.project_names[f.project_id] || f.project_id}`));
    return h("div.card", { style: { padding: "10px", marginTop: "8px" } },
      h("div.row", null, h("b", null, st.name), h("span.small.muted", null, ` · ${st.floors.length} floors`)),
      h("ul.small", null, rows),
      ...(st.plan?.warnings || []).map((w) => h("div.notice.warn", null, w)),
      st.plan_error ? h("div.notice.warn", null, st.plan_error.message) : null,
      h("div.row", null,
        here ? h("button.btn.primary", { disabled: !!st.plan_error,
          onclick: () => openHere(info, `/api/stacks/${st.id}/viewer/open-local`, {}, `${st.name} (${st.floors.length} floors)`) }, "View in Unreal Engine") : null,
        h(here ? "button.btn" : "button.btn.primary", { disabled: !!st.plan_error, onclick: () => launch(st, false) },
          here ? "Download launch file" : "Open in desktop viewer"),
        h("button.btn", { disabled: !!st.plan_error, onclick: () => launch(st, true) }, "Copy launch code"),
        h("button.btn", { onclick: () => edit(st) }, "Edit"),
        h("button.btn", { onclick: () => remove(st) }, "Delete")));
  };
  refresh();
  return h("div", null,
    h("h3", null, "Multi-floor building"),
    h("p.small.muted", null, "Stack this floor with the other floors of the building (each floor stays its own project). ",
      "The viewer opens them as one building: PgUp / PgDn jump between floors. Floors are lined up on the structural ",
      "grid of their floor plans and stacked so each floor's ceiling-void ducts fit under the next slab."),
    box,
    h("div.row", null, h("button.btn", { onclick: () => edit(null) }, "+ Multi-floor building")),
    info);
}

function unrealCard(b) {
  const ue = state.health.unreal;
  const card = h("div.card", null, h("h2", null, "Unreal Engine 5"), viewerSection(b), h("h3", null, "Editor import (developer tools)"));
  if (!ue.engine_found || !ue.project_found) {
    card.append(h("p.muted", null, ue.engine_found ? `Walkthrough project not found at ${ue.project}.` : "Unreal Engine was not found on the server (not needed for the desktop viewer)."),
      h("p.small", null, "The Unreal bundle above contains everything needed; see docs/UNREAL.md for the manual import steps."));
    return card;
  }
  const status = h("div");
  const importBtn = h("button.btn.primary", { onclick: async () => {
    try {
      const job = await post(`/api/projects/${state.project.id}/builds/${b.id}/unreal/import`, {});
      const final = await watchJob(job.id, (j) => mount(status, h("div.row", null, badge(j.status), h("span.small", null, j.stage))));
      if (final.status === "completed") {
        const r = final.result, t = r.tour || {};
        const shotUrl = (n) => `/api/projects/${state.project.id}/builds/${b.id}/files/unreal/tour/${n}`;
        const checks = t.navigation ? h("ul", null,
          h("li", null, `Navigation ${t.navigation.from} → ${t.navigation.to}: ${t.navigation.valid ? "path found" : "NO PATH"}`),
          h("li", null, `Click-to-walk: ${t.walk?.arrived ? "arrived" : `stopped ${Math.round(t.walk?.remaining_cm || 0)} cm short`}`),
          h("li", null, `Wall collision: ${t.wall_collision?.stayed_inside_room ? "blocked by the wall" : "WALKED THROUGH"}`),
          h("li", null, `Selection: ${t.selection?.label || "none"} (${t.selection?.size || ""})`),
          h("li", null, `Average ${Math.round(t.avg_fps || 0)} fps`)) : null;
        const gallery = h("div.gallery", { style: { marginTop: "8px" } }, (t.screenshots || []).map((n) => h("figure", null,
          h("a", { href: shotUrl(n), target: "_blank" }, h("img", { src: shotUrl(n), alt: n })), h("figcaption", null, n))));
        mount(status, h("div.notice.ok", null,
          `Imported into ${r.map}: ${r.actors} actors (${r.imported} meshes imported, ${r.reused} reused), placement within ${r.max_bounds_deviation_cm} cm of the scene model.`,
          checks), gallery);
      } else {
        mount(status, h("div.notice.error", null, h("b", null, final.error?.code), ": ", final.error?.message,
          final.error?.detail ? h("pre.detail", null, JSON.stringify(final.error.detail, null, 1)) : null));
      }
    } catch (e) { showError(e, "Unreal import"); }
  } }, "Import into walkthrough");
  const launchBtn = h("button.btn", { onclick: async () => {
    try {
      const r = await post(`/api/projects/${state.project.id}/builds/${b.id}/unreal/launch`, {});
      toast(`Walkthrough starting (${r.map}). Unreal opens in a new window.`, "ok", 8000);
    } catch (e) { showError(e, "Launch walkthrough"); }
  } }, "Launch walkthrough");
  card.append(h("p.small.muted", null, `${ue.engine_dir} · ${ue.project}`),
    ue.module_built ? "" : h("div.notice.warn", null, "The walkthrough template is not compiled yet: run ", h("code", null, "python -m twin.unreal.setup"), " (see docs/UNREAL.md)."),
    h("div.row", null, importBtn, launchBtn), status);
  return card;
}
