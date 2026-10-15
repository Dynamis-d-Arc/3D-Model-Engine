// @ts-check
import { del, patch, post, upload } from "./api.js";
import { refreshProject, switchTab, watchJob } from "./app.js";
import { state, on, emit } from "./state.js";
import { h, mount, toast, showError, badge, fmtBytes, confirmDialog, jobProgress } from "./ui.js";

let root;
let interpretJob = null;
let jobEl = null;              // where the interpret job is drawn; replaced on every redraw of the tab
const following = new Set();   // interpret jobs being polled

export function planFileId() {
  const files = state.project?.files || [];
  const saved = localStorage.getItem(`twin.plan.${state.project?.id}`);
  const plans = files.filter((f) => f.kind === "plan");
  return (plans.find((f) => f.id === saved) || plans[plans.length - 1])?.id || null;
}

function referenceIds() {
  const off = JSON.parse(localStorage.getItem(`twin.refsOff.${state.project?.id}`) || "[]");
  return (state.project?.files || []).filter((f) => f.kind === "reference" && !off.includes(f.id)).map((f) => f.id);
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
  const brief = structuredClone(p.brief);
  const styles = state.catalog.styles;

  const save = async () => {
    try {
      await patch(`/api/projects/${p.id}`, { name: nameInput.value, brief });
      await refreshProject();
      toast("Brief saved", "ok", 2500);
    } catch (e) { showError(e, "Saving the brief failed"); }
  };
  const nameInput = h("input", { value: p.name, style: { minWidth: "260px" } });
  const known = h("tbody");
  const drawKnown = () => mount(known, brief.known_rooms.map((k, i) => h("tr", null,
    h("td", null, h("input", { value: k.name, oninput: (e) => (k.name = e.target.value) })),
    h("td", null, numberInput(k.width, (e) => (k.width = Number(e.target.value)))),
    h("td", null, numberInput(k.depth, (e) => (k.depth = Number(e.target.value)))),
    h("td", null, h("select", { onchange: (e) => (k.basis = e.target.value) },
      h("option", { value: "clear", selected: k.basis === "clear" }, "clear (wall face to face)"),
      h("option", { value: "centreline", selected: k.basis === "centreline" }, "centreline"))),
    h("td", null, h("button.btn.small.danger", { onclick: () => { brief.known_rooms.splice(i, 1); drawKnown(); } }, "Remove")))));
  drawKnown();

  const briefCard = h("div.card", null,
    h("h2", null, "Project brief"),
    h("div.row", null, h("label.field", null, "Project name", nameInput),
      h("label.field", null, "Design style", h("select", { onchange: (e) => (brief.style = e.target.value) },
        Object.entries(styles).map(([k, s]) => h("option", { value: k, selected: brief.style === k }, s.label))))),
    h("div.row", null,
      h("label.field", null, "Overall width (outer faces, m)", numberInput(brief.overall_width, (e) => (brief.overall_width = e.target.value ? Number(e.target.value) : null))),
      h("label.field", null, "Overall depth (outer faces, m)", numberInput(brief.overall_depth, (e) => (brief.overall_depth = e.target.value ? Number(e.target.value) : null))),
      h("label.field", null, "Wall height (m)", numberInput(brief.wall_height, (e) => (brief.wall_height = Number(e.target.value)), { min: "1.8", max: "12" }))),
    h("label.field", { style: { marginTop: "10px" } }, "Describe the result you want",
      h("textarea", { value: brief.prompt, placeholder: "e.g. Warm modern flat, oak floors, a large L-shaped sofa, home office in the second bedroom…",
        oninput: (e) => (brief.prompt = e.target.value) })),
    h("h3", null, "Room dimensions you know (optional, used to check the layout)"),
    h("table.simple", null, h("thead", null, h("tr", null, h("th", null, "Room name (as on plan)"), h("th", null, "Width m"),
      h("th", null, "Depth m"), h("th", null, "Measured"), h("th"))), known),
    h("div.row", { style: { marginTop: "8px" } },
      h("button.btn.small", { onclick: () => { brief.known_rooms.push({ name: "Room", width: 3, depth: 3, basis: "clear" }); drawKnown(); } }, "+ Add room dimension"),
      h("span.spacer"), h("button.btn.primary", { onclick: save }, "Save brief")));

  const plans = p.files.filter((f) => f.kind === "plan");
  const refs = p.files.filter((f) => f.kind === "reference");
  const selectedPlan = planFileId();
  const off = JSON.parse(localStorage.getItem(`twin.refsOff.${p.id}`) || "[]");

  const planCard = h("div.card", null, h("h2", null, "Floor plan"),
    h("p.muted.small", null, "PNG, JPG or PDF. For multi-page PDFs choose the page with the plan."),
    dropzone("plan", false),
    h("div.thumbs", null, plans.map((f) => h("div.thumb" + (f.id === selectedPlan ? ".selected" : ""), {
      title: "Use this plan", onclick: () => { localStorage.setItem(`twin.plan.${p.id}`, f.id); emit("project", state.project); } },
      h("img", { src: `/api/projects/${p.id}/files/${f.id}/thumb`, alt: f.original_name }), deleteButton(f),
      h("div.meta", null, f.original_name),
      h("div.meta", null, `${f.sniffed.toUpperCase()} · ${f.width}×${f.height}px${f.pages > 1 ? ` · page ${f.page + 1}/${f.pages}` : ""} · ${fmtBytes(f.bytes)}`),
      f.id === selectedPlan ? badge("in use", "ok") : null))));

  const refCard = h("div.card", null, h("h2", null, "Style reference images"),
    h("p.muted.small", null, "Interiors or furniture you like. Used only for materials and colours, never for geometry."),
    dropzone("reference", true),
    h("div.thumbs", null, refs.map((f) => h("div.thumb", null,
      h("img", { src: `/api/projects/${p.id}/files/${f.id}/thumb`, alt: f.original_name }), deleteButton(f),
      h("label.meta", null, h("input", { type: "checkbox", checked: !off.includes(f.id), onchange: (e) => {
        const set = new Set(off);
        if (e.target.checked) set.delete(f.id); else set.add(f.id);
        localStorage.setItem(`twin.refsOff.${p.id}`, JSON.stringify([...set]));
      } }), " include")))));

  const hvacs = p.files.filter((f) => f.kind === "hvac");
  const hvacCard = h("div.card", null, h("h2", null, "HVAC drawing (optional)"),
    h("p.muted.small", null, "The air-conditioning / ACMV layout of the same floor as a vector PDF from CAD (e.g. the ACMV sheet). ",
      "It is aligned to the floor plan on the structural grid; fan-coil units, diffusers, grilles and ducts are read and placed in 3D ",
      "(Review layout → HVAC → Import). Live or simulated unit data then drives the airflow view (tab 4)."),
    dropzone("hvac", false),
    h("div.thumbs", null, hvacs.map((f) => h("div.thumb", null,
      h("img", { src: `/api/projects/${p.id}/files/${f.id}/thumb`, alt: f.original_name }), deleteButton(f),
      h("div.meta", null, f.original_name),
      h("div.meta", null, `${f.sniffed.toUpperCase()} · ${fmtBytes(f.bytes)}`)))));
  mount(root, h("div.grid2", null, h("div", null, briefCard, startCard()), h("div", null, planCard, hvacCard, refCard)));
}

/** The × on a file thumbnail: deletes that upload after a confirmation. */
function deleteButton(f) {
  const p = state.project;
  const what = { plan: "floor plan", hvac: "HVAC drawing", reference: "reference image" }[f.kind] || "file";
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

function dropzone(kind, multiple) {
  const p = state.project;
  const accept = { plan: ".png,.jpg,.jpeg,.pdf", hvac: ".pdf", reference: ".png,.jpg,.jpeg" }[kind];
  const input = h("input", { type: "file", accept, multiple, class: "hidden" });
  const pageInput = h("input", { type: "number", min: "1", value: "1", style: { width: "60px" }, title: "PDF page" });
  const zone = h("div.dropzone", { onclick: () => input.click() },
    { plan: "Drop the floor plan here or click to choose", hvac: "Drop the HVAC drawing (PDF) here or click to choose",
      reference: "Drop reference images here or click to choose" }[kind]);
  const send = async (files) => {
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      form.append("page", pageInput.value || "1");
      try {
        const rec = await upload(`/api/projects/${p.id}/files`, form);
        if (kind === "plan") localStorage.setItem(`twin.plan.${p.id}`, rec.id);
        toast(`Uploaded ${file.name}`, "ok", 2500);
      } catch (e) { showError(e, `Upload of ${file.name} failed`); }
    }
    await refreshProject();
  };
  input.addEventListener("change", () => send([...input.files]));
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); send([...e.dataTransfer.files]); });
  return h("div", null, zone, input, kind === "plan" ? h("div.row.small.muted", { style: { marginTop: "6px" } }, "PDF page:", pageInput) : null);
}

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

function startCard() {
  const p = state.project;
  const ai = state.health.ai;
  const status = jobEl = h("div");
  // an interpretation still running (e.g. started before the page was reloaded) is picked up again
  const running = p.jobs?.find((j) => j.kind === "interpret" && ["queued", "processing"].includes(j.status));
  if (running && !following.has(running.id)) follow(running).catch((e) => showError(e, "AI interpretation"));
  const job = interpretJob && interpretJob.project_id === p.id ? interpretJob : null;
  if (job) drawJob(status, job);
  const busy = !!job && ["queued", "processing"].includes(job.status);

  const aiBtn = h("button.btn.primary", { onclick: () => runAI(), disabled: !ai.configured || busy }, busy ? "Interpreting…" : "Interpret with AI");
  const runAI = async () => {
    const plan = planFileId();
    if (!plan && !p.brief.prompt) return toast("Upload a floor plan or describe the layout first.", "error");
    aiBtn.disabled = true;
    aiBtn.textContent = "Interpreting…";
    try {
      await follow(await post(`/api/projects/${p.id}/interpret`, { plan_file_id: plan, reference_file_ids: referenceIds() }));
    } catch (e) {
      showError(e, "AI interpretation");
      aiBtn.disabled = false;
      aiBtn.textContent = "Interpret with AI";
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

  return h("div.card", null, h("h2", null, "Create the layout"),
    h("div.row", null, aiBtn,
      h("span.small.muted", null, ai.configured ? `Model ${ai.model} reads the plan, your dimensions and references.`
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
