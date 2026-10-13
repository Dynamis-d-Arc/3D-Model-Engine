// @ts-check
/**
 * Web Worker: runs the airflow solver continuously and streams frames to the live view:
 * the 3D field as RGBA bytes (speed, temperature, air mask) for volume rendering, streamlines from the
 * outlets (about once a second), and — when enabled — particles and a horizontal slice; stats every second.
 */
import { AirflowSolver, Particles } from "./solver.js";

/** @type {AirflowSolver|null} */ let s = null;
/** @type {Particles|null} */ let parts = null;
let speed = 10;          // simulated seconds per real second
let sliceK = 3;
let paused = false;
let lastStats = 0;
let lastLines = 0;
let lastReal = 0;
let layers = { volume: true, lines: true, particles: false, slice: false };
let ranges = { vmax: 2.0, tmin: 16, tmax: 32 };
let nLines = 400;
let rnd = 777;
const perf = { stepMs: 0, packMs: 0, linesMs: 0, simPerReal: 0, steps: 0, t0: 0, sim0: 0 };
const rand = () => { rnd = (rnd * 1664525 + 1013904223) >>> 0; return rnd / 4294967296; };

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === "init") {
    s = new AirflowSolver(m.domain);
    if (m.readings) s.setReadings(m.readings, m.site);
    parts = new Particles(s, m.particles || 3000);
    sliceK = m.sliceK ?? sliceK;
    nLines = m.lines || nLines;
    lastReal = performance.now();
    lastLines = 0;
    self.postMessage({ type: "ready", grid: m.domain.grid, dt: s.dt });
  } else if (!s) {
    return;
  } else if (m.type === "readings") {
    s.setReadings(m.readings, m.site);
  } else if (m.type === "config") {
    if (m.speed != null) speed = m.speed;
    if (m.sliceK != null) sliceK = m.sliceK;
    if (m.paused != null) paused = m.paused;
    if (m.layers) layers = { ...layers, ...m.layers };
    if (m.ranges) ranges = { ...ranges, ...m.ranges };
    if (m.lines) nLines = m.lines;
    lastLines = 0;
  } else if (m.type === "reset") {
    s.initTemperature(25);
    s.simTime = 0;
  } else if (m.type === "probe") {
    const g = s;
    const ci = (m.x - g.x0) / g.h - 0.5, cj = (m.y - g.y0) / g.h - 0.5, ck = (m.z - g.z0) / g.dz - 0.5;
    const t = g.sample(g.T, ci, cj, ck);
    const sp = Math.hypot(g.sample(g.u, ci, cj, ck), g.sample(g.v, ci, cj, ck), g.sample(g.w, ci, cj, ck));
    self.postMessage({ type: "probe", id: m.id, T: Number.isNaN(t) ? null : t, speed: Number.isNaN(sp) ? null : sp });
  }
};

function tick() {
  if (s && parts && !paused) {
    const now = performance.now();
    const real = Math.min(0.25, (now - lastReal) / 1000);
    lastReal = now;
    const dt = s.dt;
    let budget = Math.max(dt, speed * real);
    const t0 = performance.now();
    let advanced = 0;
    while (budget > 1e-6 && performance.now() - t0 < 90) {
      const a = performance.now();
      s.step(dt);
      perf.stepMs = 0.9 * perf.stepMs + 0.1 * (performance.now() - a);
      if (layers.particles) parts.advance(dt);
      budget -= dt;
      advanced += dt;
    }
    /** @type {any} */
    const msg = { type: "frame", simTime: s.simTime, advanced, stats: null };
    /** @type {Transferable[]} */
    const transfer = [];
    if (layers.volume || layers.slice) {
      const a = performance.now();
      msg.vol = s.packVolume(new Uint8Array(s.N * 4), ranges.vmax, ranges.tmin, ranges.tmax);
      perf.packMs = 0.9 * perf.packMs + 0.1 * (performance.now() - a);
      transfer.push(msg.vol.buffer);
    }
    if (layers.slice) {
      msg.slice = s.slice(sliceK);
      msg.sliceSpeed = s.speedSlice(sliceK);
      transfer.push(msg.slice.buffer, msg.sliceSpeed.buffer);
    }
    if (layers.particles) {
      msg.pos = parts.world(new Float32Array(parts.n * 3));
      msg.temp = parts.temp.slice();
      msg.pspeed = parts.speed.slice();
      transfer.push(msg.pos.buffer, msg.temp.buffer, msg.pspeed.buffer);
    }
    if (layers.lines && now - lastLines > 900) {
      const a = performance.now();
      msg.lines = s.streamlines(nLines, 260, rand);
      perf.linesMs = performance.now() - a;
      transfer.push(msg.lines.pos.buffer, msg.lines.speed.buffer, msg.lines.arc.buffer, msg.lines.counts.buffer);
      lastLines = now;
    }
    if (now - lastStats > 1000) {
      msg.stats = s.stats();
      if (perf.t0) perf.simPerReal = (s.simTime - perf.sim0) / ((now - perf.t0) / 1000);
      perf.t0 = now; perf.sim0 = s.simTime;
      msg.perf = { ...perf };
      lastStats = now;
    }
    self.postMessage(msg, transfer);
  } else {
    lastReal = performance.now();
  }
  setTimeout(tick, 15);
}
tick();
