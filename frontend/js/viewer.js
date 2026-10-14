// @ts-check
/** three.js preview of the generated GLB (glTF is Y-up: scene (x, y, z) -> three (x, z, -y)). */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export class Viewer {
  /** @param {HTMLElement} container @param {(info:any)=>void} onPick */
  constructor(container, onPick) {
    this.container = container;
    this.onPick = onPick;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xdfe3e8);
    // neutral image-based lighting so metallic materials (steel, chrome, brass) reflect something
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.6;
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 500);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xb8b0a0, 1.6));
    const sun = new THREE.DirectionalLight(0xfff2e0, 2.2);
    sun.position.set(-8, 14, 6);
    this.scene.add(sun);
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.35));
    this.root = null;
    this.ceilings = [];
    this.highlight = null;
    this.raycaster = new THREE.Raycaster();
    this.resize = this.resize.bind(this);
    this.ro = new ResizeObserver(this.resize);
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
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    this.resize();
    loop();
  }

  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  /** @param {string} url @param {any} manifest @param {Record<string,string>} [sensorColors] kind -> colour */
  async load(url, manifest, sensorColors = {}) {
    this.manifest = manifest;
    const gltf = await new GLTFLoader().loadAsync(url);
    if (this.root) this.scene.remove(this.root);
    this.root = gltf.scene;
    this.ceilings = [];
    this.root.traverse((o) => {
      if (o.name.startsWith("ARCH_Ceiling_")) this.ceilings.push(o);
      const mesh = /** @type {any} */ (o);
      if (mesh.isMesh && mesh.material && mesh.material.transparent) mesh.material.depthWrite = false;
    });
    this.scene.add(this.root);
    this.makePins(manifest?.sensors || [], sensorColors);
    this.setCeilings(false);
    this.overview();
  }

  /** A screen-sized dot over every sensor of the layer, drawn on top so sensors read even in the overview. */
  makePins(sensors, colors) {
    if (this.pins) this.scene.remove(this.pins);
    this.pins = new THREE.Group();
    const tex = {};
    for (const s of sensors) {
      const color = colors[s.kind] || "#6F7782";
      if (!tex[color]) {
        const c = document.createElement("canvas");
        c.width = c.height = 64;
        const g = /** @type {CanvasRenderingContext2D} */ (c.getContext("2d"));
        g.beginPath(); g.arc(32, 32, 26, 0, Math.PI * 2); g.fillStyle = "#ffffff"; g.fill();
        g.beginPath(); g.arc(32, 32, 19, 0, Math.PI * 2); g.fillStyle = color; g.fill();
        tex[color] = new THREE.CanvasTexture(c);
        tex[color].colorSpace = THREE.SRGBColorSpace;
      }
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex[color], depthTest: false, sizeAttenuation: false, transparent: true }));
      const [x, y, z] = s.location_m;
      sp.position.set(x, z + 0.12, -y);
      sp.scale.set(0.028, 0.028, 1);
      sp.renderOrder = 999;
      sp.name = s.name;
      this.pins.add(sp);
    }
    this.scene.add(this.pins);
  }

  setPins(visible) { if (this.pins) this.pins.visible = visible; }

  /** Look at one sensor from the open floor of its room, a little above head height (at most 4 m away). */
  focusSensor(s) {
    const [x, y, z] = s.location_m;
    const room = (this.manifest?.rooms || []).find((r) => r.id === s.room_id);
    let [ex, ey] = room ? (room.spawn_point || room.label_point) : [x + 1.5, y - 1.5];
    const d = Math.hypot(ex - x, ey - y);
    if (d > 4) { ex = x + ((ex - x) * 4) / d; ey = y + ((ey - y) * 4) / d; }
    else if (d < 0.8) { ex = x + 1.2; ey = y - 1.2; }
    this.controls.target.set(x, z, -y);
    this.camera.position.set(ex, (room ? room.floor_z : z - 1.5) + 2.3, -ey);
    const node = this.root?.getObjectByName(s.name);
    if (node) this.select(node, s.name);
  }

  setCeilings(visible) { this.ceilings.forEach((c) => (c.visible = visible)); }

  overview() {
    const box = new THREE.Box3().setFromObject(this.root);
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.z);
    this.camera.position.set(c.x - span * 0.35, span * 1.0, c.z + span * 0.85);
    this.controls.target.copy(c);
  }

  /** Eye-level view of a room from the manifest (scene coords -> three coords). */
  roomView(room) {
    const [x, y] = room.label_point;
    const z = room.floor_z;
    const [minx, miny] = [Math.min(...room.polygon.map((p) => p[0])), Math.min(...room.polygon.map((p) => p[1]))];
    const eye = new THREE.Vector3(minx + 0.6, z + 1.6, -(miny + 0.6));
    this.camera.position.copy(eye);
    this.controls.target.set(x, z + 1.0, -y);
  }

  pick(e) {
    if (!this.root) return;
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const pin = this.pins?.visible ? this.raycaster.intersectObject(this.pins, true)[0] : null;
    const hit = pin || this.raycaster.intersectObject(this.root, true).find((i) => i.object.visible);
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    if (!hit) return this.onPick(null);
    // the topmost named ancestor: an object with several materials loads as a group whose parts are <name>_1, <name>_2
    let obj = hit.object;
    for (let x = hit.object; x && x !== this.root; x = x.parent) if (/^(ARCH|FURN|HVAC|SENSOR)_/.test(x.name)) obj = x;
    if (pin) obj = this.root.getObjectByName(obj.name) || obj;
    this.select(obj, obj.name);
  }

  select(obj, name) {
    if (this.highlight) this.scene.remove(this.highlight);
    this.highlight = new THREE.BoxHelper(obj, 0x2f5d8a);
    this.scene.add(this.highlight);
    const m = this.manifest || {};
    const info = (m.furniture || []).find((f) => f.name === name) || (m.objects || []).find((o) => o.name === name) ||
      (m.sensors || []).find((s) => s.name === name) || { name };
    this.onPick({ ...info, name });
  }

  dispose() {
    this.running = false;
    this.ro.disconnect();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
