// @ts-check
/** Generate & preview: start builds, follow progress, preview the GLB, download verified outputs. */
import { get, post } from "./api.js";
import { refreshProject, switchTab, watchJob } from "./app.js";
import { state, on, emit } from "./state.js";
import { h, mount, toast, showError, badge, fmtBytes, fmtTime, fmtM, ask } from "./ui.js";

let root;
let viewer = null;
let activeJob = null;
let buildCache = {};

export function renderGenerate(el) {
  root = el;
  on("project", draw);
  on("tab", (t) => { if (t === "generate") draw(); });
}

function versionOptions() {
  return state.project.versions.map((v) => h("option", { value: v.number, selected: v.number === state.version },
    `v${v.number} · ${v.source} · ${v.can_generate ? "ready" : "needs review"}`));
}

async function startBuild(number, render, reuse) {
  try {
    const job = await post(`/api/projects/${state.project.id}/versions/${number}/build`, { render_previews: render, reuse_previous: reuse });
    activeJob = job;
    state.buildId = job.build_id;
    await refreshProject();
    const final = await watchJob(job.id, (j) => { activeJob = { ...j, build_id: job.build_id }; drawProgress(); });
    activeJob = null;
    delete buildCache[job.build_id];
    await refreshProject();
    if (final.status === "completed") toast("3D generation completed and verified.", "ok");
    else showError(final.error || { message: "failed" }, "Generation failed");
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
  const pct = Math.round((j.progress || 0) * 100);
  mount(progressEl, h("div.card", null, h("div.row", null, h("b", null, "Generating"), badge(j.status), h("span.small", null, j.stage),
    h("span.spacer"), h("span.small.muted", null, `${pct}%`)), h("div.progress", { style: { marginTop: "8px" } }, h("div", { style: { width: `${pct}%` } }))));
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
  const verSel = h("select", null, versionOptions());
  const renderCb = h("input", { type: "checkbox", checked: true });
  const reuseCb = h("input", { type: "checkbox", checked: true });
  const vmeta = () => p.versions.find((v) => v.number === Number(verSel.value));
  const ready = h("div");
  const updReady = () => {
    const v = vmeta();
    mount(ready, v.can_generate ? h("div.notice.ok", null, "This version passed validation and can be generated.")
      : h("div.notice.warn", null, "This version has errors or unconfirmed measurements. ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); state.version = v.number; switchTab("review"); } }, "Review it"), " before generating."));
  };
  verSel.addEventListener("change", updReady);
  updReady();
  progressEl = h("div");
  const controls = h("div.card", null, h("h2", null, "Generate 3D scene"),
    h("div.row", null, h("label.field", null, "Scene version", verSel),
      h("label.small", null, renderCb, " render preview images"), h("label.small", null, reuseCb, " reuse unchanged exports from the last build")),
    h("div.row", null, ready),
    h("div.row", null, h("button.btn.primary", { disabled: !!activeJob, onclick: () => startBuild(Number(verSel.value), renderCb.checked, reuseCb.checked) },
      "Generate"), h("span.small.muted", null, "Blender builds the scene, saves the .blend, exports FBX/GLB, then re-imports every export to verify it.")));

  const builds = p.builds;
  const list = h("div.card", null, h("h2", null, "Builds"),
    builds.length ? h("table.simple", null, h("thead", null, h("tr", null, h("th", null, "Build"), h("th", null, "Version"), h("th", null, "Status"), h("th", null, "Finished"), h("th"))),
      h("tbody", null, builds.map((b) => h("tr", { style: { background: b.id === state.buildId ? "#eef3f8" : "" } },
        h("td.mono", null, b.id.slice(2)), h("td", null, `v${b.version}`), h("td", null, badge(b.status)),
        h("td.small", null, fmtTime(b.finished_at)),
        h("td", null, h("button.btn.small", { onclick: () => { state.buildId = b.id; draw(); } }, "Open"))))))
      : h("p.muted", null, "No builds yet."));

  const detail = h("div");
  mount(root, progressEl, h("div.gen-layout", null, h("div", null, detail), h("div.side", null, controls, list)));
  drawProgress();
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
  const overlay = h("div.overlay", null, h("button.btn.small", { onclick: () => viewer?.overview() }, "Overview"), ...roomBtns,
    h("label.btn.small", null, ceilCb, " ceilings"));
  viewWrap.append(overlay);

  const files = b.files;
  const fileSize = (rel) => files.find((f) => f.path === rel)?.bytes;
  const dl = (label, rel) => fileSize(rel) ? h("tr", null, h("td", null, label), h("td.small.muted", null, fmtBytes(fileSize(rel))),
    h("td", null, h("a.btn.small", { href: `${base}/files/${rel}?download=true` }, "Download"))) : null;
  const previews = m.files.previews || [];
  const v = m.verification;
  const accepted = m.accepted_estimates || [];
  mount(el,
    h("div.card", null, h("div.row", null, h("h2", { style: { margin: 0 } }, `Build ${bid.slice(2)} · scene v${b.version}`), badge("completed"),
      h("span.spacer"), h("span.small.muted", null, `${m.blender} · ${fmtTime(b.finished_at)}`)),
      h("div.small", { style: { margin: "8px 0" } }, `${m.rooms.length} rooms · ${m.objects.length} architectural objects · ${m.furniture.length} furniture items · `,
        `${v.files_checked} exported files re-imported and checked, max deviation ${(v.max_reimport_delta_m * 1000).toFixed(3)} mm (tolerance ${v.tolerance_m * 1000} mm)`,
        m.reused_exports.length ? ` · ${m.reused_exports.length} unchanged exports reused` : ""),
      accepted.length ? h("div.notice.warn", null, `Built with ${accepted.length} accepted estimate(s): ${accepted.join(", ")}`) : null,
      viewWrap),
    previews.length ? h("div.card", null, h("h2", null, "Blender renders"), h("div.gallery", null, previews.map((rel) => h("figure", null,
      h("a", { href: `${base}/files/${rel}`, target: "_blank" }, h("img", { src: `${base}/files/${rel}`, alt: rel, loading: "lazy" })),
      h("figcaption", null, rel.replace("preview/", "").replace(".png", "").replace("room_", "room ")))))) : null,
    h("div.card", null, h("h2", null, "Downloads"),
      h("table.simple", null, h("tbody", null,
        dl("Blender project (.blend)", "scene.blend"),
        h("tr", null, h("td", null, "Unreal bundle (per-object FBX + manifest)"), h("td.small.muted", null, "zip"),
          h("td", null, h("a.btn.small", { href: `${base}/bundle/unreal` }, "Download"))),
        dl("Whole scene FBX", "export/fbx/scene_full.fbx"),
        dl("Web model (GLB)", "export/web/scene.glb"),
        dl("Unreal import manifest", "manifest.json"),
        dl("Scene model (source of truth)", "scene.json"),
        dl("Verification report", "verify_report.json"),
        h("tr", null, h("td", null, "Everything"), h("td.small.muted", null, "zip"), h("td", null, h("a.btn.small", { href: `${base}/bundle/all` }, "Download")))))),
    h("div.card", null, h("h2", null, "Rooms"), h("table.simple", null,
      h("thead", null, h("tr", null, h("th", null, "Room"), h("th", null, "Clear size m"), h("th", null, "Area m²"), h("th", null, "Floor"), h("th"))),
      h("tbody", null, m.rooms.map((r) => h("tr", null, h("td", null, r.name), h("td", null, `${fmtM(r.dims_clear[0])} × ${fmtM(r.dims_clear[1])}`),
        h("td", null, fmtM(r.area_net, 1)), h("td.small", null, r.floor_material),
        h("td", null, h("button.btn.small", { onclick: () => { state.version = b.version; switchTab("review"); } }, "Edit"))))))),
    unrealCard(b));
  try {
    const { Viewer } = await import("./viewer.js");
    viewer = new Viewer(viewWrap, (info) => {
      if (!info) return picked.classList.add("hidden");
      picked.classList.remove("hidden");
      mount(picked, h("b", null, info.label || info.name), h("div.mono", null, info.name),
        info.room_name ? h("div", null, `Room: ${info.room_name}`) : null,
        info.size_m ? h("div", null, `Size: ${info.size_m.map((x) => fmtM(x)).join(" × ")} m`) : null,
        info.asset_id ? h("div", null, `Asset: ${info.asset_id}`) : null,
        info.kind ? h("div", null, `Kind: ${info.kind}`) : null,
        info.label && !info.asset_id ? h("div", null, info.label) : null,
        state.health.ai.configured ? h("button.btn.small", { style: { marginTop: "6px" }, onclick: () => fixPicked(b.version, info) }, "Fix this with AI…") : null);
    });
    await viewer.load(`${base}/files/export/web/scene.glb`, m);
  } catch (e) {
    mount(viewWrap, h("div.notice.error", { style: { margin: "20px" } }, "3D preview unavailable: ", String(e.message || e),
      " (run `npm install` in frontend/ to provide three.js)"));
  }
}

/** Selection id for the fix-with-AI request from a picked 3D object (manifest metadata). */
function pickedTarget(info) {
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

function unrealCard(b) {
  const ue = state.health.unreal;
  const card = h("div.card", null, h("h2", null, "Unreal Engine 5"));
  if (!ue.engine_found || !ue.project_found) {
    card.append(h("p.muted", null, ue.engine_found ? `Walkthrough project not found at ${ue.project}.` : "Unreal Engine was not found (set TWIN_UNREAL_ENGINE_DIR)."),
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
    ue.module_built ? null : h("div.notice.warn", null, "The walkthrough template is not compiled yet: run ", h("code", null, "python -m twin.unreal.setup"), " (see docs/UNREAL.md)."),
    h("div.row", null, importBtn, launchBtn), status);
  return card;
}
