// @ts-check
/**
 * Qualitative circulation paths, not a velocity solution, with outlet-aware loops.
 *
 * Viewer frame: x east, y up, z = -plan y (metres); box, polygon, outlets and paths are all in this frame.
 *
 * A zone r = { b: [x0, z0, x1, z1], poly: [[x, z], ...], unitCount, units: [[x, y, z]],
 *              outlet?: { o: [x, y, z], dir: [dx, 0, dz] | null, ceiling } }.
 * sources(r) -> [{ o, dirs }]: one outlet per unit; without a known discharge face the jet leaves along
 * +-x or +-z, away from the nearest edge of the box.
 * paths(r) -> Float32Array[]: 6 closed Catmull-Rom loops per outlet direction, 192 samples each: from the
 * outlet along the ceiling, down to 1.6 m and 0.65 m, back along the floor at 0.35 m and up again; reach
 * min(8.5 m, 0.82 x box span). Every sample is kept inside the box and the floor polygon.
 */

const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
export const SAMPLES = 192;
export const LOOPS_PER_OUTLET = 6;

/** Point in the zone's floor polygon (x, z). */
export function inside(r, x, z) {
  if (!r.poly) return true;
  let yes = false;
  for (let i = 0, j = r.poly.length - 1; i < r.poly.length; j = i++) {
    const a = r.poly[i], b = r.poly[j];
    if ((a[1] > z) !== (b[1] > z) && x < ((b[0] - a[0]) * (z - a[1])) / (b[1] - a[1]) + a[0]) yes = !yes;
  }
  return yes;
}

/** Clamp a point into the zone box (0.12 m inset, y 0.22 m..top) and pull it towards the box centre until it
 * lies inside the floor polygon. */
export function contain(r, p, top = 2.85) {
  const [a, b, c, d] = r.b, cx = (a + c) / 2, cz = (b + d) / 2;
  p = [clamp(p[0], a + 0.12, c - 0.12), clamp(p[1], 0.22, top), clamp(p[2], b + 0.12, d - 0.12)];
  for (let n = 0; n < 25 && !inside(r, p[0], p[2]); n++) { p[0] = p[0] * 0.85 + cx * 0.15; p[2] = p[2] * 0.85 + cz * 0.15; }
  return p;
}

/** Default outlet choice (no known discharge face). */
function baseSources(r, placement = "baseline") {
  const [a, b, c, d] = r.b;
  return Array.from({ length: r.unitCount }, (_, i) => {
    const o = contain(r, placement === "baseline" && r.units[i] ? r.units[i]
      : [a + ((c - a) * (i + 1)) / (r.unitCount + 1), 2.85,
        placement === "balanced" ? (b + d) / 2 : placement === "rear" ? d - 0.35 : i % 2 ? d - 0.35 : b + 0.35]);
    const distances = [o[2] - b, d - o[2], o[0] - a, c - o[0]], side = distances.indexOf(Math.min(...distances));
    const dirs = placement === "balanced" ? [[0, 0, 1], [0, 0, -1]] : [[0, 0, 1], [0, 0, -1], [1, 0, 0], [-1, 0, 0]].filter((_, j) => j === side);
    return { o, dirs };
  });
}

const top = (r) => (r.outlet ? r.outlet.ceiling - 0.05 : 2.85);

/** One outlet per unit: the casing's discharge face when known, else the default rule. The outlet
 * is kept just below the ceiling (ducted casings hang above it in this model). */
export function sources(r, placement = "baseline") {
  const u = r.outlet;
  if (!u || placement !== "baseline") return baseSources(r, placement);
  const o = contain(r, u.o.slice(), top(r));
  if (u.dir) return [{ o, dirs: [u.dir.slice()] }];
  const base = baseSources(r, placement)[0];
  return [{ o, dirs: base ? base.dirs : [[0, 0, 1]] }];
}

function loop(r, nodes, ceiling) {
  const arr = new Float32Array(SAMPLES * 3);
  for (let k = 0; k < SAMPLES; k++) {
    const u = (k / SAMPLES) * nodes.length, i = Math.floor(u), t = u - i;
    const p0 = nodes[(i + nodes.length - 1) % nodes.length], p1 = nodes[i], p2 = nodes[(i + 1) % nodes.length], p3 = nodes[(i + 2) % nodes.length];
    const p = contain(r, [0, 1, 2].map((n) => 0.5 * ((2 * p1[n]) + (-p0[n] + p2[n]) * t + (2 * p0[n] - 5 * p1[n] + 4 * p2[n] - p3[n]) * t * t
      + (-p0[n] + 3 * p1[n] - 3 * p2[n] + p3[n]) * t * t * t)), ceiling);
    arr.set(p, k * 3);
  }
  return arr;
}

/** Closed tracer loops: 6 per outlet direction. With a known discharge face the loop leaves the louvres level
 * for a short way before it falls, as supply air does; otherwise the default loop shape. */
export function paths(r, placement = "baseline") {
  const [a, b, c, d] = r.b, out = [];
  const withOutlet = !!r.outlet && placement === "baseline";
  const ceiling = withOutlet ? top(r) : 2.85;
  for (const s of sources(r, placement)) for (const dir of s.dirs) for (let j = 0; j < LOOPS_PER_OUTLET; j++) {
    const alongX = Math.abs(dir[0]) > 0.5;
    const span = alongX ? c - a : d - b, reach = Math.min(span * 0.82, 8.5);
    const side = (j - 2.5) * Math.min(0.24, (alongX ? d - b : c - a) / 18), dx = dir[0], dz = dir[2], o = s.o;
    const at = (t, y, offset = 0) => contain(r, [o[0] + dx * reach * t + dz * (side + offset), y, o[2] + dz * reach * t - dx * (side + offset)], ceiling);
    const nodes = withOutlet
      ? [o, at(0.12, o[1] - 0.25), at(0.3, Math.min(o[1] - 0.9, 2.4)), at(0.6, 1.5), at(0.88, 0.65), at(0.65, 0.35, side * 0.6), at(0.25, 0.65, side), at(0.06, 1.75, side)]
      : [o, at(0.18, 2.55), at(0.55, 1.6), at(0.88, 0.65), at(0.65, 0.35, side * 0.6), at(0.25, 0.65, side), at(0.06, 1.75, side)];
    out.push(loop(r, nodes, ceiling));
  }
  return out;
}
