// @ts-check
/**
 * Live HVAC: the building as a 3D twin with an airflow illustration
 * (frontend/js/airflow/illustration.js, school-flow.js, thermal.js). Not CFD: one zone per AC unit, a
 * temperature cloud with a cooling jet from the casing's discharge face, tracer loops and faint path lines;
 * walls and furniture do not block the flow, the clouds are only clipped to the floor outline.
 *
 * Two drivers:
 *   - MATLAB day (default when the project has a saved results file): west/east zone temperatures and AC power
 *     of one setpoint scenario over eight hours; timeline slider and Play;
 *   - Live airflow (chip; always on without a results file): the BMS readings (/hvac/live: real data pushed to
 *     /hvac/telemetry, else the simulated BMS) - units that are off are not drawn, running units take their own
 *     air temperature.
 * Scene (x, y, z) -> three.js (x, z, -y).
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { api, get } from "./api.js";
import { switchTab } from "./app.js";
import { state, on } from "./state.js";
import { h, mount, showError, fmtTime, toast } from "./ui.js";
import { cloudColor, createAirflow } from "./airflow/illustration.js";
import { FULL_DAY, liveState, matchFloor, matlabState, zoneValues } from "./airflow/thermal.js";

const POLL_MS = 4000;
const BG = 0x182a30;
const MUTED = 31;                 // render layer the camera never draws (room isolation)
const CUT_HEIGHT = 1.05;          // cutaway walls
const ls = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
let root = null;
let session = null;

const fmtC = (v) => (v == null || Number.isNaN(v) ? "–" : `${v.toFixed(1)}°`);
const fmt1 = (v) => Number(v).toFixed(1);
const rgb = ([r, g, b]) => `rgb(${(r * 255) | 0},${(g * 255) | 0},${(b * 255) | 0})`;
/** Floor heat map colours (clouds off): 18-25 blue->yellow, 25-34 yellow->red . */
function zoneColor(t) {
  const [a, b, lo, hi] = t <= 25 ? ["#248ce5", "#f0cf43", 18, 25] : ["#f0cf43", "#e74c3c", 25, 34];
  return new THREE.Color(a).lerp(new THREE.Color(b), Math.max(0, Math.min(1, (t - lo) / (hi - lo))));
}

class LiveView {
  /** @param {HTMLElement} container @param {(hit:any)=>void} onPick */
  constructor(container, onPick) {
    this.container = container;
    this.onPick = onPick;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.localClippingEnabled = true;
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(BG);
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.1, 400);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xe9f7ff, 0x78806e, 1.6));
    const sun = new THREE.DirectionalLight(0xfff4de, 2.0);
    sun.position.set(-25, 55, 35);
    this.scene.add(sun);
    this.cutPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), CUT_HEIGHT);
    this.cut = true;
    this.pickables = [];
    this.unitMeshes = new Map();
    this.groups = { walls: [], openings: [], floors: [], furniture: [], units: [], ducts: [], terminals: [], sensors: [] };
    this.highlight = null;
    this.raycaster = new THREE.Raycaster();
    this.muted = new Set();
    this.ghostMats = new Map();
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(container);
    let down = null;
    this.renderer.domElement.addEventListener("pointerdown", (e) => (down = [e.clientX, e.clientY]));
    this.renderer.domElement.addEventListener("pointerup", (e) => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 4) this.pick(e);
    });
    this.running = true;
    const loop = () => {
      if (!this.running) return;
      this.controls.update();
      this.airflow?.tick(performance.now());
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    this.resize();
    loop();
  }

  resize() {
    const w = this.container.clientWidth, hh = this.container.clientHeight;
    this.renderer.setSize(w, hh);
    this.camera.aspect = w / Math.max(1, hh);
    this.camera.updateProjectionMatrix();
  }

  /** Object name of a mesh: the topmost ancestor with a twin prefix (glTF meshes with several materials load as a
   * group named after the object whose parts are called <name>_1, <name>_2, …). */
  topName(o) {
    let best = null;
    for (let x = o; x && x !== this.root; x = x.parent) if (/^(ARCH|FURN|HVAC|SENSOR)_/.test(x.name || "")) best = x.name;
    return best || o.name || "";
  }

  /** The generated GLB with its built materials. */
  async loadGlb(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    this.root = gltf.scene;
    const objs = [];
    this.root.traverse((o) => objs.push(o));
    for (const o of objs) {
      const m = /** @type {any} */ (o);
      if (!m.isMesh) continue;
      const top = this.topName(o);
      if (/^ARCH_(Ceiling|Slab)_/.test(top)) { m.visible = false; continue; }
      if (/^ARCH_Floor_/.test(top)) { this.groups.floors.push(m); this.pickables.push(m); }
      else if (/^ARCH_(Door|Window)/.test(top)) this.groups.openings.push(m);
      else if (/^ARCH_/.test(top)) this.groups.walls.push(m);
      else if (/^FURN_/.test(top)) this.groups.furniture.push(m);
      else if (/^HVAC_Unit_/.test(top)) {
        m.material = new THREE.MeshStandardMaterial({ color: 0xffb54a, emissive: 0x4a2a00, emissiveIntensity: 0.5, roughness: 0.6 });
        const id = top.replace(/^HVAC_Unit_/, "");
        if (!this.unitMeshes.has(id)) this.unitMeshes.set(id, []);
        this.unitMeshes.get(id).push(m);
        this.groups.units.push(m);
        this.pickables.push(m);
      } else if (/^HVAC_Duct_/.test(top)) { this.groups.ducts.push(m); this.pickables.push(m); }
      else if (/^HVAC_(Diffuser|Grille)_/.test(top)) { this.groups.terminals.push(m); this.pickables.push(m); }
      else if (/^SENSOR_/.test(top)) { this.groups.sensors.push(m); this.pickables.push(m); }
    }
    for (const m of [...this.groups.walls, ...this.groups.openings]) {
      for (const mat of Array.isArray(m.material) ? m.material : [m.material]) mat.side = THREE.DoubleSide;
    }
    this.scene.add(this.root);
  }

  /** Without a build: extrude walls, furniture and HVAC straight from the scene model. */
  buildFromScene(scene, placement, catalog) {
    this.root = new THREE.Group();
    const lv = scene.levels[0];
    const wallMat = new THREE.MeshStandardMaterial({ color: 0xeeeee7, roughness: 0.85, side: THREE.DoubleSide });
    for (const w of scene.walls) {
      const dx = w.end[0] - w.start[0], dy = w.end[1] - w.start[1], L = Math.hypot(dx, dy);
      const H = w.height ?? lv.wall_height;
      const m = new THREE.Mesh(new THREE.BoxGeometry(L, H, w.thickness), wallMat);
      m.position.set((w.start[0] + w.end[0]) / 2, lv.elevation + H / 2, -(w.start[1] + w.end[1]) / 2);
      m.rotation.y = Math.atan2(dy, dx);
      m.name = `ARCH_Wall_${w.id}`;
      this.groups.walls.push(m);
      this.root.add(m);
    }
    const floorMat = new THREE.MeshStandardMaterial({ color: 0xdce0d5, roughness: 0.9, side: THREE.DoubleSide });
    for (const r of scene.rooms) {
      const shape = new THREE.Shape(r.polygon.map(([x, y]) => new THREE.Vector2(x, y)));
      const m = new THREE.Mesh(new THREE.ShapeGeometry(shape), floorMat);
      m.rotation.x = -Math.PI / 2;
      m.position.y = lv.elevation;
      m.name = `ARCH_Floor_${r.id}`;
      this.groups.floors.push(m);
      this.root.add(m);
      this.pickables.push(m);
    }
    const box = (name, x, y, z0, z1, w, d, rot, mat, group) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, Math.max(0.01, z1 - z0), d), mat);
      m.position.set(x, (z0 + z1) / 2, -y);
      m.rotation.y = (rot * Math.PI) / 180;
      m.name = name;
      this.root.add(m);
      this.groups[group].push(m);
      return m;
    };
    const assets = Object.fromEntries((catalog?.assets || []).map((a) => [a.id, a]));
    const furnMat = new THREE.MeshStandardMaterial({ color: 0xc9bfae, roughness: 0.8 });
    const steelMat = new THREE.MeshStandardMaterial({ color: 0xb9c0c7, roughness: 0.4, metalness: 0.5 });
    for (const f of scene.furniture) {
      const a = assets[f.asset_id];
      if (!a || a.placement === "floor_layer") continue;
      const [w, d, hh] = f.size ? [f.size.width, f.size.depth, f.size.height] : a.size;
      const z0 = lv.elevation + (f.elevation || 0);
      box(`FURN_${f.room_id}_${f.id}_${f.asset_id}`, f.position[0], f.position[1], z0, z0 + hh, w, d, f.rotation_deg,
        a.category === "kitchen_equipment" ? steelMat : furnMat, "furniture");
    }
    for (const u of scene.hvac.units) {
      const z = placement[u.id];
      const m = box(`HVAC_Unit_${u.id}`, u.position[0], u.position[1], z.z0, z.z1, u.size.width, u.size.depth, u.rotation_deg,
        new THREE.MeshStandardMaterial({ color: 0xffb54a, emissive: 0x4a2a00, emissiveIntensity: 0.5 }), "units");
      this.unitMeshes.set(u.id, [m]);
      this.pickables.push(m);
    }
    const dMat = new THREE.MeshStandardMaterial({ color: 0x9fb4c8, roughness: 0.5, metalness: 0.5 });
    for (const d of scene.hvac.ducts) {
      const z = placement[d.id];
      for (let i = 0; i + 1 < d.path.length; i++) {
        const [a, b] = [d.path[i], d.path[i + 1]];
        const m = box(`HVAC_Duct_${d.id}`, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, z.z0, z.z1, Math.hypot(b[0] - a[0], b[1] - a[1]), d.width,
          (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI, dMat, "ducts");
        this.pickables.push(m);
      }
    }
    const tMat = new THREE.MeshStandardMaterial({ color: 0xf4f6f6, roughness: 0.4 });
    for (const t of scene.hvac.terminals) {
      const z = placement[t.id];
      const sup = t.kind.startsWith("supply") || t.kind === "linear_slot";
      const m = box(`HVAC_${sup ? "Diffuser" : "Grille"}_${t.id}`, t.position[0], t.position[1], z.z0, z.z1, t.width, t.depth, t.rotation_deg, tMat, "terminals");
      this.pickables.push(m);
    }
    const kindColor = Object.fromEntries((catalog?.sensor_kinds || []).map((k) => [k.id, k.color]));
    for (const se of scene.sensors || []) {   // the sensor layer: small discs (the build's markers are 12 cm pucks)
      const top = placement[se.id]?.z1 ?? lv.elevation + lv.wall_height;
      const zc = se.mounting === "ceiling" && se.elevation == null ? top - 0.07 : lv.elevation + (se.elevation ?? 1.5);
      const m = box(`SENSOR_${se.id}`, se.position[0], se.position[1], zc - 0.06, zc + 0.06, 0.13, 0.13, 0,
        new THREE.MeshStandardMaterial({ color: kindColor[se.kind] || 0x6f7782, emissive: kindColor[se.kind] || 0x6f7782, emissiveIntensity: 0.4 }), "sensors");
      this.pickables.push(m);
    }
    this.scene.add(this.root);
  }

  /** Cutaway walls (walls, glazing and doors cut at 1.05 m). */
  setCut(on) {
    this.cut = on;
    for (const m of [...this.groups.walls, ...this.groups.openings]) {
      for (const mat of Array.isArray(m.material) ? m.material : [m.material]) {
        mat.clippingPlanes = on ? [this.cutPlane] : null;
        mat.needsUpdate = true;
      }
    }
  }

  setVisible(group, on) { for (const m of this.groups[group]) m.visible = on; }

  /** Volume style: walls and openings at 12 %, furniture at 30 %, dark slab. */
  ghost(on) {
    const swap = (m, opacity, color = null) => {
      if (on) {
        m.userData.orig = m.userData.orig || m.material;
        const list = Array.isArray(m.userData.orig) ? m.userData.orig : [m.userData.orig];
        const g = list.map((mat) => {
          const key = `${mat.uuid}:${opacity}:${color ?? ""}`;
          if (!this.ghostMats.has(key)) {
            const c = mat.clone();
            if (color != null) { c.color = new THREE.Color(color); c.map = null; }
            else { c.transparent = true; c.opacity = opacity; c.depthWrite = false; }
            this.ghostMats.set(key, c);
          }
          return this.ghostMats.get(key);
        });
        m.material = Array.isArray(m.userData.orig) ? g : g[0];
      } else if (m.userData.orig) {
        m.material = m.userData.orig;
      }
    };
    for (const m of [...this.groups.walls, ...this.groups.openings]) swap(m, 0.12);
    for (const m of this.groups.furniture) swap(m, 0.3);
    for (const m of this.groups.floors) swap(m, 1, 0x23353a);
    this.setCut(this.cut);
  }

  /** Floor heat map when the clouds are off: west / east zone colours split at the zone line (18-34 C). */
  setupHeatPlane(model) {
    const shape = new THREE.Shape(model.outline.map(([x, z]) => new THREE.Vector2(x, -z)));
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    this.heatPlane = new THREE.Mesh(g, new THREE.ShaderMaterial({
      uniforms: { split: { value: model.floors[0].splitX }, west: { value: new THREE.Color() }, east: { value: new THREE.Color() } },
      vertexShader: "varying float localX;void main(){localX=position.x;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}",
      fragmentShader: "varying float localX;uniform float split;uniform vec3 west;uniform vec3 east;void main(){gl_FragColor=vec4(localX<split?west:east,0.62);}",
      transparent: true, depthWrite: false,
    }));
    this.heatPlane.position.y = 0.045;
    this.heatPlane.visible = false;
    this.scene.add(this.heatPlane);
  }

  /** Room isolation: clipping planes at the room rectangle + 0.3 m, and everything that has no
   * part in the room moved to a layer the camera never draws. bounds = [x0, z0, x1, z1] or null. */
  isolate(bounds) {
    for (const o of this.muted) o.traverse((c) => c.layers.set(0));
    this.muted.clear();
    if (!bounds) { this.renderer.clippingPlanes = []; return; }
    const pad = 0.3, [x0, z0, x1, z1] = bounds;
    this.renderer.clippingPlanes = [
      new THREE.Plane(new THREE.Vector3(1, 0, 0), -(x0 - pad)), new THREE.Plane(new THREE.Vector3(-1, 0, 0), x1 + pad),
      new THREE.Plane(new THREE.Vector3(0, 0, 1), -(z0 - pad)), new THREE.Plane(new THREE.Vector3(0, 0, -1), z1 + pad)];
    const b = new THREE.Box3();
    for (const group of Object.values(this.groups)) {
      for (const m of group) {
        b.setFromObject(m);
        if (b.isEmpty() || b.max.x < x0 - pad || b.min.x > x1 + pad || b.max.z < z0 - pad || b.min.z > z1 + pad) {
          m.traverse((c) => c.layers.set(MUTED));
          this.muted.add(m);
        }
      }
    }
    if (this.heatPlane) { this.heatPlane.layers.set(MUTED); this.muted.add(this.heatPlane); }
  }

  /** @param {string} id @param {"cooling"|"off"|"alarm"|"warm"} status */
  setUnitStatus(id, status) {
    const col = { cooling: [0xffb54a, 0x4a2a00], off: [0x6b7580, 0x111111], alarm: [0xff5a4a, 0x5a0b0b], warm: [0xffd27a, 0x5a3a0b] }[status];
    for (const m of this.unitMeshes.get(id) || []) { m.material.color.setHex(col[0]); m.material.emissive.setHex(col[1]); }
  }

  /** Dollhouse view (theta 0.48, phi 0.77) of the floor or of a room rectangle. */
  frame(bounds) {
    let cx, cz, span;
    if (bounds) {
      cx = (bounds[0] + bounds[2]) / 2; cz = (bounds[1] + bounds[3]) / 2;
      span = Math.max(12, Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1]) * 2.8);
    } else {
      const box = new THREE.Box3().setFromObject(this.root);
      const c = box.getCenter(new THREE.Vector3()), sz = box.getSize(new THREE.Vector3());
      cx = c.x; cz = c.z; span = Math.max(sz.x, sz.z) * 1.35;
    }
    const theta = 0.48, phi = 0.77;
    this.controls.target.set(cx, 0, cz);
    this.camera.position.set(cx + span * Math.sin(phi) * Math.sin(theta), span * Math.cos(phi), cz + span * Math.sin(phi) * Math.cos(theta));
  }

  focus(x, y, z) {
    const target = new THREE.Vector3(x, z, -y);
    const off = this.camera.position.clone().sub(this.controls.target).setLength(16);
    this.controls.target.copy(target);
    this.camera.position.copy(target.clone().add(off));
  }

  /** Screen-sized pins over the sensor markers (drawn on top: a 12 cm puck is invisible from a whole-floor view). */
  addSensorPins(sensors, colors) {
    const tex = {};
    const b = new THREE.Box3();
    for (const se of sensors) {
      const mesh = this.groups.sensors.find((m) => this.topName(m) === `SENSOR_${se.id}`);
      if (!mesh) continue;
      const color = colors[se.kind] || "#6F7782";
      if (!tex[color]) {
        const c = document.createElement("canvas");
        c.width = c.height = 64;
        const g = /** @type {CanvasRenderingContext2D} */ (c.getContext("2d"));
        g.beginPath(); g.arc(32, 32, 27, 0, Math.PI * 2); g.fillStyle = "#ffffff"; g.fill();
        g.beginPath(); g.arc(32, 32, 20, 0, Math.PI * 2); g.fillStyle = color; g.fill();
        tex[color] = new THREE.CanvasTexture(c);
        tex[color].colorSpace = THREE.SRGBColorSpace;
      }
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex[color], depthTest: false, sizeAttenuation: false, transparent: true }));
      b.setFromObject(mesh).getCenter(sp.position);
      sp.position.y += 0.1;
      sp.scale.set(0.026, 0.026, 1);
      sp.renderOrder = 999;
      sp.name = `SENSOR_${se.id}`;
      this.scene.add(sp);
      this.groups.sensors.push(sp);
      this.pickables.push(sp);
    }
  }

  pick(e) {
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const pins = this.pickables.filter((m) => m.visible && /** @type {any} */ (m).isSprite);
    const hit = this.raycaster.intersectObjects(pins, false)[0] ||
      this.raycaster.intersectObjects(this.pickables.filter((m) => m.visible && !(/** @type {any} */ (m).isSprite)), false)[0];
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    if (!hit) return this.onPick(null);
    const name = this.topName(hit.object);
    if (!/^ARCH_Floor_/.test(name)) {
      let obj = hit.object;
      while (obj.parent && obj.parent !== this.root && this.topName(obj.parent) === name && obj.parent.name) obj = obj.parent;
      this.highlight = new THREE.BoxHelper(obj, 0xffffff);
      this.scene.add(this.highlight);
    }
    const p = hit.point;
    this.onPick({ name, point: { x: p.x, y: -p.z, z: p.y } });
  }

  dispose() {
    this.running = false;
    this.ro.disconnect();
    this.airflow?.dispose();
    for (const m of this.ghostMats.values()) m.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

// ---------------------------------------------------------------------------------------------
// panel
// ---------------------------------------------------------------------------------------------

const prefs = { volume: true, tracers: true, thermal: true, cut: true, furniture: true, ducts: false, setpoint: 25 };

export function renderLive(el) {
  root = el;
  on("project", () => { stop(); if (state.activeTab === "live") draw(); });
  on("tab", (t) => {
    if (t === "live") draw();
    else if (session && session.playing) setPlaying(session, false);
  });
}

function stop() {
  if (!session) return;
  clearInterval(session.timer);
  clearInterval(session.playTimer);
  session.view.dispose();
  session = null;
}

async function draw() {
  if (!root || !state.project) return;
  const pid = state.project.id;
  if (session && session.pid === pid) return;
  stop();
  let versions;
  try { versions = await get(`/api/projects/${pid}/hvac/versions`); } catch (e) { return showError(e, "HVAC versions"); }
  if (!versions.length) {
    return mount(root, h("div.card", null, h("h2", null, "No HVAC layer yet"),
      h("p", null, "Upload the HVAC drawing (vector PDF) in step 1, then use “Import HVAC drawing” in Review layout. ",
        "The import places the fan-coil units, diffusers, grilles and ducts on the floor plan; this tab then draws their airflow."),
      h("button.btn", { onclick: () => switchTab("brief") }, "← Brief & files")));
  }
  const pref = versions.find((v) => v.number === state.version) || versions[0];
  await start(pid, pref.number, versions);
}

async function start(pid, number, versions) {
  stop();
  const viewEl = h("div.live-viewer");
  const pickedEl = h("div.picked.hidden");
  const hud = h("div.live-hud");
  const badgeEl = h("div.live-badge.hidden");
  const legendBox = h("div");
  viewEl.append(pickedEl, hud, legendBox, badgeEl);
  const side = h("div.side.live-side");
  mount(root, h("div.live-layout", null, viewEl, side));

  const s = { pid, version: number, versions, view: null, timer: null, playTimer: null, readings: null, picked: null,
    forceSim: false, model: null, scene: null, unitsById: {}, roomsById: {}, roomBounds: {}, results: null, resultsFloor: 1,
    live: false, minute: 0, playing: false, room: "", thermal: null, lastValues: {}, hidden: null, running: 0,
    side, hud, badgeEl, legendBox, buildNote: "" };
  session = s;
  s.view = new LiveView(viewEl, (hit) => onPick(s, hit, pickedEl));
  mount(hud, h("span.small", null, "Loading the airflow zones…"));
  let ver;
  try {
    [s.model, ver, s.results] = await Promise.all([
      get(`/api/projects/${pid}/versions/${number}/airflow-zones`),
      get(`/api/projects/${pid}/versions/${number}`),
      get(`/api/projects/${pid}/hvac/thermal-results`).catch(() => null)]);
  } catch (e) { return showError(e, "Airflow zones"); }
  if (session !== s) return;
  s.scene = ver.scene;
  s.unitsById = Object.fromEntries(ver.scene.hvac.units.map((u) => [u.id, u]));
  s.roomsById = Object.fromEntries(ver.scene.rooms.map((r) => [r.id, r]));
  s.roomBounds = Object.fromEntries(s.model.rooms.map((r) => [r.id, r.bounds]));
  if (s.results) s.resultsFloor = matchFloor(s.results, s.model);
  // MATLAB day by default with the "Live airflow" chip remembered; without a results file only live
  s.live = !s.results || ls.get("twin-live-flow") === "1";
  const build = state.project.builds.find((b) => b.status === "completed" && b.version === number);
  try {
    if (build) await s.view.loadGlb(`/api/projects/${pid}/builds/${build.id}/files/export/web/scene.glb`);
    else s.view.buildFromScene(ver.scene, placementFromModel(ver.scene, s.model), state.catalog);
  } catch (e) { showError(e, "3D model"); s.view.buildFromScene(ver.scene, placementFromModel(ver.scene, s.model), state.catalog); }
  if (session !== s) return;
  s.buildNote = build ? `build ${build.id.slice(2)}` : "simple extrusion (generate a build for the full model)";
  s.view.addSensorPins(ver.scene.sensors || [], Object.fromEntries((state.catalog.sensor_kinds || []).map((k) => [k.id, k.color])));
  s.view.airflow = createAirflow(s.view.scene, { onGhost: (on) => s.view.ghost(on) });
  s.view.setupHeatPlane(s.model);
  s.view.setCut(prefs.cut);
  s.view.setVisible("furniture", prefs.furniture);
  s.view.setVisible("ducts", prefs.ducts);
  s.view.frame(null);

  await fetchLive(s);
  refresh(s);
  s.timer = setInterval(async () => {
    if (state.activeTab !== "live" || session !== s) return;
    await fetchLive(s);
    if (s.live) refresh(s); else fillSide(s);
  }, POLL_MS);
}

/** Recompute the thermal state from the active driver and hand it to the renderer . */
function refresh(s) {
  let d, values, drops, hidden = null, playing, minute;
  if (s.live) {
    const L = liveState(s.model, s.readings?.units);
    d = L;
    ({ values, drops, hidden } = L);
    playing = true;
    minute = FULL_DAY;
    s.running = L.running;
  } else {
    d = matlabState(s.results, prefs.setpoint, s.minute, s.model, s.resultsFloor);
    ({ values, drops } = zoneValues(s.model, d));
    playing = s.playing;
    minute = s.minute;
  }
  s.thermal = d;
  s.hidden = hidden;
  s.lastValues = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, hidden && hidden.has(k) ? null : v[0]]));
  const room = s.room ? s.roomBounds[s.room] : null;
  s.view.airflow.update({ thermal: prefs.thermal, volume: prefs.volume, tracers: prefs.tracers, playing, minute,
    zones: s.model.zones, values, drops, hidden, room });
  const hp = s.view.heatPlane;
  if (hp) {
    const z = d.zones[0];
    hp.visible = prefs.thermal && !prefs.volume;
    hp.material.uniforms.west.value.copy(zoneColor(z.west));
    hp.material.uniforms.east.value.copy(zoneColor(z.east));
  }
  for (const id of Object.keys(s.unitsById)) s.view.setUnitStatus(id, unitStatus(s, id));
  badge(s);
  legend(s);
  if (sideKey(s) !== s.sideKey) drawSide(s); else fillSide(s);
  mount(s.hud, h("span.small", null, `${s.room ? `${s.roomsById[s.room]?.name} (isolated)` : "Whole floor"} · `,
    `${s.model.zones.length} AC zones · 3D: ${s.buildNote}`));
}

function clock(m) { return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; }

function badge(s) {
  const el = s.badgeEl;
  if (!prefs.thermal) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  let text;
  if (s.live) {
    const r = s.readings;
    const src = r?.source === "live" ? "LIVE · BMS feed" : "SIMULATED BMS";
    text = r ? `${src} ${new Date(r.ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })} · ` +
      `${s.running} of ${s.model.zones.length} running · ` +
      (prefs.volume ? "return-air clouds 12–36 °C" : "floor colours = mean air temperature 18–34 °C") + " · not CFD"
      : "LIVE · waiting for readings…";
  } else {
    text = `MATLAB · ${prefs.setpoint} °C comparison · Level ${s.resultsFloor} · ${clock(s.minute)} elapsed · ` +
      (prefs.volume ? "illustrative cloud 12–36 °C" : "zone averages 18–34 °C");
  }
  el.textContent = text;
}

function legend(s) {
  const key = `${prefs.thermal}|${prefs.volume}`;
  if (s.legendKey === key) return;   // it only changes with the display options
  s.legendKey = key;
  if (!prefs.thermal) { mount(s.legendBox); return; }
  const cloud = prefs.volume;
  const [lo, hi] = cloud ? [12, 36] : [18, 34];
  const stops = Array.from({ length: 13 }, (_, i) => {
    const t = hi - ((hi - lo) * i) / 12;
    return cloud ? rgb(cloudColor(t)) : `#${zoneColor(t).getHexString()}`;
  });
  const ticks = Array.from({ length: 7 }, (_, i) => hi - ((hi - lo) * i) / 6);
  mount(s.legendBox, h("div.live-legend", null,
    h("div.title", null, cloud ? "Air temperature" : "Zone average", h("span.unit", null, "°C")),
    h("div.scale", null,
      h("div.bar", { style: { background: `linear-gradient(180deg, ${stops.join(",")})` } }),
      h("div.ticks", null, ...ticks.map((t) => h("span", null, t.toFixed(0))))),
    h("div.time", null, cloud ? "illustrative, not CFD" : "west / east halves")));
}

function placementFromModel(scene, model) {
  const top = model.ceiling;
  const out = {};
  for (const u of scene.hvac.units) {
    const z0 = u.elevation != null ? u.elevation : u.kind === "fcu_ducted" ? top + 0.08 : u.kind === "split_wall" ? 2.2 : top - u.size.height;
    out[u.id] = { z0, z1: z0 + u.size.height };
  }
  for (const d of scene.hvac.ducts) out[d.id] = { z0: d.elevation ?? top + 0.08, z1: (d.elevation ?? top + 0.08) + d.height };
  for (const t of scene.hvac.terminals) out[t.id] = { z0: top - 0.025, z1: top + 0.005 };
  return out;
}

async function fetchLive(s) {
  try {
    const r = await get(`/api/projects/${s.pid}/hvac/live?version=${s.version}${s.forceSim ? "&simulated=true" : ""}`);
    s.readings = r;
    s.liveError = null;
    return r;
  } catch (e) { s.liveError = e; return null; }
}

function unitStatus(s, id) {
  if (!s.live) return "cooling";
  const r = s.readings?.units?.[id];
  if (!r) return "cooling";
  if (r.on === false) return r.alarm ? "alarm" : "off";
  if (r.alarm) return "warm";
  return "cooling";
}

function setPlaying(s, on) {
  s.playing = on;
  clearInterval(s.playTimer);
  if (on) {
    if (s.minute >= FULL_DAY) s.minute = 0;
    s.playTimer = setInterval(() => {   // one minute of the day every 100 ms
      if (session !== s || state.activeTab !== "live") return;
      s.minute = Math.min(FULL_DAY, s.minute + 1);
      if (s.minute >= FULL_DAY) setPlaying(s, false);
      else refresh(s);
    }, 100);
  }
  refresh(s);
}

function chart(s) {
  const f = s.thermal?.floorSeries;
  if (!f) return null;
  const vals = f.series.flatMap((v) => v.slice(0, 2));
  const low = Math.floor(Math.min(18, ...vals) / 2) * 2, high = Math.ceil(Math.max(34, ...vals) / 2) * 2;
  const x = (m) => 40 + (m / FULL_DAY) * 340, y = (t) => 130 - ((t - low) / (high - low)) * 115;
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v)); return e; };
  const svg = el("svg", { viewBox: "0 0 390 150", class: "live-chart" });
  for (let t = low; t <= high; t += 4) {
    svg.append(el("line", { x1: 40, x2: 380, y1: y(t), y2: y(t), stroke: "#d5ddd2" }));
    const tx = el("text", { x: 2, y: y(t) + 4, "font-size": 10 }); tx.textContent = `${t} °C`; svg.append(tx);
  }
  for (let hr = 0; hr <= 8; hr += 2) { const tx = el("text", { x: x(hr * 60) - 6, y: 146, "font-size": 10 }); tx.textContent = `${hr} h`; svg.append(tx); }
  svg.append(el("line", { x1: 40, x2: 380, y1: y(25.5), y2: y(25.5), stroke: "#89958c", "stroke-dasharray": "4 4" }));
  for (let z = 0; z < 2; z++) {
    svg.append(el("polyline", { points: f.series.map((v, m) => `${x(m)},${y(v[z])}`).join(" "), fill: "none", stroke: "#157b83",
      "stroke-width": 2, ...(z ? { "stroke-dasharray": "5 4" } : {}) }));
  }
  svg.append(el("line", { x1: x(s.minute), x2: x(s.minute), y1: 10, y2: 130, stroke: "#243f3a" }));
  return svg;
}

function check(s, label, key, after) {
  return h("label.small.chk", null, h("input", { type: "checkbox", checked: prefs[key], onchange: (e) => {
    prefs[key] = e.target.checked; after?.(); refresh(s);
  } }), " ", label);
}

/** What decides the panel's structure; values inside it are updated in place (fillSide), so the slider and
 * buttons are not replaced while they are used (the timeline refreshes ten times a second). */
function sideKey(s) {
  const alarms = s.live ? Object.entries(s.readings?.units || {}).filter(([, u]) => u.alarm).map(([k]) => k).join(",") : "";
  return [s.version, s.live, prefs.setpoint, s.room, !!s.results, s.playing, s.forceSim, s.picked, alarms,
    prefs.volume, prefs.tracers, prefs.thermal, prefs.cut, prefs.furniture, prefs.ducts].join("|");
}

function drawSide(s) {
  s.sideKey = sideKey(s);
  const r = s.readings;
  const ui = (s.ui = {});
  const verSel = h("select", { onchange: (e) => start(s.pid, Number(e.target.value), s.versions) },
    s.versions.map((v) => h("option", { value: v.number, selected: v.number === s.version }, `v${v.number} · ${v.units} units · ${v.terminals} terminals`)));
  const roomSel = h("select", { onchange: (e) => isolate(s, e.target.value) },
    h("option", { value: "", selected: !s.room }, "Entire floor"),
    ...s.model.rooms.slice().sort((x, y) => x.name.localeCompare(y.name)).map((x) => h("option", { value: x.id, selected: x.id === s.room }, x.name)));
  ui.sourceBadge = h("span.badge");
  const head = h("div.card", null,
    h("div.row", null, h("h2", { style: { margin: 0 } }, "Airflow"), h("span.spacer"), ui.sourceBadge),
    h("div.kv", { style: { marginTop: "8px" } },
      h("span", null, "Scene version"), verSel,
      h("span", null, "Isolate a room"), roomSel),
    s.room ? h("div.row", null, h("span.spacer"), h("button.btn.small", { onclick: () => isolate(s, "") }, "Show the whole floor")) : null,
    h("label.small.chk", { title: s.results ? "Clouds and tracers follow the AC units as they are now (BMS feed), not the MATLAB day" : "No MATLAB results saved: the BMS feed drives the airflow" },
      h("input", { type: "checkbox", checked: s.live, disabled: !s.results, onchange: (e) => {
        s.live = e.target.checked; ls.set("twin-live-flow", s.live ? "1" : "0"); setPlaying(s, false);
      } }), " Live airflow (BMS feed)"),
    h("div.chks", null, check(s, "Volumetric cloud", "volume"), check(s, "Airflow tracers", "tracers"),
      check(s, "Thermal visualization", "thermal"),
      check(s, "Cutaway walls", "cut", () => s.view.setCut(prefs.cut)),
      check(s, "Equipment & furniture", "furniture", () => s.view.setVisible("furniture", prefs.furniture)),
      check(s, "Ducts", "ducts", () => s.view.setVisible("ducts", prefs.ducts))),
    h("div.row", null,
      h("button.btn.small", { onclick: () => s.view.frame(s.room ? s.roomBounds[s.room] : null) }, "Reset view"),
      h("a.btn.small", { href: `/api/projects/${s.pid}/versions/${s.version}/hvac-overlay`, target: "_blank" }, "Plan overlay")));

  let driver;
  if (s.live) {
    ui.feed = h("div.small", { style: { margin: "0 0 6px" } });
    ui.means = h("div.small.muted", { style: { marginTop: "6px" } });
    driver = h("div.card", null, h("h2", null, "BMS feed"), ui.feed,
      h("label.small", null, h("input", { type: "checkbox", checked: s.forceSim, onchange: async (e) => {
        s.forceSim = e.target.checked; await fetchLive(s); refresh(s);
      } }), " use simulated data even when live data arrives"),
      ui.means,
      h("p.small.muted", null, "Push real readings to ", h("code", null, `POST /api/projects/${s.pid}/hvac/telemetry`), "."));
  } else {
    ui.clock = h("label.small", { style: { display: "block", marginTop: "8px" } });
    ui.slider = h("input", { type: "range", min: "0", max: String(FULL_DAY), step: "1", value: s.minute, style: { width: "100%" },
      oninput: (e) => { s.minute = Number(e.target.value); refresh(s); } });
    ui.metrics = h("div.live-metrics");
    ui.zoneLine = h("div.small");
    ui.chart = h("div");
    driver = h("div.card", null, h("h2", null, "MATLAB day"),
      h("p.small.muted", { style: { margin: "0 0 6px" } },
        `Calculated by MATLAB ${s.results.engine?.version || ""} · ${s.results.engine?.generatedAt || ""}. Six well-mixed zones (west/east halves); this floor = Level ${s.resultsFloor}.`),
      h("div.setpoints", null, ...s.results.scenarios.map((sc) => h("button.btn.small" + (sc.setpoint === prefs.setpoint ? ".primary" : ""),
        { onclick: () => { prefs.setpoint = sc.setpoint; refresh(s); } }, `${sc.setpoint} °C`))),
      ui.clock, ui.slider,
      h("div.row", null, h("button.btn.small" + (s.playing ? "" : ".primary"), { onclick: () => setPlaying(s, !s.playing) }, s.playing ? "Pause timeline" : "Play timeline")),
      ui.metrics, ui.zoneLine, ui.chart,
      h("div.small.muted", null, "Solid: west · dashed: east · dashed line: 25.5 °C"));
  }
  const uploadCard = h("div.card", null, h("h2", null, "MATLAB results"),
    h("p.small.muted", { style: { margin: "0 0 6px" } }, s.results
      ? "Replace the saved results with another MATLAB run."
      : "Upload a MATLAB thermal results JSON to play the eight-hour day; until then the BMS feed drives the airflow."),
    h("input", { type: "file", accept: ".json,application/json", onchange: (e) => uploadResults(s, e.target.files[0]) }));

  const units = Object.values(s.unitsById).sort((x, y) => x.tag.localeCompare(y.tag, undefined, { numeric: true }));
  ui.rows = new Map();
  const unitTable = h("div.card", null, h("h2", null, `Units (${units.length})`),
    h("table.simple.live-units", null,
      h("thead", null, h("tr", null, h("th", null, "Unit"), h("th", null, "SP"), h("th", null, "Supply"), h("th", null, "Room"),
        h("th", { title: "temperature the unit's cloud is drawn with" }, "Cloud"))),
      h("tbody", null, units.map((u) => {
        const cells = { dot: h("span.dot"), sp: h("td"), sup: h("td"), room: h("td"), cloud: h("td") };
        ui.rows.set(u.id, cells);
        return h("tr", { class: s.picked === u.id ? "sel" : "", onclick: () => focusUnit(s, u.id) },
          h("td", null, cells.dot, u.tag), cells.sp, cells.sup, cells.room, cells.cloud);
      }))));
  const alarms = s.live ? units.filter((u) => r?.units?.[u.id]?.alarm).map((u) => h("div.notice.warn", null, h("b", null, u.tag), ": ", r.units[u.id].alarm)) : [];
  const sensors = s.scene.sensors || [];
  ui.sensorCells = new Map();
  const kinds = Object.fromEntries((state.catalog.sensor_kinds || []).map((k) => [k.id, k]));
  const sensorTable = sensors.length ? h("div.card", null, h("h2", null, `Sensors (${sensors.length})`),
    h("table.simple.live-units", null,
      h("thead", null, h("tr", null, h("th", null, "Sensor"), h("th", null, "Reading"), h("th", null, "Room"), h("th", null, "Source"))),
      h("tbody", null, sensors.map((x) => {
        const cells = { value: h("td"), src: h("td.small.muted") };
        ui.sensorCells.set(x.id, cells);
        return h("tr", { class: s.picked === `sensor:${x.id}` ? "sel" : "", title: `${kinds[x.kind]?.label || x.kind}${x.bms_point ? ` · ${x.bms_point}` : ""}`,
          onclick: () => focusSensor(s, x.id) },
          h("td", null, h("span.sensor-dot", { style: { background: kinds[x.kind]?.color, marginRight: "6px" } }), x.tag), cells.value,
          h("td.small", null, s.roomsById[x.room_id || r?.sensors?.[x.id]?.room_id]?.name || "–"), cells.src);
      })))) : null;
  mount(s.side, head, driver, alarms.length ? h("div.card", null, h("h2", null, "Alarms"), ...alarms) : null, sensorTable, unitTable, uploadCard,
    h("p.small.muted", null, "Illustrative airflow: one zone per AC unit; clouds combine the zone ",
      "temperatures with assumed cooling jets from each casing's discharge face; white tracers show illustrative circulation. ",
      "Outlet directions, wall interactions and turbulence are not solved. Not CFD."));
  fillSide(s);
}

/** The values inside the panel, updated in place. */
function fillSide(s) {
  const ui = s.ui, r = s.readings;
  if (!ui) return;
  const src = s.live ? (r?.source === "live" ? ["ok", "LIVE DATA"] : ["warning", "SIMULATED BMS"]) : ["ok", "MATLAB DAY"];
  ui.sourceBadge.className = `badge ${src[0]}`;
  ui.sourceBadge.textContent = src[1];
  if (s.live) {
    ui.feed.textContent = r ? `Readings ${fmtTime(r.ts)} · outdoor ${fmtC(r.site?.outdoor_c)} · occupancy ${Math.round((r.site?.occupancy ?? 0) * 100)}% · ${s.running} of ${s.model.zones.length} units running`
      : s.liveError ? `No readings: ${s.liveError.message}` : "No readings";
    ui.means.textContent = s.thermal ? `Zone means: west ${fmtC(s.thermal.zones[0].west)} · east ${fmtC(s.thermal.zones[0].east)}. Units that are off are not drawn; running units take their own air temperature.` : "";
  } else {
    const f = s.thermal?.floorSeries, row = f?.series[s.minute];
    ui.clock.textContent = `Simulation time · ${clock(s.minute)}`;
    if (document.activeElement !== ui.slider) ui.slider.value = String(s.minute);
    if (row) {
      const energy = row[3] ?? 0, other = row[4] != null ? (row[4] * s.minute) / 60 : 0;
      mount(ui.metrics,
        h("div", null, h("b", null, `${fmt1(row[2])} kW`), h("span", null, "AC power now")),
        h("div", null, h("b", null, `${fmt1(energy)} kWh`), h("span", null, "AC energy so far")),
        h("div", null, h("b", null, `${fmt1(energy + other)} kWh`), h("span", null, "Total electricity so far")),
        h("div", null, h("b", null, `${fmt1(Math.max(row[0], row[1]))} °C`), h("span", null, "Warmest zone now")));
      ui.zoneLine.textContent = `Level ${s.resultsFloor}: west ${fmt1(row[0])} °C · east ${fmt1(row[1])} °C`;
    }
    mount(ui.chart, chart(s));
  }
  for (const [id, c] of ui.rows) {
    const rd = r?.units?.[id] || {};
    const v = s.lastValues[id];
    c.dot.className = `dot ${unitStatus(s, id)}`;
    c.sp.textContent = s.live ? fmtC(rd.setpoint_c) : "–";
    c.sup.textContent = !s.live ? "–" : rd.on === false ? "off" : fmtC(rd.supply_air_c);
    c.room.textContent = s.live ? fmtC(rd.room_temp_c ?? rd.return_air_c) : "–";
    c.cloud.textContent = v == null ? "not drawn" : fmtC(v);
  }
  for (const [id, c] of ui.sensorCells || []) {
    const rd = r?.sensors?.[id];
    c.value.textContent = rd ? sensorValue(rd) : "–";
    c.src.textContent = rd ? (rd.source === "live" ? "live" : "simulated") : "";
  }
}

function sensorValue(rd) {
  const v = rd.value;
  const txt = rd.unit === "°C" ? `${v.toFixed(1)} °C` : rd.unit === "%RH" ? `${v.toFixed(0)} %RH` : rd.unit === "ppm" ? `${v.toFixed(0)} ppm`
    : rd.unit === "people" ? `${Math.round(v)} people` : `${v.toFixed(1)} ${rd.unit}`;
  return txt + (rd.humidity != null ? ` · ${rd.humidity.toFixed(0)} %RH` : "");
}

function focusSensor(s, id) {
  const x = (s.scene.sensors || []).find((y) => y.id === id);
  if (!x) return;
  s.picked = `sensor:${id}`;
  s.view.focus(x.position[0], x.position[1], 2.0);
  showSensor(s, id);
  drawSide(s);
}

function showSensor(s, id) {
  const el = s.view.container.querySelector(".picked");
  const x = (s.scene.sensors || []).find((y) => y.id === id);
  if (!x || !el) return;
  const rd = s.readings?.sensors?.[id];
  const k = (state.catalog.sensor_kinds || []).find((y) => y.id === x.kind);
  const u = x.unit_id ? s.unitsById[x.unit_id] : null;
  el.classList.remove("hidden");
  mount(el, h("b", null, `${k?.label || x.kind} ${x.tag}`),
    h("div", null, rd ? `${sensorValue(rd)} · ${rd.source === "live" ? "live reading" : "simulated"} ${fmtTime(rd.ts)}` : "no reading"),
    h("div", null, `Monitors: ${u ? u.tag : "no unit"} · BMS point: ${x.bms_point || "not set"}`),
    (x.room_id || rd?.room_id) ? h("div.small.muted", null, `Room: ${s.roomsById[x.room_id || rd.room_id]?.name || x.room_id}`) : null);
}

async function uploadResults(s, file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await api(`/api/projects/${s.pid}/hvac/thermal-results`, { method: "PUT", body: form });
    toast(`MATLAB results saved: ${res.setpoints.length} settings, floors ${res.floors.join(", ")}, ${res.minutes} minutes.`, "ok", 6000);
    s.results = await get(`/api/projects/${s.pid}/hvac/thermal-results`);
    s.resultsFloor = matchFloor(s.results, s.model);
    s.live = ls.get("twin-live-flow") === "1";
    refresh(s);
  } catch (e) { showError(e, "MATLAB results"); }
}

function isolate(s, roomId) {
  s.room = roomId || "";
  const b = s.room ? s.roomBounds[s.room] : null;
  s.view.isolate(b);
  s.view.frame(b);
  refresh(s);
}

function focusUnit(s, id) {
  const u = s.unitsById[id];
  if (!u) return;
  s.picked = id;
  s.view.focus(u.position[0], u.position[1], 2.5);
  showUnit(s, id);
  drawSide(s);
}

function showUnit(s, id) {
  const el = s.view.container.querySelector(".picked");
  const u = s.unitsById[id], rd = s.readings?.units?.[id] || {};
  const zone = s.model.zones.find((z) => z.key === id);
  const terms = s.scene.hvac.terminals.filter((t) => t.unit_id === id);
  el.classList.remove("hidden");
  mount(el, h("b", null, `${u.tag} · ${u.kind.replace("_", " ")}`),
    h("div", null, `${u.airflow_lps ?? "?"} L/s (drawing) · ${terms.filter((t) => t.kind.startsWith("supply")).length} diffusers, ${terms.filter((t) => !t.kind.startsWith("supply")).length} grilles`),
    zone ? h("div", null, `${zone.units[0][0] < s.model.floors[0].splitX ? "West" : "East"} zone · jet ${zone.outlet.dir ? "from the discharge face" : "from the casing underside"}`) : null,
    s.live ? h("div", null, `Status: ${rd.on === false ? "OFF" : rd.mode || "cool"}${rd.alarm ? ` — ${rd.alarm}` : ""} · setpoint ${fmtC(rd.setpoint_c)} · supply ${fmtC(rd.supply_air_c)}`) : null,
    h("div", null, `Cloud temperature ${s.lastValues[id] == null ? "– (not drawn)" : fmtC(s.lastValues[id])}`),
    u.room_id ? h("div.small.muted", null, `Room: ${s.roomsById[u.room_id]?.name || u.room_id}`) : null);
}

function onPick(s, hit, el) {
  if (!hit) { el.classList.add("hidden"); s.picked = null; return; }
  const name = hit.name;
  let m;
  if ((m = /^HVAC_Unit_(.+)$/.exec(name))) { s.picked = m[1]; return showUnit(s, m[1]); }
  if ((m = /^SENSOR_(.+)$/.exec(name))) { s.picked = `sensor:${m[1]}`; return showSensor(s, m[1]); }
  el.classList.remove("hidden");
  const terminal = /^HVAC_(Diffuser|Grille)_(.+)$/.exec(name);
  const floor = /^ARCH_Floor_(.+)$/.exec(name);
  const west = hit.point.x < s.model.floors[0].splitX;
  const zt = s.thermal?.zones?.[0];
  if (terminal) {
    const t = s.scene.hvac.terminals.find((x) => x.id === terminal[2]);
    const u = t?.unit_id ? s.unitsById[t.unit_id] : null;
    mount(el, h("b", null, `${terminal[1]} ${terminal[2]}`), h("div", null, `${t?.airflow_lps ?? "?"} L/s · ${t ? `${(t.width * 1000) | 0}×${(t.depth * 1000) | 0} mm` : ""}`),
      h("div", null, `Unit: ${u ? u.tag : "not linked"}`));
  } else if (floor) {
    const room = s.roomsById[floor[1]];
    mount(el, h("b", null, room?.name || floor[1]),
      zt ? h("div", null, `${west ? "West" : "East"} zone: ${fmtC(west ? zt.west : zt.east)}`) : null,
      h("div.small.muted", null, s.live ? "Mean of the units' readings on this side of the floor." : `MATLAB ${prefs.setpoint} °C setting at ${clock(s.minute)}.`));
  } else {
    mount(el, h("b", null, name));
  }
}
