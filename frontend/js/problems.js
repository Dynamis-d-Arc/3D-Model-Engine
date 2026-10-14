// @ts-check
/**
 * Review checklist: the validation report grouped into a few plain-language problems, each with the fix that fits
 * it (twin/scene/problems.py says which):
 *   rule    free, instant fix in the editor (undoable)              -> "Fix"
 *   ai      needs judgement: the AI problem solver proposes a version -> "Ask AI"
 *   decide  the user picks (open plan / add the wall), or lets the AI decide from the drawing
 *   accept  confirm the AI's estimated sizes                          -> "Accept all"
 *   answer  questions from the AI                                     -> answer / "Accept as drawn"
 *   manual  only the user knows: click the item and edit it
 *   info    notes, folded away
 * "Fix problems automatically" runs the rules and then the AI solver as one job and saves one new version.
 */
import { state } from "./state.js";
import { h, badge, toast, friendly } from "./ui.js";

const SEV_ORDER = { error: 0, review: 1, warning: 2, info: 3 };
const SEV_LABEL = { error: "Must fix", review: "Needs your OK", warning: "Check", info: "Note" };
const ITEMS_SHOWN = 8;
const openGroups = new Set();
let useAi = true;
let notesOpen = false;
let busy = null;   // label of the free fix running on the server (they take a few seconds on big plans)

/**
 * @typedef {{report:any, reportError:any, scene:any, dirty:boolean, version:number, select:(t:string)=>void,
 *   tool:(path:string, body:any, msg?:(r:any)=>string)=>Promise<any>, edit:(fn:()=>void)=>void,
 *   solve:(opts:any)=>void, solveJob:any, estimate:any}} Ctx
 */

function info(code) {
  return state.catalog?.problems?.[code] || { title: code.replace(/^[A-Z]_/, "").replace(/_/g, " ").toLowerCase(), why: "", fix: "manual" };
}

/** Group the report's issues by code, worst severity first. */
export function groupIssues(report) {
  const groups = new Map();
  for (const i of report?.issues || []) {
    if (!groups.has(i.code)) groups.set(i.code, { code: i.code, severity: i.severity, items: [], ...info(i.code) });
    const g = groups.get(i.code);
    g.items.push(i);
    if (SEV_ORDER[i.severity] < SEV_ORDER[g.severity]) g.severity = i.severity;
  }
  return [...groups.values()].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity] || b.items.length - a.items.length);
}

const isRunning = (job) => job && !["completed", "needs_review", "failed"].includes(job.status);

/** @param {Ctx} ctx */
export function problemsCard(ctx) {
  const { report, reportError } = ctx;
  if (reportError) {
    return h("div.card.problems", null, h("h2", null, "Problems"),
      h("div.notice.error", null, "The layout could not be checked: ", reportError.message,
        Array.isArray(reportError.detail) ? h("ul", null, reportError.detail.map((d) => h("li", null, d))) : null));
  }
  if (!report) return null;
  const groups = groupIssues(report);
  const actionable = groups.filter((g) => g.severity !== "info");
  const notes = groups.filter((g) => g.severity === "info");
  const blocking = actionable.filter((g) => g.severity === "error" || g.severity === "review").reduce((n, g) => n + g.items.length, 0);
  const checks = actionable.filter((g) => g.severity === "warning").reduce((n, g) => n + g.items.length, 0);
  const ruleCodes = new Set(state.catalog?.rule_codes || []);
  const autoFixable = actionable.some((g) => ruleCodes.has(g.code) || ["ai", "decide"].includes(g.fix));
  const running = isRunning(ctx.solveJob) || !!busy;

  const status = report.can_generate
    ? (checks ? `Ready to build. ${checks} thing${checks > 1 ? "s" : ""} worth a look first.` : "Ready to build. No problems found.")
    : `${blocking} thing${blocking > 1 ? "s" : ""} to sort out before you can build${checks ? `, ${checks} more to check` : ""}.`;

  return h("div.card.problems", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "Problems"), h("span.spacer"),
      report.can_generate ? badge("ready to build", "ok") : badge("not ready", report.ok ? "review" : "error")),
    h("p.small", { style: { margin: "6px 0 8px" } }, status),
    autoFixable ? autoFixBox(ctx, running) : null,
    busy ? h("div.notice.busy", null, h("span.spinner"), `Fixing: ${busy}…`) : null,
    ctx.solveJob && ctx.solveJob.project_id === state.project.id ? solveStatus(ctx.solveJob) : null,
    actionable.length ? h("div.pgroups", null, actionable.map((g) => groupEl(ctx, g, running))) : h("div.notice.ok", null, "Nothing to fix."),
    notes.length ? h("details.pnotes", { open: notesOpen, ontoggle: (e) => { notesOpen = e.target.open; } },
      h("summary", null, `${notes.reduce((n, g) => n + g.items.length, 0)} notes (no action needed)`),
      notes.map((g) => h("div.pnote", null, h("b", null, `${g.title} (${g.items.length})`), h("div.small.muted", null, g.why),
        h("ul.pitems", null, g.items.slice(0, ITEMS_SHOWN).map((i) => itemEl(ctx, g, i)))))) : null);
}

function autoFixBox(ctx, running) {
  const est = ctx.estimate;
  const ai = state.health?.ai;
  const canAi = !!ai?.configured;
  const model = est?.model || ai?.solver_model || "the AI";
  const parts = [];
  if (est) {
    if (est.rule_fixable) parts.push(`${est.rule_fixable} can be fixed for free`);
    if (est.ai_problems) parts.push(canAi && useAi ? `${est.ai_problems} need judgement: ${model} looks at the plan and proposes a fix` +
      (est.est_usd != null ? ` (about $${Math.max(0.01, est.est_usd).toFixed(2)})` : "") : `${est.ai_problems} need judgement (AI off)`);
  }
  return h("div.autofix", null,
    h("div.row", null,
      h("button.btn.primary", { disabled: running, onclick: () => ctx.solve({ use_ai: canAi && useAi }) },
        running ? "Fixing…" : "Fix problems automatically"),
      canAi ? h("label.small", { title: `Send what the free rules cannot fix to ${model}` },
        h("input", { type: "checkbox", checked: useAi, onchange: (e) => { useAi = e.target.checked; } }), ` use AI (${model})`) : null),
    h("div.small.muted", { style: { marginTop: "4px" } },
      (parts.length ? parts.join("; ") + ". " : "") + "You get a new version to look at; nothing is overwritten."));
}

function solveStatus(job) {
  const r = job.result;
  if (isRunning(job)) {
    const pct = Math.round((job.progress || 0) * 100);
    return h("div.solve-status", null, h("div.small", null, job.stage || "working…"),
      h("div.progress", { style: { marginTop: "4px" } }, h("div", { style: { width: `${pct}%` } })));
  }
  if (job.status === "failed") return h("div.notice.error.solve-status", null, friendly(job.error));
  if (!r) return null;
  const ai = r.ai;
  const fixedByRules = r.rule_fixes?.length || 0;
  const done = [fixedByRules ? `${fixedByRules} fixed for free` : null,
    ai?.changes?.length ? `${ai.solved?.length || 0} solved by ${ai.model}` : null].filter(Boolean);
  const head = r.version ? `Saved as v${r.version}: ${done.join(", ")}.` : "Nothing could be fixed automatically.";
  return h("div.notice." + (r.version ? "ok" : "warn") + ".solve-status", null,
    h("div", null, head),
    ai?.summary ? h("div.small", { style: { marginTop: "4px" } }, ai.summary) : null,
    ai?.error ? h("div.small", { style: { marginTop: "4px" } }, `AI step skipped: ${friendly(ai.error)}`) : null,
    ai?.rejected ? h("div.small", { style: { marginTop: "4px" } }, `The AI's changes were not used (${ai.rejected}).`) : null,
    ai?.questions?.length ? h("div.small", { style: { marginTop: "4px" } }, h("b", null, "The AI asks: "), ai.questions.join(" · ")) : null,
    fixedByRules ? h("details.small", null, h("summary", null, `What the free fixes did (${fixedByRules})`),
      h("ul.fix-changes", null, r.rule_fixes.map((a) => h("li", null, a.text)))) : null,
    ai?.changes?.length ? h("details.small", null, h("summary", null, `What the AI changed (${ai.changes.length})`),
      h("ul.fix-changes", null, ai.changes.map((c) => h("li", null, c.reason || `${c.op} ${c.collection} ${c.id}`)))) : null,
    ai?.cost_usd != null ? h("div.small.muted", null, `AI cost $${ai.cost_usd.toFixed(3)}`) : null);
}

function groupEl(ctx, g, running) {
  const open = openGroups.has(g.code);
  const toggle = () => { open ? openGroups.delete(g.code) : openGroups.add(g.code); ctx.redraw(); };
  const n = g.items.length;
  return h("div.pgroup.sev-" + g.severity, null,
    h("div.pgroup-head", { onclick: toggle, title: open ? "Hide the items" : "Show the items" },
      h("span.caret", null, open ? "▾" : "▸"),
      h("span.pchip." + g.severity, null, SEV_LABEL[g.severity]),
      h("span.ptitle", null, g.title, " ", h("span.pcount", null, String(n))),
      h("span.pactions", null, groupActions(ctx, g, running))),
    g.why ? h("div.pwhy", null, g.why) : null,
    open ? h("ul.pitems", null, g.items.slice(0, Math.max(ITEMS_SHOWN, openGroups.has(g.code + ":all") ? n : 0)).map((i) => itemEl(ctx, g, i)),
      n > ITEMS_SHOWN && !openGroups.has(g.code + ":all")
        ? h("li.more", { onclick: () => { openGroups.add(g.code + ":all"); ctx.redraw(); } }, `Show all ${n}`) : null) : null);
}

/** Buttons on a group's heading row; clicks must not toggle the group. */
function groupActions(ctx, g, running) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  const ruleCodes = new Set(state.catalog?.rule_codes || []);
  const aiOk = !!state.health?.ai?.configured;
  const out = [];
  if (ruleCodes.has(g.code)) {
    out.push(h("button.btn.small", { disabled: running, title: "Free; you can undo it", onclick: stop(() => fixWithRules(ctx, { codes: [g.code] }, g.title)) }, "Fix"));
  }
  if (g.fix === "ai" || g.fix === "decide") {
    out.push(h("button.btn.small", { disabled: running || !aiOk, title: aiOk ? "The AI looks at the plan and proposes a fix as a new version" : "AI is not configured",
      onclick: stop(() => ctx.solve({ use_ai: true, codes: [g.code] })) }, "Ask AI"));
  }
  if (g.fix === "decide" && g.code === "W_ROOM_EDGE_NO_WALL") {
    out.push(h("button.btn.small", { title: "These edges are meant to be open; stop warning about them",
      onclick: stop(() => ctx.edit(() => { for (const i of g.items) setOpenPlan(ctx, i.targets[0]); })) }, "All open plan"));
  }
  if (g.fix === "accept") {
    out.push(h("button.btn.small", { onclick: stop(() => ctx.tool("/api/scene/accept-estimates", {}, (r) => `${r.changed} values accepted as estimates`)) }, "Accept all"));
  }
  if (g.fix === "answer") {
    out.push(h("button.btn.small", { title: "Keep the layout as drawn for every open question",
      onclick: stop(() => ctx.edit(() => { for (const q of ctx.scene.open_questions) if (!q.resolved) { q.resolved = true; q.answer = q.answer || "accepted as drawn"; } })) }, "Accept all as drawn"));
  }
  return out;
}

function setOpenPlan(ctx, target) {
  const id = String(target || "").split(":")[1];
  const room = ctx.scene.rooms.find((r) => r.id === id);
  if (room) room.open_plan = true;
}

async function fixWithRules(ctx, body, label) {
  if (busy) return;
  busy = label;
  ctx.redraw();
  try {
    const res = await ctx.tool("/api/scene/autofix", { ...body, brief: state.project.brief },
      (r) => r.actions.length ? `${label}: ${r.actions.length} fixed. Save the version to keep it (Ctrl+Z undoes it).` : "");
    if (res && !res.actions.length) toast("The free fix could not solve this one. Try “Ask AI”, or click the item and edit it.", "info", 7000);
  } finally {
    busy = null;
    ctx.redraw();
  }
}

function itemEl(ctx, g, i) {
  const t = i.targets.find((x) => !x.startsWith("question:")) || i.targets[0];
  const qid = i.targets.find((x) => x.startsWith("question:"))?.split(":")[1];
  const ruleCodes = new Set(state.catalog?.rule_codes || []);
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  const extra = [];
  if (ruleCodes.has(g.code) && t) extra.push(h("button.btn.tiny", { disabled: !!busy, onclick: stop(() => fixWithRules(ctx, { targets: [t] }, i.message.split(":")[0].slice(0, 60))) }, "Fix"));
  if (g.code === "W_ROOM_EDGE_NO_WALL" && t) {
    const rid = t.split(":")[1];
    extra.push(h("button.btn.tiny", { title: "The open edge is intentional", onclick: stop(() => ctx.edit(() => setOpenPlan(ctx, t))) }, "Open plan"),
      h("button.btn.tiny", { title: "Add walls along the open edges of this room",
        onclick: stop(() => ctx.tool("/api/scene/add-missing-walls", { room_id: rid }, (r) => r.notes.length ? r.notes.join("; ") : "No open edge longer than 30 cm")) }, "Add wall"));
  }
  if (g.code === "Q_OPEN_QUESTION" && qid) {
    const q = ctx.scene.open_questions.find((x) => x.id === qid);
    if (q && !q.resolved) {
      const inp = h("input", { placeholder: "Your answer (optional)", onclick: (e) => e.stopPropagation() });
      return h("li.pq", null, h("div", null, i.message),
        h("div.row", { style: { marginTop: "4px" } }, inp,
          h("button.btn.tiny", { onclick: stop(() => ctx.edit(() => { q.answer = inp.value || "answered"; q.resolved = true; })) }, "Answer"),
          h("button.btn.tiny", { onclick: stop(() => ctx.edit(() => { q.answer = "accepted as drawn"; q.resolved = true; })) }, "Accept as drawn")));
    }
  }
  return h("li", { onclick: () => t && ctx.select(t), title: t ? "Show it on the plan" : "" },
    h("span", null, i.message), extra.length ? h("span.pitem-actions", null, extra) : null);
}
