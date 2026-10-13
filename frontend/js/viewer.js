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

  /** @param {string} url @param {any} manifest */
  async load(url, manifest) {
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
    this.setCeilings(false);
    this.overview();
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
    const hit = this.raycaster.intersectObject(this.root, true).find((i) => i.object.visible);
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    if (!hit) return this.onPick(null);
    let obj = hit.object;
    while (obj.parent && obj.parent !== this.root && !/^(ARCH|FURN|HVAC)_/.test(obj.name)) obj = obj.parent;
    const name = obj.name;
    this.highlight = new THREE.BoxHelper(obj, 0x2f5d8a);
    this.scene.add(this.highlight);
    const m = this.manifest || {};
    const info = (m.furniture || []).find((f) => f.name === name) || (m.objects || []).find((o) => o.name === name) || { name };
    this.onPick({ ...info, name });
  }

  dispose() {
    this.running = false;
    this.ro.disconnect();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
