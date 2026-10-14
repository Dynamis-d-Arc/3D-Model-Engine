// @ts-check
/** Review & edit: 2D plan editor over the uploaded plan, inspector, live validation, versioning. */
import { get, post, put } from "./api.js";
import { refreshProject, switchTab } from "./app.js";
import { planFileId } from "./brief.js";
import { state, on, emit, assetById, ROOM_COLORS } from "./state.js";
import { h, mount, toast, showError, badge, ask, confirmDialog, fmtM } from "./ui.js";
import { problemsCard } from "./problems.js";

const SVGNS = "http://www.w3.org/2000/svg";
const SNAP = 0.05;
const STATUS_NEEDS = ["extracted", "estimated"];

/** @type {any} */ let scene = null;
let loadedVersion = null;
let loadedProject = null;
let dirty = false;
let undo = [], redo = [];
/** @type {{type:string,id:string,index?:number}|null} */ let sel = null;
let report = null, reportError = null;
const view = { cx: 5, cy: 4, zoom: 60, w: 800, h: 600 };
let underlay = { visible: true, opacity: 0.45 };
let hvacVisible = true;
let sensorsVisible = true;
/** the loaded version as saved: its sensors and everything else, to tell layer-only edits from building edits */
let savedSensors = "[]", savedBuilding = "";
let layerOffer = null;   // {version, options}: after a save that changed only sensors, offer the in-place update
let layerJob = null;
let hvacJob = null, fixJob = null, furnJob = null, solveJob = null;
let solveEstimate = null;   // {key, data}: what "Fix problems" would do with the loaded version
const folds = new Map();   // side cards the user opened / closed
let furnEstimate = null;   // {key, data}: cost estimate of the furniture detection for the loaded version
let mode = "select";
let calib = null; // {step, a, b, mPerPx}
let drawRect = null; // {x0, y0, x1, y1} while dragging a new room
let hintEl = null;
let root, svg, sideEl, toolbarEl, validateTimer;

// ---------------------------------------------------------------------------------------------
// geometry helpers
// ---------------------------------------------------------------------------------------------
const sx = (x) => view.w / 2 + (x - view.cx) * view.zoom;
const sy = (y) => view.h / 2 - (y - view.cy) * view.zoom;
const wx = (px) => view.cx + (px - view.w / 2) / view.zoom;
const wy = (py) => view.cy - (py - view.h / 2) / view.zoom;
const snap = (v, ev) => (ev && ev.altKey ? Math.round(v * 1000) / 1000 : Math.round(v / SNAP) * SNAP);
const r3 = (v) => Math.round(v * 1000) / 1000;

function frame(w) {
  const dx = w.end[0] - w.start[0], dy = w.end[1] - w.start[1];
  const L = Math.hypot(dx, dy) || 1;
  const ux = dx / L, uy = dy / L;
  return { ox: w.start[0], oy: w.start[1], ux, uy, nx: -uy, ny: ux, L,
    at: (s, d = 0) => [w.start[0] + ux * s - uy * d, w.start[1] + uy * s + ux * d] };
}
function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function centroid(poly) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i], [x1, y1] = poly[(i + 1) % poly.length];
    const f = x0 * y1 - x1 * y0;
    a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f;
  }
  if (Math.abs(a) < 1e-9) return poly[0];
  const c = [cx / (3 * a), cy / (3 * a)];
  return pointInPoly(c[0], c[1], poly) ? c : poly.reduce((s, p) => [s[0] + p[0] / poly.length, s[1] + p[1] / poly.length], [0, 0]);
}
function rectCorners(cx, cy, w, d, rot) {
  const a = (rot * Math.PI) / 180, c = Math.cos(a), s = Math.sin(a);
  return [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]].map(([lx, ly]) => [cx + lx * c - ly * s, cy + lx * s + ly * c]);
}
function furnSize(f) {
  const a = assetById(f.asset_id);
  if (f.size) return [f.size.width, f.size.depth, f.size.height];
  return a ? a.size : [0.5, 0.5, 0.5];
}
const byId = (coll, id) => scene[coll].find((e) => e.id === id);
const sensorKind = (k) => state.catalog.sensor_kinds.find((x) => x.id === k) || { id: k, label: k, color: "#6F7782", prefix: "S", unit: "" };
/** scene JSON without the sensor layer and bookkeeping (what decides whether the building must be regenerated) */
function buildingJson(sc) {
  const { sensors, provenance, open_questions, ...rest } = sc;
  return JSON.stringify(rest);
}
function sensorChanges() {
  const a = Object.fromEntries(JSON.parse(savedSensors).map((x) => [x.id, x]));
  const b = Object.fromEntries((scene.sensors || []).map((x) => [x.id, x]));
  const out = { added: [], removed: [], moved: [], edited: [] };
  for (const id of Object.keys(b)) if (!a[id]) out.added.push(b[id].tag);
  for (const id of Object.keys(a)) if (!b[id]) out.removed.push(a[id].tag);
  for (const id of Object.keys(b)) {
    if (!a[id] || JSON.stringify(a[id]) === JSON.stringify(b[id])) continue;
    const moved = ["position", "elevation", "mounting", "level_id"].some((k) => JSON.stringify(a[id][k]) !== JSON.stringify(b[id][k]));
    (moved ? out.moved : out.edited).push(b[id].tag);
  }
  return out;
}
function nextTag(kind) {
  const prefix = sensorKind(kind).prefix;
  const tags = new Set(scene.sensors.map((x) => x.tag));
  let n = 1 + Math.max(0, ...scene.sensors.map((x) => (x.tag.match(new RegExp(`^${prefix}-(\\d+)$`)) || [])[1]).filter(Boolean).map(Number));
  while (tags.has(`${prefix}-${String(n).padStart(2, "0")}`)) n++;
  return `${prefix}-${String(n).padStart(2, "0")}`;
}
function nextSensorId() {
  const nums = scene.sensors.map((x) => /^S(\d+)$/.exec(x.id)).filter(Boolean).map((m) => Number(m[1]));
  return `S${Math.max(0, ...nums) + 1}`;
}
function roomAt(x, y) {
  return scene.rooms.find((r) => pointInPoly(x, y, r.polygon)) || null;
}
/** Nearest wall face within `reach` of (x, y): the point on that face, pushed out by the marker radius. */
function wallFacePoint(x, y, reach = 1.0) {
  let best = null;
  for (const w of scene.walls) {
    const f = frame(w);
    const s = (x - w.start[0]) * f.ux + (y - w.start[1]) * f.uy;
    const d = (x - w.start[0]) * f.nx + (y - w.start[1]) * f.ny;
    if (s < 0.05 || s > f.L - 0.05) continue;
    const gap = Math.abs(d) - w.thickness / 2;
    if (gap > reach || (best && gap >= best.gap)) continue;
    const side = d >= 0 ? 1 : -1;
    best = { gap, wall: w.id, p: f.at(s, side * (w.thickness / 2 + 0.065)).map(r3) };
  }
  return best;
}
const HVAC_COLL = { hvac_unit: "units", hvac_terminal: "terminals", duct: "ducts" };
const hvacById = (type, id) => (scene.hvac?.[HVAC_COLL[type]] || []).find((e) => e.id === id);
const hvacCount = () => (scene?.hvac ? scene.hvac.units.length + scene.hvac.terminals.length + scene.hvac.ducts.length : 0);
function nextId(prefix, coll) {
  const nums = scene[coll].map((e) => parseInt(String(e.id).replace(/^\D+/, ""), 10)).filter((n) => !Number.isNaN(n));
  return `${prefix}${Math.max(0, ...nums) + 1}`;
}
function levelHeight() { return scene.levels[0]?.wall_height ?? 2.7; }

// ---------------------------------------------------------------------------------------------
// editing primitives
// ---------------------------------------------------------------------------------------------
function snapshot() {
  undo.push(JSON.stringify(scene));
  if (undo.length > 100) undo.shift();
  redo = [];
}
function changed() {
  dirty = true;
  draw();
  drawSide();
  scheduleValidate();
}
function edit(fn) {
  snapshot();
  fn();
  changed();
}
function doUndo() { if (!undo.length) return; redo.push(JSON.stringify(scene)); scene = JSON.parse(undo.pop()); changed(); }
function doRedo() { if (!redo.length) return; undo.push(JSON.stringify(scene)); scene = JSON.parse(redo.pop()); changed(); }

/** Move every room vertex and wall endpoint at `from` to `to` (keeps shared corners joined). */
function moveLinkedPoint(from, to) {
  const same = (p) => Math.abs(p[0] - from[0]) < 1e-3 && Math.abs(p[1] - from[1]) < 1e-3;
  for (const r of scene.rooms) r.polygon = r.polygon.map((p) => (same(p) ? [...to] : p));
  for (const w of scene.walls) {
    if (same(w.start)) w.start = [...to];
    if (same(w.end)) w.end = [...to];
  }
}

function deleteSelection() {
  if (!sel) return;
  const s = sel;
  edit(() => {
    if (s.type === "furniture") scene.furniture = scene.furniture.filter((f) => f.id !== s.id);
    if (s.type === "sensor") scene.sensors = scene.sensors.filter((x) => x.id !== s.id);
    if (s.type in HVAC_COLL) {
      const key = HVAC_COLL[s.type];
      scene.hvac[key] = scene.hvac[key].filter((e) => e.id !== s.id);
      if (s.type === "hvac_unit") for (const e of [...scene.hvac.terminals, ...scene.hvac.ducts, ...scene.sensors]) if (e.unit_id === s.id) e.unit_id = null;
    }
    if (s.type === "opening") scene.openings = scene.openings.filter((o) => o.id !== s.id);
    if (s.type === "wall") {
      scene.walls = scene.walls.filter((w) => w.id !== s.id);
      scene.openings = scene.openings.filter((o) => o.wall_id !== s.id);
    }
    if (s.type === "room") {
      scene.rooms = scene.rooms.filter((r) => r.id !== s.id);
      scene.furniture = scene.furniture.filter((f) => f.room_id !== s.id);
      scene.openings.forEach((o) => { if (o.opens_into === s.id) o.opens_into = null; });
      for (const e of [...scene.sensors, ...scene.hvac.units, ...scene.hvac.terminals]) if (e.room_id === s.id) e.room_id = null;
    }
  });
  sel = null;
  drawSide();
}

function confirmCertainty(el) {
  el.certainty = { ...el.certainty, status: "confirmed", source: "user" };
}

// ---------------------------------------------------------------------------------------------
// loading / saving / validation
// ---------------------------------------------------------------------------------------------
async function loadVersion(n, force = false) {
  if (dirty && !force && !(await confirmDialog("Discard changes?", "You have unsaved edits in the open version.", "Discard"))) return;
  try {
    const v = await get(`/api/projects/${state.project.id}/versions/${n}`);
    scene = v.scene;
    scene.sensors = scene.sensors || [];
    savedSensors = JSON.stringify(scene.sensors);
    savedBuilding = buildingJson(scene);
    if (layerOffer && layerOffer.version !== n) layerOffer = null;
    loadedVersion = n;
    loadedProject = state.project.id;
    report = v.validation;
    reportError = null;
    dirty = false; undo = []; redo = []; sel = null;
    state.version = n;
    fitView();
    drawAll();
  } catch (e) { showError(e, `Loading version ${n}`); }
}

async function save() {
  const sc = sensorChanges();
  const onlySensors = buildingJson(scene) === savedBuilding;
  const nSensor = sc.added.length + sc.removed.length + sc.moved.length + sc.edited.length;
  const note = onlySensors && nSensor
    ? [sc.added.length && `add sensor${sc.added.length > 1 ? "s" : ""} ${sc.added.join(", ")}`, sc.moved.length && `move ${sc.moved.join(", ")}`,
      sc.edited.length && `edit ${sc.edited.join(", ")}`, sc.removed.length && `remove ${sc.removed.join(", ")}`].filter(Boolean).join("; ").slice(0, 300)
    : "edited in review";
  const v = await ask("Save as a new version", [{ name: "note", label: `What changed? (v${loadedVersion} stays as it is)`, value: note }], "Save");
  if (!v) return;
  try {
    const res = await post(`/api/projects/${state.project.id}/versions`, { scene, parent: loadedVersion, note: v.note || "edited", source: "edit" });
    dirty = false;
    await refreshProject();
    await loadVersion(res.number, true);
    toast(`Saved version ${res.number}${res.validation.can_generate ? " — ready to generate" : ""}`, "ok");
    if (res.validation.can_generate && res.changes && !res.changes.geometry_changed) {
      try {
        const opts = await get(`/api/projects/${state.project.id}/versions/${res.number}/build-options`);
        if (opts.layer.possible && !opts.layer.up_to_date) { layerOffer = { version: res.number, options: opts }; drawSide(); }
      } catch { /* the Generate tab offers it too */ }
    }
  } catch (e) { showError(e, "Saving failed"); }
}

/** Step 9 of the flow: put this version's sensor layer on the saved model (no Blender, seconds). */
async function updateSavedModel(version) {
  try {
    const job = await post(`/api/projects/${state.project.id}/versions/${version}/build`, { mode: "layer" });
    layerJob = job;
    drawSide();
    const { watchJob } = await import("./app.js");
    const final = await watchJob(job.id, (j) => { layerJob = j; drawSide(); });
    layerJob = null;
    layerOffer = null;
    await refreshProject();
    if (final.status === "completed") {
      const r = final.result;
      state.buildId = r.build_id;
      toast(`3D model updated in ${r.timings?.total_s ?? "?"} s: ${r.sensors} sensors on the saved building (build ${r.base_build.slice(2)}). Nothing was regenerated.`, "ok", 9000);
      switchTab("generate");
    } else showError(final.error || { message: "failed" }, "Updating the 3D model");
  } catch (e) { layerJob = null; drawSide(); showError(e, "Updating the 3D model"); }
}

function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    try {
      report = await post("/api/scene/validate", { scene, brief: state.project.brief });
      reportError = null;
    } catch (e) {
      reportError = e;
    }
    draw();
    drawSide();
  }, 350);
}

async function serverTool(path, body, message) {
  try {
    const res = await post(path, { scene, ...body });
    edit(() => { scene = res.scene; });
    const text = message && message(res);
    if (text) toast(text, "ok");
    return res;
  } catch (e) { showError(e, path.split("/").pop()); }
}

// ---------------------------------------------------------------------------------------------
// view
// ---------------------------------------------------------------------------------------------
function fitView() {
  const pts = [...scene.rooms.flatMap((r) => r.polygon), ...scene.walls.flatMap((w) => [w.start, w.end])];
  if (!pts.length) pts.push([0, 0], [10, 8]);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const [minx, maxx, miny, maxy] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  measure();
  view.cx = (minx + maxx) / 2;
  view.cy = (miny + maxy) / 2;
  view.zoom = Math.max(8, Math.min(400, 0.85 * Math.min(view.w / Math.max(1, maxx - minx), view.h / Math.max(1, maxy - miny))));
}
function measure() {
  if (!svg) return;
  const r = svg.getBoundingClientRect();
  view.w = r.width || 800;
  view.h = r.height || 600;
}

function underlayPlacement() {
  const p = state.project;
  const fid = planFileId();
  const f = p?.files.find((x) => x.id === fid);
  if (!f) return null;
  let cal = f.calibration;
  let calibrated = true;
  if (!cal) {
    calibrated = false;
    const b = p.brief;
    const mPerPx = b.overall_width ? (b.overall_width / f.width) : 0.01;
    cal = { m_per_px: mPerPx, origin_px: 0, origin_py: f.height };
  }
  return { f, cal, calibrated };
}

// ---------------------------------------------------------------------------------------------
// SVG drawing
// ---------------------------------------------------------------------------------------------
function s(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, String(v));
  if (parent) parent.append(el);
  return el;
}
const pts = (poly) => poly.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(" ");

function issueTargets() {
  const bad = new Set();
  for (const i of report?.issues || []) if (i.severity === "error" || i.severity === "warning") i.targets.forEach((t) => bad.add(t));
  return bad;
}

function draw() {
  if (!svg || !scene) return;
  measure();
  svg.replaceChildren();
  const bad = issueTargets();
  // grid
  const g = s("g", {}, svg);
  const step = view.zoom > 30 ? 1 : 5;
  const x0 = Math.floor(wx(0) / step) * step, x1 = wx(view.w), y0 = Math.floor(wy(view.h) / step) * step, y1 = wy(0);
  for (let x = x0; x <= x1; x += step) s("line", { x1: sx(x), x2: sx(x), y1: 0, y2: view.h, class: "svg-grid" + (x % 5 === 0 ? " major" : "") }, g);
  for (let y = y0; y <= y1; y += step) s("line", { y1: sy(y), y2: sy(y), x1: 0, x2: view.w, class: "svg-grid" + (y % 5 === 0 ? " major" : "") }, g);
  s("line", { x1: sx(0), x2: sx(0), y1: sy(0) - 6, y2: sy(0) + 6, class: "svg-axis" }, g);
  s("line", { x1: sx(0) - 6, x2: sx(0) + 6, y1: sy(0), y2: sy(0), class: "svg-axis" }, g);

  // plan underlay
  const ul = underlayPlacement();
  if (ul && underlay.visible) {
    const { f, cal } = ul;
    const left = -cal.origin_px * cal.m_per_px, top = cal.origin_py * cal.m_per_px;
    s("image", { href: `/api/projects/${state.project.id}/files/${f.id}/normalized`, x: sx(left), y: sy(top),
      width: f.width * cal.m_per_px * view.zoom, height: f.height * cal.m_per_px * view.zoom,
      opacity: underlay.opacity, preserveAspectRatio: "none" }, svg);
  }

  // rooms
  for (const r of scene.rooms) {
    const cls = "svg-room" + (sel?.type === "room" && sel.id === r.id ? " sel" : "") +
      (STATUS_NEEDS.includes(r.certainty?.status) ? " low" : "");
    const poly = s("polygon", { points: pts(r.polygon), class: cls, fill: ROOM_COLORS[r.type] || "#eee",
      "fill-opacity": ul && underlay.visible ? 0.3 : 0.6 }, svg);
    poly.addEventListener("pointerdown", (e) => { if (mode === "select") { e.stopPropagation(); select({ type: "room", id: r.id }); } });
  }
  // walls
  for (const w of scene.walls) {
    const f = frame(w), t = w.thickness / 2;
    const quad = [f.at(0, -t), f.at(f.L, -t), f.at(f.L, t), f.at(0, t)];
    const el = s("polygon", { points: pts(quad), class: `svg-wall ${w.kind}` + (sel?.type === "wall" && sel.id === w.id ? " sel" : "") }, svg);
    el.addEventListener("pointerdown", (e) => { if (mode === "select") { e.stopPropagation(); select({ type: "wall", id: w.id }); } });
  }
  // openings
  for (const o of scene.openings) {
    const w = byId("walls", o.wall_id);
    if (!w) continue;
    const f = frame(w), t = w.thickness / 2 + 0.01, a = o.offset - o.width / 2, b = o.offset + o.width / 2;
    const gap = s("polygon", { points: pts([f.at(a, -t), f.at(b, -t), f.at(b, t), f.at(a, t)]),
      class: "svg-open-gap" + (sel?.type === "opening" && sel.id === o.id ? " sel" : "") }, svg);
    gap.addEventListener("pointerdown", (e) => { if (mode === "select") { e.stopPropagation(); select({ type: "opening", id: o.id }); } });
    if (o.kind === "window") {
      for (const d of [-w.thickness / 6, w.thickness / 6]) {
        const [p, q] = [f.at(a, d), f.at(b, d)];
        s("line", { x1: sx(p[0]), y1: sy(p[1]), x2: sx(q[0]), y2: sy(q[1]), class: "svg-window" }, svg);
      }
    } else if (o.kind === "door") {
      let side = 1;
      if (o.opens_into) {
        const room = byId("rooms", o.opens_into);
        const probe = f.at(o.offset, w.thickness / 2 + 0.05);
        if (room && !pointInPoly(probe[0], probe[1], room.polygon)) side = -1;
      }
      const hinge = o.hinge_side === "start" ? a : b, dir = o.hinge_side === "start" ? 1 : -1;
      const hp = f.at(hinge, side * w.thickness / 2), leaf = f.at(hinge, side * (w.thickness / 2 + o.width));
      const closed = f.at(hinge + dir * o.width, side * w.thickness / 2);
      s("line", { x1: sx(hp[0]), y1: sy(hp[1]), x2: sx(leaf[0]), y2: sy(leaf[1]), class: "svg-open-door", "stroke-width": 2 }, svg);
      const R = o.width * view.zoom;
      const sweep = (side * dir > 0) ? 0 : 1;
      s("path", { d: `M ${sx(leaf[0])} ${sy(leaf[1])} A ${R} ${R} 0 0 ${sweep} ${sx(closed[0])} ${sy(closed[1])}`, class: "svg-open-door", "stroke-dasharray": "4 3" }, svg);
    }
  }
  // furniture
  for (const fu of scene.furniture) {
    const [w, d] = furnSize(fu);
    const a = assetById(fu.asset_id);
    const corners = rectCorners(fu.position[0], fu.position[1], w, d, fu.rotation_deg);
    const isSel = sel?.type === "furniture" && sel.id === fu.id;
    const el = s("polygon", { points: pts(corners), class: "svg-furn" + (a?.placement === "floor_layer" ? " rug" : "") +
      (bad.has(`furniture:${fu.id}`) ? " bad" : "") + (isSel ? " sel" : "") }, svg);
    s("line", { x1: sx(corners[0][0]), y1: sy(corners[0][1]), x2: sx(corners[1][0]), y2: sy(corners[1][1]), class: "svg-front" }, svg);
    if (view.zoom > 35 && a?.placement !== "floor_layer") {
      const txt = s("text", { x: sx(fu.position[0]), y: sy(fu.position[1]) + 3, class: "svg-label" }, svg);
      txt.textContent = (fu.label || a?.name || fu.asset_id).split(" ")[0];
    }
    el.addEventListener("pointerdown", (e) => {
      if (mode !== "select") return;
      e.stopPropagation();
      const off = [wx(e.offsetX) - fu.position[0], wy(e.offsetY) - fu.position[1]];
      select({ type: "furniture", id: fu.id });
      startDrag(e, (x, y, ev) => { fu.position = [r3(snap(x - off[0], ev)), r3(snap(y - off[1], ev))]; });
    });
  }
  if (hvacVisible && scene.hvac) drawHvac();
  // room labels
  const sums = Object.fromEntries((report?.rooms || []).map((r) => [r.id, r]));
  for (const r of scene.rooms) {
    const [cx, cy] = centroid(r.polygon);
    const t1 = s("text", { x: sx(cx), y: sy(cy) - 4, class: "svg-label room" }, svg);
    t1.textContent = r.name;
    const sm = sums[r.id];
    if (sm) {
      const t2 = s("text", { x: sx(cx), y: sy(cy) + 10, class: "svg-label dim" }, svg);
      t2.textContent = `${fmtM(sm.width_clear)} × ${fmtM(sm.depth_clear)} m · ${fmtM(sm.area_net, 1)} m²`;
    }
  }
  if (sensorsVisible && scene.sensors?.length) drawSensors(bad);
  // vertex handles for the selected room
  if (sel?.type === "room") {
    const r = byId("rooms", sel.id);
    r?.polygon.forEach((p, i) => {
      const hnd = s("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 6, class: "svg-handle" }, svg);
      hnd.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        let cur = [...p];
        startDrag(e, (x, y, ev) => {
          const to = [r3(snap(x, ev)), r3(snap(y, ev))];
          moveLinkedPoint(cur, to);
          cur = to;
        });
      });
    });
  }
  // new-room rectangle preview
  if (mode === "draw-room" && drawRect) {
    const { x0, y0, x1, y1 } = drawRect;
    s("polygon", { points: pts([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]), class: "svg-room sel", fill: "#cfe0f1", "fill-opacity": 0.5 }, svg);
    const t = s("text", { x: sx((x0 + x1) / 2), y: sy((y0 + y1) / 2), class: "svg-label room" }, svg);
    t.textContent = `${fmtM(Math.abs(x1 - x0))} × ${fmtM(Math.abs(y1 - y0))} m`;
  }
  // calibration markers
  if (mode === "calibrate" && calib) {
    for (const p of [calib.a, calib.b].filter(Boolean)) s("circle", { cx: p.sx, cy: p.sy, r: 5, class: "svg-cal" }, svg);
    if (calib.a && calib.b) s("line", { x1: calib.a.sx, y1: calib.a.sy, x2: calib.b.sx, y2: calib.b.sy, class: "svg-cal" }, svg);
  }
}

function ductQuads(d) {
  const out = [];
  for (let i = 0; i + 1 < d.path.length; i++) {
    const [a, b] = [d.path[i], d.path[i + 1]];
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const nx = -(b[1] - a[1]) / L, ny = (b[0] - a[0]) / L;
    const w0 = d.width / 2, w1 = (d.width_end ?? d.width) / 2;
    out.push([[a[0] + nx * w0, a[1] + ny * w0], [b[0] + nx * w1, b[1] + ny * w1], [b[0] - nx * w1, b[1] - ny * w1], [a[0] - nx * w0, a[1] - ny * w0]]);
  }
  return out;
}

/** HVAC layer: ducts, diffusers/grilles (with links to the selected unit) and units; units and terminals drag. */
function drawHvac() {
  const hv = scene.hvac;
  const g = s("g", {}, svg);
  const isSel = (type, id) => sel?.type === type && sel.id === id;
  const selUnit = sel?.type === "hvac_unit" ? sel.id : sel?.type === "hvac_terminal" ? hvacById("hvac_terminal", sel.id)?.unit_id : null;
  for (const d of hv.ducts) {
    for (const q of ductQuads(d)) {
      const el = s("polygon", { points: pts(q), class: `svg-hvac-duct ${d.system}` + (isSel("duct", d.id) ? " sel" : "") }, g);
      el.addEventListener("pointerdown", (e) => { if (mode === "select") { e.stopPropagation(); select({ type: "duct", id: d.id }); } });
    }
  }
  const unitPos = Object.fromEntries(hv.units.map((u) => [u.id, u.position]));
  for (const t of hv.terminals) {
    if (selUnit && t.unit_id === selUnit && unitPos[selUnit]) {
      const [ux, uy] = unitPos[selUnit];
      s("line", { x1: sx(t.position[0]), y1: sy(t.position[1]), x2: sx(ux), y2: sy(uy), class: "svg-hvac-link" }, g);
    }
    const c = rectCorners(t.position[0], t.position[1], t.width, t.depth, t.rotation_deg);
    const ret = t.kind.startsWith("return") || t.kind.startsWith("exhaust");
    const el = s("polygon", { points: pts(c), class: "svg-hvac-term" + (ret ? " return" : "") + (isSel("hvac_terminal", t.id) ? " sel" : "") }, g);
    const diag = ret ? [[0, 2]] : [[0, 2], [1, 3]];
    for (const [a, b] of diag) s("line", { x1: sx(c[a][0]), y1: sy(c[a][1]), x2: sx(c[b][0]), y2: sy(c[b][1]), class: "svg-hvac-term" + (ret ? " return" : ""), "pointer-events": "none" }, g);
    el.addEventListener("pointerdown", (e) => {
      if (mode !== "select") return;
      e.stopPropagation();
      select({ type: "hvac_terminal", id: t.id });
      const off = [wx(e.offsetX) - t.position[0], wy(e.offsetY) - t.position[1]];
      startDrag(e, (x, y, ev) => { t.position = [r3(snap(x - off[0], ev)), r3(snap(y - off[1], ev))]; });
    });
    if (view.zoom > 45 && t.airflow_lps) {
      const lab = s("text", { x: sx(t.position[0]), y: sy(t.position[1]) - (t.depth * view.zoom) / 2 - 3, class: "svg-label dim" }, g);
      lab.textContent = `${Math.round(t.airflow_lps)}`;
    }
  }
  for (const u of hv.units) {
    const c = rectCorners(u.position[0], u.position[1], u.size.width, u.size.depth, u.rotation_deg);
    const el = s("polygon", { points: pts(c), class: "svg-hvac-unit" + (isSel("hvac_unit", u.id) ? " sel" : "") }, g);
    el.addEventListener("pointerdown", (e) => {
      if (mode !== "select") return;
      e.stopPropagation();
      select({ type: "hvac_unit", id: u.id });
      const off = [wx(e.offsetX) - u.position[0], wy(e.offsetY) - u.position[1]];
      startDrag(e, (x, y, ev) => { u.position = [r3(snap(x - off[0], ev)), r3(snap(y - off[1], ev))]; });
    });
    if (view.zoom > 14) {
      const lab = s("text", { x: sx(u.position[0]), y: sy(u.position[1]) + 3, class: "svg-label hvac" }, g);
      lab.textContent = u.tag;
    }
  }
}

/** Sensor layer: a dot in the kind's colour (ring = needs review, red ring = has an issue), tag when zoomed in;
 * the selected sensor shows a dashed link to the unit it monitors. Dots drag; Alt = no snap. */
function drawSensors(bad) {
  const g = s("g", {}, svg);
  const unitPos = Object.fromEntries((scene.hvac?.units || []).map((u) => [u.id, u.position]));
  const R = Math.max(4.5, Math.min(9, 0.09 * view.zoom));
  for (const se of scene.sensors) {
    const [x, y] = se.position;
    const isSel = sel?.type === "sensor" && sel.id === se.id;
    if (isSel && se.unit_id && unitPos[se.unit_id]) {
      const [ux, uy] = unitPos[se.unit_id];
      s("line", { x1: sx(x), y1: sy(y), x2: sx(ux), y2: sy(uy), class: "svg-sensor-link" }, g);
    }
    const review = STATUS_NEEDS.includes(se.certainty?.status);
    const el = s("circle", { cx: sx(x), cy: sy(y), r: R, fill: sensorKind(se.kind).color,
      class: "svg-sensor" + (se.mounting === "ceiling" ? " ceiling" : "") + (review ? " review" : "") +
        (bad.has(`sensor:${se.id}`) ? " bad" : "") + (isSel ? " sel" : "") }, g);
    s("title", {}, el).textContent = `${se.tag} · ${sensorKind(se.kind).label}${se.bms_point ? ` · ${se.bms_point}` : ""}`;
    if (view.zoom > 28 || isSel) {
      const t = s("text", { x: sx(x) + R + 2, y: sy(y) + 3, class: "svg-label sensor" }, g);
      t.textContent = se.tag;
    }
    el.addEventListener("pointerdown", (e) => {
      if (mode !== "select") return;
      e.stopPropagation();
      select({ type: "sensor", id: se.id });
      const off = [wx(e.offsetX) - x, wy(e.offsetY) - y];
      startDrag(e, (px, py, ev) => { se.position = [r3(snap(px - off[0], ev)), r3(snap(py - off[1], ev))]; });
    });
  }
}

async function placeSensorClick(e) {
  const x = r3(wx(e.offsetX)), y = r3(wy(e.offsetY));
  const room = roomAt(x, y);
  const units = scene.hvac?.units || [];
  // default link: the unit serving this room, else the nearest one
  const near = units.filter((u) => room && u.room_id === room.id)[0] ||
    [...units].sort((a, b) => Math.hypot(a.position[0] - x, a.position[1] - y) - Math.hypot(b.position[0] - x, b.position[1] - y))[0];
  const kinds = state.catalog.sensor_kinds;
  const lastKind = placeSensorClick.kind || "temperature";
  const v = await ask(`New sensor ${room ? `in ${room.name}` : "(outside every room)"}`, [
    { name: "kind", label: "Type", value: lastKind, options: kinds.map((k) => [k.id, k.label]) },
    { name: "tag", label: "Tag (empty = next free, e.g. TS-03)", value: "" },
    { name: "mounting", label: "Mounting", value: placeSensorClick.mounting || "wall",
      options: [["wall", "on the nearest wall"], ["ceiling", "under the ceiling"], ["free", "free-standing / exactly here"]] },
    { name: "elevation", label: "Height above floor m (empty = 1.5 m, or under the ceiling)", type: "number", value: "" },
    { name: "unit", label: "Monitors HVAC unit", value: near?.id || "", options: [["", "— none —"], ...units.map((u) => [u.id, u.tag])] },
    { name: "bms", label: "BMS / IoT point (for live readings)", value: "" },
  ], "Place");
  if (!v) return;
  placeSensorClick.kind = v.kind;
  placeSensorClick.mounting = v.mounting;
  let pos = [x, y];
  if (v.mounting === "wall") {
    const face = wallFacePoint(x, y);
    if (face) pos = face.p; else toast("No wall within 1 m: placed where you clicked.", "info");
  }
  const id = nextSensorId();
  const tag = (v.tag || "").trim() || nextTag(v.kind);
  if (scene.sensors.some((x2) => x2.tag === tag)) return toast(`Tag ${tag} is already used.`, "error");
  edit(() => scene.sensors.push({ id, tag, kind: v.kind, level_id: (room?.level_id) || scene.levels[0].id,
    room_id: roomAt(pos[0], pos[1])?.id ?? room?.id ?? null, position: pos, elevation: v.elevation ?? null,
    mounting: v.mounting, unit_id: v.unit || null, bms_point: (v.bms || "").trim(), label: "",
    certainty: { status: "confirmed", source: "user", confidence: null, note: "placed in review" } }));
  sel = { type: "sensor", id };
  drawAll();
}

async function importSensorSheet(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const res = await post("/api/scene/import-sensors", { scene, text });
    if (res.added.length || res.updated.length) edit(() => { scene = res.scene; });
    const msg = `Sheet ${file.name}: ${res.added.length} added, ${res.updated.length} updated` +
      (res.problems.length ? `, ${res.problems.length} row(s) skipped — ${res.problems.slice(0, 3).join("; ")}` : "");
    toast(msg + (res.added.length || res.updated.length ? ". Check them, then save a new version." : ""), res.problems.length ? "error" : "ok", 12000);
  } catch (e) { showError(e, "Sensor sheet"); }
}

function startDrag(e, apply) {
  snapshot();
  let moved = false;
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    apply(wx(ev.clientX - rect.left), wy(ev.clientY - rect.top), ev);
    moved = true;
    draw();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (moved) changed(); else undo.pop();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function select(target) {
  sel = target;
  draw();
  drawSide();
}

function attachCanvasEvents() {
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const before = [wx(e.offsetX), wy(e.offsetY)];
    view.zoom = Math.max(5, Math.min(600, view.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    view.cx += before[0] - wx(e.offsetX);
    view.cy += before[1] - wy(e.offsetY);
    draw();
  }, { passive: false });
  svg.addEventListener("pointermove", (e) => {
    if (hintEl) hintEl.textContent = `x ${fmtM(wx(e.offsetX))} m · y ${fmtM(wy(e.offsetY))} m  —  ` +
      (mode === "draw-room" ? "drag to draw a room (snaps to 5 cm) · Esc cancels"
        : mode === "place-sensor" ? "click where the sensor goes (wall sensors snap to the nearest wall face) · Esc stops placing"
          : "wheel = zoom · drag = pan · 1 grid = 1 m · origin = red cross");
  });
  svg.addEventListener("pointerdown", (e) => {
    if (mode === "calibrate") return calibrateClick(e);
    if (mode === "draw-room") return drawRoomStart(e);
    if (mode === "place-sensor") return placeSensorClick(e);
    sel = null;
    drawSide();
    const start = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy };
    const move = (ev) => {
      view.cx = start.cx - (ev.clientX - start.x) / view.zoom;
      view.cy = start.cy + (ev.clientY - start.y) / view.zoom;
      draw();
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); draw(); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  window.addEventListener("keydown", (e) => {
    if (state.activeTab !== "review" || !scene) return;
    const tag = /** @type {HTMLElement} */ (e.target).tagName;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); doUndo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); doRedo(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelection(); }
    else if (e.key === "Escape") { mode = "select"; calib = null; drawRect = null; sel = null; drawAll(); }
    else if (e.key.toLowerCase() === "r" && (sel?.type === "hvac_unit" || sel?.type === "hvac_terminal")) {
      const el = hvacById(sel.type, sel.id);
      edit(() => { el.rotation_deg = ((el.rotation_deg + (e.shiftKey ? 15 : 90)) % 360 + 360) % 360; });
    }
    else if (e.key.toLowerCase() === "r" && sel?.type === "furniture") {
      const fu = byId("furniture", sel.id);
      edit(() => { fu.rotation_deg = ((fu.rotation_deg + (e.shiftKey ? 15 : 90)) % 360 + 360) % 360; });
    }
  });
  new ResizeObserver(() => draw()).observe(svg);
}

function drawRoomStart(e) {
  const x0 = r3(snap(wx(e.offsetX), e)), y0 = r3(snap(wy(e.offsetY), e));
  drawRect = { x0, y0, x1: x0, y1: y0 };
  const move = (ev) => {
    const r = svg.getBoundingClientRect();
    drawRect.x1 = r3(snap(wx(ev.clientX - r.left), ev));
    drawRect.y1 = r3(snap(wy(ev.clientY - r.top), ev));
    draw();
  };
  const up = async () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    const { x0, y0, x1, y1 } = drawRect;
    drawRect = null;
    mode = "select";
    if (Math.abs(x1 - x0) < 0.5 || Math.abs(y1 - y0) < 0.5) { draw(); return toast("Room too small (min 0.5 m); nothing added.", "error"); }
    const v = await ask("New room", [
      { name: "name", label: "Name", value: `Room ${scene.rooms.length + 1}` },
      { name: "type", label: "Type", value: "bedroom", options: state.catalog.room_types.map((t) => [t, t]) }], "Add");
    if (!v) return draw();
    const [ax, bx] = [Math.min(x0, x1), Math.max(x0, x1)], [ay, by] = [Math.min(y0, y1), Math.max(y0, y1)];
    const id = nextId("R", "rooms");
    edit(() => scene.rooms.push({ id, name: v.name, type: v.type, level_id: scene.levels[0].id,
      polygon: [[ax, ay], [bx, ay], [bx, by], [ax, by]], floor_offset: 0, ceiling_height: null,
      finishes: { floor: null, walls: null, ceiling: null }, certainty: { status: "confirmed", source: "user", confidence: null, note: "traced" } }));
    select({ type: "room", id });
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

// ---------------------------------------------------------------------------------------------
// calibration of the plan underlay
// ---------------------------------------------------------------------------------------------
function startCalibration() {
  const ul = underlayPlacement();
  if (!ul) return toast("Upload a floor plan first (step 1).", "error");
  mode = "calibrate";
  calib = { step: 1, ul };
  underlay.visible = true;
  toast("Calibrate: click both ends of a dimension you know on the plan image.", "info", 8000);
  drawAll();
}
async function calibrateClick(e) {
  const { cal } = calib.ul;
  const x = wx(e.offsetX), y = wy(e.offsetY);
  const px = cal.origin_px + x / cal.m_per_px, py = cal.origin_py - y / cal.m_per_px;
  const pt = { px, py, sx: e.offsetX, sy: e.offsetY };
  if (calib.step === 1) { calib.a = pt; calib.step = 2; draw(); return; }
  if (calib.step === 2) {
    calib.b = pt;
    draw();
    const v = await ask("Known length", [{ name: "len", label: "Real length of that line (m)", type: "number", value: 3.0 }]);
    if (!v || !v.len) { mode = "select"; calib = null; return drawAll(); }
    const dpx = Math.hypot(calib.b.px - calib.a.px, calib.b.py - calib.a.py);
    calib.mPerPx = v.len / dpx;
    calib.step = 3;
    toast("Now click the plan's origin: the south-west (bottom-left) corner of the exterior wall centreline.", "info", 9000);
    return;
  }
  if (calib.step === 3) {
    const calibration = { m_per_px: calib.mPerPx, origin_px: px, origin_py: py };
    try {
      await put(`/api/projects/${state.project.id}/files/${calib.ul.f.id}/calibration`, { calibration });
      await refreshProject();
      toast(`Plan calibrated: ${(1 / calib.mPerPx).toFixed(1)} px per metre.`, "ok");
    } catch (err) { showError(err, "Saving calibration"); }
    mode = "select";
    calib = null;
    drawAll();
  }
}

// ---------------------------------------------------------------------------------------------
// side panel
// ---------------------------------------------------------------------------------------------
function numField(label, value, onset, attrs = {}) {
  return [h("span", null, label), h("input", { type: "number", step: "0.01", value: value ?? "", ...attrs,
    onchange: (e) => edit(() => onset(e.target.value === "" ? null : Number(e.target.value))) })];
}
function selectField(label, value, options, onset) {
  return [h("span", null, label), h("select", { onchange: (e) => edit(() => onset(e.target.value)) },
    options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)))];
}
function textField(label, value, onset) {
  return [h("span", null, label), h("input", { value: value ?? "", onchange: (e) => edit(() => onset(e.target.value)) })];
}
function certaintyRow(el) {
  const c = el.certainty || { status: "confirmed" };
  return [h("span", null, "Measurement"), h("div.row", null, badge(c.status),
    c.confidence !== null && c.confidence !== undefined ? h("span.small.muted", null, `${Math.round(c.confidence * 100)}%`) : null,
    c.status !== "confirmed" ? h("button.btn.small", { onclick: () => edit(() => confirmCertainty(el)) }, "Confirm") : null,
    STATUS_NEEDS.includes(c.status) ? h("button.btn.small", { onclick: () => edit(() => { el.certainty = { ...c, status: "accepted_estimate" }; }) }, "Accept estimate") : null),
    c.note ? h("span") : null, c.note ? h("span.small.muted", null, c.note) : null];
}
function materialOptions(current, emptyLabel = "— style default —") {
  const opts = [["", emptyLabel]];
  for (const m of scene.materials) opts.push([m.id, `${m.name} (custom)`]);
  for (const m of state.catalog.materials) opts.push([m.id, m.name]);
  if (current && !opts.some(([v]) => v === current)) opts.push([current, current]);
  return opts;
}

function inspector() {
  if (!sel) {
    const lv = scene.levels[0];
    return h("div.card.inspector", null, h("h2", null, "Level & project"),
      h("div.kv", null,
        ...numField("Wall height m", lv.wall_height, (v) => (lv.wall_height = v ?? 2.7)),
        ...selectField("Ceilings", String(lv.ceilings), [["true", "yes"], ["false", "no"]], (v) => (lv.ceilings = v === "true")),
        ...selectField("Style", scene.project.style, Object.entries(state.catalog.styles).map(([k, s]) => [k, s.label]), (v) => (scene.project.style = v)),
        ...selectField("Lighting", scene.lighting.mood, [["warm", "warm"], ["neutral", "neutral"], ["cool", "cool"], ["daylight", "daylight"]], (v) => (scene.lighting.mood = v)),
        ...certaintyRow(lv)),
      h("p.small.muted", null, "Click a room, wall, opening or furniture item to edit it. Drag furniture to move it (Alt = no snap), R rotates 90°, Shift+R 15°, Delete removes, Ctrl+Z undoes. Drag the white handles of a selected room to reshape it; shared corners move together."));
  }
  if (sel.type === "room") {
    const r = byId("rooms", sel.id);
    if (!r) return null;
    const sm = (report?.rooms || []).find((x) => x.id === r.id);
    const xs = r.polygon.map((p) => p[0]), ys = r.polygon.map((p) => p[1]);
    const isRect = r.polygon.length === 4 && r.polygon.every(([x, y]) =>
      (Math.abs(x - Math.min(...xs)) < 1e-6 || Math.abs(x - Math.max(...xs)) < 1e-6) && (Math.abs(y - Math.min(...ys)) < 1e-6 || Math.abs(y - Math.max(...ys)) < 1e-6));
    const resize = (axis, value) => {
      const minv = axis === 0 ? Math.min(...xs) : Math.min(...ys), maxv = axis === 0 ? Math.max(...xs) : Math.max(...ys);
      const target = minv + value;
      for (const p of r.polygon.filter((p) => Math.abs(p[axis] - maxv) < 1e-6).map((p) => [...p])) {
        const to = [...p];
        to[axis] = r3(target);
        moveLinkedPoint(p, to);
      }
    };
    return h("div.card.inspector", null, h("h2", null, `Room ${r.id}`),
      h("div.kv", null,
        ...textField("Name", r.name, (v) => (r.name = v || r.name)),
        ...selectField("Type", r.type, state.catalog.room_types.map((t) => [t, t]), (v) => (r.type = v)),
        ...(isRect ? [...numField("Width (centre) m", r3(Math.max(...xs) - Math.min(...xs)), (v) => v > 0.3 && resize(0, v)),
          ...numField("Depth (centre) m", r3(Math.max(...ys) - Math.min(...ys)), (v) => v > 0.3 && resize(1, v))] : []),
        h("span", null, "Clear size"), h("span", null, sm ? `${fmtM(sm.width_clear)} × ${fmtM(sm.depth_clear)} m, ${fmtM(sm.area_net, 1)} m²` : "–"),
        ...selectField("Floor", r.finishes.floor || "", materialOptions(r.finishes.floor), (v) => (r.finishes.floor = v || null)),
        ...selectField("Walls", r.finishes.walls || "", materialOptions(r.finishes.walls), (v) => (r.finishes.walls = v || null)),
        ...selectField("Ceiling", r.finishes.ceiling || "", materialOptions(r.finishes.ceiling), (v) => (r.finishes.ceiling = v || null)),
        ...selectField("Lower band", r.finishes.dado || "", materialOptions(r.finishes.dado, "— none —"), (v) => (r.finishes.dado = v || null)),
        ...(r.finishes.dado ? numField("Lower band top m", r.finishes.dado_height ?? 1.1, (v) => v > 0.05 && (r.finishes.dado_height = v)) : []),
        ...selectField("Upper band", r.finishes.frieze || "", materialOptions(r.finishes.frieze, "— none —"), (v) => (r.finishes.frieze = v || null)),
        ...(r.finishes.frieze ? numField("Upper band from m", r.finishes.frieze_from ?? 2.4, (v) => v > 0.3 && (r.finishes.frieze_from = v)) : []),
        ...certaintyRow(r)),
      h("h3", null, "Boundary (wall centrelines)"),
      h("table.simple", null, h("tbody", null, r.polygon.map((p, i) => h("tr", null,
        h("td.small.muted", null, i + 1),
        h("td", null, h("input", { type: "number", step: "0.01", value: p[0], style: { width: "80px" }, onchange: (e) => edit(() => moveLinkedPoint([...p], [Number(e.target.value), p[1]])) })),
        h("td", null, h("input", { type: "number", step: "0.01", value: p[1], style: { width: "80px" }, onchange: (e) => edit(() => moveLinkedPoint([...p], [p[0], Number(e.target.value)])) })),
        h("td", null, h("button.btn.small", { title: "Insert a vertex after this one", onclick: () => edit(() => {
          const q = r.polygon[(i + 1) % r.polygon.length];
          r.polygon.splice(i + 1, 0, [r3((p[0] + q[0]) / 2), r3((p[1] + q[1]) / 2)]);
        }) }, "+"), r.polygon.length > 3 ? h("button.btn.small.danger", { onclick: () => edit(() => r.polygon.splice(i, 1)) }, "×") : null))))),
      h("div.row", { style: { marginTop: "8px" } },
        h("button.btn.small", { onclick: addFurnitureDialog }, "+ Furniture here"),
        h("button.btn.small", { onclick: () => redesignDialog(r) }, "Redesign with AI"),
        h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete room")));
  }
  if (sel.type === "wall") {
    const w = byId("walls", sel.id);
    if (!w) return null;
    const addOpening = (kind) => edit(() => {
      const f = frame(w);
      const id = nextId("O", "openings");
      scene.openings.push({ id, kind, wall_id: w.id, offset: r3(f.L / 2), width: kind === "door" ? 0.9 : 1.2,
        height: kind === "door" ? 2.1 : 1.3, sill_height: kind === "door" ? 0 : 0.9, hinge_side: "start", opens_into: null,
        certainty: { status: "confirmed", source: "user", confidence: null, note: "" } });
      sel = { type: "opening", id };
    });
    return h("div.card.inspector", null, h("h2", null, `Wall ${w.id}`),
      h("div.kv", null,
        ...numField("Start x", w.start[0], (v) => moveLinkedPoint([...w.start], [v, w.start[1]])),
        ...numField("Start y", w.start[1], (v) => moveLinkedPoint([...w.start], [w.start[0], v])),
        ...numField("End x", w.end[0], (v) => moveLinkedPoint([...w.end], [v, w.end[1]])),
        ...numField("End y", w.end[1], (v) => moveLinkedPoint([...w.end], [w.end[0], v])),
        h("span", null, "Length"), h("span", null, `${fmtM(frame(w).L)} m`),
        ...numField("Thickness m", w.thickness, (v) => (w.thickness = v ?? w.thickness)),
        ...numField("Height m", w.height, (v) => (w.height = v), { placeholder: `${levelHeight()} (level)` }),
        ...selectField("Kind", w.kind, [["exterior", "exterior"], ["interior", "interior"]], (v) => (w.kind = v)),
        ...certaintyRow(w)),
      h("div.row", { style: { marginTop: "8px" } }, h("button.btn.small", { onclick: () => addOpening("door") }, "+ Door"),
        h("button.btn.small", { onclick: () => addOpening("window") }, "+ Window"), h("button.btn.small", { onclick: () => addOpening("opening") }, "+ Opening"),
        h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete wall")));
  }
  if (sel.type === "opening") {
    const o = byId("openings", sel.id);
    if (!o) return null;
    return h("div.card.inspector", null, h("h2", null, `${o.kind} ${o.id}`),
      h("div.kv", null,
        ...selectField("Kind", o.kind, [["door", "door"], ["window", "window"], ["opening", "opening (no leaf)"]], (v) => { o.kind = v; if (v === "door") o.sill_height = 0; }),
        ...selectField("Wall", o.wall_id, scene.walls.map((w) => [w.id, w.id]), (v) => (o.wall_id = v)),
        ...numField("Offset (centre) m", o.offset, (v) => (o.offset = v ?? o.offset)),
        ...numField("Width m", o.width, (v) => (o.width = v ?? o.width)),
        ...numField("Height m", o.height, (v) => (o.height = v ?? o.height)),
        ...numField("Sill m", o.sill_height, (v) => (o.sill_height = v ?? 0)),
        ...selectField("Hinge side", o.hinge_side, [["start", "wall start"], ["end", "wall end"]], (v) => (o.hinge_side = v)),
        ...selectField("Opens into", o.opens_into || "", [["", "—"], ...scene.rooms.map((r) => [r.id, r.name])], (v) => (o.opens_into = v || null)),
        ...certaintyRow(o)),
      h("div.row", { style: { marginTop: "8px" } }, h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete")));
  }
  if (sel.type === "furniture") {
    const fu = byId("furniture", sel.id);
    if (!fu) return null;
    const a = assetById(fu.asset_id);
    const [w, d, hgt] = furnSize(fu);
    const setSize = (k, v) => {
      const cur = fu.size || { width: a.size[0], depth: a.size[1], height: a.size[2] };
      fu.size = { ...cur, [k]: v ?? a.size[{ width: 0, depth: 1, height: 2 }[k]] };
      if (fu.size.width === a.size[0] && fu.size.depth === a.size[1] && fu.size.height === a.size[2]) fu.size = null;
    };
    return h("div.card.inspector", null, h("h2", null, `${a?.name || fu.asset_id} (${fu.id})`),
      h("div.kv", null,
        ...selectField("Asset", fu.asset_id, state.catalog.assets.map((x) => [x.id, x.name]), (v) => { fu.asset_id = v; fu.size = null; fu.materials = {}; fu.elevation = assetById(v)?.default_elevation ?? 0; }),
        ...selectField("Room", fu.room_id, scene.rooms.map((r) => [r.id, r.name]), (v) => (fu.room_id = v)),
        ...numField("x m", fu.position[0], (v) => (fu.position = [v ?? 0, fu.position[1]])),
        ...numField("y m", fu.position[1], (v) => (fu.position = [fu.position[0], v ?? 0])),
        ...numField("Rotation °", fu.rotation_deg, (v) => (fu.rotation_deg = v ?? 0), { step: "15" }),
        ...numField("Width m", w, (v) => setSize("width", v)),
        ...numField("Depth m", d, (v) => setSize("depth", v)),
        ...numField("Height m", hgt, (v) => setSize("height", v)),
        ...numField("Elevation m", fu.elevation, (v) => (fu.elevation = Math.max(0, v ?? a?.default_elevation ?? 0))),
        ...textField("Label", fu.label, (v) => (fu.label = v || null)),
        ...certaintyRow(fu)),
      h("div.row", { style: { marginTop: "8px" } },
        h("button.btn.small", { onclick: () => serverTool("/api/scene/fit-furniture", { furniture_id: fu.id }, (r) => `Moved ${r.shift_m} m to fit`) }, "Fit inside room"),
        h("button.btn.small", { onclick: () => edit(() => { const id = nextId("F", "furniture"); scene.furniture.push({ ...structuredClone(fu), id, position: [fu.position[0] + 0.3, fu.position[1] - 0.3] }); sel = { type: "furniture", id }; }) }, "Duplicate"),
        h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete")));
  }
  if (sel.type in HVAC_COLL) return hvacInspector();
  if (sel.type === "sensor") return sensorInspector();
  return null;
}

function sensorInspector() {
  const se = byId("sensors", sel.id);
  if (!se) return null;
  const k = sensorKind(se.kind);
  const pos = (label, i) => numField(label, se.position[i], (v) => { const p = [...se.position]; p[i] = v ?? p[i]; se.position = p; });
  const units = scene.hvac?.units || [];
  return h("div.card.inspector", null,
    h("div.row", null, h("span.sensor-dot", { style: { background: k.color } }), h("h2", { style: { margin: 0 } }, `${k.label} ${se.tag}`)),
    h("div.kv", { style: { marginTop: "8px" } },
      ...textField("Tag", se.tag, (v) => { if (v && !scene.sensors.some((x) => x.tag === v && x.id !== se.id)) se.tag = v; else toast("Tags must be unique.", "error"); }),
      ...selectField("Type", se.kind, state.catalog.sensor_kinds.map((x) => [x.id, x.label]), (v) => (se.kind = v)),
      ...selectField("Mounting", se.mounting, [["wall", "wall"], ["ceiling", "ceiling"], ["free", "free-standing"]], (v) => (se.mounting = v)),
      ...pos("x m", 0), ...pos("y m", 1),
      ...numField("Height m", se.elevation, (v) => (se.elevation = v), { placeholder: se.mounting === "ceiling" ? "under the ceiling" : "1.5 (default)" }),
      ...selectField("Room", se.room_id || "", [["", "— where it stands —"], ...scene.rooms.map((r) => [r.id, r.name])], (v) => (se.room_id = v || null)),
      ...selectField("Monitors unit", se.unit_id || "", [["", "— none —"], ...units.map((u) => [u.id, u.tag])], (v) => (se.unit_id = v || null)),
      ...textField("BMS point", se.bms_point, (v) => (se.bms_point = (v || "").trim())),
      ...textField("Label", se.label, (v) => (se.label = v || "")),
      ...certaintyRow(se)),
    h("div.row", { style: { marginTop: "8px" } },
      h("button.btn.small", { title: "Move it onto the face of the nearest wall (within 1 m)", onclick: () => {
        const f = wallFacePoint(se.position[0], se.position[1]);
        if (!f) return toast("No wall within 1 m.", "error");
        edit(() => { se.position = f.p; se.mounting = "wall"; });
      } }, "Snap to wall"),
      h("button.btn.small", { onclick: () => edit(() => {
        const id = nextSensorId();
        scene.sensors.push({ ...structuredClone(se), id, tag: nextTag(se.kind), bms_point: "", position: [r3(se.position[0] + 0.5), se.position[1]] });
        sel = { type: "sensor", id };
      }) }, "Duplicate"),
      h("button.btn.small", { onclick: () => fixDialog(`sensor:${se.id}`) }, "Fix with AI…"),
      h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete")));
}

/** Steps 6-9 of the flow in one card: the sensor layer of this version, what changed since it was saved, and the
 * in-place update of the saved 3D model. */
function sensorsCard() {
  const list = scene.sensors || [];
  const sc = sensorChanges();
  const nChanged = sc.added.length + sc.removed.length + sc.moved.length + sc.edited.length;
  const buildingChanged = buildingJson(scene) !== savedBuilding;
  const fileIn = h("input", { type: "file", accept: ".csv,text/csv", style: { display: "none" },
    onchange: (e) => { importSensorSheet(e.target.files[0]); e.target.value = ""; } });
  const byKind = {};
  for (const x of list) byKind[x.kind] = (byKind[x.kind] || 0) + 1;
  const changeLine = !dirty ? null : h("div.notice." + (buildingChanged ? "warn" : nChanged ? "ok" : "info"), { style: { margin: "6px 0" } },
    nChanged ? h("div", null, `Since v${loadedVersion}: ` + [sc.added.length && `${sc.added.length} added`, sc.moved.length && `${sc.moved.length} moved`,
      sc.edited.length && `${sc.edited.length} edited`, sc.removed.length && `${sc.removed.length} removed`].filter(Boolean).join(", ") + " sensor(s).") : null,
    h("div.small", null, buildingChanged
      ? "The building itself changed too, so it will be generated again (Blender)."
      : "Only the sensor layer changed: after saving, the saved 3D model is updated in place — nothing is regenerated."));
  let offer = null;
  if (layerJob) {
    offer = h("div.notice", { style: { margin: "6px 0" } }, badge(layerJob.status), " ", layerJob.stage || "updating the saved model…");
  } else if (layerOffer && layerOffer.version === loadedVersion && !dirty) {
    const L = layerOffer.options.layer;
    const c = L.changes;
    offer = h("div.notice.ok", { style: { margin: "6px 0" } },
      h("div", null, h("b", null, `v${loadedVersion} saved. `),
        `Only sensors changed since the saved model (build ${L.base_build.id.slice(2)} · v${L.base_build.version}): ` +
        [c.added.length && `${c.added.length} added`, c.moved.length && `${c.moved.length} moved`, c.edited.length && `${c.edited.length} edited`,
          c.removed.length && `${c.removed.length} removed`].filter(Boolean).join(", ") + "."),
      h("div.row", { style: { marginTop: "6px" } },
        h("button.btn.primary", { onclick: () => updateSavedModel(loadedVersion) }, "Update the 3D model"),
        h("span.small.muted", null, "walls, HVAC and furniture are reused as they are")));
  }
  return h("div.card", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "Sensors"), h("span.spacer"),
      h("label.small", null, h("input", { type: "checkbox", checked: sensorsVisible, onchange: (e) => { sensorsVisible = e.target.checked; draw(); } }), " show")),
    h("div.small", { style: { margin: "6px 0" } }, list.length
      ? Object.entries(byKind).map(([kk, n]) => `${n}× ${sensorKind(kk).label.toLowerCase()}`).join(", ")
      : "No sensors yet. Place them on the plan or import a placement sheet; the saved 3D model is then updated without regenerating it."),
    changeLine, offer,
    h("div.row", null,
      h("button.btn.small" + (mode === "place-sensor" ? ".primary" : ""), { onclick: () => { mode = mode === "place-sensor" ? "select" : "place-sensor"; sel = null; drawAll(); },
        title: "Click on the plan to place sensors; Esc stops" }, mode === "place-sensor" ? "Placing… (Esc)" : "+ Place sensors"),
      h("button.btn.small", { onclick: () => fileIn.click(), title: "CSV: tag, kind, x, y, elevation, mounting, room, unit, bms_point, label" }, "Import sheet (CSV)"),
      h("a.small", { href: "/api/sensors/template.csv" }, "template"), fileIn),
    list.length ? h("div.sensor-list", null, list.slice(0, 200).map((x) => h("div.sensor-item" + (sel?.type === "sensor" && sel.id === x.id ? ".active" : ""),
      { onclick: () => select({ type: "sensor", id: x.id }) },
      h("span.sensor-dot", { style: { background: sensorKind(x.kind).color } }), h("b", null, x.tag),
      h("span.small.muted", null, [scene.rooms.find((r) => r.id === (x.room_id || roomAt(...x.position)?.id))?.name,
        x.elevation != null ? `${fmtM(x.elevation)} m` : x.mounting, x.bms_point].filter(Boolean).join(" · "))))) : null);
}

function hvacInspector() {
  const el = hvacById(sel.type, sel.id);
  if (!el) return null;
  const unitOpts = [["", "— none —"], ...scene.hvac.units.map((u) => [u.id, u.tag])];
  const roomOpts = [["", "— none —"], ...scene.rooms.map((r) => [r.id, r.name])];
  const pos = (label, i) => numField(label, el.position[i], (v) => { const p = [...el.position]; p[i] = v ?? p[i]; el.position = p; });
  const actions = h("div.row", { style: { marginTop: "8px" } },
    h("button.btn.small", { onclick: () => fixDialog(`${sel.type}:${el.id}`) }, "Fix with AI…"),
    h("span.spacer"), h("button.btn.small.danger", { onclick: deleteSelection }, "Delete"));
  if (sel.type === "hvac_unit") {
    const terms = scene.hvac.terminals.filter((t) => t.unit_id === el.id);
    const sup = terms.filter((t) => t.kind.startsWith("supply") || t.kind === "linear_slot");
    const sum = sup.reduce((a, t) => a + (t.airflow_lps || 0), 0);
    return h("div.card.inspector", null, h("h2", null, `HVAC unit ${el.tag}`),
      h("div.kv", null,
        ...textField("Tag", el.tag, (v) => (el.tag = v || el.tag)),
        ...selectField("Kind", el.kind, [["fcu_ducted", "ducted fan coil (ceiling void)"], ["fcu_exposed", "exposed / suspended unit"],
          ["fcu_cassette", "ceiling cassette"], ["split_wall", "wall split unit"], ["ahu", "air handling unit"], ["exhaust_fan", "exhaust fan"], ["other", "other"]], (v) => (el.kind = v)),
        ...pos("x m", 0), ...pos("y m", 1),
        ...numField("Rotation °", el.rotation_deg, (v) => (el.rotation_deg = v ?? 0), { step: "15" }),
        ...numField("Length m", el.size.width, (v) => (el.size = { ...el.size, width: v ?? el.size.width })),
        ...numField("Width m", el.size.depth, (v) => (el.size = { ...el.size, depth: v ?? el.size.depth })),
        ...numField("Height m", el.size.height, (v) => (el.size = { ...el.size, height: v ?? el.size.height })),
        ...numField("Elevation m", el.elevation, (v) => (el.elevation = v), { placeholder: "auto (ceiling void)" }),
        ...numField("Airflow L/s", el.airflow_lps, (v) => (el.airflow_lps = v)),
        ...selectField("Room", el.room_id || "", roomOpts, (v) => (el.room_id = v || null)),
        h("span", null, "Terminals"), h("span", null, `${sup.length} supply (${Math.round(sum)} L/s), ${terms.length - sup.length} return`),
        ...certaintyRow(el)), actions);
  }
  if (sel.type === "hvac_terminal") {
    return h("div.card.inspector", null, h("h2", null, `${el.kind.replace(/_/g, " ")} ${el.id}`),
      h("div.kv", null,
        ...selectField("Kind", el.kind, [["supply_diffuser", "supply diffuser"], ["supply_grille", "supply grille"], ["linear_slot", "linear slot"],
          ["return_grille", "return grille"], ["exhaust_grille", "exhaust grille"]], (v) => (el.kind = v)),
        ...selectField("Unit", el.unit_id || "", unitOpts, (v) => (el.unit_id = v || null)),
        ...pos("x m", 0), ...pos("y m", 1),
        ...numField("Rotation °", el.rotation_deg, (v) => (el.rotation_deg = v ?? 0), { step: "15" }),
        ...numField("Face width m", el.width, (v) => (el.width = v ?? el.width)),
        ...numField("Face depth m", el.depth, (v) => (el.depth = v ?? el.depth)),
        ...numField("Airflow L/s", el.airflow_lps, (v) => (el.airflow_lps = v)),
        ...selectField("Room", el.room_id || "", roomOpts, (v) => (el.room_id = v || null)),
        ...certaintyRow(el)), actions);
  }
  return h("div.card.inspector", null, h("h2", null, `${el.system} duct ${el.id}`),
    h("div.kv", null,
      ...selectField("System", el.system, [["supply", "supply"], ["return", "return"], ["exhaust", "exhaust"], ["fresh_air", "fresh air"]], (v) => (el.system = v)),
      ...selectField("Unit", el.unit_id || "", unitOpts, (v) => (el.unit_id = v || null)),
      ...numField("Width m", el.width, (v) => (el.width = v ?? el.width)),
      ...numField("Height m", el.height, (v) => (el.height = v ?? el.height)),
      ...numField("Elevation m", el.elevation, (v) => (el.elevation = v), { placeholder: "auto (ceiling void)" }),
      h("span", null, "Path"), h("span.small", null, el.path.map((p) => `(${fmtM(p[0])}, ${fmtM(p[1])})`).join(" → ")),
      ...certaintyRow(el)), actions);
}

// ---------------------------------------------------------------------------------------------
// HVAC import and "fix with AI"
// ---------------------------------------------------------------------------------------------
function hvacCard() {
  const files = state.project.files.filter((f) => f.kind === "hvac");
  const hv = scene.hvac || { units: [], terminals: [], ducts: [] };
  const status = h("div");
  if (hvacJob && hvacJob.project_id === state.project.id) drawHvacJob(status, hvacJob);
  const running = hvacJob && !["completed", "needs_review", "failed"].includes(hvacJob.status);
  const fileSel = h("select", null, files.map((f) => h("option", { value: f.id }, f.original_name)));
  const nSup = hv.terminals.filter((t) => t.kind.startsWith("supply") || t.kind === "linear_slot").length;
  return h("div.card", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "HVAC"), h("span.spacer"),
      h("label.small", null, h("input", { type: "checkbox", checked: hvacVisible, onchange: (e) => { hvacVisible = e.target.checked; draw(); } }), " show")),
    h("div.small", { style: { margin: "6px 0" } }, hvacCount()
      ? `${hv.units.length} units · ${nSup} supply diffusers · ${hv.terminals.length - nSup} grilles · ${hv.ducts.length} duct runs` +
        (hv.source?.file_name ? ` — from ${hv.source.file_name} (${hv.source.method}${hv.source.residual_m != null ? `, ±${(hv.source.residual_m * 1000).toFixed(0)} mm` : ""})` : "")
      : "No HVAC layer in this version."),
    files.length ? h("div.row", null, fileSel,
      h("button.btn.small" + (hvacCount() ? "" : ".primary"), { onclick: () => importHvac(fileSel.value), disabled: dirty || running,
        title: dirty ? "Save your edits first" : "Align the drawing to this plan, read it and place the HVAC elements (creates a new version)" },
        hvacCount() ? "Re-import" : "Import HVAC drawing"))
      : h("p.small.muted", null, "Upload the HVAC / ACMV drawing (vector PDF) in step 1 to import it."),
    hvacCount() ? h("div.row", { style: { marginTop: "6px" } },
      h("a.btn.small", { href: `/api/projects/${state.project.id}/versions/${loadedVersion}/hvac-overlay`, target: "_blank" }, "Plan + HVAC overlay"),
      h("button.btn.small", { onclick: () => switchTab("live") }, "Live HVAC →")) : null,
    status);
}

function drawHvacJob(el, job) {
  const pct = Math.round((job.progress || 0) * 100);
  const r = job.result;
  mount(el, h("div", { style: { marginTop: "8px" } },
    h("div.row", null, badge(job.status), h("span.small", null, job.stage || ""), h("span.spacer"), h("span.small.muted", null, `${pct}%`)),
    h("div.progress", { style: { marginTop: "6px" } }, h("div", { style: { width: `${pct}%` } })),
    job.status === "failed" ? h("div.notice.error", { style: { marginTop: "6px" } }, h("b", null, job.error?.code), ": ", job.error?.message) : null,
    r ? h("div.notice.ok", { style: { marginTop: "6px" } },
      h("div", null, `v${r.version}: ${r.counts.units} units, ${r.counts.supply_diffusers} diffusers (${r.counts.diffusers_with_labelled_airflow} with a labelled airflow), ${r.counts.return_grilles} grilles, ${r.counts.ducts} ducts (${r.counts.duct_length_m} m).`),
      h("div.small", null, `Aligned by ${r.registration.method}, residual ${(r.registration.residual_m * 1000).toFixed(1)} mm · ${r.timings.total_s} s`),
      r.overlay ? h("a", { href: r.overlay, target: "_blank" }, "Plan + HVAC overlay") : null, " · ",
      h("a", { href: r.report, target: "_blank" }, "full report"),
      r.issues?.length ? h("details", null, h("summary", null, `${r.issues.length} things to check`), h("ul.fix-changes", null, r.issues.map((i) => h("li", null, i)))) : null) : null));
}

async function importHvac(fileId) {
  if (!fileId) return;
  try {
    hvacJob = await post(`/api/projects/${state.project.id}/versions/${loadedVersion}/hvac`, { file_id: fileId });
    drawSide();
    const { watchJob } = await import("./app.js");
    const final = await watchJob(hvacJob.id, (j) => { hvacJob = j; drawSide(); });
    hvacJob = final;
    await refreshProject();
    if (final.status === "needs_review") {
      await loadVersion(final.result.version, true);
      toast(`HVAC placed: ${final.result.counts.units} units, ${final.result.counts.supply_diffusers} diffusers. Review it, then accept the estimates.`, "ok", 8000);
    } else showError(final.error || { message: "failed" }, "HVAC import");
  } catch (e) { showError(e, "HVAC import"); }
}

function furnitureCard() {
  const ai = state.health.ai;
  const status = h("div");
  if (furnJob && furnJob.project_id === state.project.id) drawFurnJob(status, furnJob);
  const running = furnJob && !["completed", "needs_review", "failed"].includes(furnJob.status);
  const key = `${state.project.id}:${loadedVersion}`;
  if (!furnEstimate || furnEstimate.key !== key) {
    furnEstimate = { key, data: null };
    get(`/api/projects/${state.project.id}/versions/${loadedVersion}/detect-furniture/estimate`)
      .then((d) => { if (furnEstimate.key === key) { furnEstimate.data = d; drawSide(); } }).catch(() => {});
  }
  const est = furnEstimate.data;
  const counts = {};
  for (const f of scene.furniture) counts[f.asset_id] = (counts[f.asset_id] || 0) + 1;
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 6)
    .map(([k, n]) => `${n}× ${assetById(k)?.name || k}`).join(", ");
  return h("div.card", null, h("h2", null, "Furniture & equipment"),
    h("div.small", { style: { margin: "0 0 6px" } }, scene.furniture.length
      ? `${scene.furniture.length} items in ${new Set(scene.furniture.map((f) => f.room_id)).size} rooms — ${top}` : "No furniture in this version."),
    est && !est.available ? h("p.small.muted", null, est.reason) : null,
    est && est.available ? h("p.small.muted", { style: { margin: "0 0 6px" } },
      `Reads every drawn item on the vector plan in ${est.tiles} high-resolution tiles with ${est.model} ` +
      `(≈ $${est.est_usd?.toFixed(2) ?? "?"}${est.remaining_usd != null ? ` of $${est.remaining_usd.toFixed(2)} left` : ""}). ` +
      "Creates a new version; items you placed by hand are kept.") : null,
    h("div.row", null, h("span.spacer"),
      h("button.btn.primary", { disabled: !ai.configured || dirty || running || !(est && est.available),
        title: !ai.configured ? "AI is not configured" : dirty ? "Save your edits first" : "",
        onclick: () => runFurniture() }, running ? "Detecting…" : "Detect furniture (AI)")),
    status);
}

function drawFurnJob(el, job) {
  const pct = Math.round((job.progress || 0) * 100);
  const r = job.result;
  mount(el, h("div", { style: { marginTop: "8px" } },
    h("div.row", null, badge(job.status), h("span.small", null, job.stage || ""), h("span.spacer"), h("span.small.muted", null, `${pct}%`)),
    h("div.progress", { style: { marginTop: "6px" } }, h("div", { style: { width: `${pct}%` } })),
    job.status === "failed" ? h("div.notice.error", { style: { marginTop: "6px" } }, h("b", null, job.error?.code), ": ", job.error?.message,
      Array.isArray(job.error?.detail) ? h("ul.fix-changes", null, job.error.detail.map((d) => h("li", null, d))) : null) : null,
    r ? h("div.notice.ok", { style: { marginTop: "6px" } },
      h("div", null, `v${r.version}: ${r.items} items in ${r.rooms_with_furniture} rooms from ${r.tiles} tiles` +
        (r.kept_user_items ? `, ${r.kept_user_items} of your own kept` : "") + ` · AI cost $${(r.cost_usd || 0).toFixed(2)}`),
      h("div.small", null, Object.entries(r.by_asset || {}).slice(0, 12).map(([k, n]) => `${n}× ${assetById(k)?.name || k}`).join(", ")),
      r.missing_tiles?.length ? h("div.row", { style: { marginTop: "6px" } },
        h("span.small", null, `${r.missing_tiles.length} tile${r.missing_tiles.length > 1 ? "s" : ""} got no answer (provider error).`),
        h("span.spacer"),
        h("button.btn.small", { onclick: () => runFurniture({ reuse_job_id: job.id, retry_missing: true }) }, "Retry missing tiles")) : null,
      r.notes?.length ? h("details", null, h("summary", null, `${r.notes.length} notes`), h("ul.fix-changes", null, r.notes.map((n) => h("li", null, n)))) : null) : null));
}

async function runFurniture(body = {}) {
  try {
    // a retry runs against the version the first run started from
    const base = body.reuse_job_id && furnJob?.version ? furnJob.version : loadedVersion;
    furnJob = await post(`/api/projects/${state.project.id}/versions/${base}/detect-furniture`, body);
    drawSide();
    const { watchJob } = await import("./app.js");
    const final = await watchJob(furnJob.id, (j) => { furnJob = j; drawSide(); });
    furnJob = final;
    await refreshProject();
    if (final.status === "needs_review") {
      await loadVersion(final.result.version, true);
      toast(`Furniture detected: ${final.result.items} items. Review them, then accept the estimates.`, "ok", 8000);
    } else showError(final.error || { message: "failed" }, "Furniture detection");
  } catch (e) { showError(e, "Furniture detection"); }
}

function fixCard() {
  const ai = state.health.ai;
  const status = h("div");
  if (fixJob && fixJob.project_id === state.project.id) drawFixJob(status, fixJob);
  const ta = h("textarea", { rows: 3, style: { width: "100%" },
    placeholder: "e.g. The north wall is slanted: it runs from (0.0, 8.2) to (10.0, 8.6) · Move unit FCU-3 1.5 m west · " +
      "Diffuser SD12 belongs to FCU-4 · Add a door between the kitchen and the corridor" });
  const selText = sel ? `${sel.type}:${sel.id}` : null;
  const running = fixJob && !["completed", "needs_review", "failed"].includes(fixJob.status);
  return h("div.card", null, h("h2", null, "Ask AI to change something"),
    h("p.small.muted", { style: { margin: "0 0 6px" } }, "Describe what is wrong with the model in plain words. The AI proposes edits and you get a new version to review; nothing is overwritten."),
    ta,
    h("div.row", { style: { marginTop: "6px" } },
      h("span.small.muted", null, selText ? `About: ${selText}` : "Tip: select a room, unit or diffuser first"),
      h("span.spacer"),
      h("button.btn.primary", { disabled: !ai.configured || dirty || running, title: !ai.configured ? "AI is not configured" : dirty ? "Save your edits first" : "",
        onclick: () => (ta.value.trim().length >= 3 ? runFix(ta.value.trim(), selText ? [selText] : []) : toast("Describe the problem first.", "error")) }, "Fix")),
    status);
}

function drawFixJob(el, job) {
  const r = job.result;
  mount(el, h("div", { style: { marginTop: "8px" } },
    h("div.row", null, badge(job.status), h("span.small", null, job.stage || "")),
    job.status === "failed" ? h("div.notice.error", { style: { marginTop: "6px" } }, h("b", null, job.error?.code), ": ", job.error?.message,
      Array.isArray(job.error?.detail) ? h("ul.fix-changes", null, job.error.detail.map((d) => h("li", null, d))) : null) : null,
    r ? h("div.notice." + (r.version ? "ok" : "warn"), { style: { marginTop: "6px" } },
      h("div", null, r.version ? `v${r.version}: ${r.summary}` : r.summary || "No change."),
      r.changes?.length ? h("ul.fix-changes", null, r.changes.map((c) => h("li", null, `${c.op} ${c.collection} ${c.id}${c.reason ? " — " + c.reason : ""}`))) : null,
      r.questions?.length ? h("div", null, h("b", null, "Questions: "), h("ul.fix-changes", null, r.questions.map((q) => h("li", null, q)))) : null,
      h("div.small.muted", null, `AI cost $${(r.cost_usd || 0).toFixed(3)}`)) : null));
}

async function runFix(prompt, selection) {
  try {
    fixJob = await post(`/api/projects/${state.project.id}/versions/${loadedVersion}/fix`, { prompt, selection });
    drawSide();
    const { watchJob } = await import("./app.js");
    const final = await watchJob(fixJob.id, (j) => { fixJob = j; drawSide(); });
    fixJob = final;
    await refreshProject();
    if (final.status === "needs_review" && final.result.version) {
      await loadVersion(final.result.version, true);
      toast(`Fixed: ${final.result.summary}`, "ok", 8000);
    } else if (final.status === "failed") showError(final.error || { message: "failed" }, "Fix with AI");
    else drawSide();
  } catch (e) { showError(e, "Fix with AI"); }
}

async function fixDialog(target) {
  const v = await ask("Fix with AI", [{ name: "prompt", label: `What is wrong with ${target}?`, type: "textarea", value: "" }], "Fix");
  if (v && v.prompt) runFix(v.prompt, [target]);
}


/** Save unsaved edits as a new version without asking (jobs start from a saved version). */
async function ensureSaved(note = "edited in review") {
  if (!dirty) return true;
  try {
    const res = await post(`/api/projects/${state.project.id}/versions`, { scene, parent: loadedVersion, note, source: "edit" });
    dirty = false;
    await refreshProject();
    await loadVersion(res.number, true);
    toast(`Your edits were saved as v${res.number} first.`, "info");
    return true;
  } catch (e) { showError(e, "Saving failed"); return false; }
}

/** "Fix problems": free rules, then the AI problem solver for the rest -> one new version, opened when ready. */
async function runSolve(opts = {}) {
  if (!(await ensureSaved())) return;
  try {
    solveJob = await post(`/api/projects/${state.project.id}/versions/${loadedVersion}/solve`, opts);
    drawSide();
    const { watchJob } = await import("./app.js");
    const final = await watchJob(solveJob.id, (j) => { solveJob = j; drawSide(); });
    solveJob = final;
    await refreshProject();
    if (final.status === "needs_review" && final.result.version) {
      await loadVersion(final.result.version, true);
      const r = final.result;
      const done = [r.rule_fixes.length ? `${r.rule_fixes.length} fixed for free` : null,
        r.ai?.changes?.length ? `${r.ai.solved?.length || 0} solved by the AI` : null].filter(Boolean);
      toast(`v${r.version} saved: ${done.join(", ")}. Look it over; the old version is kept.`, "ok", 9000);
    } else if (final.status === "failed") showError(final.error || { message: "failed" }, "Fix problems");
    else drawSide();
  } catch (e) { showError(e, "Fix problems"); }
}

function problemsCtx() {
  const key = `${state.project.id}:${loadedVersion}`;
  if (!dirty && (!solveEstimate || solveEstimate.key !== key)) {
    solveEstimate = { key, data: null };
    get(`/api/projects/${state.project.id}/versions/${loadedVersion}/solve/estimate`)
      .then((d) => { if (solveEstimate.key === key) { solveEstimate.data = d; drawSide(); } }).catch(() => {});
  }
  const target = (t) => {
    const [type, id] = String(t).split(":");
    if (["room", "wall", "opening", "furniture", "hvac_unit", "hvac_terminal", "duct", "sensor"].includes(type)) select({ type, id });
  };
  return { report, reportError, scene, dirty, version: loadedVersion, select: target, tool: serverTool, edit,
    solve: runSolve, solveJob, estimate: solveEstimate?.key === key ? solveEstimate.data : null, redraw: drawSide };
}

/** A side card that folds to its title; remembers whether the user opened it. */
function fold(card, key, open = false) {
  if (!card) return null;
  const title = card.querySelector("h2");
  const det = h("details.card.fold", { open: folds.has(key) ? folds.get(key) : open,
    ontoggle: (e) => folds.set(key, /** @type {HTMLDetailsElement} */ (e.target).open) },
    h("summary", null, title ? title.textContent : key));
  title?.remove();
  det.append(...card.childNodes);
  return det;
}

/** "+2 sensors · 1 wall" from a version's stored change summary (vs its parent). */
function changeChips(ch) {
  if (!ch || !ch.counts) return null;
  const names = { sensors: "sensor", rooms: "room", walls: "wall", openings: "opening", furniture: "furniture", hvac_units: "unit",
    hvac_terminals: "terminal", ducts: "duct", materials: "material", levels: "level", open_questions: "question" };
  const parts = Object.entries(ch.counts).map(([k, c]) => {
    const n = c.added + c.removed + c.changed;
    const sign = c.added && !c.removed && !c.changed ? "+" : c.removed && !c.added && !c.changed ? "−" : "~";
    return `${sign}${n} ${names[k] || k}${n > 1 && !(names[k] || "").endsWith("ture") ? "s" : ""}`;
  });
  if (!parts.length && !ch.sections?.length) return null;
  return h("span.small" + (ch.geometry_changed ? ".muted" : ".chg-layer"), { title: ch.geometry_changed ? "the building changed" : "only layers changed: the saved model can be updated in place" },
    [...parts, ...(ch.sections || [])].join(" · "));
}

function versionsCard() {
  const vs = state.project.versions;
  return h("div.card", null, h("h2", null, "Versions"),
    h("p.small.muted", { style: { margin: "0 0 6px" } }, "Every save is a new version; older ones never change. Open one to compare or restore it."),
    h("div.version-list", null, vs.map((v) => h("div.version-item" + (v.number === loadedVersion ? ".active" : ""), { onclick: () => loadVersion(v.number) },
      h("b", null, `v${v.number}`), v.parent ? h("span.small.muted", null, `← v${v.parent}`) : null,
      badge(v.source, "info"), v.can_generate ? badge("ready", "ok") : badge("review", "review"), changeChips(v.changes),
      h("span.small.muted", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, v.note)))),
    loadedVersion !== vs[0]?.number && !dirty ? h("div.row", { style: { marginTop: "8px" } },
      h("button.btn.small", { title: "Save this older version again as the newest one (nothing is deleted)", onclick: restoreVersion }, `Restore v${loadedVersion} as a new version`)) : null);
}

async function restoreVersion() {
  const old = loadedVersion;
  if (!(await confirmDialog(`Restore v${old}`, `Save a copy of v${old} as the newest version? Every version stays available.`, "Restore"))) return;
  try {
    const res = await post(`/api/projects/${state.project.id}/versions`, { scene, parent: old, note: `restored from v${old}`, source: "edit" });
    await refreshProject();
    await loadVersion(res.number, true);
    toast(`v${old} restored as v${res.number}.`, "ok");
  } catch (e) { showError(e, "Restore"); }
}

function drawSide() {
  if (!sideEl || !scene) return;
  mount(sideEl, problemsCard(problemsCtx()), inspector(), fold(fixCard(), "fix", !!fixJob),
    fold(furnitureCard(), "furniture"), fold(sensorsCard(), "sensors", (scene.sensors || []).length > 0),
    fold(hvacCard(), "hvac", hvacCount() > 0), fold(versionsCard(), "versions"));
  drawToolbar();
}

function drawToolbar() {
  const ul = underlayPlacement();
  const menuItem = (label, fn, title, disabled = false) => h("button.menu-item", { title, disabled, onclick: (e) => {
    /** @type {HTMLElement} */ (e.target).closest("details")?.removeAttribute("open"); fn(); } }, label);
  mount(toolbarEl,
    h("b", null, `v${loadedVersion}${dirty ? " · unsaved" : ""}`),
    h("button.btn.small", { onclick: doUndo, disabled: !undo.length, title: "Undo (Ctrl+Z)" }, "↶ Undo"),
    h("button.btn.small", { onclick: doRedo, disabled: !redo.length, title: "Redo (Ctrl+Y)" }, "↷ Redo"),
    h("span.tb-sep"),
    h("button.btn.small" + (mode === "draw-room" ? ".primary" : ""), { onclick: () => { mode = mode === "draw-room" ? "select" : "draw-room"; sel = null; drawAll(); },
      title: "Drag a rectangle on the plan (wall centrelines); snaps to 5 cm, hold Alt for 1 mm" }, "Draw room"),
    h("button.btn.small", { onclick: addFurnitureDialog, title: "Add a furniture item to the selected room" }, "+ Furniture"),
    h("button.btn.small" + (mode === "place-sensor" ? ".primary" : ""), { onclick: () => { mode = mode === "place-sensor" ? "select" : "place-sensor"; sel = null; drawAll(); },
      title: "Click on the plan to place sensors (Esc stops)" }, "+ Sensor"),
    h("details.menu", null, h("summary.btn.small", { title: "Less common layout tools" }, "More tools ▾"),
      h("div.menu-pop", null,
        menuItem("Add a room by coordinates…", addRoomDialog, "Add a rectangular room by typing its coordinates"),
        menuItem("Rebuild walls from rooms", () => serverTool("/api/scene/derive-walls", {}, (r) => `Walls rebuilt from rooms (${r.scene.walls.length} walls)${r.notes.length ? "; " + r.notes.join("; ") : ""}`),
          "Replace the walls with new ones along the room outlines"),
        menuItem("Scale to the size in the brief", fitToBrief, "Scale the layout to the brief's outer dimensions"),
        menuItem(ul?.calibrated ? "Re-calibrate the plan image" : "Calibrate the plan image", startCalibration, "Click two points a known distance apart", !ul))),
    h("span.tb-sep"),
    ul ? h("label.small", { title: "Show the uploaded plan under the layout" }, h("input", { type: "checkbox", checked: underlay.visible, onchange: (e) => { underlay.visible = e.target.checked; draw(); } }), " plan") : null,
    ul ? h("input", { type: "range", min: "0.1", max: "1", step: "0.05", value: underlay.opacity, title: "Plan image opacity", style: { width: "70px" }, oninput: (e) => { underlay.opacity = Number(e.target.value); draw(); } }) : null,
    h("button.btn.small", { onclick: () => { fitView(); draw(); }, title: "Show the whole layout" }, "Zoom to fit"),
    h("span.spacer"),
    report?.can_generate && !dirty ? h("button.btn.small", { onclick: () => switchTab("generate") }, "Go to generate →") : null,
    h("button.btn.primary", { onclick: save, disabled: !dirty, title: dirty ? "Save your edits as a new version" : "No unsaved edits" }, "Save version"));
}

async function addRoomDialog() {
  const [cx, cy] = [r3(snap(view.cx)), r3(snap(view.cy))];
  const v = await ask("Add room (rectangle)", [
    { name: "name", label: "Name", value: "Room" },
    { name: "type", label: "Type", value: "bedroom", options: state.catalog.room_types.map((t) => [t, t]) },
    { name: "x", label: "South-west corner x (m)", type: "number", value: cx },
    { name: "y", label: "South-west corner y (m)", type: "number", value: cy },
    { name: "w", label: "Width (centreline, m)", type: "number", value: 3 },
    { name: "d", label: "Depth (centreline, m)", type: "number", value: 3 },
  ], "Add");
  if (!v) return;
  const id = nextId("R", "rooms");
  edit(() => scene.rooms.push({ id, name: v.name, type: v.type, level_id: scene.levels[0].id,
    polygon: [[v.x, v.y], [v.x + v.w, v.y], [v.x + v.w, v.y + v.d], [v.x, v.y + v.d]].map((p) => p.map(r3)),
    floor_offset: 0, ceiling_height: null, finishes: { floor: null, walls: null, ceiling: null },
    certainty: { status: "confirmed", source: "user", confidence: null, note: "" } }));
  select({ type: "room", id });
  toast("Room added. Use 'Derive walls' to create its walls.", "info");
}

async function addFurnitureDialog() {
  if (!scene.rooms.length) return toast("Add a room first.", "error");
  const room0 = sel?.type === "room" ? sel.id : scene.rooms[0].id;
  const v = await ask("Add furniture", [
    { name: "asset", label: "Item", value: "sofa_3seat", options: state.catalog.assets.map((a) => [a.id, `${a.name} (${a.size.join(" × ")} m)`]) },
    { name: "room", label: "Room", value: room0, options: scene.rooms.map((r) => [r.id, r.name]) },
  ], "Add");
  if (!v) return;
  const room = byId("rooms", v.room);
  const c = centroid(room.polygon);
  const id = nextId("F", "furniture");
  const elevation = assetById(v.asset)?.default_elevation ?? 0; // wall cabinets, hoods, countertop appliances
  edit(() => scene.furniture.push({ id, asset_id: v.asset, room_id: v.room, position: [r3(c[0]), r3(c[1])], elevation,
    rotation_deg: 0, size: null, materials: {}, label: null, certainty: { status: "confirmed", source: "user", confidence: null, note: "" } }));
  select({ type: "furniture", id });
}

async function fitToBrief() {
  const b = state.project.brief;
  if (!b.overall_width || !b.overall_depth) return toast("Enter the overall outer dimensions in the brief first.", "error");
  if (!(await confirmDialog("Fit layout to brief", `Scale all coordinates so the outer footprint is ${b.overall_width} × ${b.overall_depth} m? Wall thicknesses and furniture sizes are kept.`, "Scale"))) return;
  serverTool("/api/scene/fit-overall", { width: b.overall_width, depth: b.overall_depth }, () => "Layout scaled to the brief dimensions");
}

async function redesignDialog(room) {
  if (!state.health.ai.configured) return toast("AI is not configured; edit the furniture manually instead.", "error");
  if (dirty) return toast("Save your edits first; the redesign starts from a saved version.", "error");
  const v = await ask(`Redesign ${room.name} with AI`, [{ name: "prompt", label: "What should change in this room only?", type: "textarea", value: "" }], "Redesign");
  if (!v || !v.prompt) return;
  try {
    const job = await post(`/api/projects/${state.project.id}/versions/${loadedVersion}/rooms/${room.id}/redesign`, { prompt: v.prompt });
    toast("Redesign started; a new version will appear when it is ready.", "info");
    const { watchJob } = await import("./app.js");
    const final = await watchJob(job.id);
    await refreshProject();
    if (final.status === "needs_review") { await loadVersion(final.result.version, true); toast(final.result.summary || "Room redesigned", "ok"); }
    else showError(final.error || { message: "failed" }, "Room redesign");
  } catch (e) { showError(e, "Room redesign"); }
}

function drawAll() {
  if (!root) return;
  if (!state.project || !scene) {
    mount(root, h("div.card", null, h("h2", null, "No layout yet"),
      h("p", null, "Create a layout in step 1: interpret the plan with AI, start from the overall dimensions, or use the sample apartment."),
      h("button.btn", { onclick: () => switchTab("brief") }, "← Back to brief")));
    svg = null;
    return;
  }
  if (!svg || !root.contains(svg)) {
    svg = /** @type {any} */ (document.createElementNS(SVGNS, "svg"));
    toolbarEl = h("div.editor-toolbar");
    sideEl = h("div.side");
    mount(root, h("div.review-layout", null,
      h("div", null, toolbarEl, h("div.editor-wrap", null, svg, hintEl = h("div.editor-hint", null, "wheel = zoom · drag = pan · 1 grid = 1 m · origin = red cross"))),
      sideEl));
    attachCanvasEvents();
    requestAnimationFrame(() => { fitView(); draw(); });
  }
  draw();
  drawSide();
}

export function renderReview(el) {
  root = el;
  on("project", async (p) => {
    if (!p) { scene = null; return drawAll(); }
    if (scene && loadedVersion && loadedProject === p.id && p.versions.some((v) => v.number === loadedVersion)) return drawAll();
    if (p.versions.length) await loadVersion(state.version ?? p.versions[0].number, true);
    else { scene = null; loadedProject = p.id; drawAll(); }
  });
  on("version", (n) => loadVersion(n, true));
  on("select", async (target) => {   // e.g. "sensor:S3" from a picked object in the 3D preview
    const [type, id] = String(target).split(":");
    if (state.version != null && state.version !== loadedVersion) await loadVersion(state.version);
    if (scene && state.version === loadedVersion) select({ type, id });
  });
  on("tab", (t) => { if (t === "review") requestAnimationFrame(() => { measure(); draw(); }); });
}

export function currentScene() { return { scene, version: loadedVersion, dirty, report }; }
