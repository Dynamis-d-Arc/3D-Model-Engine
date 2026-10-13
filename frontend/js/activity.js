// @ts-check
/** Activity: every job of the project with status, progress and full error details. */
import { get } from "./api.js";
import { state, on } from "./state.js";
import { h, mount, badge, fmtTime, showError } from "./ui.js";

let root, timer = null;

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
  mount(root, h("div.card", null, h("div.row", null, h("h2", { style: { margin: 0 } }, "Jobs"), h("span.spacer"),
    usage ? h("span.small.muted", null, `AI today: ${usage.today.calls}/${usage.daily_limit} requests · ${usage.today.input_tokens + usage.today.output_tokens} tokens`) : null,
    h("button.btn.small", { onclick: refresh }, "Refresh")),
    jobs.length ? h("table.simple", { style: { marginTop: "10px" } }, h("thead", null, h("tr", null, ["Job", "Kind", "Status", "Progress", "Stage", "Created", "Finished"].map((t) => h("th", null, t)))),
      h("tbody", null, jobs.flatMap((j) => [
        h("tr", null, h("td.mono", null, j.id), h("td", null, j.kind), h("td", null, badge(j.status)),
          h("td", null, `${Math.round(j.progress * 100)}%`), h("td.small", null, j.stage), h("td.small", null, fmtTime(j.created_at)),
          h("td.small", null, fmtTime(j.finished_at))),
        j.error ? h("tr", null, h("td", { colspan: "7" }, h("div.notice.error", null, h("b", null, j.error.code), ": ", j.error.message,
          j.error.detail ? h("pre.detail", null, JSON.stringify(j.error.detail, null, 1)) : null))) : null,
      ]))) : h("p.muted", null, "No jobs yet.")));
  if (jobs.some((j) => ["queued", "processing"].includes(j.status))) timer = setTimeout(refresh, 2000);
}
