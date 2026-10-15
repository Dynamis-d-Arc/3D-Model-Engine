// @ts-check
/** DOM helpers shared by all panels. */

/**
 * h("div.card", {onclick}, child, "text") -> HTMLElement
 * @param {string} tag
 * @param {Record<string, any> | null} [attrs]
 * @param {...any} children
 */
export function h(tag, attrs, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className += (el.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "value") /** @type {any} */ (el).value = v;
    else if (k === "checked") /** @type {any} */ (el).checked = !!v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** @param {HTMLElement} el @param {...any} children */
export function mount(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

export function toast(message, kind = "info", ms = 5000) {
  const t = h("div.toast" + (kind !== "info" ? "." + kind : ""), null, message);
  document.getElementById("toasts")?.append(t);
  setTimeout(() => t.remove(), ms);
}

/** Show an ApiError (or any error) in words the user can act on; the code and details go to the console. */
export function showError(err, context = "") {
  toast(`${context ? context + ": " : ""}${friendly(err)}`, "error", 9000);
  console.error(context, err);
}

/** Error codes in words a user can act on; technical detail stays in the Activity tab. */
export function friendly(err) {
  if (!err) return "Something went wrong.";
  const d = err.detail || {};
  const map = {
    AI_BUDGET_EXCEEDED: () => `The AI spending limit is reached ($${(d.committed_usd ?? 0).toFixed(2)} of $${(d.cap_usd ?? 0).toFixed(2)} used). Nothing was charged. Raise TWIN_AI_BUDGET_USD in .env to continue.`,
    AI_BUDGET_PRICES_MISSING: () => "The AI model's prices are not set, so the spending limit cannot be enforced. Nothing was sent.",
    AI_NOT_CONFIGURED: () => "AI is not set up on this server (no key or model).",
    AI_LIMIT_REACHED: () => "Today's AI request limit is reached. Try again tomorrow or raise TWIN_AI_DAILY_REQUEST_LIMIT.",
    AI_AUTH: () => "The AI service rejected the API key. Check TWIN_AI_API_KEY.",
    AI_RATE_LIMITED: () => "The AI service is busy. Wait a minute and try again.",
    AI_TIMEOUT: () => "The AI took too long to answer. Try again.",
    AI_UNAVAILABLE: () => "The AI service could not be reached. Check the internet connection and try again.",
    AI_FIX_INVALID: () => "The AI's changes did not fit the model, so nothing was changed. Try describing the problem differently.",
    AI_REFUSED: () => "The AI declined this request.",
    AI_INCOMPLETE: () => "The AI's answer was cut off. Try again, or fix fewer problems at once.",
    NETWORK: () => "The server is not reachable. Is it still running?",
  };
  return map[err.code] ? map[err.code]() : (err.message || String(err));
}


export function badge(text, kind = text) {
  return h("span.badge." + String(kind).replace(/[^a-z_]/gi, ""), null, String(text).replace(/_/g, " "));
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "";
}

/** "Today 12:04 PM", "Yesterday 3:10 PM", "14 Oct, 9:02 AM" */
export function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const ago = Math.round((day(new Date()) - day(d)) / 86400000);
  if (ago === 0) return `Today ${time}`;
  if (ago === 1) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })}, ${time}`;
}

export const fmtM = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));

/** 75 -> "1:15" */
export function fmtElapsed(s) {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

export const isRunningJob = (job) => ["queued", "processing"].includes(job?.status);
const AI_JOBS = ["interpret", "redesign", "fix", "solve", "furniture"];
const BUILD_ANIM_S = 8;   // one cycle of the building animation; the bar's stripes and shimmer divide it evenly

// a building going up floor by floor under a crane (CSS animations in styles.css, .build-anim)
const BUILD_SVG = `<svg viewBox="0 0 120 84" width="96" height="68" aria-hidden="true">
  <line class="ba-ground" x1="6" y1="78.5" x2="114" y2="78.5"/>
  <g class="ba-crane"><path d="M98 78 V12 M38 12 H114 M98 12 L106 4 L114 12"/>
    <g class="ba-hook"><line x1="54" y1="12" x2="54" y2="22"/><rect x="51" y="22" width="6" height="4"/></g></g>
  ${[64, 50, 36, 22].map((y, i) => `<g class="ba-floor ba-f${i + 1}"><rect x="30" y="${y}" width="48" height="14"/>` +
    [36, 51, 66].map((x) => `<rect class="ba-win" x="${x}" y="${y + 4}" width="7" height="6"/>`).join("") + "</g>").join("")}
</svg>`;

/**
 * A background job's progress. While it runs: a building going up, what it is doing, the time since it started
 * and a moving bar, so a long wait (an AI answer on a whole plan can take minutes) never looks frozen.
 * Panels redraw this on every poll; the animations follow one clock (--phase), so they carry on instead of restarting.
 * @param {any} job  @param {{title?: string}} [opts]
 */
export function jobProgress(job, opts = {}) {
  const pct = Math.round((job.progress || 0) * 100);
  const running = isRunningJob(job);
  const head = (extra) => h("div.row", null, opts.title ? h("b", null, opts.title) : null, badge(job.status),
    h("span.small", null, job.stage || (job.status === "queued" ? "waiting for its turn" : "")), h("span.spacer"), extra);
  const bar = h("div.progress" + (running ? ".running" : ""), { style: { marginTop: "6px" } }, h("div", { style: { width: `${pct}%` } }));
  if (!running) return h("div", null, head(h("span.small.muted", null, `${pct}%`)), bar);
  const since = job.started_at || job.created_at;
  const elapsed = since ? fmtElapsed(Math.max(0, Date.now() / 1000 - since)) : "";
  const el = h("div.job-progress", null, h("div.build-anim", { html: BUILD_SVG }),
    h("div.job-info", null, head(h("span.small.muted", { title: "time since it started · progress" }, `${elapsed} · ${pct}%`)), bar,
      AI_JOBS.includes(job.kind) && job.status === "processing"
        ? h("div.small.muted", { style: { marginTop: "6px" } }, "AI answers on a whole plan can take several minutes. This keeps updating by itself.")
        : null));
  el.style.setProperty("--phase", `-${((performance.now() / 1000) % BUILD_ANIM_S).toFixed(2)}s`);
  return el;
}

/**
 * Simple modal prompt built on <dialog>. Resolves with form values or null.
 * @param {string} title
 * @param {Array<{name:string,label:string,type?:string,value?:any,options?:Array<[string,string]>,step?:number}>} fields
 */
export function ask(title, fields, okLabel = "OK") {
  const dlg = /** @type {HTMLDialogElement} */ (document.getElementById("dialog"));
  return new Promise((resolve) => {
    const inputs = {};
    const body = fields.map((f) => {
      let input;
      if (f.options) {
        input = h("select", { name: f.name }, f.options.map(([v, l]) => h("option", { value: v, selected: v === f.value }, l)));
      } else if (f.type === "textarea") {
        input = h("textarea", { name: f.name, value: f.value ?? "" });
      } else {
        input = h("input", { name: f.name, type: f.type || "text", value: f.value ?? "", step: f.step ?? "any" });
      }
      inputs[f.name] = input;
      return h("label.field", { style: { marginBottom: "8px" } }, f.label, input);
    });
    const form = h("form", { method: "dialog" },
      h("h2", null, title), ...body,
      h("div.row", { style: { justifyContent: "flex-end", marginTop: "12px" } },
        h("button.btn", { value: "cancel", type: "submit" }, "Cancel"),
        h("button.btn.primary", { value: "ok", type: "submit" }, okLabel)));
    mount(dlg, form);
    dlg.onclose = () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      const out = {};
      for (const f of fields) {
        const v = inputs[f.name].value;
        out[f.name] = f.type === "number" ? (v === "" ? null : Number(v)) : v;
      }
      resolve(out);
    };
    dlg.showModal();
    const first = Object.values(inputs)[0];
    if (first) first.focus();
  });
}

/** danger: the OK button is red (for deletes that cannot be undone). */
export function confirmDialog(title, message, okLabel = "Continue", danger = false) {
  const dlg = /** @type {HTMLDialogElement} */ (document.getElementById("dialog"));
  return new Promise((resolve) => {
    mount(dlg, h("form", { method: "dialog" }, h("h2", null, title), h("p", null, message),
      h("div.row", { style: { justifyContent: "flex-end" } },
        h("button.btn", { value: "cancel" }, "Cancel"),
        h(danger ? "button.btn.primary.danger" : "button.btn.primary", { value: "ok" }, okLabel))));
    dlg.onclose = () => resolve(dlg.returnValue === "ok");
    dlg.showModal();
  });
}
