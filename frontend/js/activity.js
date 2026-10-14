// @ts-check
/** Activity: every job of the project with status, progress and full error details. */
import { get } from "./api.js";
import { state, on } from "./state.js";
import { h, mount, badge, fmtTime, showError, friendly } from "./ui.js";

let root, timer = null;

const KIND_NAMES = { interpret: "AI reads the floor plan", fix: "AI change request", solve: "Fix problems", furniture: "AI furniture detection",
  build: "Build 3D model (Blender)", layer_build: "Update 3D model (sensors)", unreal_import: "Prepare Unreal scene",
  hvac_import: "Read HVAC drawing", redesign: "AI room redesign" };
const STATUS_NAMES = { needs_review: "ready to review", completed: "done", failed: "failed", processing: "running", queued: "waiting" };

export function renderActivity(el) {
  root = el;
  on("tab", (t) => { if (t === "activity") refresh(); else stop(); });
  on("project", () => { if (state.activeTab === "activity") refresh(); });
}

function stop() { if (timer) { clearTimeout(timer); timer = null; } }

async function refresh() {
  stop();
  if (!state.project) return;
  let jobs;
  try { jobs = await get(`/api/projects/${state.project.id}/jobs`); } catch (e) { return showError(e, "Loading jobs"); }
  let usage = null;
  try { usage = await get("/api/usage"); } catch { /* optional */ }
  mount(root, h("div.card", null, h("div.row", null, h("h2", { style: { margin: 0 } }, "Activity"), h("span.spacer"),
    usage ? h("span.small.muted", null, `AI today: ${usage.today.calls}/${usage.daily_limit} requests · ${usage.today.input_tokens + usage.today.output_tokens} tokens`) : null,
    h("button.btn.small", { onclick: refresh }, "Refresh")),
    jobs.length ? h("table.simple", { style: { marginTop: "10px" } }, h("thead", null, h("tr", null, ["What", "Status", "Progress", "Started", "Finished", "Job"].map((t) => h("th", null, t)))),
      h("tbody", null, jobs.flatMap((j) => [
        h("tr", null, h("td.job-kind", null, KIND_NAMES[j.kind] || j.kind), h("td", null, badge(STATUS_NAMES[j.status] || j.status, j.status)),
          h("td", null, `${Math.round(j.progress * 100)}%`, j.stage && !["completed", "needs_review", "failed"].includes(j.stage) ? h("div.small.muted", null, j.stage) : null),
          h("td.small", null, fmtTime(j.created_at)), h("td.small", null, fmtTime(j.finished_at)), h("td.mono.muted", null, j.id)),
        j.error ? h("tr", null, h("td", { colspan: "6" }, h("div.notice.error", null, friendly(j.error),
          h("details.tech", null, h("summary", null, "Technical details"),
            h("div.small", null, h("b", null, j.error.code), ": ", j.error.message),
            j.error.detail ? h("pre.detail", null, JSON.stringify(j.error.detail, null, 1)) : null)))) : null,
      ]))) : h("p.muted", null, "Nothing has run yet.")));
  if (jobs.some((j) => ["queued", "processing"].includes(j.status))) timer = setTimeout(refresh, 2000);
}
