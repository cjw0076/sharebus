import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import type { Message, Seat } from './types';
import { isWorking, messageKind } from './types';
import { copy } from './mainCopy';

export interface Anchor { x: number; y: number; footY: number }
interface Props { seats: Seat[]; messages: Message[]; currentRoot?: string; workspaceKey?: string; reducedMotion: boolean; demo: boolean; paused: boolean; onAnchors: (anchors: Record<string, Anchor>) => void; interactionRevision?: number }
interface Figure { group: THREE.Group; hands: THREE.Object3D[]; head: THREE.Object3D; baseY: number }

const mat = (color: THREE.ColorRepresentation, roughness = .72, metalness = 0) => new THREE.MeshStandardMaterial({ color, roughness, metalness });
function mesh(geometry: THREE.BufferGeometry, material: THREE.Material, parent: THREE.Object3D, x = 0, y = 0, z = 0) {
  const object = new THREE.Mesh(geometry, material);
  object.position.set(x, y, z); object.castShadow = true; object.receiveShadow = true; parent.add(object); return object;
}
const rounded = (w: number, h: number, d: number, radius = .12) => new RoundedBoxGeometry(w, h, d, 3, radius);
function box(parent: THREE.Object3D, material: THREE.Material, w: number, h: number, d: number, x = 0, y = 0, z = 0, radius = .08) { return mesh(rounded(w, h, d, radius), material, parent, x, y, z); }
function sphere(parent: THREE.Object3D, material: THREE.Material, r: number, x = 0, y = 0, z = 0, scale?: [number, number, number]) {
  const object = mesh(new THREE.SphereGeometry(r, 24, 16), material, parent, x, y, z); if (scale) object.scale.set(...scale); return object;
}

function makeChair(parent: THREE.Object3D) {
  const lilac = mat('#e5f1f8', .85), inset = mat('#f7fcff', .95), chrome = mat('#bed9e5', .28, .3);
  const chair = new THREE.Group(); parent.add(chair);
  box(chair, lilac, 1.52, .22, 1.24, 0, .72, 0, .12);
  box(chair, lilac, 1.48, 1.24, .27, 0, 1.3, -.45, .2);
  box(chair, inset, 1.25, .98, .11, 0, 1.34, -.29, .13);
  for (const side of [-1, 1]) box(chair, lilac, .16, .57, .94, side * .72, .96, .08, .08);
  mesh(new THREE.CylinderGeometry(.075, .075, .55, 16), chrome, chair, 0, .38, 0);
  for (let i = 0; i < 5; i++) {
    const angle = i / 5 * Math.PI * 2;
    const leg = box(chair, chrome, .68, .055, .09, Math.sin(angle) * .28, .13, Math.cos(angle) * .28, .02);
    leg.rotation.y = Math.PI / 2 - angle;
    const wheel = mesh(new THREE.CylinderGeometry(.09, .09, .07, 16), mat('#4f5664', .65), chair, Math.sin(angle) * .59, .1, Math.cos(angle) * .59);
    wheel.rotation.z = Math.PI / 2;
  }
}

function makeLaptop(parent: THREE.Object3D, accent: string) {
  const laptop = new THREE.Group(); laptop.position.set(0, 1.62, .77); parent.add(laptop);
  const silver = mat('#b7d8e7', .38, .25), dark = mat('#53829a', .56, .15);
  box(laptop, silver, 1.11, .045, .7, 0, 0, 0, .03);
  box(laptop, dark, .85, .008, .3, 0, .026, -.08, .02);
  for (let row = 0; row < 3; row++) for (let col = 0; col < 10; col++) box(laptop, mat('#b9c2cd', .7), .059, .007, .055, (col - 4.5) * .076, .031, -.17 + row * .08, .008);
  box(laptop, mat('#c3cbd4', .5, .3), .32, .008, .14, 0, .026, .18, .01);
  const hinge = new THREE.Group(); hinge.position.set(0, .02, .33); hinge.rotation.x = -.19; laptop.add(hinge);
  box(hinge, silver, 1.14, .53, .045, 0, .26, 0, .045);
  const glow = new THREE.MeshStandardMaterial({ color: accent, emissive: accent, emissiveIntensity: .55, roughness: .4 });
  box(hinge, glow, .15, .075, .014, 0, .26, .03, .035);
  box(hinge, dark, 1.02, .42, .012, 0, .26, -.029, .025);
  for (let i = 0; i < 4; i++) box(hinge, glow, .32 + i % 2 * .18, .018, .006, -.12 + i % 2 * .08, .14 + i * .07, -.038, .003);
}

function makeClaude(parent: THREE.Object3D): Figure {
  const body = new THREE.Group(); parent.add(body);
  const orange = mat('#e89355', .94), highlight = mat('#efad6c', .93), eye = mat('#303036', .84);
  const head = box(body, orange, 1.02, .92, .65, 0, 2.13, 0, .075);
  for (const side of [-1, 1]) {
    box(body, eye, .085, .2, .035, side * .24, 2.14, .341, .022);
    const arm = box(body, orange, .22, .5, .25, side * .59, 1.73, .06, .03);
    arm.rotation.z = side * -.35;
    for (let i = 0; i < 2; i++) box(body, orange, .18, .22, .24, side * (.19 + i * .24), 1.52, .05, .025);
  }
  const hands = [-1, 1].map(side => { const h = box(body, highlight, .25, .24, .35, side * .34, 1.72, .47, .035); h.rotation.x = -.22; return h; });
  return { group: body, hands, head, baseY: 0 };
}

function makeCodex(parent: THREE.Object3D): Figure {
  const body = new THREE.Group(); parent.add(body);
  const blue = mat('#7299ed', .84), sideBlue = mat('#6088dc', .82), face = mat('#192a42', .45), cyan = new THREE.MeshStandardMaterial({ color: '#74e2ff', emissive: '#47bfe9', emissiveIntensity: 1.05, roughness: .4 });
  box(body, blue, .66, .68, .5, 0, 1.63, 0, .22);
  const head = new THREE.Group(); head.position.y = 2.16; body.add(head);
  box(head, sideBlue, 1.08, .96, .68, 0, 0, 0, .27);
  box(head, blue, .99, .87, .69, 0, .025, .035, .23);
  box(head, face, .8, .62, .09, 0, -.015, .38, .16);
  for (const side of [-1, 1]) {
    const ear = mesh(new THREE.CylinderGeometry(.21, .21, .1, 28), sideBlue, head, side * .57, -.02, -.02);
    ear.rotation.z = Math.PI / 2;
    const nub = sphere(body, blue, .19, side * .29, 1.37, .15); nub.scale.y = .8;
  }
  const upper = box(head, cyan, .21, .042, .026, -.13, .055, .439, .012); upper.rotation.z = -.65;
  const lower = box(head, cyan, .21, .042, .026, -.13, -.067, .439, .012); lower.rotation.z = .65;
  box(head, cyan, .17, .037, .026, .18, -.08, .439, .01);
  const hands = [-1, 1].map(side => sphere(body, blue, .18, side * .34, 1.73, .47, [1, .75, 1.15]));
  return { group: body, hands, head, baseY: 0 };
}

function makeStar(parent: THREE.Object3D): Figure {
  const body = new THREE.Group(); parent.add(body);
  const white = mat('#f4f3ef', .98), eye = mat('#30333b', .8);
  const shape = new THREE.Shape();
  const points: Array<[number, number]> = [[0,.69],[.21,.21],[.68,0],[.24,-.23],[0,-.65],[-.23,-.23],[-.68,0],[-.21,.21]];
  points.forEach(([x,y], i) => i ? shape.lineTo(x,y) : shape.moveTo(x,y)); shape.closePath();
  const head = mesh(new THREE.ExtrudeGeometry(shape, { depth: .2, bevelEnabled: true, bevelSegments: 5, steps: 1, bevelSize: .105, bevelThickness: .13, curveSegments: 16 }), white, body, 0, 2.06, 0);
  const tips = [sphere(body, mat('#83b4ec', .95), .145, 0, 2.69, .055, [.9, 1.15, 1]), sphere(body, mat('#ebd272', .95), .13, -.63, 2.06, .07), sphere(body, mat('#df9b9b', .95), .14, .62, 2.06, .07), sphere(body, mat('#91b8a3', .95), .13, 0, 1.43, .05)];
  tips.forEach(t => t.castShadow = true);
  for (const side of [-1, 1]) {
    sphere(body, eye, .045, side * .18, 2.1, .36, [.8,1.5,.7]);
    sphere(body, mat('#edb9b1', .98), .072, side * .3, 1.99, .34, [1,.55,.18]);
  }
  sphere(body, mat('#7e5655', .95), .052, 0, 1.99, .36, [1,.75,.35]);
  const hands = [-1,1].map(side => sphere(body, white, .16, side * .32, 1.73, .48, [1,.8,1.2]));
  return { group: body, hands, head, baseY: 0 };
}

function makeGuest(parent: THREE.Object3D): Figure {
  const body = new THREE.Group(); parent.add(body); const material = mat('#a9a5ca', .93);
  const head = sphere(body, material, .48, 0, 2.1, 0, [1,1.06,.7]);
  for (const side of [-1,1]) sphere(body, mat('#394050'), .04, side * .16, 2.12, .34, [1,1.5,.5]);
  const hands = [-1,1].map(side => sphere(body, material, .15, side * .31, 1.72, .47));
  return { group: body, hands, head, baseY: 0 };
}

const accents: Record<string, string> = { claude: '#f79862', codex: '#72abfa', gemini: '#a8deef' };
function positions(count: number, compact: boolean): Array<[number, number]> {
  if (count === 1) return [[0, 0]];
  if (count === 2) return [[-1.9, 0], [1.9, 0]];
  if (count === 3) return compact ? [[-1.7, 1.1], [0, -1.8], [1.7, 1.1]] : [[-3.1, .65], [0, -1.1], [3.1, .65]];
  return Array.from({ length: count }, (_, index) => [(index % 3 - 1) * (compact ? 2.05 : 2.7), Math.floor(index / 3) * 2.65 - 1.45]);
}

export default function CouncilScene({ seats, messages, currentRoot, workspaceKey, reducedMotion, demo, paused, onAnchors, interactionRevision = 0 }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const props = useRef({ seats, reducedMotion, demo, paused, onAnchors }); props.current = { seats, reducedMotion, demo, paused, onAnchors };
  const [fallback, setFallback] = useState(false);
  const [activeEvents, setActiveEvents] = useState<Message[]>([]);
  const signature = seats.map(seat => `${seat.key}:${seat.provider}`).join('|');
  const drawRef = useRef<(() => void) | null>(null);
  const resetRef = useRef<(() => void) | null>(null);
  const interactRef = useRef<(() => void) | null>(null);
  const emitRef = useRef<((events: Message[], reset?: boolean) => void) | null>(null);
  const cursor = useRef<{ workspace: string; root?: string; seq: number; ids: Set<string>; disabled: boolean } | null>(null);
  useEffect(() => { drawRef.current?.(); }, [reducedMotion, paused, seats]);
  useEffect(() => { if (interactionRevision) interactRef.current?.(); }, [interactionRevision]);

  useEffect(() => {
    if (!fallback || !host.current) return;
    const element = host.current;
    const resize = () => {
      const width = element.clientWidth, height = element.clientHeight;
      const layout = positions(seats.length, width <= 700);
      onAnchors(Object.fromEntries(seats.map((seat, index) => [seat.key, { x: width * (.5 + layout[index][0] / 9), y: height * .2, footY: Math.min(height - 62, height * (.68 + layout[index][1] * .06)) }])));
    };
    const observer = new ResizeObserver(resize); observer.observe(element); resize();
    return () => observer.disconnect();
  }, [fallback, signature, onAnchors]);

  useEffect(() => {
    if (!host.current) return;
    const element = host.current;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' }); } catch { setFallback(true); return; }
    setFallback(false);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.8));
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.2;
    renderer.domElement.setAttribute('aria-hidden', 'true'); renderer.domElement.dataset.renderer = 'three-webgl'; element.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-6, 6, 4, -4, .1, 60);
    camera.position.set(0, 8, 12); camera.lookAt(0, 1.1, 0);
    scene.add(new THREE.HemisphereLight('#ffffff', '#c7edf5', 3));
    const key = new THREE.DirectionalLight('#ffffff', 3.5); key.position.set(-3, 8, 5); key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024); key.shadow.camera.left = -8; key.shadow.camera.right = 8; key.shadow.camera.top = 7; key.shadow.camera.bottom = -7; key.shadow.normalBias = .035; scene.add(key);
    const fill = new THREE.DirectionalLight('#c7edf5', 1.5); fill.position.set(4, 4, -3); scene.add(fill);
    const water = new THREE.Group(); scene.add(water);
    const depth = mesh(new THREE.CylinderGeometry(1, 1, .42, 96), new THREE.MeshBasicMaterial({ color: '#c7edf5', transparent: true, opacity: .64, toneMapped: false }), water, 0, .28, 0); depth.scale.set(6.5, 1, 3.3); depth.castShadow = false;
    const surface = mesh(new THREE.CircleGeometry(1, 96), new THREE.MeshBasicMaterial({ color: '#e4f8fc', transparent: true, opacity: .72, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }), water, 0, .56, 0); surface.rotation.x = -Math.PI / 2; surface.scale.set(6.5, 3.3, 1); surface.castShadow = false; surface.renderOrder = 2;
    const waterline = mesh(new THREE.TorusGeometry(1, .006, 6, 128), new THREE.MeshBasicMaterial({ color: '#99d9e9', transparent: true, opacity: .65 }), water, 0, .56, 0); waterline.rotation.x = Math.PI / 2; waterline.scale.set(6.5, 3.3, 1); waterline.castShadow = false;
    const figures: Figure[] = [], stations: THREE.Group[] = [], origins = new Map<string, THREE.Vector3>();
    seats.forEach(seat => {
      const station = new THREE.Group(); scene.add(station); stations.push(station);
      makeChair(station);
      const figure = seat.provider === 'claude' ? makeClaude(station) : seat.provider === 'codex' ? makeCodex(station) : seat.provider === 'gemini' ? makeStar(station) : makeGuest(station);
      figures.push(figure);
      // The waterline at .56 covers the lower third of the 1.62-high desk support.
      const desk = mesh(new THREE.CylinderGeometry(1.1, 1.08, .13, 64), mat('#f8fcff', .38), station, 0, 1.56, .75); desk.scale.z = .67;
      for (const x of [-.78, .78]) box(station, mat('#bedbe7'), .09, 1.47, .09, x, .78, .75);
      const base = mesh(new THREE.CylinderGeometry(.96, .96, .12, 64), new THREE.MeshStandardMaterial({ color: '#c7edf5', transparent: true, opacity: .62, roughness: .42 }), station, 0, .17, .6); base.scale.z = .68;
      const veil = mesh(new THREE.CircleGeometry(.97, 64), new THREE.MeshBasicMaterial({ color: '#e4f8fc', transparent: true, opacity: .28, side: THREE.DoubleSide, depthWrite: false }), station, 0, .565, .6); veil.rotation.x = -Math.PI / 2; veil.scale.y = .68; veil.castShadow = false; veil.renderOrder = 3;
      makeLaptop(station, accents[seat.provider] || '#9bb5c9');
    });
    type Ripple = { ring: THREE.Mesh<THREE.RingGeometry, THREE.MeshBasicMaterial>; start: number; duration: number; source: 'public' | 'pointer'; eventId?: string };
    type Arc = { line: THREE.Mesh<THREE.TubeGeometry, THREE.MeshBasicMaterial>; start: number };
    const ripples: Ripple[] = [], arcs: Arc[] = [];
    let frame = 0, disposed = false, lost = false, lastInteraction = 0;
    const removeRipple = (index: number) => { const ripple = ripples[index]; scene.remove(ripple.ring); ripple.ring.geometry.dispose(); ripple.ring.material.dispose(); ripples.splice(index, 1); };
    const removeArc = (index: number) => { const arc = arcs[index]; scene.remove(arc.line); arc.line.geometry.dispose(); arc.line.material.dispose(); arcs.splice(index, 1); };
    const addRipple = (at: THREE.Vector3, source: Ripple['source'], eventId?: string) => {
      if (ripples.length >= 3) removeRipple(0);
      const ring = new THREE.Mesh(new THREE.RingGeometry(.97, 1, 96), new THREE.MeshBasicMaterial({ color: source === 'public' ? '#57bbd4' : '#99d9e9', transparent: true, opacity: .65, side: THREE.DoubleSide, depthWrite: false }));
      ring.rotation.x = -Math.PI / 2; ring.position.set(at.x, .58, at.z); ring.renderOrder = 4; ring.userData.eventId = eventId; scene.add(ring);
      ripples.push({ ring, start: performance.now(), duration: source === 'public' ? 1800 : 650, source, eventId });
    };
    const render = () => { if (!disposed && !lost) renderer.render(scene, camera); };
    const tick = (time: number) => {
      frame = 0; if (disposed || lost) return;
      const moving = !props.current.reducedMotion && !props.current.paused && !document.hidden;
      figures.forEach((figure, index) => {
        const working = moving && isWorking(props.current.seats[index]?.participant);
        figure.hands.forEach((hand, handIndex) => { hand.position.y = 1.73 + (working ? Math.sin(time * .008 + index * 1.9 + handIndex * Math.PI) * .018 : 0); });
        if (figure.head instanceof THREE.Group) figure.head.rotation.z = working ? Math.sin(time * .0008 + index) * .018 : 0;
        stations[index].userData.working = working;
      });
      for (let index = ripples.length - 1; index >= 0; index--) {
        const ripple = ripples[index], progress = (time - ripple.start) / ripple.duration;
        if (progress >= 1 || props.current.reducedMotion || document.hidden || (props.current.paused && ripple.source === 'public')) { removeRipple(index); continue; }
        ripple.ring.scale.setScalar(.22 + progress * (ripple.source === 'public' ? 2.9 : 1.2)); ripple.ring.material.opacity = .56 * (1 - progress);
      }
      for (let index = arcs.length - 1; index >= 0; index--) {
        const progress = (time - arcs[index].start) / 1400;
        if (progress >= 1 || !moving) { removeArc(index); continue; } arcs[index].line.material.opacity = .52 * (1 - progress);
      }
      element.dataset.communicationCount = String(ripples.filter(ripple => ripple.source === 'public').length);
      element.dataset.publicEventIds = ripples.filter(ripple => ripple.source === 'public').map(ripple => ripple.eventId).join(',');
      element.dataset.interactionCount = String(ripples.filter(ripple => ripple.source === 'pointer').length);
      element.dataset.workingCount = String(stations.filter(station => station.userData.working).length);
      render();
      if (!document.hidden && (ripples.length || arcs.length || (moving && props.current.seats.some(seat => isWorking(seat.participant))))) frame = requestAnimationFrame(tick);
    };
    const draw = () => { if (disposed || lost) return; if (frame) cancelAnimationFrame(frame); frame = 0; tick(performance.now()); };
    drawRef.current = draw;
    emitRef.current = (events, reset) => {
      if (reset) { while (ripples.length) removeRipple(0); while (arcs.length) removeArc(0); }
      if (!props.current.paused && !props.current.reducedMotion && !document.hidden) for (const event of events) {
        const start = origins.get(event.actor), end = event.recipient ? origins.get(event.recipient) : null;
        if (!start) continue; addRipple(start, 'public', event.id);
        if (end && event.actor !== event.recipient) {
          if (arcs.length >= 3) removeArc(0);
          const middle = start.clone().lerp(end, .5); middle.y = 2.6;
          const curve = new THREE.QuadraticBezierCurve3(start.clone().setY(1.9), middle, end.clone().setY(1.9));
          const line = new THREE.Mesh(new THREE.TubeGeometry(curve, 32, .012, 6, false), new THREE.MeshBasicMaterial({ color: '#57bbd4', transparent: true, opacity: .52, depthWrite: false })); scene.add(line); arcs.push({ line, start: performance.now() });
        }
      }
      draw();
    };
    const interact = (event?: PointerEvent) => {
      if (props.current.reducedMotion || document.hidden || performance.now() - lastInteraction < 650) return;
      lastInteraction = performance.now();
      const box = element.getBoundingClientRect();
      const at = event ? new THREE.Vector3((event.clientX - box.left - box.width / 2) / box.width * 8, .58, (event.clientY - box.top - box.height / 2) / box.height * 3) : new THREE.Vector3(0, .58, 2.5);
      addRipple(at, 'pointer'); draw();
    };
    interactRef.current = () => interact(); element.addEventListener('pointerdown', interact);
    const resize = () => {
      const width = element.clientWidth, height = element.clientHeight; if (!width || !height) return;
      const compact = width <= 700, layout = positions(seats.length, compact), aspect = width / height;
      const halfWidth = compact ? 4.3 : Math.max(5.4, 3.35 * aspect), halfHeight = halfWidth / aspect;
      camera.left = -halfWidth; camera.right = halfWidth; camera.top = halfHeight; camera.bottom = -halfHeight; camera.updateProjectionMatrix(); camera.updateMatrixWorld();
      renderer.setSize(width, height);
      const anchors: Record<string, Anchor> = {};
      stations.forEach((station, index) => {
        station.position.set(layout[index][0], 0, layout[index][1]); station.scale.setScalar(seats.length > 3 ? .84 : 1); station.updateMatrixWorld(true);
        origins.set(seats[index].key, station.localToWorld(new THREE.Vector3(0, .58, .7)));
        const head = station.localToWorld(new THREE.Vector3(0, 3.1, 0)).project(camera), foot = station.localToWorld(new THREE.Vector3(0, .25, .7)).project(camera);
        anchors[seats[index].key] = { x: Math.max(compact ? 48 : 100, Math.min(width - (compact ? 48 : 100), (head.x * .5 + .5) * width)), y: Math.max(76, (-head.y * .5 + .5) * height), footY: Math.max(140, Math.min(height - 62, (-foot.y * .5 + .5) * height)) };
      });
      props.current.onAnchors(anchors); draw();
    };
    resetRef.current = resize;
    const observer = new ResizeObserver(resize); observer.observe(element);
    const visibility = () => { if (document.hidden && cursor.current) cursor.current.disabled = true; draw(); }; document.addEventListener('visibilitychange', visibility);
    const contextLost = (event: Event) => { event.preventDefault(); lost = true; if (frame) cancelAnimationFrame(frame); element.dataset.communicationCount = '0'; element.dataset.workingCount = '0'; element.dataset.publicEventIds = ''; setFallback(true); };
    renderer.domElement.addEventListener('webglcontextlost', contextLost);
    resize();
    return () => {
      disposed = true; drawRef.current = null; emitRef.current = null; resetRef.current = null; interactRef.current = null; if (frame) cancelAnimationFrame(frame);
      observer.disconnect(); document.removeEventListener('visibilitychange', visibility); element.removeEventListener('pointerdown', interact); renderer.domElement.removeEventListener('webglcontextlost', contextLost);
      while (ripples.length) removeRipple(0); while (arcs.length) removeArc(0);
      const materials = new Set<THREE.Material>(), geometries = new Set<THREE.BufferGeometry>();
      scene.traverse(object => { if (object instanceof THREE.Mesh) { geometries.add(object.geometry); (Array.isArray(object.material) ? object.material : [object.material]).forEach(material => materials.add(material)); } });
      materials.forEach(material => material.dispose()); geometries.forEach(geometry => geometry.dispose()); renderer.dispose(); renderer.domElement.remove();
    };
  }, [signature]);

  useEffect(() => {
    if (!workspaceKey || demo) return;
    const seq = Math.max(0, ...messages.map(message => Number.isSafeInteger(message.seq) ? message.seq! : 0));
    const disabled = paused || document.hidden;
    const previous = cursor.current;
    if (!previous || previous.workspace !== workspaceKey || previous.root !== currentRoot || (previous.disabled && !disabled)) {
      cursor.current = { workspace: workspaceKey, root: currentRoot, seq, ids: new Set(messages.map(message => message.id)), disabled };
      emitRef.current?.([], true); setActiveEvents([]); return;
    }
    const fresh = messages.filter(message => !previous.ids.has(message.id) && Number.isSafeInteger(message.seq) && message.seq! > previous.seq);
    previous.seq = Math.max(previous.seq, seq); previous.disabled = disabled;
    for (const message of messages) previous.ids.add(message.id);
    if (previous.ids.size > 4096) previous.ids = new Set(messages.map(message => message.id));
    if (disabled || !currentRoot) { emitRef.current?.([], true); return; }
    const participants = new Set(seats.map(seat => seat.key));
    const selected = fresh.filter(message => message.root_id === currentRoot && participants.has(message.actor)).slice(-3);
    if (selected.length) { setActiveEvents(selected); emitRef.current?.(selected); }
  }, [messages, currentRoot, workspaceKey, reducedMotion, paused, demo, seats]);
  return <div className={`council-webgl ${fallback ? 'is-fallback' : ''}`}><div className="scene-host" ref={host} role="img" aria-label={copy('scene_aria_label')}>
    {fallback && <div className="still-room"><div className="still-water" /><div className={`still-stations count-${seats.length}`}>{seats.map(seat => <div className={`still-character ${seat.provider}`} key={seat.key}>{['claude', 'codex', 'gemini'].includes(seat.provider) ? <img src={`/assets/characters/sharebus-${seat.provider}.svg`} alt="" /> : <span aria-hidden="true">◌</span>}</div>)}</div><p className="fallback-note">{copy('scene_fallback_notice')}</p></div>}
  </div>{!fallback && seats.length > 0 && <button className="camera-reset text-button" onClick={() => resetRef.current?.()}>{copy('scene_camera_reset_button')}</button>}<div className="communication-announcements sr-only" aria-live="polite">{activeEvents.filter(event => event.root_id === currentRoot).map(event => <span key={event.id} data-event-id={event.id}>{event.recipient ? copy('scene_peer_announcement', { actor: seats.find(seat => seat.key === event.actor)?.label || '', recipient: seats.find(seat => seat.key === event.recipient)?.label || '', kind: messageKind(event.kind) }) : `${seats.find(seat => seat.key === event.actor)?.label} · ${messageKind(event.kind)}`}</span>)}</div></div>;
}
