// @ts-check
/**
 * Brief & files: one drop zone for every drawing (each file is sorted by the server: floor plan, HVAC drawing,
 * furniture list, sensor sheet or reference image), the one-click autopilot, and the optional brief (saved as you type).
 */
import { del, patch, post, upload } from "./api.js";
import { refreshProject, switchTab, watchJob } from "./app.js";
import { autopilotCard, autopilotRunning } from "./autopilot.js";
import { state, on, emit } from "./state.js";
import { h, mount, toast, showError, badge, fmtBytes, confirmDialog, jobProgress } from "./ui.js";

let root;
let interpretJob = null;
let jobEl = null;              // where the interpret job is drawn; replaced on every redraw of the tab
const following = new Set();   // interpret jobs being polled
let briefOpen = null;          // the user opened / closed the brief section

const KIND_LABEL = { plan: "Floor plan", hvac: "HVAC drawing", furniture_list: "Furniture list", sensor_sheet: "Sensor sheet",
  reference: "Style reference" };
const ACCEPT = ".png,.jpg,.jpeg,.pdf,.csv";

/** The plan in use: the one the user picked, else the newest vector PDF (it carries the scale), else the newest. */
export function planFileId() {
  const files = state.project?.files || [];
  const saved = localStorage.getItem(`twin.plan.${state.project?.id}`);
  const plans = files.filter((f) => f.kind === "plan");
  const pdfs = plans.filter((f) => f.sniffed === "pdf");
  return (plans.find((f) => f.id === saved) || pdfs[pdfs.length - 1] || plans[plans.length - 1])?.id || null;
}

export function referenceIds() {
  const off = JSON.parse(localStorage.getItem(`twin.refsOff.${state.project?.id}`) || "[]");
  return (state.project?.files || []).filter((f) => f.kind === "reference" && !off.includes(f.id)).map((f) => f.id);
}

/**
 * Upload files into a project, each sorted by the server (kind=auto, PDF page picked automatically).
 * @param {string} pid @param {File[]} files @param {{page?: string}} [opts]
 */
export async function uploadFiles(pid, files, opts = {}) {
  const done = [];
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", "auto");
    form.append("page", opts.page || "0");
    try {
      done.push(await upload(`/api/projects/${pid}/files`, form));
    } catch (e) { showError(e, `Upload of ${file.name} failed`); }
  }
  const plans = done.filter((r) => r.kind === "plan");
  const best = plans.filter((r) => r.sniffed === "pdf").pop() || plans.pop();
  if (best) localStorage.setItem(`twin.plan.${pid}`, best.id);
  if (done.length) {
    toast(h("div", null, `Added ${done.length} file${done.length > 1 ? "s" : ""}:`,
      h("ul.toast-list", null, done.map((r) => h("li", null, h("b", null, r.original_name), ` → ${KIND_LABEL[r.kind] || r.kind}`,
        r.page_reason ? ` (${r.page_reason})` : "")))), "ok", 8000);
  }
  return done;
}

export function renderBrief(el) {
  root = el;
  on("project", draw);
  on("health", draw);
}

function numberInput(value, onchange, attrs = {}) {
  return h("input", { type: "number", step: "0.01", min: "0", value: value ?? "", onchange, ...attrs });
}

function draw() {
  const p = state.project;
  if (!p) return mount(root);
  mount(root, h("div.grid2", null,
    h("div", null, autopilotCard(), briefCard(), otherStartsCard()),
    h("div", null, filesCard())));
}

// ---------------------------------------------------------------------------------------------
// the brief: optional, saved as you type
// ---------------------------------------------------------------------------------------------
function briefCard() {
  const p = state.project;
  const brief = structuredClone(p.brief);
  const styles = state.catalog.styles;
  const savedEl = h("span.small.muted");
  let timer = null;
  const save = (refresh = false) => {
    clearTimeout(timer);
    mount(savedEl, "Saving…");
    timer = setTimeout(async () => {
      try {
        await patch(`/api/projects/${p.id}`, { name: nameInput.value || p.name, brief });
        state.project.brief = structuredClone(brief);   // no redraw: the user may still be typing
        mount(savedEl, "Saved");
        if (refresh) await refreshProject();
      } catch (e) { mount(savedEl); showError(e, "Saving the brief failed"); }
    }, 600);
  };
  const set = (fn) => (e) => { fn(e.target.value); save(); };
  const num = (v) => (v === "" ? null : Number(v));
  const nameInput = h("input", { value: p.name, style: { minWidth: "220px" }, onchange: () => save(true) });
  const known = h("tbody");
  const drawKnown = () => mount(known, brief.known_rooms.map((k, i) => h("tr", null,
    h("td", null, h("input", { value: k.name, oninput: set((v) => (k.name = v)) })),
    h("td", null, numberInput(k.width, set((v) => (k.width = Number(v))))),
    h("td", null, numberInput(k.depth, set((v) => (k.depth = Number(v))))),
    h("td", null, h("select", { onchange: set((v) => (k.basis = v)) },
      h("option", { value: "clear", selected: k.basis === "clear" }, "clear (wall face to face)"),
      h("option", { value: "centreline", selected: k.basis === "centreline" }, "centreline"))),
    h("td", null, h("button.btn.small.danger", { onclick: () => { brief.known_rooms.splice(i, 1); drawKnown(); save(); } }, "Remove")))));
  drawKnown();
  const hasAny = brief.prompt || brief.overall_width || brief.known_rooms.length;
  return h("details.card.fold", { open: briefOpen ?? !!hasAny, ontoggle: (e) => { briefOpen = e.target.open; } },
    h("summary", null, "Name, style and sizes you know ", h("span.small.muted", null, "(optional)"), " ", savedEl),
    h("p.small.muted", null, "Leave it empty and the AI takes everything from the drawings. Sizes you enter here are used to check the layout."),
    h("div.row", null, h("label.field", null, "Project name", nameInput),
      h("label.field", null, "Design style", h("select", { onchange: set((v) => (brief.style = v)) },
        Object.entries(styles).map(([k, s]) => h("option", { value: k, selected: brief.style === k }, s.label))))),
    h("label.field", { style: { marginTop: "10px" } }, "Anything the AI should know?",
      h("textarea", { value: brief.prompt, placeholder: "e.g. Warm modern flat, oak floors, a large L-shaped sofa, home office in the second bedroom…",
        oninput: set((v) => (brief.prompt = v)) })),
    h("div.row", null,
      h("label.field", null, "Overall width (outer faces, m)", numberInput(brief.overall_width, set((v) => (brief.overall_width = num(v))))),
      h("label.field", null, "Overall depth (outer faces, m)", numberInput(brief.overall_depth, set((v) => (brief.overall_depth = num(v))))),
      h("label.field", null, "Wall height (m)", numberInput(brief.wall_height, set((v) => (brief.wall_height = Number(v))), { min: "1.8", max: "12" }))),
    h("h3", null, "Room sizes you know"),
    h("table.simple", null, h("thead", null, h("tr", null, h("th", null, "Room name (as on plan)"), h("th", null, "Width m"),
      h("th", null, "Depth m"), h("th", null, "Measured"), h("th"))), known),
    h("div.row", { style: { marginTop: "8px" } },
      h("button.btn.small", { onclick: () => { brief.known_rooms.push({ name: "Room", width: 3, depth: 3, basis: "clear" }); drawKnown(); save(); } }, "+ Add room size")));
}

// ---------------------------------------------------------------------------------------------
// files: one drop zone, sorted by the server, correctable on the thumbnail
// ---------------------------------------------------------------------------------------------
function filesCard() {
  const p = state.project;
  const selectedPlan = planFileId();
  const off = JSON.parse(localStorage.getItem(`twin.refsOff.${p.id}`) || "[]");
  const order = ["plan", "hvac", "furniture_list", "sensor_sheet", "reference"];
  const byKind = order.map((k) => [k, p.files.filter((f) => f.kind === k)]).filter(([, fs]) => fs.length);

  const thumb = (f) => {
    const isPlan = f.kind === "plan";
    const kindSel = f.sniffed === "csv" ? h("div.meta", null, KIND_LABEL[f.kind]) : h("select.kind-sel", {
      title: "What this file is used as", onclick: (e) => e.stopPropagation(), onchange: (e) => setKind(f, e.target.value) },
      [["plan", "Floor plan"], ...(f.sniffed === "pdf" ? [["hvac", "HVAC drawing"]] : []), ["reference", "Style reference"]]
        .map(([v, l]) => h("option", { value: v, selected: f.kind === v }, l)));
    return h("div.thumb" + (isPlan && f.id === selectedPlan ? ".selected" : ""), {
      title: isPlan ? "Use this plan" : f.original_name,
      onclick: isPlan ? () => { localStorage.setItem(`twin.plan.${p.id}`, f.id); emit("project", state.project); } : null },
      f.sniffed === "csv" ? h("div.csv-tile", null, "CSV") : h("img", { src: `/api/projects/${p.id}/files/${f.id}/thumb`, alt: f.original_name }),
      deleteButton(f),
      h("div.meta", null, f.original_name),
      h("div.meta", null, f.sniffed === "csv" ? fmtBytes(f.bytes)
        : `${f.sniffed.toUpperCase()}${f.pages > 1 ? ` · page ${f.page + 1}/${f.pages}` : ""} · ${fmtBytes(f.bytes)}`),
      kindSel,
      isPlan && f.id === selectedPlan ? badge("in use", "ok") : null,
      isPlan && f.calibration && f.sniffed === "pdf" ? h("span.small.muted", { title: "The printed scale was read: sizes are exact" }, " to scale") : null,
      f.kind === "reference" ? h("label.meta", { onclick: (e) => e.stopPropagation() }, h("input", { type: "checkbox", checked: !off.includes(f.id), onchange: (e) => {
        const s = new Set(off);
        if (e.target.checked) s.delete(f.id); else s.add(f.id);
        localStorage.setItem(`twin.refsOff.${p.id}`, JSON.stringify([...s]));
      } }), " include") : null);
  };

  return h("div.card", null, h("h2", null, "Your drawings"),
    h("p.small.muted", null, "Drop everything at once: the floor plan (a PDF from CAD is best, it carries the scale), the HVAC drawing, ",
      "a furniture list or sensor sheet (CSV) and photos you like. Each file is sorted by itself; change it under its picture if it is wrong."),
    dropzone(),
    byKind.length ? byKind.map(([k, fs]) => h("div", null, h("h3", null, KIND_LABEL[k] + (fs.length > 1 && k !== "reference" ? `s (${fs.length})` : "")),
      h("div.thumbs", null, fs.map(thumb)))) : null);
}

async function setKind(f, kind) {
  try {
    await patch(`/api/projects/${state.project.id}/files/${f.id}`, { kind });
    toast(`${f.original_name} is now used as: ${KIND_LABEL[kind]}`, "ok", 3000);
  } catch (e) { showError(e, "Changing the file"); }
  await refreshProject();
}

/** The × on a file thumbnail: deletes that upload after a confirmation. */
function deleteButton(f) {
  const p = state.project;
  const what = (KIND_LABEL[f.kind] || "file").toLowerCase();
  const remove = async (e) => {
    e.stopPropagation();   // a click on a plan thumbnail would otherwise select it
    const ok = await confirmDialog(`Delete ${what}`,
      `Delete "${f.original_name}"? Saved layout versions and 3D models are kept.`, "Delete", true);
    if (!ok) return;
    try {
      await del(`/api/projects/${p.id}/files/${f.id}`);
      if (localStorage.getItem(`twin.plan.${p.id}`) === f.id) localStorage.removeItem(`twin.plan.${p.id}`);
      toast(`Deleted ${f.original_name}`, "ok", 2500);
    } catch (err) { showError(err, `Deleting ${f.original_name} failed`); }
    await refreshProject();
  };
  return h("button.btn.small.thumb-del", { type: "button", title: `Delete this ${what}`,
    "aria-label": `Delete ${f.original_name}`, onclick: remove }, "×");
}

/** A drop zone that takes any mix of files. */
export function dropzone(onFiles = null, label = "Drop your drawings here, or click to choose files") {
  const input = h("input", { type: "file", accept: ACCEPT, multiple: true, class: "hidden" });
  const pageInput = h("input", { type: "number", min: "1", placeholder: "auto", style: { width: "64px" },
    title: "Which page of a multi-page PDF is the plan; empty = found automatically" });
  const zone = h("div.dropzone", { onclick: () => input.click() }, label);
  const send = async (files) => {
    if (!files.length) return;
    if (onFiles) return onFiles(files);
    zone.classList.add("busy");
    mount(zone, h("span.spinner"), ` Adding ${files.length} file${files.length > 1 ? "s" : ""}…`);
    await uploadFiles(state.project.id, files, { page: pageInput.value || "0" });
    await refreshProject();
  };
  input.addEventListener("change", () => send([...input.files]));
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); send([...e.dataTransfer.files]); });
  return h("div", null, zone, input, onFiles ? null : h("div.row.small.muted", { style: { marginTop: "6px" } },
    "Plan page in a multi-page PDF:", pageInput, h("span", null, "(empty = found automatically)")));
}

// ---------------------------------------------------------------------------------------------
// other ways to start: only read the plan, trace it by hand, or the sample apartment
// ---------------------------------------------------------------------------------------------
/** Poll an interpret job until it ends, drawing it into whatever jobEl is current (the tab redraws meanwhile). */
async function follow(job) {
  following.add(job.id);
  interpretJob = job;
  const show = (j) => { if (jobEl && state.project?.id === j.project_id) drawJob(jobEl, j); };
  show(job);
  const final = await watchJob(job.id, (j) => { interpretJob = j; show(j); });
  await refreshProject();
  if (final.status === "needs_review" && state.project?.id === final.project_id) {
    state.version = final.result.version;
    toast("Interpretation ready for review. Check every dimension before generating.", "ok");
    switchTab("review");
    emit("version", state.version);
  }
}

function otherStartsCard() {
  const p = state.project;
  const ai = state.health.ai;
  const status = jobEl = h("div");
  // an interpretation still running (e.g. started before the page was reloaded) is picked up again, unless the
  // autopilot started it (the autopilot follows its own steps)
  const running = p.jobs?.find((j) => j.kind === "interpret" && ["queued", "processing"].includes(j.status));
  if (running && !following.has(running.id) && !autopilotRunning()) follow(running).catch((e) => showError(e, "AI interpretation"));
  const job = interpretJob && interpretJob.project_id === p.id ? interpretJob : null;
  if (job) drawJob(status, job);
  const busy = !!job && ["queued", "processing"].includes(job.status);

  const aiBtn = h("button.btn", { onclick: () => runAI(), disabled: !ai.configured || busy }, busy ? "Reading…" : "Only read the plan (AI)");
  const runAI = async () => {
    const plan = planFileId();
    if (!plan && !p.brief.prompt) return toast("Upload a floor plan or describe the layout first.", "error");
    aiBtn.disabled = true;
    aiBtn.textContent = "Reading…";
    try {
      await follow(await post(`/api/projects/${p.id}/interpret`, { plan_file_id: plan, reference_file_ids: referenceIds() }));
    } catch (e) {
      showError(e, "AI interpretation");
      aiBtn.disabled = false;
      aiBtn.textContent = "Only read the plan (AI)";
    }
  };
  const fromFixture = async () => {
    try {
      const v = await post(`/api/projects/${p.id}/versions/fixture`);
      await refreshProject();
      state.version = v.number;
      switchTab("review");
      emit("version", v.number);
    } catch (e) { showError(e, "Sample apartment"); }
  };
  const fromBlank = async () => {
    try {
      const v = await post(`/api/projects/${p.id}/versions/blank`);
      await refreshProject();
      state.version = v.number;
      switchTab("review");
      emit("version", v.number);
      toast("Blank layout created. Calibrate the plan image, then split and edit rooms to match it.", "ok", 7000);
    } catch (e) { showError(e, "Blank layout"); }
  };

  return h("details.card.fold", { open: busy || undefined }, h("summary", null, "Other ways to start"),
    h("div.row", null, aiBtn,
      h("span.small.muted", null, ai.configured ? `${ai.model} reads the plan into a layout; you review and build it step by step.`
        : "AI is not configured yet (set TWIN_AI_API_KEY and TWIN_AI_MODEL in .env and restart).")),
    h("div.row", null, h("button.btn", { onclick: fromBlank }, "Start from overall dimensions"),
      h("span.small.muted", null, "One room the size of the brief; trace the rest over the plan image.")),
    h("div.row", null, h("button.btn", { onclick: fromFixture }, "Use sample apartment"),
      h("span.small.muted", null, "Reference layout with known measurements (for testing the pipeline).")),
    status);
}

function drawJob(el, job) {
  mount(el, h("div", { style: { marginTop: "12px" } }, jobProgress(job),
    job.status === "failed" ? h("div.notice.error", { style: { marginTop: "8px" } },
      h("b", null, job.error?.code || "Failed"), ": ", job.error?.message || "",
      job.error?.detail ? h("pre.detail", null, JSON.stringify(job.error.detail, null, 1)) : null) : null,
    job.status === "needs_review" ? h("div.notice.ok", { style: { marginTop: "8px" } },
      `Version ${job.result.version} created: ${job.result.summary || ""}`,
      job.result.notes?.length ? h("ul", null, job.result.notes.map((n) => h("li", null, n))) : null) : null));
}

