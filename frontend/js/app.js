// @ts-check
import { get, post } from "./api.js";
import { state, emit, on } from "./state.js";
import { h, mount, ask, showError, toast } from "./ui.js";
import { renderBrief } from "./brief.js";
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

/** Poll a job until it finishes. onUpdate gets every snapshot. */
export async function watchJob(jobId, onUpdate) {
  for (;;) {
    const job = await get(`/api/jobs/${jobId}`);
    onUpdate && onUpdate(job);
    if (["completed", "needs_review", "failed"].includes(job.status)) return job;
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

async function openProject(pid) {
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
  } catch (e) {
    showError(e, "Could not open project");
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

function updateVisibility() {
  const has = !!state.project;
  $("emptyState").classList.toggle("hidden", has);
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", !has));
  document.querySelectorAll(".tab").forEach((t) => /** @type {HTMLButtonElement} */ (t).disabled = !has);
}

async function init() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.getAttribute("data-tab"))));
  $("newProjectBtn").addEventListener("click", newProject);
  $("emptyNewBtn").addEventListener("click", newProject);
  $("projectSelect").addEventListener("change", (e) => openProject(/** @type {HTMLSelectElement} */ (e.target).value));
  try {
    [state.health, state.catalog, state.projects] = await Promise.all([get("/api/health"), get("/api/catalog"), get("/api/projects")]);
  } catch (e) {
    showError(e, "Startup");
    return;
  }
  renderStatus();
  renderProjectSelect();
  on("project", () => { renderProjectSelect(); updateVisibility(); });
  on("project", () => get("/api/projects").then((p) => { state.projects = p; renderProjectSelect(); }).catch(() => {}));
  renderBrief($("tab-brief"));
  renderReview($("tab-review"));
  renderGenerate($("tab-generate"));
  renderActivity($("tab-activity"));
  renderLive($("tab-live"));
  setInterval(() => get("/api/health").then((hl) => { state.health = hl; renderStatus(); }).catch(() => {}), 30000);
  const last = localStorage.getItem("twin.project");
  if (last && state.projects.some((p) => p.id === last)) await openProject(last);
  else updateVisibility();
}

init();
