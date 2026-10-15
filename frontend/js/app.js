// @ts-check
import { del, get, post, upload } from "./api.js";
import { state, emit, on } from "./state.js";
import { h, mount, ask, showError, toast, confirmDialog, notify, isRunningJob } from "./ui.js";
import { renderBrief, dropzone } from "./brief.js";
import { buildingRun, currentBuilding, latestRun, resume, startAutopilot, startBuilding } from "./autopilot.js";
import { renderReview } from "./editor.js";
import { renderGenerate } from "./generate.js";
import { renderActivity } from "./activity.js";
import { renderLive } from "./live.js";

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));

export async function refreshProject() {
  if (!state.project) return;
  state.project = await get(`/api/projects/${state.project.id}`);
  emit("project", state.project);
}

/** Poll a job until it finishes. onUpdate gets every snapshot. A long job that ends while the user looks elsewhere
 * says so (the autopilot sends its own, more specific message). */
export async function watchJob(jobId, onUpdate) {
  const t0 = Date.now();
  for (;;) {
    const job = await get(`/api/jobs/${jobId}`);
    onUpdate && onUpdate(job);
    if (["completed", "needs_review", "failed"].includes(job.status)) {
      if (job.kind !== "autopilot" && Date.now() - t0 > 20000) {
        notify(job.status === "failed" ? "Something failed" : "Done", `${JOB_LABELS[job.kind] || job.kind}: ${job.status.replace("_", " ")}`);
      }
      return job;
    }
    await new Promise((r) => setTimeout(r, 900));
  }
}

export function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.getAttribute("data-tab") === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  emit("tab", name);
}

function renderStatus() {
  const hl = state.health;
  if (!hl) return;
  const pill = (label, cls, title) => h("span.pill." + cls, { title }, h("span.dot"), label);
  const b = hl.ai_budget || {};
  const spend = b.cap_usd != null ? ` · $${(b.spent_usd || 0).toFixed(2)} / $${b.cap_usd.toFixed(2)}` : "";
  const ai = hl.ai.configured
    ? pill(`AI · ${hl.ai.model}${spend}`, b.cap_usd != null && b.remaining_usd <= 0 ? "bad" : "ok",
           `${hl.ai.api_style} @ ${hl.ai.endpoint_host}${b.cap_usd != null ? `
Spending cap $${b.cap_usd}, remaining $${b.remaining_usd}` : ""}`)
    : pill(hl.ai.key_present ? "AI · model not set" : "AI · not configured", "bad",
           "Set TWIN_AI_API_KEY and TWIN_AI_MODEL in .env, restart, then run: python -m twin.ai.probe");
  const bl = hl.blender.available
    ? pill(hl.blender.version.replace(/\(.*/, "").trim(), "ok", hl.blender.path)
    : pill("Blender · missing", "bad", hl.blender.error || "");
  const ueState = !hl.unreal.project_found ? ["Unreal · no project", "mid"]
    : !hl.unreal.module_built ? ["Unreal · template not compiled", "mid"] : ["Unreal · ready", "ok"];
  const ue = hl.unreal.engine_found
    ? pill(ueState[0], ueState[1], `${hl.unreal.engine_dir}\n${hl.unreal.project}` +
           (hl.unreal.module_built ? "" : "\nRun: python -m twin.unreal.setup"))
    : pill("Unreal · not found", "mid", "Set TWIN_UNREAL_ENGINE_DIR");
  mount($("systemStatus"), ai, bl, ue);
}

function renderProjectSelect() {
  const sel = /** @type {HTMLSelectElement} */ ($("projectSelect"));
  mount(sel, h("option", { value: "" }, state.projects.length ? "— choose a project —" : "no projects yet"),
    state.projects.map((p) => h("option", { value: p.id, selected: state.project?.id === p.id }, `${p.name}  ·  ${p.status}`)));
}

export async function openProject(pid) {
  if (!pid) {
    state.project = null;
    emit("project", null);
    return;
  }
  try {
    state.project = await get(`/api/projects/${pid}`);
    state.version = state.project.versions[0]?.number ?? null;
    state.buildId = state.project.builds.find((b) => b.status === "completed")?.id ?? state.project.builds[0]?.id ?? null;
    localStorage.setItem("twin.project", pid);
    emit("project", state.project);
    resume(state.project);   // an autopilot run still going is followed again
  } catch (e) {
    showError(e, "Could not open project");
  }
}

/**
 * Files dropped before any project exists. The server sorts them: one project with the files in it, or, when the
 * floor plans name two or more floors, one project per floor stacked as a multi-floor building.
 */
async function startFromFiles(files) {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const zone = document.querySelector("#emptyDrop .dropzone");
  if (zone) mount(zone, h("span.spinner"), ` Reading ${files.length} file${files.length > 1 ? "s" : ""}…`);
  try {
    const r = await upload("/api/drawing-sets", form);
    state.projects = await get("/api/projects");
    renderProjectSelect();
    if (r.kind === "building") {
      await openProject(r.floors[0].project_id);
      toast(h("div", null, `Found a building with ${r.floors.length} floors: ${r.floors.map((f) => f.label).join(", ")}. `,
        `Each floor is its own project, stacked as ${r.stack.name}.`,
        r.unassigned.length ? h("div", null, `Not assigned to a floor: ${r.unassigned.map((s) => s.name + (s.pages > 1 ? ` p.${s.page}` : "")).join(", ")}.`) : null),
        "ok", 12000);
    } else {
      await openProject(r.project.id);
      const ok = r.files.filter((f) => !f.error);
      toast(`Added ${ok.length} file${ok.length === 1 ? "" : "s"}` + (ok.length < r.files.length ? `; ${r.files.length - ok.length} could not be read` : "") + ".", "ok", 6000);
    }
    switchTab("brief");
  } catch (e) {
    showError(e, "Could not read the files");
    if (zone) mount(zone, "Drop your drawings here to start: floor plan, HVAC drawing, furniture list, photos");
  }
}

async function newProject() {
  const v = await ask("New project", [
    { name: "name", label: "Project name", value: "My apartment" },
    { name: "style", label: "Design style", value: "modern",
      options: Object.entries(state.catalog.styles).map(([k, s]) => [k, s.label]) },
  ], "Create");
  if (!v || !v.name) return;
  try {
    const p = await post("/api/projects", { name: v.name, brief: { style: v.style } });
    state.projects = await get("/api/projects");
    renderProjectSelect();
    await openProject(p.id);
    switchTab("brief");
    toast("Project created. Add your floor plan and the dimensions you know.", "ok");
  } catch (e) {
    showError(e, "Could not create project");
  }
}

async function deleteProject() {
  const p = state.project;
  if (!p) return;
  const n = (k, one) => `${k} ${one}${k === 1 ? "" : "s"}`;
  const ok = await confirmDialog("Delete project",
    `Delete "${p.name}" with its ${n(p.files.length, "uploaded file")}, ${n(p.versions.length, "layout version")} and ` +
    `${n(p.builds.length, "3D model build")}? This cannot be undone.`, "Delete project", true);
  if (!ok) return;
  try {
    await del(`/api/projects/${p.id}`);
  } catch (e) {
    showError(e, "Could not delete project");
    return;
  }
  for (const key of [`twin.plan.${p.id}`, `twin.refsOff.${p.id}`]) localStorage.removeItem(key);
  if (localStorage.getItem("twin.project") === p.id) localStorage.removeItem("twin.project");
  state.projects = state.projects.filter((x) => x.id !== p.id);
  state.version = null;
  state.buildId = null;
  await openProject("");
  toast(`Deleted ${p.name}`, "ok");
}

/**
 * Where the project stands and the one thing to do next (a slim bar under the tabs, plus ticks on finished steps).
 * Steps: 1 plan uploaded + layout created, 2 layout ready to build, 3 3D model built for the newest version.
 */
function projectProgress(p) {
  const plan = p.files.some((f) => f.kind === "plan");
  const v = p.versions[0];
  const built = v && p.builds.find((b) => b.version === v.number && b.status === "completed");
  // a job left "processing" by a server restart must not hold the bar forever
  const running = p.jobs?.find((j) => ["queued", "processing"].includes(j.status) && Date.now() / 1000 - j.created_at < 3600);
  const ap = latestRun();
  const bj = buildingRun();
  const apRunning = isRunningJob(ap) || isRunningJob(bj);
  const counts = v?.counts || {};
  const blocking = (counts.error || 0) + (counts.review || 0);
  const done = { brief: !!v, review: !!v?.can_generate, generate: !!built };
  const aiOn = !!state.health?.ai?.configured;
  let msg, action = null, tab = null, cls = "", auto = null;
  if (isRunningJob(bj)) {
    msg = `Making the building${bj.stage ? `: ${bj.stage}` : "…"} You can leave this page; it tells you when it is done.`;
  } else if (apRunning) {
    msg = `Making your 3D model${ap.stage ? `: ${ap.stage}` : "…"} You can leave this page; it tells you when it is done.`;
  } else if (running) {
    msg = `Working: ${JOB_LABELS[running.kind] || running.kind}${running.stage ? ` (${running.stage})` : ""}…`;
  } else if (!v && !plan) {
    msg = "Start by dropping in your drawings: the floor plan (PDF, PNG or JPG), and the HVAC drawing, furniture list or photos if you have them.";
    action = "Add drawings"; tab = "brief";
  } else if (!v) {
    msg = aiOn ? "Your drawings are in. One click reads them and builds the 3D model." : "Your plan is uploaded. Set up the AI to read it, or trace it by hand.";
    const whole = currentBuilding();
    if (aiOn) auto = whole ? { label: "Make the whole building", building: true, title: `Read and build all ${whole.floors.length} floors of ${whole.name}, then stack them` }
      : { label: "Make the 3D model", title: "Read the plan, fix the problems, accept the estimates and build the model" };
    else { action = "Other ways to start"; tab = "brief"; }
  } else if (!v.can_generate) {
    const stopped = ap?.result?.stopped && ap.result.version === v.number ? ap.result.stopped : null;
    msg = stopped?.code === "NEEDS_YOU" ? stopped.message
      : `Layout v${v.number} has ${blocking} thing${blocking === 1 ? "" : "s"} to sort out before the 3D model can be built. Most are fixed automatically.`;
    auto = { label: "Finish automatically", title: "Fix what can be fixed, accept the estimates and build the model" };
    action = "Look at the layout"; tab = "review";
  } else if (!built) {
    msg = `Layout v${v.number} is ready${counts.warning ? ` (${counts.warning} minor thing${counts.warning === 1 ? "" : "s"} worth a look)` : ""}. Build the 3D model next.`;
    action = "Build the 3D model"; tab = "generate";
  } else {
    msg = `The 3D model of v${v.number} is ready. Walk through it, or see the live HVAC view.`;
    action = "View in 3D"; tab = "generate"; cls = "done";
  }
  return { msg, action, tab, done, cls, running: !!running || apRunning, built, auto, apRunning };
}

export const JOB_LABELS = { autopilot: "making the 3D model", building: "making the building", interpret: "the AI is reading the plan", fix: "the AI is changing the layout", solve: "fixing problems",
  furniture: "detecting furniture", build: "building the 3D model", layer_build: "updating the 3D model",
  unreal_import: "preparing the Unreal scene", hvac_import: "reading the HVAC drawing", redesign: "redesigning a room" };

function renderNextStep() {
  const el = $("nextStep");
  const p = state.project;
  document.querySelectorAll(".tab .tick").forEach((t) => t.remove());
  if (!p) { el.classList.add("hidden"); return; }
  const st = projectProgress(p);
  for (const [tab, ok] of Object.entries(st.done)) {
    if (ok) document.querySelector(`.tab[data-tab=${tab}]`)?.append(h("span.tick", { title: "done" }, "✓"));
  }
  const steps = [["brief", "1 Plan"], ["review", "2 Layout"], ["generate", "3 3D model"]];
  const now = steps.find(([t]) => !st.done[t])?.[0];
  el.className = "next-step" + (st.cls ? " " + st.cls : "");
  mount(el,
    h("div.steps", null, steps.map(([t, label]) => h("span" + (st.done[t] ? ".done" : t === now ? ".now" : ""), null, (st.done[t] ? "✓ " : "") + label))),
    st.running ? h("span.spinner", { title: "working" }) : null,
    h("div.msg", null, st.msg),
    st.apRunning && state.activeTab !== "brief" ? h("button.btn.small", { onclick: () => switchTab("brief"), title: "See every step" }, "Details") : null,
    st.auto ? h("button.btn.primary.small", { title: st.auto.title, onclick: () => (st.auto.building ? startBuilding() : startAutopilot()) }, st.auto.label) : null,
    st.built && state.health?.desktop_viewer?.available
      ? h("button.btn.primary.small", { title: "Walk through the newest 3D model in Unreal Engine on this PC", onclick: () => viewInUnreal(p, st.built) },
        "View in Unreal Engine") : null,
    st.action && state.activeTab !== st.tab ? h("button.btn" + (st.built || st.auto ? "" : ".primary") + ".small", { onclick: () => switchTab(st.tab) }, st.action + " →") : null);
}

/** One click from anywhere: the newest built model opens in the packaged Unreal viewer on this PC. */
async function viewInUnreal(p, build) {
  try {
    const r = await post(`/api/projects/${p.id}/viewer/open-local`, { build_id: build.id });
    toast(r.started ? "Opening the 3D model in Unreal Engine… (a few seconds)" : "The Unreal viewer is already opening.", "ok", 6000);
  } catch (e) { showError(e, "View in Unreal Engine"); }
}

function updateVisibility() {
  const has = !!state.project;
  $("emptyState").classList.toggle("hidden", has);
  $("deleteProjectBtn").classList.toggle("hidden", !has);
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", !has));
  document.querySelectorAll(".tab").forEach((t) => /** @type {HTMLButtonElement} */ (t).disabled = !has);
}

async function init() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.getAttribute("data-tab"))));
  $("newProjectBtn").addEventListener("click", newProject);
  $("deleteProjectBtn").addEventListener("click", deleteProject);
  $("emptyNewBtn").addEventListener("click", newProject);
  mount($("emptyDrop"), dropzone(startFromFiles, "Drop your drawings here to start: floor plan, HVAC drawing, furniture list, photos"));
  $("projectSelect").addEventListener("change", (e) => openProject(/** @type {HTMLSelectElement} */ (e.target).value));
  try {
    [state.health, state.catalog, state.projects] = await Promise.all([get("/api/health"), get("/api/catalog"), get("/api/projects")]);
  } catch (e) {
    showError(e, "Startup");
    return;
  }
  renderStatus();
  renderProjectSelect();
  on("project", () => { renderProjectSelect(); updateVisibility(); renderNextStep(); });
  on("tab", () => renderNextStep());
  on("autopilot", () => renderNextStep());
  on("project", () => get("/api/projects").then((p) => { state.projects = p; renderProjectSelect(); }).catch(() => {}));
  renderBrief($("tab-brief"));
  renderReview($("tab-review"));
  renderGenerate($("tab-generate"));
  renderActivity($("tab-activity"));
  renderLive($("tab-live"));
  setInterval(() => get("/api/health").then((hl) => { state.health = hl; renderStatus(); renderNextStep(); }).catch(() => {}), 30000);
  const last = localStorage.getItem("twin.project");
  if (last && state.projects.some((p) => p.id === last)) await openProject(last);
  else updateVisibility();
}

init();
