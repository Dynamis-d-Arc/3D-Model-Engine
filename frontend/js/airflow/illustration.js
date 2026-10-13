// @ts-check
/**
 * Animated airflow illustration driven by zone temperatures. Not CFD.
 * Jets leave the casing's discharge face and the cloud boxes reach the ceiling; when a room is isolated its
 * own clouds are pulled in to its rectangle and the other zones are muted.
 *
 * One zone per AC unit (GET /airflow-zones). Each zone has
 *   - a cloud: a box raymarched in 16 steps; air temperature = mix(front, rear) + 0.13 (y - 1.5) - drop * jet,
 *     jet = a Gaussian beam from the outlet falling 2.1 m over its reach; 12-36 C colour ramp with a ripple
 *     animated by `phase`; clipped to the floor polygon;
 *   - tracer dots: 6 per path, moving only while `playing`;
 *   - faint path lines.
 * Walls and furniture do not block anything.
 */
import * as THREE from "three";
import { LOOPS_PER_OUTLET, SAMPLES, paths as flowPaths, sources as flowSources } from "./school-flow.js";

const DOTS_PER_PATH = 6;
const ROOM_PAD = 0.3, ROOM_GRACE = 0.6;   // room focus: walls on the line / outlet off the unit centre

const CLOUD_VS = "varying vec3 wp;void main(){vec4 p=modelMatrix*vec4(position,1.);wp=p.xyz;gl_Position=projectionMatrix*viewMatrix*p;}";
const CLOUD_FS = /* glsl */`precision highp float;varying vec3 wp;
uniform vec3 lo,hi,outlets[6],directions[6];uniform int count,polyCount;uniform vec2 poly[64];
uniform float front,rear,drop,phase,elapsed,ceiling;
bool inside(vec2 p){if(polyCount<3)return true;bool ok=false;int j=polyCount-1;for(int i=0;i<64;i++){if(i>=polyCount)break;vec2 a=poly[i],b=poly[j];if(((a.y>p.y)!=(b.y>p.y))&&(p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x))ok=!ok;j=i;}return ok;}
vec3 heat(float t){float u=clamp((t-12.)/24.,0.,1.);if(u<.2)return mix(vec3(.28,.16,.55),vec3(.04,.65,.95),u/.2);if(u<.42)return mix(vec3(.04,.65,.95),vec3(.13,.94,.64),(u-.2)/.22);if(u<.64)return mix(vec3(.13,.94,.64),vec3(.85,.93,.28),(u-.42)/.22);if(u<.82)return mix(vec3(.85,.93,.28),vec3(1.,.56,.23),(u-.64)/.18);return mix(vec3(1.,.56,.23),vec3(.94,.2,.19),(u-.82)/.18);}
float beam(vec3 p,vec3 o,vec3 dir){vec3 v=p-o;float d=dot(v,dir);if(ceiling>.5)d=abs(d);float len=min(8.5,(abs(dir.x)>.5?hi.x-lo.x:hi.z-lo.z)*.82);float h=o.y-2.1*smoothstep(0.,len,d);float lateral=dot(v,vec3(dir.z,0.,-dir.x));float w=.20+d*.20;float jet=exp(-(lateral*lateral+pow(p.y-h,2.))/(w*w));return jet*smoothstep(-.1,.25,d)*(1.-smoothstep(len*.8,len*1.25,d));}
void main(){vec3 ro=cameraPosition;vec3 rd=normalize(wp-cameraPosition)+vec3(.000001);vec3 a=(lo-ro)/rd,b=(hi-ro)/rd,n=min(a,b),f=max(a,b);
float t0=max(0.,max(n.x,max(n.y,n.z))),t1=min(f.x,min(f.y,f.z));if(t1<=t0)discard;float ds=(t1-t0)/16.;vec4 sum=vec4(0.);
for(int j=0;j<16;j++){vec3 p=ro+rd*(t0+(float(j)+.5)*ds);if(!inside(p.xz))continue;float jet=0.;for(int i=0;i<6;i++){if(i>=count)break;jet=max(jet,beam(p,outlets[i],directions[i]));}
jet*=smoothstep(0.,.35,elapsed);float temp=mix(front,rear,clamp((p.z-lo.z)/(hi.z-lo.z),0.,1.))+.13*(p.y-lo.y-1.5)-drop*jet;
float ripple=.83+.17*sin(p.x*2.1+p.z*1.6+p.y*3.-phase*2.2);float density=(.035+.38*jet)*ripple;float alpha=1.-exp(-density*ds);
sum.rgb+=(1.-sum.a)*heat(temp)*alpha;sum.a+=(1.-sum.a)*alpha;}if(sum.a<.001)discard;gl_FragColor=vec4(sum.rgb/sum.a,min(.8,sum.a));}`;

/** The study's cloud colour ramp (12-36 C), for legends: t in C -> [r, g, b] 0..1. */
export function cloudColor(t) {
  const u = Math.min(1, Math.max(0, (t - 12) / 24));
  const stops = [[0, [0.28, 0.16, 0.55]], [0.2, [0.04, 0.65, 0.95]], [0.42, [0.13, 0.94, 0.64]], [0.64, [0.85, 0.93, 0.28]], [0.82, [1, 0.56, 0.23]], [1, [0.94, 0.2, 0.19]]];
  for (let i = 1; i < stops.length; i++) {
    if (u <= stops[i][0]) {
      const [a0, ca] = stops[i - 1], [a1, cb] = stops[i], f = (u - a0) / (a1 - a0);
      return ca.map((v, k) => v + (cb[k] - v) * f);
    }
  }
  return stops[stops.length - 1][1];
}

/**
 * @param {THREE.Scene} scene
 * @param {{ onGhost?: (on: boolean) => void, elevation?: number }} [opts]
 */
export function createAirflow(scene, opts = {}) {
  const root = new THREE.Group();
  root.name = "AIRFLOW_illustration";
  scene.add(root);
  const zones = new Map();
  let state = null, phase = 0, last = 0, ghost = false;
  const y0 = opts.elevation || 0;
  const dotMaterial = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, depthTest: false, clipping: true, uniforms: { size: { value: 2.8 } },
    vertexShader: "#include <clipping_planes_pars_vertex>\nuniform float size;void main(){vec4 mvPosition=modelViewMatrix*vec4(position,1.);\n#include <clipping_planes_vertex>\ngl_Position=projectionMatrix*mvPosition;gl_PointSize=size;}",
    fragmentShader: "#include <clipping_planes_pars_fragment>\nvoid main(){\n#include <clipping_planes_fragment>\nfloat r=length(gl_PointCoord-.5);if(r>.5)discard;gl_FragColor=vec4(.89,1.,.97,(1.-smoothstep(.2,.5,r))*.9);}",
  });
  const lineMaterial = new THREE.LineBasicMaterial({ color: 0xbce9df, transparent: true, opacity: 0.09, depthTest: false, depthWrite: false });

  function create(r) {
    const [a, b, c, d] = r.b;
    const ceiling = r.outlet?.ceiling ?? 2.95;
    const h = ceiling - 0.15;                         // the box reaches the ceiling
    const group = new THREE.Group();
    const mat = new THREE.ShaderMaterial({
      transparent: true, side: THREE.BackSide, depthWrite: false, depthTest: false,
      uniforms: {
        lo: { value: new THREE.Vector3(a + 0.1, y0 + 0.15, b + 0.1) }, hi: { value: new THREE.Vector3(c - 0.1, y0 + ceiling, d - 0.1) },
        front: { value: 30 }, rear: { value: 30 }, drop: { value: 0 }, phase: { value: 0 }, elapsed: { value: 0 }, ceiling: { value: 0 },
        count: { value: 0 }, outlets: { value: Array.from({ length: 6 }, () => new THREE.Vector3()) },
        directions: { value: Array.from({ length: 6 }, () => new THREE.Vector3()) },
        poly: { value: Array.from({ length: 64 }, (_, i) => new THREE.Vector2(...(r.poly?.[i] || [0, 0]))) },
        polyCount: { value: Math.min(64, r.poly?.length || 0) },
      },
      vertexShader: CLOUD_VS, fragmentShader: CLOUD_FS,
    });
    const cloud = new THREE.Mesh(new THREE.BoxGeometry(c - a - 0.2, h, d - b - 0.2), mat);
    cloud.position.set((a + c) / 2, y0 + 0.15 + h / 2, (b + d) / 2);
    cloud.renderOrder = 20;
    cloud.frustumCulled = false;
    group.add(cloud);
    const dots = new THREE.Points(new THREE.BufferGeometry(), dotMaterial);
    dots.frustumCulled = false;
    dots.renderOrder = 30;
    group.add(dots);
    const lines = new THREE.LineSegments(new THREE.BufferGeometry(), lineMaterial);
    lines.renderOrder = 21;
    group.add(lines);
    root.add(group);
    const box = { lx: a + 0.1, lz: b + 0.1, hx: c - 0.1, hz: d - 0.1 };
    return { r, group, cloud, dots, lines, paths: [], source: [], box };
  }

  function layout(v) {
    v.paths = flowPaths(v.r).map((p) => { const q = p.slice(); for (let k = 1; k < q.length; k += 3) q[k] += y0; return q; });
    v.source = flowSources(v.r);
    const n = v.paths.length * DOTS_PER_PATH;
    v.dots.geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    v.cloud.material.uniforms.count.value = Math.min(6, v.source.length);
    const pos = [];
    for (let j = 0; j < v.paths.length; j += 3) {
      const path = v.paths[j];
      for (let k = 0; k < SAMPLES - 1; k++) pos.push(...path.subarray(k * 3, k * 3 + 3), ...path.subarray((k + 1) * 3, (k + 1) * 3 + 3));
    }
    v.lines.geometry.dispose();
    v.lines.geometry = new THREE.BufferGeometry();
    v.lines.geometry.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  }

  /**
   * @param {{ thermal: boolean, volume: boolean, tracers: boolean, playing: boolean, minute: number,
   *   zones: any[], values: Record<string, number[]>, drops: Record<string, number>, hidden?: Set<string>,
   *   room?: number[] | null }} s   room = [x0, z0, x1, z1] in the viewer frame (isolate a room)
   */
  function update(s) {
    state = s;
    root.visible = !!s.thermal && (s.volume || s.tracers);
    const nextGhost = !!s.thermal && !!s.volume;
    if (ghost !== nextGhost) { ghost = nextGhost; opts.onGhost?.(ghost); }
    if (!root.visible) return;
    const room = s.room || null;
    dotMaterial.uniforms.size.value = room ? 3.5 : 2.8;
    for (const r of s.zones) {
      let v = zones.get(r.key);
      if (!v) { v = create(r); zones.set(r.key, v); }
      if (!v.paths.length) layout(v);
      const o = v.source[0]?.o;
      const near = !room || (o && o[0] >= room[0] - ROOM_GRACE && o[0] <= room[2] + ROOM_GRACE && o[2] >= room[1] - ROOM_GRACE && o[2] <= room[3] + ROOM_GRACE);
      v.group.visible = !(s.hidden && s.hidden.has(r.key)) && !!near;
      const u = v.cloud.material.uniforms;
      // room focus: the room's own cloud is pulled in to its rectangle (the planes do not reach this shader)
      u.lo.value.x = room ? Math.max(v.box.lx, room[0] - ROOM_PAD) : v.box.lx;
      u.lo.value.z = room ? Math.max(v.box.lz, room[1] - ROOM_PAD) : v.box.lz;
      u.hi.value.x = room ? Math.min(v.box.hx, room[2] + ROOM_PAD) : v.box.hx;
      u.hi.value.z = room ? Math.min(v.box.hz, room[3] + ROOM_PAD) : v.box.hz;
      v.source.slice(0, 6).forEach((src, i) => {
        u.outlets.value[i].set(src.o[0], src.o[1] + y0, src.o[2]);
        u.directions.value[i].set(...src.dirs[0]);
      });
      u.front.value = s.values[r.key]?.[0] ?? 30;
      u.rear.value = s.values[r.key]?.[1] ?? 30;
      u.drop.value = s.drops?.[r.key] || 0;
      u.elapsed.value = s.minute;
      v.cloud.visible = !!s.volume;
      v.dots.visible = !!s.tracers;
      v.lines.visible = !!s.tracers;
    }
    tick(performance.now(), true);
  }

  function tick(now, force = false) {
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
    last = now;
    if (!state || !root.visible) return;
    if (state.playing) phase += dt;
    else if (!force) return;
    for (const v of zones.values()) {
      if (!v.group.visible) continue;
      v.cloud.material.uniforms.phase.value = phase;
      const attr = v.dots.geometry.attributes.position;
      if (!attr) continue;
      const arr = /** @type {Float32Array} */ (attr.array);
      for (let i = 0; i < attr.count; i++) {
        const pi = Math.floor(i / DOTS_PER_PATH), path = v.paths[pi];
        const u = ((((i % DOTS_PER_PATH) / DOTS_PER_PATH + phase * 0.065 + (pi % 7) * 0.017) % 1) * SAMPLES);
        const k = Math.floor(u), f = u - k, k1 = (k + 1) % SAMPLES;
        for (let axis = 0; axis < 3; axis++) arr[i * 3 + axis] = path[k * 3 + axis] * (1 - f) + path[k1 * 3 + axis] * f;
      }
      attr.needsUpdate = true;
    }
  }

  function dispose() {
    for (const v of zones.values()) {
      v.cloud.geometry.dispose(); v.cloud.material.dispose(); v.dots.geometry.dispose(); v.lines.geometry.dispose();
    }
    dotMaterial.dispose(); lineMaterial.dispose();
    scene.remove(root);
  }

  return { update, tick, dispose, root, zones, loops: LOOPS_PER_OUTLET };
}
