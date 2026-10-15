// @ts-check
/**
 * Autopilot: one click from the uploaded drawings to a built 3D model (twin/api/autopilot.py).
 *
 * A run is one job that starts the usual jobs in order (read the plan, HVAC, furniture, fix problems, accept the
 * estimates, build). This module starts it, follows it from every tab (also a run that was going before a page
 * reload), and shows what it did and what it assumed, so the user checks the result instead of every step.
 *
 * A floor of a multi-floor building (a stack) offers "Make the whole building" instead: one run per floor that is
 * not built yet, then the stacked building for the Unreal viewer.
 */
import { get, post } from "./api.js";
import { openProject, refreshProject, switchTab, watchJob } from "./app.js";
import { planFileId, referenceIds } from "./brief.js";
import { state, on, emit } from "./state.js";
import { h, mount, toast, showError, friendly, notify, askNotifications, jobProgress, isRunningJob } from "./ui.js";

const following = new Set();
let run = null;          // newest autopilot job seen for the open project
let planCache = null;    // {key, data}: what a run would do (GET .../autopilot/plan)
let cardEl = null;       // the card in Brief & files, redrawn on every update
const KEY_REVIEW = "twin.autopilot.reviewFirst";
let reviewFirst = (() => { try { return localStorage.getItem(KEY_REVIEW) === "1"; } catch { return false; } })();

const ICON = { done: "✓", running: "", skipped: "–", failed: "✗", stopped: "■", waiting: "○", pending: "○" };
const TARGET_NAMES = { room: ["room", "rooms"], wall: ["wall", "walls"], opening: ["door/window", "doors and windows"],
  furniture: ["furniture item", "furniture items"], level: ["wall height", "wall heights"], sensor: ["sensor", "sensors"],
  hvac_unit: ["HVAC unit", "HVAC units"], hvac_terminal: ["air outlet", "air outlets"], duct: ["duct", "ducts"] };

/** The newest autopilot job of the open project (from the live run, else from the project's job list). */
export function latestRun() {
  const p = state.project;
  if (!p) return null;
  if (run && run.project_id === p.id) return run;
  return (p.jobs || []).find((j) => j.kind === "autopilot") || null;
}
export const autopilotRunning = () => isRunningJob(latestRun());

/** Start a run with the user's options; opts can override them (e.g. {fresh: true}). */
export async function startAutopilot(opts = {}) {
  const p = state.project;
  if (!p) return;
  askNotifications();
  try {
    const job = await post(`/api/projects/${p.id}/autopilot`, {
      review_first: reviewFirst, plan_file_id: planFileId(), reference_file_ids: referenceIds(), ...opts });
    toast("Making the 3D model. You can leave this page; it keeps working and tells you when it is done.", "ok", 7000);
    follow(job).catch((e) => showError(e, "Make the 3D model"));
  } catch (e) { showError(e, "Make the 3D model"); }
}

async function stop() {
  const j = latestRun();
  if (!j) return;
  try {
    await post(`/api/jobs/${j.id}/cancel`);
    toast("Stopping after the current step. Everything finished so far is kept.", "info", 6000);
  } catch (e) { showError(e, "Stop"); }
}

/** Follow a run to its end; every finished step saved a version, so the project is refreshed as steps complete. */
export async function follow(job) {
  if (following.has(job.id)) return;
  following.add(job.id);
  run = job;
  emit("autopilot", run);
  let settled = 0;
  try {
    const final = await watchJob(job.id, (j) => {
      run = j;
      const n = (j.result?.steps || []).filter((s) => !["pending", "running"].includes(s.status)).length;
      if (n !== settled) { settled = n; refreshProject().catch(() => {}); }
      emit("autopilot", run);
    });
    run = final;
    await refreshProject();
    finished(final);
  } finally {
    following.delete(job.id);
    emit("autopilot", run);
  }
}

/** Pick up a run that is still going (after a page reload, or one started in another tab). */
export function resume(p) {
  const j = (p?.jobs || []).find((x) => x.kind === "autopilot" && isRunningJob(x) && Date.now() / 1000 - x.created_at < 6 * 3600);
  if (j) follow(j).catch(() => {});
}

function finished(job) {
  const r = job.result || {};
  if (isRunningJob(buildingRun())) return;   // a floor of a building run: the building run reports at the end
  const mine = state.project?.id === job.project_id;
  const name = state.projects.find((p) => p.id === job.project_id)?.name || "your project";
  if (job.status === "failed") {
    notify("The 3D model could not be made", friendly(job.error));
    if (mine) showError(job.error || { message: "failed" }, "Make the 3D model");
    return;
  }
  if (job.status === "completed") {
    notify("Your 3D model is ready", `${name}: built and verified${r.assumptions?.length ? `, ${r.assumptions.length} estimates to check` : ""}.`);
    if (!mine) return;
    state.version = r.version;
    if (r.build_id) state.buildId = r.build_id;
    emit("version", r.version);
    switchTab("generate");
    toast(`The 3D model is ready${r.assumptions?.length ? `. It assumed ${r.assumptions.length} estimated values; the list is above the model` : ""}.`, "ok", 9000);
    return;
  }
  const st = r.stopped || {};
  notify(st.code === "STOPPED" ? "Stopped" : "Your input is needed", `${name}: ${st.message || ""}`);
  if (!mine) return;
  if (r.version) { state.version = r.version; emit("version", r.version); }
  if (["NEEDS_YOU", "REVIEW_FIRST"].includes(st.code)) switchTab("review");
  toast(st.message || "The autopilot stopped.", st.code === "NEEDS_YOU" ? "info" : "ok", 10000);
}

// ---------------------------------------------------------------------------------------------
// Brief & files: the card that starts it and shows how it is going
// ---------------------------------------------------------------------------------------------
export function autopilotCard() {
  cardEl = h("div.card.autopilot");
  drawCard();
  return cardEl;
}
on("autopilot", () => drawCard());

// ---------------------------------------------------------------------------------------------
// A floor of a multi-floor building: "Make the whole building"
// ---------------------------------------------------------------------------------------------
let building = null;          // {pid, stack, job, loaded}: the building the open project is a floor of, its newest run
const followingBuilding = new Set();

/** The newest "Make the whole building" run of the open project's building, if it has one. */
export const buildingRun = () => (building && building.pid === state.project?.id ? building.job : null);
/** The multi-floor building the open project is a floor of (once known), else null. */
export const currentBuilding = () => (building && building.pid === state.project?.id ? building.stack : null);

function loadBuilding(p) {
  if (building?.pid === p.id) return building;
  building = { pid: p.id, stack: null, job: null, loaded: false };
  const pid = p.id;
  get(`/api/stacks?project_id=${encodeURIComponent(pid)}`).then(async (list) => {
    if (building?.pid !== pid) return;
    building.stack = list.find((s) => s.floors.length >= 2) || null;
    if (building.stack) {
      building.job = await get(`/api/stacks/${building.stack.id}/autopilot`).catch(() => null);
      if (isRunningJob(building.job)) followBuilding(building.job).catch(() => {});
    }
    building.loaded = true;
    emit("autopilot", run);
  }).catch(() => {});
  return building;
}
on("project", (p) => { if (p && building?.pid !== p.id) loadBuilding(p); });

export async function startBuilding() {
  const st = building?.stack;
  if (!st) return;
  askNotifications();
  try {
    const job = await post(`/api/stacks/${st.id}/autopilot`, { review_first: reviewFirst });
    toast(`Making ${st.name}: every floor is read and built (two at a time), then they are stacked. You can leave this page.`, "ok", 8000);
    followBuilding(job).catch((e) => showError(e, "Make the building"));
  } catch (e) { showError(e, "Make the building"); }
}

async function stopBuilding() {
  const j = buildingRun();
  if (!j) return;
  try {
    await post(`/api/jobs/${j.id}/cancel`);
    toast("Stopping: every floor stops after the step it is on and keeps what it finished.", "info", 6000);
  } catch (e) { showError(e, "Stop"); }
}

async function followBuilding(job) {
  if (followingBuilding.has(job.id)) return;
  followingBuilding.add(job.id);
  const keep = (j) => { if (building?.stack?.id === j.project_id) building.job = j; emit("autopilot", run); };
  keep(job);
  let built = -1;
  try {
    const final = await watchJob(job.id, (j) => {
      keep(j);
      if ((j.result?.built ?? 0) !== built) { built = j.result?.built ?? 0; refreshProject().catch(() => {}); }
    });
    keep(final);
    await refreshProject();
    buildingFinished(final);
  } finally { followingBuilding.delete(job.id); }
}

function buildingFinished(job) {
  const r = job.result || {};
  const name = r.name || "The building";
  if (job.status === "failed") {
    notify("The building could not be made", friendly(job.error));
    return showError(job.error || { message: "failed" }, "Make the building");
  }
  const here = (r.floors || []).find((f) => f.project_id === state.project?.id);
  if (here?.build_id) state.buildId = here.build_id;
  if (job.status === "completed") {
    notify("Your building is ready", `${name}: ${r.floors.length} floors built and stacked.`);
    toast(`${name} is ready: ${r.floors.length} floors built and stacked. View it in Unreal Engine from Generate & preview.`, "ok", 10000);
    return switchTab("generate");
  }
  notify(r.stopped?.code === "STOPPED" ? "Stopped" : "Your input is needed", `${name}: ${r.stopped?.message || ""}`);
  toast(`${name}: ${r.stopped?.message || "stopped"}`, r.stopped?.code === "STOPPED" ? "ok" : "info", 12000);
}

function floorList(st, job) {
  const live = job?.result?.floors;
  const names = st.project_names || {};
  return h("ol.ap-steps", null, st.floors.map((f, i) => {
    const lf = live?.find((x) => x.project_id === f.project_id);
    const status = lf ? lf.status : "pending";
    const here = f.project_id === state.project?.id;
    return h("li.ap-step." + status, null,
      h("span.ap-icon", null, status === "running" ? h("span.spinner") : ICON[status] || "○"),
      h("span.ap-label", null, here ? h("b", null, f.label || `Floor ${i + 1}`)
        : h("a", { href: "#", title: `Open ${names[f.project_id] || "this floor"}`,
          onclick: (e) => { e.preventDefault(); openProject(f.project_id); } }, f.label || `Floor ${i + 1}`)),
      h("span.ap-note", null, lf ? (status === "running" ? lf.stage : lf.note) : names[f.project_id] || ""));
  }));
}

/** The card for a floor of a building; null when the open project is not one (or not known yet). */
function buildingCard(p) {
  const b = loadBuilding(p);
  const st = b.stack;
  if (!st) return null;
  const j = b.job;
  if (isRunningJob(j)) {
    return [h("div.row", null, h("h2", { style: { margin: 0 } }, `Making ${st.name}`), h("span.spacer"),
      h("button.btn.small", { onclick: stopBuilding, title: "Every floor stops after the step it is on and keeps what it finished" }, "Stop")),
    h("p.small.muted", null, "Every floor is read, fixed and built, two at a time; then they are stacked into one building. ",
      "You can leave this page; it tells you when it is done."),
    jobProgress(j),
    floorList(st, j)];
  }
  const r = j?.result;
  return [h("h2", null, "Make the whole building"),
    h("p.small", { style: { margin: "0 0 8px" } }, `This floor is part of ${st.name} (${st.floors.length} floors). One click reads, fixes and `,
      "builds every floor that is not built yet, then stacks them into one building you can walk through in Unreal Engine."),
    floorList(st, j),
    h("div.row", { style: { marginTop: "10px" } },
      h("button.btn.primary.big", { onclick: startBuilding }, `Make the whole building (${st.floors.length} floors)`),
      h("button.btn", { onclick: () => startAutopilot(), title: "Run the autopilot for this floor only" }, "Only this floor")),
    j?.status === "completed" ? h("div.notice.ok", { style: { marginTop: "10px" } }, h("b", null, "Last run: "),
      `${r.floors.length} floors built and stacked. View it from Generate & preview → Multi-floor building.`)
      : r?.stopped ? h("div.notice." + (r.stopped.code === "STOPPED" ? "ok" : "warn"), { style: { marginTop: "10px" } },
        h("b", null, "Last run: "), r.stopped.message) : null];
}

function loadPlan(p) {
  const files = p.files.map((f) => `${f.id}:${f.kind}`).join(",");
  const key = `${p.id}|${reviewFirst}|${files}|${p.versions.length}|${p.versions.filter((v) => v.source === "furniture_list").length}`;
  if (planCache?.key === key) return planCache.data;
  planCache = { key, data: null };
  get(`/api/projects/${p.id}/autopilot/plan?review_first=${reviewFirst}`)
    .then((d) => { if (planCache?.key === key) { planCache.data = d; drawCard(); } }).catch(() => {});
  return null;
}

function stepList(steps, live) {
  return h("ol.ap-steps", null, steps.map((s) => {
    const status = live ? s.status : (s.run ? "pending" : "skipped");
    return h("li.ap-step." + status, null,
      h("span.ap-icon", null, status === "running" ? h("span.spinner") : ICON[status] || "○"),
      h("span.ap-label", null, s.label),
      s.note ? h("span.ap-note", null, s.note) : null);
  }));
}

function drawCard() {
  if (!cardEl) return;
  const p = state.project;
  if (!p) return mount(cardEl);
  const whole = buildingCard(p);
  if (whole) return mount(cardEl, whole);
  const j = latestRun();
  const running = isRunningJob(j);
  if (running) {
    const r = j.result || {};
    return mount(cardEl,
      h("div.row", null, h("h2", { style: { margin: 0 } }, "Making your 3D model"), h("span.spacer"),
        h("button.btn.small", { onclick: stop, title: "Stop after the step it is on; everything finished is kept" }, "Stop")),
      h("p.small.muted", null, "You can leave this page or close it: the work carries on, and this page tells you when it is done."),
      jobProgress(j),
      r.steps ? stepList(r.steps, true) : null);
  }
  const plan = loadPlan(p);
  const hasLayout = p.versions.length > 0;
  const blocked = plan?.blocked;
  const cb = h("input", { type: "checkbox", checked: reviewFirst, onchange: (e) => {
    reviewFirst = e.target.checked;
    try { localStorage.setItem(KEY_REVIEW, reviewFirst ? "1" : "0"); } catch { /* private window */ }
    drawCard();
  } });
  const go = h("button.btn.primary.big", { disabled: !plan || !!blocked, onclick: () => startAutopilot() },
    hasLayout ? (reviewFirst ? "Fix it for me" : "Finish it for me") : "Make the 3D model");
  const budget = plan?.budget_remaining_usd;
  return mount(cardEl,
    h("h2", null, hasLayout ? "Finish the 3D model for me" : "Make the 3D model for me"),
    h("p.small", { style: { margin: "0 0 8px" } }, hasLayout
      ? `Starts from layout v${p.versions[0].number}: fixes what it can, accepts the estimates and builds the 3D model. You check what it assumed afterwards.`
      : "Reads your drawings, places the HVAC drawing and the furniture, fixes the problems, accepts the estimates and builds the 3D model. You check what it assumed afterwards."),
    plan ? stepList(plan.steps, false) : h("p.small.muted", null, "Checking your files…"),
    blocked ? h("div.notice.warn", { style: { margin: "8px 0" } }, blocked) : null,
    h("div.row", { style: { marginTop: "10px" } }, go,
      h("label.small", { title: "Stop after fixing problems, so you look at the layout before anything is accepted or built" }, cb,
        " Let me check the layout before it is built")),
    h("div.small.muted", { style: { marginTop: "6px" } },
      plan?.ai_configured ? `Uses the AI (it stops at the spending cap${budget != null ? `: $${budget.toFixed(2)} left` : ""}). Every step is saved as its own version; nothing is overwritten.`
        : "The AI is not set up: only free fixes are made."),
    hasLayout ? h("div.small", { style: { marginTop: "6px" } }, h("a", { href: "#", onclick: (e) => { e.preventDefault(); startAutopilot({ fresh: true }); } },
      "Read the plan again from scratch")) : null,
    j && !running ? lastRunNote(j) : null);
}

function lastRunNote(j) {
  const r = j.result || {};
  if (j.status === "failed") return h("div.notice.error", { style: { marginTop: "10px" } }, h("b", null, "Last run failed: "), friendly(j.error));
  if (r.stopped) return h("div.notice." + (r.stopped.code === "NEEDS_YOU" ? "warn" : "ok"), { style: { marginTop: "10px" } }, h("b", null, "Last run: "), r.stopped.message);
  return null;
}

// ---------------------------------------------------------------------------------------------
// Generate & preview: what the run did and what it assumed, above the model it built
// ---------------------------------------------------------------------------------------------
export function autopilotSummary(build) {
  const j = latestRun();
  const r = j?.result;
  if (!r || j.status !== "completed" || r.build_id !== build.id) return null;
  const groups = {};
  for (const t of r.assumptions || []) (groups[t.split(":")[0]] ||= []).push(t);
  const listEl = h("div.ap-assume-list");
  const open = (type) => {
    const items = groups[type];
    mount(listEl, h("div.small.muted", null, `Click one to see it on the plan (${items.length}):`),
      h("div.ap-chips", null, items.slice(0, 60).map((t) => h("button.btn.tiny", { onclick: () => {
        state.version = r.version;
        switchTab("review");
        emit("select", t);
      } }, t.split(":")[1]))), items.length > 60 ? h("span.small.muted", null, ` and ${items.length - 60} more`) : null);
  };
  const n = r.assumptions?.length || 0;
  return h("div.card.ap-summary", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "Made automatically"), h("span.spacer"),
      r.cost_usd ? h("span.small.muted", null, `AI cost about $${r.cost_usd.toFixed(2)}`) : null),
    h("div.ap-done", null, r.steps.filter((s) => s.status === "done").map((s) => h("span.ap-chip", { title: s.note || "" }, "✓ ", s.label))),
    n ? h("div", { style: { marginTop: "8px" } },
      h("div.small", null, h("b", null, `Built with ${n} estimated value${n > 1 ? "s" : ""}`),
        " the AI read or scaled from the drawing. Worth a look: ",
        Object.entries(groups).map(([type, items]) => h("button.btn.small", { style: { marginLeft: "4px" }, onclick: () => open(type) },
          `${items.length} ${(TARGET_NAMES[type] || [type, type + "s"])[items.length > 1 ? 1 : 0]}`))),
      listEl) : h("div.small", { style: { marginTop: "8px" } }, "Nothing had to be assumed."),
    r.questions?.length ? h("details.small", { style: { marginTop: "6px" } },
      h("summary", null, `${r.questions.length} AI question${r.questions.length > 1 ? "s" : ""} answered "as drawn"`),
      h("ul", null, r.questions.map((q) => h("li", null, q.question)))) : null);
}
