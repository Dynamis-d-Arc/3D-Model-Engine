// @ts-check
/**
 * Real-time airflow + temperature solver on the voxel domain from /airflow-domain (twin/hvac/airflow.py).
 *
 * Coarse "stable fluids" scheme on a collocated grid (Stam 1999), good enough to show where conditioned air
 * goes and how temperatures settle — not a design-grade CFD:
 *   1. HVAC boundary conditions from the live/simulated readings: every running unit injects its airflow
 *      over the face cells of its outlets and blows it out of the jet cells around them (along the ceiling
 *      for 4/2/1-way diffusers, down for "down" outlets, across the width of wall/exposed units), and
 *      extracts the same airflow at its return cells, so each unit is mass-balanced; whatever does not
 *      balance inside the simulated region (a single room) leaves through its relief cells (doors, open
 *      edges);
 *   2. buoyancy (warm air rises), semi-Lagrangian advection of velocity, turbulent damping;
 *   3. pressure projection (red-black SOR) so that div(u) equals the HVAC mass sources;
 *   4. temperature: advection, eddy diffusion, internal loads (people, cooking, lighting) scaled by occupancy,
 *      envelope gains from the outdoor temperature, cold rooms held at their set temperature.
 * Index of cell (i, j, k) = i + nx * (j + ny * k); i east, j north, k up (scene coordinates, metres).
 * Pure ES module without DOM access: runs in a Web Worker and in Node (tests).
 */

const BETA_G = 9.81 / 300;    // buoyancy, 1/K * m/s2
const BUOYANCY_SCALE = 0.35;  // coarse cells exaggerate plumes; tempered
const DAMPING = 0.18;         // 1/s, stands in for turbulent dissipation at this resolution
const EDDY_DIFFUSIVITY = 0.03; // m2/s
const SOR_ITERS = 14;          // warm-started from the previous step
const SOR_OMEGA = 1.6;
const MAX_JET = 2.5;          // m/s
const MIN_JET = 0.3;          // m/s: below this a coarse cell would not carry a visible jet
const MAX_BUOY_DT = 12;       // K
const THROW_NUDGE = 0.25;     // per step: how strongly the cells in a jet's throw are pulled to the jet profile
const SPEED_CAP = 1.3 * MAX_JET;
const TINY = 1e-20;           // values below this are flushed to zero: float32 denormals are very slow

/** @param {number} x */
const flush = (x) => (x > TINY || x < -TINY ? x : 0);

/** @param {string} b64 @param {any} Ctor */
export function decode(b64, Ctor) {
  const bin = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Ctor(bytes.buffer);
}

export class AirflowSolver {
  /** @param {any} domain */
  constructor(domain) {
    const g = domain.grid;
    this.domain = domain;
    this.nx = g.nx; this.ny = g.ny; this.nz = g.nz;
    this.h = g.h; this.dz = g.dz; this.x0 = g.x0; this.y0 = g.y0; this.z0 = g.z0;
    const N = this.N = this.nx * this.ny * this.nz;
    this.solid = decode(domain.solid, Uint8Array);
    this.roomIndex = decode(domain.room_index, Int16Array);
    this.owner = decode(domain.owner, Int16Array);
    const loads2d = decode(domain.loads_w, Float32Array);
    const profile = decode(domain.load_profile, Uint8Array);
    this.fixed2d = decode(domain.fixed_c, Float32Array);
    this.V = this.h * this.h * this.dz;
    this.rhoCp = domain.constants.rho_cp;
    for (const k of ["u", "v", "w", "T", "u0", "v0", "w0", "T0", "p", "div", "src", "qInt", "qLight", "ua"]) this[k] = new Float32Array(N);
    this.fluid = new Uint8Array(N);
    for (let c = 0; c < N; c++) this.fluid[c] = this.solid[c] ? 0 : 1;
    this.buildNeighbours();
    // internal loads (W per cell at full occupancy) and lighting into the top fluid layer
    const nxy = this.nx * this.ny;
    const lightW = domain.constants.lighting_w_m2 * this.h * this.h;
    for (let col = 0; col < nxy; col++) {
      const layers = [];
      for (let k = 0; k < this.nz; k++) if (this.fluid[col + nxy * k]) layers.push(k);
      if (!layers.length || !Number.isNaN(this.fixed2d[col])) continue;
      this.qLight[col + nxy * layers[layers.length - 1]] += lightW;
      const W = loads2d[col];
      if (W <= 0) continue;
      const zc = (k) => (k + 0.5) * this.dz;
      let hot = layers.filter((k) => (profile[col] === 1 ? zc(k) >= 0.7 && zc(k) <= 1.6 : zc(k) <= 1.8));
      if (!hot.length) hot = layers;
      const share = profile[col] === 1 ? 0.65 : 1.0;
      for (const k of hot) this.qInt[col + nxy * k] += (W * share) / hot.length;
      if (share < 1) for (const k of layers) this.qInt[col + nxy * k] += (W * (1 - share)) / layers.length;
    }
    for (const [i, j, ua] of domain.envelope) {
      for (let k = 0; k < this.nz; k++) { const c = i + this.nx * (j + this.ny * k); if (this.fluid[c]) this.ua[c] = ua / this.nz; }
    }
    this.relief = (domain.relief || []).map(([i, j, k]) => this.idx(i, j, k)).filter((c) => this.fluid[c]);
    if (!this.relief.length) {  // fallback: the whole top layer can vent (keeps the pressure solve consistent)
      for (let c = nxy * (this.nz - 1); c < N; c++) if (this.fluid[c]) this.relief.push(c);
    }
    this.units = domain.units;
    this.readings = {};
    this.site = { outdoor_c: 32, occupancy: 0.8 };
    this.outlets = [];   // {q, Ts, mix: cell[], jets: [{c, vx, vy, vz}]}
    this.jets = [];      // flat list of jet cells {c, vx, vy, vz, q} (particles, streamlines)
    this.simTime = 0;
    // time step: 0.5 s on the coarse whole-floor grid, ~0.3 s on a fine room grid (a jet moves ~2-3 cells;
    // semi-Lagrangian advection stays stable, buoyancy is clamped)
    this.dt = Math.min(0.5, Math.max(0.25, 2.5 * this.h));
    this.initTemperature(25);
    this.applyReadings();
  }

  idx(i, j, k) { return i + this.nx * (j + this.ny * k); }

  /** @param {number} base */
  initTemperature(base) {
    const nxy = this.nx * this.ny;
    const zoneT = this.units.map((u) => { const r = this.readings[u.id]; return r && r.return_air_c != null ? r.return_air_c : base; });
    for (let c = 0; c < this.N; c++) {
      const col = c % nxy;
      const fx = this.fixed2d[col];
      const o = this.owner[col];
      this.T[c] = !Number.isNaN(fx) ? fx : o >= 0 ? zoneT[o] : base;
      this.u[c] = this.v[c] = this.w[c] = this.p[c] = 0;
    }
  }

  /** @param {Record<string, any>} readings @param {any} site */
  setReadings(readings, site) {
    const first = !Object.keys(this.readings).length;
    this.readings = readings || {};
    if (site) this.site = { ...this.site, ...Object.fromEntries(Object.entries(site).filter(([, v]) => v != null)) };
    this.applyReadings();
    if (first) this.initTemperature(25);
  }

  /** Mass sources, outlets (jets) and supply temperatures from the readings. */
  applyReadings() {
    this.src.fill(0);
    this.outlets = [];
    this.jets = [];
    this.throwCells = [];   // {c, vx, vy, vz}: the jet's path along the ceiling (Coanda) or down, decaying
    this.unitState = [];
    const side = this.h * this.dz;          // cross-section of a cell seen from the side (horizontal jets)
    const top = this.h * this.h;            // ... seen from above (downward jets)
    for (const u of this.units) {
      const r = this.readings[u.id] || {};
      const on = r.on !== false && (r.fan_speed == null || r.fan_speed > 0);
      const Q = on ? (r.airflow_lps != null && r.airflow_lps > 0 ? r.airflow_lps : u.airflow_lps * (r.fan_speed ?? 1)) / 1000 : 0;
      const Ts = r.supply_air_c != null ? r.supply_air_c : 14;
      this.unitState.push({ on, Q, Ts });
      if (Q <= 0) continue;
      const scale = Q / (u.airflow_lps / 1000);
      const outs = u.supplies.length ? u.supplies : u.discharge ? [u.discharge] : [];
      for (const s of outs) {
        const q = (s.q_lps / 1000) * scale;
        const faces = (s.cells || [s.cell]).map(([i, j, k]) => this.idx(i, j, k)).filter((c) => this.fluid[c]);
        if (!faces.length) continue;
        for (const c of faces) this.src[c] += q / (this.V * faces.length);
        let jetDefs = s.jets;
        if (!jetDefs) {  // domain v1: one cell, a direction for wall units, straight down for diffusers
          const d = s.dir || [0, 0, -1];
          jetDefs = [[s.cell[0], s.cell[1], s.cell[2], d[0], d[1], d[2]]];
        }
        const jets = [];
        for (const [i, j, k, dx, dy, dz] of jetDefs) {
          const c = this.idx(i, j, k);
          if (!this.fluid[c]) continue;
          const n = Math.hypot(dx, dy, dz) || 1;
          jets.push({ c, dx: dx / n, dy: dy / n, dz: dz / n, horizontal: Math.abs(dz / n) < 0.7 });
        }
        if (!jets.length) continue;
        // mass-consistent jet speed: the outlet airflow leaves through the jet cells' faces
        const area = jets.reduce((a, j) => a + (j.horizontal ? side : top), 0);
        const vj = Math.min(MAX_JET, Math.max(MIN_JET, (1.3 * q) / area));
        const outlet = { q, Ts, mix: [...new Set([...faces, ...jets.map((j) => j.c)])], jets: [] };
        for (const j of jets) {
          const jet = { c: j.c, vx: vj * j.dx, vy: vj * j.dy, vz: vj * j.dz, q: q / jets.length };
          outlet.jets.push(jet);
          this.jets.push(jet);
          this.addThrow(j, vj);
        }
        this.outlets.push(outlet);
      }
      for (const s of u.returns) {
        const cells = (s.cells || [s.cell]).map(([i, j, k]) => this.idx(i, j, k)).filter((c) => this.fluid[c]);
        for (const c of cells) this.src[c] -= (Q * (s.q_lps / u.airflow_lps)) / (this.V * cells.length);
      }
    }
    // whatever does not balance inside the region leaves (or enters) through the relief cells
    let net = 0;
    for (let c = 0; c < this.N; c++) if (this.fluid[c]) net += this.src[c];
    if (Math.abs(net) > 1e-9 && this.relief.length) {
      const d = net / this.relief.length;
      for (const c of this.relief) this.src[c] -= d;
    }
  }

  /** Cells along a jet's direction for its throw (~2.2 s of travel at the outlet speed, 0.8-3.5 m): the jet
   * clings to the ceiling and slows down linearly to 40 % — coarse cells would otherwise lose it at once. */
  addThrow(j, vj) {
    const { nx, ny, nz } = this;
    const i0 = j.c % nx, j0 = ((j.c / nx) | 0) % ny, k0 = (j.c / (nx * ny)) | 0;
    const horizontal = Math.abs(j.dz) < 0.7;
    const len = Math.min(3.5, Math.max(0.8, 2.2 * vj));
    const step = horizontal ? this.h : this.dz;
    const n = Math.max(1, Math.round(len / step));
    for (let m = 1; m <= n; m++) {
      const i = Math.round(i0 + j.dx * m), jj = Math.round(j0 + j.dy * m), k = horizontal ? k0 : Math.round(k0 + j.dz * m);
      if (i < 0 || jj < 0 || k < 0 || i >= nx || jj >= ny || k >= nz) break;
      const c = i + nx * (jj + ny * k);
      if (!this.fluid[c] || this.src[c] < 0) break;
      const v = vj * (1 - (0.6 * m) / n);
      this.throwCells.push({ c, vx: v * j.dx, vy: v * j.dy, vz: v * (horizontal ? j.dz * 0.5 : j.dz) });
    }
  }

  /** Pull the throw cells towards their jet profile (a momentum source, not a hard boundary condition). */
  nudgeThrow() {
    const a = THROW_NUDGE;
    for (const t of this.throwCells) {
      this.u[t.c] += a * (t.vx - this.u[t.c]);
      this.v[t.c] += a * (t.vy - this.v[t.c]);
      this.w[t.c] += a * (t.vz - this.w[t.c]);
    }
  }

  /** Trilinear sample of field f at continuous cell coordinates; solid corners are ignored. */
  sample(f, x, y, z) {
    const nx = this.nx, ny = this.ny, nz = this.nz;
    x = Math.min(Math.max(x, 0), nx - 1.001); y = Math.min(Math.max(y, 0), ny - 1.001); z = Math.min(Math.max(z, 0), nz - 1.001);
    const i = x | 0, j = y | 0, k = z | 0;
    const fx = x - i, fy = y - j, fz = z - k;
    const k1 = Math.min(k + 1, nz - 1);
    let acc = 0, wsum = 0;
    for (let c = 0; c < 8; c++) {
      const ii = i + (c & 1), jj = j + ((c >> 1) & 1), kk = (c & 4) ? k1 : k;
      const id = ii + nx * (jj + ny * kk);
      if (!this.fluid[id]) continue;
      const wgt = ((c & 1) ? fx : 1 - fx) * (((c >> 1) & 1) ? fy : 1 - fy) * ((c & 4) ? fz : 1 - fz);
      acc += f[id] * wgt; wsum += wgt;
    }
    return wsum > 1e-6 ? acc / wsum : NaN;
  }

  setJets() {
    for (const j of this.jets) { this.u[j.c] = j.vx; this.v[j.c] = j.vy; this.w[j.c] = j.vz; }
  }

  /** One time step of dt seconds (default: the grid's own step). */
  step(dt = this.dt) {
    const { nx, ny, nz, N, fluid } = this;
    const nxy = nx * ny;
    const h = this.h, dz = this.dz;
    this.setJets();
    // buoyancy around the mean fluid temperature
    let tm = 0, nf = 0;
    for (let c = 0; c < N; c++) if (fluid[c]) { tm += this.T[c]; nf++; }
    tm /= Math.max(1, nf);
    const bz = BETA_G * BUOYANCY_SCALE * dt;
    for (let c = 0; c < N; c++) {
      if (!fluid[c]) continue;
      const dT = this.T[c] - tm;   // clamped: a -18 C freezer cell must not drive an unbounded plume
      this.w[c] += bz * (dT > MAX_BUOY_DT ? MAX_BUOY_DT : dT < -MAX_BUOY_DT ? -MAX_BUOY_DT : dT);
    }
    // advect velocity (semi-Lagrangian, in cell units)
    this.u0.set(this.u); this.v0.set(this.v); this.w0.set(this.w);
    const sx = dt / h, sz = dt / dz;
    const damp = 1 / (1 + DAMPING * dt);
    for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const c = i + nx * (j + ny * k);
      if (!fluid[c]) { this.u[c] = this.v[c] = this.w[c] = 0; continue; }
      const x = i - this.u0[c] * sx, y = j - this.v0[c] * sx, z = k - this.w0[c] * sz;
      const a = this.sample(this.u0, x, y, z), b = this.sample(this.v0, x, y, z), d = this.sample(this.w0, x, y, z);
      this.u[c] = Number.isNaN(a) ? 0 : flush(a * damp);
      this.v[c] = Number.isNaN(b) ? 0 : flush(b * damp);
      this.w[c] = Number.isNaN(d) ? 0 : flush(d * damp);
    }
    this.setJets();
    this.nudgeThrow();
    this.project();
    this.capSpeed();
    // temperature
    this.T0.set(this.T);
    for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const c = i + nx * (j + ny * k);
      if (!fluid[c]) continue;
      const t = this.sample(this.T0, i - this.u[c] * sx, j - this.v[c] * sx, k - this.w[c] * sz);
      this.T[c] = Number.isNaN(t) ? this.T0[c] : t;
    }
    // eddy diffusion (explicit, fluid neighbours only), in sub-steps: on fine room grids one explicit step
    // would be unstable (4 ax + 2 az must stay below 1)
    const kx = (EDDY_DIFFUSIVITY * dt) / (h * h), kz = (EDDY_DIFFUSIVITY * dt) / (dz * dz);
    const sub = Math.min(12, Math.max(1, Math.ceil((4 * kx + 2 * kz) / 0.8)));
    const ax = kx / sub, az = kz / sub;
    const T0 = this.T0, fc = this.fluidCells, { nE, nW, nN, nS, nU, nD } = this;
    for (let it = 0; it < sub; it++) {
      T0.set(this.T);
      for (let n = 0; n < fc.length; n++) {
        const c = fc[n];
        const t = T0[c];
        this.T[c] = t + ax * (T0[nE[c]] + T0[nW[c]] + T0[nN[c]] + T0[nS[c]] - 4 * t) + az * (T0[nU[c]] + T0[nD[c]] - 2 * t);
      }
    }
    // heat sources, envelope, supply air, cold rooms
    const occ = this.site.occupancy ?? 0.8, tout = this.site.outdoor_c ?? 32;
    const kq = dt / (this.rhoCp * this.V);
    for (let c = 0; c < N; c++) {
      if (!fluid[c]) continue;
      let q = this.qInt[c] * (0.35 + 0.65 * occ) + this.qLight[c];
      if (this.ua[c] > 0) q += this.ua[c] * (tout - this.T[c]);
      this.T[c] += q * kq;
    }
    // supply air: q*dt of air at Ts replaces room air in the outlet's face and jet cells (energy-consistent)
    for (const o of this.outlets) {
      const f = Math.min(1, (o.q * dt) / (this.V * o.mix.length));
      for (const c of o.mix) this.T[c] += (o.Ts - this.T[c]) * f;
    }
    for (let c = 0; c < N; c++) {
      const fx = this.fixed2d[c % nxy];
      if (fluid[c] && !Number.isNaN(fx)) this.T[c] = fx;
    }
    this.simTime += dt;
  }

  /** Neighbour index tables (a solid neighbour points back at the cell: zero pressure gradient) and the
   * fluid cells split by red/black parity for the SOR sweeps. */
  buildNeighbours() {
    const { nx, ny, nz, N, fluid } = this;
    const nxy = nx * ny;
    for (const k of ["nE", "nW", "nN", "nS", "nU", "nD"]) this[k] = new Int32Array(N);
    const red = [], black = [];
    for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const c = i + nx * (j + ny * k);
      this.nE[c] = i < nx - 1 && fluid[c + 1] ? c + 1 : c;
      this.nW[c] = i > 0 && fluid[c - 1] ? c - 1 : c;
      this.nN[c] = j < ny - 1 && fluid[c + nx] ? c + nx : c;
      this.nS[c] = j > 0 && fluid[c - nx] ? c - nx : c;
      this.nU[c] = k < nz - 1 && fluid[c + nxy] ? c + nxy : c;
      this.nD[c] = k > 0 && fluid[c - nxy] ? c - nxy : c;
      if (fluid[c]) ((i + j + k) & 1 ? black : red).push(c);
    }
    this.red = Int32Array.from(red);
    this.black = Int32Array.from(black);
    this.fluidCells = Int32Array.from([...red, ...black].sort((a, b) => a - b));
  }

  /** Make div(u) equal the HVAC mass sources: solve lap(p) = div(u) - src (red-black SOR, warm-started from
   * the previous step's pressure), then u -= grad(p). */
  project() {
    const { u, v, w, p, div, nE, nW, nN, nS, nU, nD } = this;
    const h = this.h, dz = this.dz;
    this.computeDivergence();
    const ih2 = 1 / (h * h), iz2 = 1 / (dz * dz);
    const idiag = 1 / (4 * ih2 + 2 * iz2);
    const om = SOR_OMEGA;
    for (let it = 0; it < SOR_ITERS; it++) {
      for (const cells of [this.red, this.black]) {
        for (let n = 0; n < cells.length; n++) {
          const c = cells[n];
          const pc = p[c];
          const gs = ((p[nE[c]] + p[nW[c]] + p[nN[c]] + p[nS[c]]) * ih2 + (p[nU[c]] + p[nD[c]]) * iz2 - div[c]) * idiag;
          p[c] = flush(pc + om * (gs - pc));
        }
      }
    }
    const fc = this.fluidCells, i2h = 1 / (2 * h), i2z = 1 / (2 * dz);
    for (let n = 0; n < fc.length; n++) {
      const c = fc[n];
      u[c] = flush(u[c] - (p[nE[c]] - p[nW[c]]) * i2h);
      v[c] = flush(v[c] - (p[nN[c]] - p[nS[c]]) * i2h);
      w[c] = flush(w[c] - (p[nU[c]] - p[nD[c]]) * i2z);
    }
  }

  /** Safety net: no cell moves faster than the fastest outlet allows (keeps a coarse explicit scheme sane). */
  capSpeed() {
    const { u, v, w } = this, fc = this.fluidCells, cap2 = SPEED_CAP * SPEED_CAP;
    for (let n = 0; n < fc.length; n++) {
      const c = fc[n];
      const s2 = u[c] * u[c] + v[c] * v[c] + w[c] * w[c];
      if (s2 > cap2) { const f = SPEED_CAP / Math.sqrt(s2); u[c] *= f; v[c] *= f; w[c] *= f; }
    }
  }

  /** div(u) - src per fluid cell (walls reflect the normal velocity). */
  computeDivergence() {
    const { u, v, w, div, nE, nW, nN, nS, nU, nD } = this;
    const i2h = 1 / (2 * this.h), i2z = 1 / (2 * this.dz);
    const fc = this.fluidCells;
    for (let n = 0; n < fc.length; n++) {
      const c = fc[n];
      const ue = nE[c] !== c ? u[nE[c]] : -u[c], uw = nW[c] !== c ? u[nW[c]] : -u[c];
      const vn = nN[c] !== c ? v[nN[c]] : -v[c], vs = nS[c] !== c ? v[nS[c]] : -v[c];
      const wu = nU[c] !== c ? w[nU[c]] : -w[c], wd = nD[c] !== c ? w[nD[c]] : -w[c];
      div[c] = flush((ue - uw) * i2h + (vn - vs) * i2h + (wu - wd) * i2z - this.src[c]);
    }
  }

  /** Residual divergence (minus sources) over fluid cells, 1/s (diagnostics and tests). */
  divergenceRms() {
    this.computeDivergence();
    let s = 0, n = 0;
    for (let c = 0; c < this.N; c++) if (this.fluid[c]) { s += this.div[c] * this.div[c]; n++; }
    return Math.sqrt(s / Math.max(1, n));
  }

  /** Temperature of layer k as an nx*ny array (NaN where solid). */
  slice(k) {
    const nxy = this.nx * this.ny;
    const out = new Float32Array(nxy);
    const off = nxy * Math.max(0, Math.min(this.nz - 1, k));
    for (let c = 0; c < nxy; c++) out[c] = this.fluid[off + c] ? this.T[off + c] : NaN;
    return out;
  }

  /** Air speed of layer k as an nx*ny array, m/s (NaN where solid). */
  speedSlice(k) {
    const nxy = this.nx * this.ny;
    const out = new Float32Array(nxy);
    const off = nxy * Math.max(0, Math.min(this.nz - 1, k));
    for (let c = 0; c < nxy; c++) {
      const id = off + c;
      out[c] = this.fluid[id] ? Math.hypot(this.u[id], this.v[id], this.w[id]) : NaN;
    }
    return out;
  }

  /**
   * The whole field as RGBA bytes for a 3D texture (cell order = texel order):
   * R = speed / vmax, G = (T - tmin) / (tmax - tmin), B = 255 for air, 0 for solids.
   * @param {Uint8Array} out @param {number} vmax @param {number} tmin @param {number} tmax
   */
  packVolume(out, vmax, tmin, tmax) {
    const { N, fluid, u, v, w, T } = this;
    const ks = 255 / vmax, kt = 255 / (tmax - tmin);
    for (let c = 0, o = 0; c < N; c++, o += 4) {
      if (!fluid[c]) { out[o] = 0; out[o + 1] = 0; out[o + 2] = 0; out[o + 3] = 255; continue; }
      const sp = Math.sqrt(u[c] * u[c] + v[c] * v[c] + w[c] * w[c]) * ks;
      const t = (T[c] - tmin) * kt;
      out[o] = sp > 255 ? 255 : sp;
      out[o + 1] = t < 0 ? 0 : t > 255 ? 255 : t;
      out[o + 2] = 255;
      out[o + 3] = 255;
    }
    return out;
  }

  /**
   * Streamlines of the current velocity field, seeded at the outlet jets (RK2, half-cell steps).
   * Returns world-space (three.js: x, z, -y) points with speed and arc length, plus the point count per line.
   * @param {number} maxLines @param {number} maxSteps @param {() => number} rand
   */
  streamlines(maxLines = 400, maxSteps = 220, rand = Math.random) {
    const { nx, ny, nz, h, dz } = this;
    const jets = this.jets;
    const seeds = [];
    if (jets.length) {
      const per = Math.max(1, Math.floor(maxLines / jets.length));
      const pick = jets.length > maxLines ? jets.filter(() => rand() < maxLines / jets.length) : jets;
      for (const j of pick) {
        const i = j.c % nx, jj = ((j.c / nx) | 0) % ny, k = (j.c / (nx * ny)) | 0;
        for (let s = 0; s < (jets.length > maxLines ? 1 : per); s++) {
          seeds.push([i + (rand() - 0.5) * 0.6, jj + (rand() - 0.5) * 0.6, k - rand() * 0.15]);   // inside the jet layer
        }
      }
    }
    const pos = [], spd = [], arc = [], counts = [];
    const vel = (x, y, z) => [this.sample(this.u, x, y, z), this.sample(this.v, x, y, z), this.sample(this.w, x, y, z)];
    for (const [sx, sy, sz] of seeds) {
      let x = sx, y = sy, z = sz, len = 0, n = 0;
      for (let st = 0; st < maxSteps; st++) {
        const a = vel(x, y, z);
        if (Number.isNaN(a[0])) break;
        const sp = Math.hypot(a[0], a[1], a[2]);
        pos.push(this.x0 + (x + 0.5) * h, this.z0 + (z + 0.5) * dz, -(this.y0 + (y + 0.5) * h));
        spd.push(sp); arc.push(len); n++;
        if (sp < 0.004) break;
        // half a cell per step, whichever axis moves fastest
        const rate = Math.max(Math.abs(a[0]) / h, Math.abs(a[1]) / h, Math.abs(a[2]) / dz, 1e-6);
        const t = 0.5 / rate;
        const mx = x + (a[0] / h) * t * 0.5, my = y + (a[1] / h) * t * 0.5, mz = z + (a[2] / dz) * t * 0.5;
        const b = vel(mx, my, mz);
        if (Number.isNaN(b[0])) break;
        const nx2 = x + (b[0] / h) * t, ny2 = y + (b[1] / h) * t, nz2 = z + (b[2] / dz) * t;
        if (nz2 < -0.5 || nz2 > nz - 0.5 || nx2 < 0 || ny2 < 0 || nx2 > nx - 1 || ny2 > ny - 1) break;
        const c = Math.round(nx2) + nx * (Math.round(ny2) + ny * Math.round(Math.min(nz - 1, Math.max(0, nz2))));
        if (!this.fluid[c]) break;
        len += Math.hypot((nx2 - x) * h, (ny2 - y) * h, (nz2 - z) * dz);
        x = nx2; y = ny2; z = nz2;
        if (this.src[c] < 0 && st > 4) break;   // reached a return grille
      }
      if (n >= 3) counts.push(n);
      else { pos.length -= n * 3; spd.length -= n; arc.length -= n; }
    }
    return { pos: new Float32Array(pos), speed: new Float32Array(spd), arc: new Float32Array(arc), counts: new Uint32Array(counts) };
  }

  /** Per-unit simulated return/zone temperatures and per-room occupied-zone statistics. */
  stats() {
    const { nx, ny, nz, T, fluid } = this;
    const nxy = nx * ny;
    const kTop = Math.max(1, Math.round(1.8 / this.dz));
    const units = this.units.map((u, ui) => {
      let rs = 0, rn = 0;
      for (const r of u.returns) { const c = this.idx(r.cell[0], r.cell[1], r.cell[2]); if (fluid[c]) { rs += T[c]; rn++; } }
      return { id: u.id, return_c: rn ? rs / rn : null, zone_sum: 0, zone_n: 0, ...this.unitState[ui] };
    });
    const rooms = this.domain.rooms.map((r) => ({ id: r.id, sum: 0, n: 0, min: Infinity, max: -Infinity, vsum: 0 }));
    let vmax = 0;
    for (let col = 0; col < nxy; col++) {
      const ri = this.roomIndex[col], o = this.owner[col];
      for (let k = 0; k < Math.min(kTop, nz); k++) {
        const c = col + nxy * k;
        if (!fluid[c]) continue;
        const t = T[c];
        const sp = Math.hypot(this.u[c], this.v[c], this.w[c]);
        if (sp > vmax) vmax = sp;
        if (ri >= 0) { const r = rooms[ri]; r.sum += t; r.n++; r.vsum += sp; if (t < r.min) r.min = t; if (t > r.max) r.max = t; }
        if (o >= 0) { units[o].zone_sum += t; units[o].zone_n++; }
      }
    }
    let maxSpeed = 0;
    const fc = this.fluidCells;
    for (let n = 0; n < fc.length; n++) {
      const c = fc[n];
      const sp = this.u[c] * this.u[c] + this.v[c] * this.v[c] + this.w[c] * this.w[c];
      if (sp > maxSpeed) maxSpeed = sp;
    }
    return {
      simTime: this.simTime,
      maxSpeed: Math.sqrt(maxSpeed),
      occupiedMaxSpeed: vmax,
      units: units.map((u) => ({ id: u.id, on: u.on, q: u.Q, supply_c: u.Ts, return_c: u.return_c,
        zone_c: u.zone_n ? u.zone_sum / u.zone_n : null })),
      rooms: rooms.map((r) => ({ id: r.id, avg: r.n ? r.sum / r.n : null, min: r.n ? r.min : null, max: r.n ? r.max : null,
        speed: r.n ? r.vsum / r.n : null })),
    };
  }
}

/** Particles that follow the simulated air: emitted at supply outlets, recycled at returns and with age. */
export class Particles {
  /** @param {AirflowSolver} s @param {number} n */
  constructor(s, n) {
    this.s = s;
    this.n = n;
    this.pos = new Float32Array(n * 3);  // cell coordinates
    this.age = new Float32Array(n);
    this.life = new Float32Array(n);
    this.temp = new Float32Array(n);
    this.speed = new Float32Array(n);
    this.seed = 12345;
    this.fluidCells = [];
    for (let c = 0; c < s.N; c++) if (s.fluid[c]) this.fluidCells.push(c);
    for (let p = 0; p < n; p++) { this.respawn(p); this.age[p] = this.rand() * this.life[p]; }
  }

  rand() { this.seed = (this.seed * 1664525 + 1013904223) >>> 0; return this.seed / 4294967296; }

  respawn(p) {
    const s = this.s;
    const jets = s.jets;
    let c;
    if (jets.length && this.rand() < 0.75) {
      // weighted by outlet airflow
      let tot = 0;
      for (const j of jets) tot += j.q;
      let r = this.rand() * tot;
      let jet = jets[0];
      for (const j of jets) { r -= j.q; if (r <= 0) { jet = j; break; } }
      c = jet.c;
    } else c = this.fluidCells[(this.rand() * this.fluidCells.length) | 0];
    const nxy = s.nx * s.ny;
    const i = c % s.nx, j = ((c / s.nx) | 0) % s.ny, k = (c / nxy) | 0;
    this.pos[p * 3] = i + this.rand() - 0.5;
    this.pos[p * 3 + 1] = j + this.rand() - 0.5;
    this.pos[p * 3 + 2] = k + this.rand() * 0.4 - 0.2;
    this.age[p] = 0;
    this.life[p] = 6 + this.rand() * 14;
  }

  /** @param {number} dt seconds of simulated time */
  advance(dt) {
    const s = this.s;
    const sx = dt / s.h, sz = dt / s.dz;
    for (let p = 0; p < this.n; p++) {
      let x = this.pos[p * 3], y = this.pos[p * 3 + 1], z = this.pos[p * 3 + 2];
      const u = s.sample(s.u, x, y, z), v = s.sample(s.v, x, y, z), w = s.sample(s.w, x, y, z);
      this.age[p] += dt;
      if (Number.isNaN(u) || this.age[p] > this.life[p]) { this.respawn(p); continue; }
      x += u * sx; y += v * sx; z += w * sz;
      const c = Math.round(x) + s.nx * (Math.round(y) + s.ny * Math.round(z));
      if (z < 0 || z > s.nz - 1 || c < 0 || c >= s.N || !s.fluid[c] || (s.src[c] < 0 && this.rand() < 0.3)) { this.respawn(p); continue; }
      this.pos[p * 3] = x; this.pos[p * 3 + 1] = y; this.pos[p * 3 + 2] = z;
      this.temp[p] = s.sample(s.T, x, y, z);
      this.speed[p] = Math.hypot(u, v, w);
    }
  }

  /** Positions in three.js world coordinates (scene (x, y, z) -> (x, z, -y)). */
  world(out) {
    const s = this.s;
    for (let p = 0; p < this.n; p++) {
      const x = s.x0 + (this.pos[p * 3] + 0.5) * s.h;
      const y = s.y0 + (this.pos[p * 3 + 1] + 0.5) * s.h;
      const z = s.z0 + (this.pos[p * 3 + 2] + 0.5) * s.dz;
      out[p * 3] = x; out[p * 3 + 1] = z; out[p * 3 + 2] = -y;
    }
    return out;
  }
}
