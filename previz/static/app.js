import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { JOINTS, jointAngles, POSE_PRESETS, newCharacter, newId } from './core.js';

// ============ 全局状态 ============
const state = {
  scene: null, camera: null, renderer: null, controls: null,
  mannequins: new Map(),      // characterId -> {group, joints, labelEl, data}
  props: new Map(),           // propId -> {group, data}
  selected: null,             // {kind:'char'|'prop', id}
  editHelpers: null,          // THREE.Group：编辑辅助（选中环等）
};
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
let dragState = null;

// ============ 场景 ============
export function initScene() {
  const el = document.getElementById('viewport');
  state.scene = new THREE.Scene();
  state.scene.background = new THREE.Color(0x111827);
  state.camera = new THREE.PerspectiveCamera(50, el.clientWidth / el.clientHeight, 0.1, 500);
  state.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  state.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  state.renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(state.renderer.domElement);
  state.controls = new OrbitControls(state.camera, state.renderer.domElement);
  state.controls.target.set(0, 1, 0);
  state.controls.update();
  state.scene.add(new THREE.AmbientLight(0xffffff, 0.85));
  const dir = new THREE.DirectionalLight(0xffffff, 1.3);
  dir.position.set(4, 8, 3);
  state.scene.add(dir);
  const grid = new THREE.GridHelper(20, 20, 0x475569, 0x1e293b);
  state.scene.add(grid);
  // 地平线参考：一条远处横线，落在 y=0 平面上 z=-9
  const horizon = new THREE.Mesh(
    new THREE.BoxGeometry(0.06, 0.06, 20),
    new THREE.MeshBasicMaterial({ color: 0x64748b }));
  horizon.position.set(0, 1.4, -9);
  state.scene.add(horizon);
  state.editHelpers = new THREE.Group();
  state.scene.add(state.editHelpers);
}

// ============ 木人 ============
function part(geo, mat) { return new THREE.Mesh(geo, mat); }
const _jointGeo = new THREE.SphereGeometry(0.045, 12, 12);
const _jointMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.4 });

export function buildMannequin(color) {
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.75 });
  const root = new THREE.Group();                    // 原点 = 脚底
  const hips = new THREE.Group(); hips.position.y = 0.95; root.add(hips);
  hips.add(part(new THREE.CapsuleGeometry(0.15, 0.14, 8, 16), mat));   // 骨盆
  const spine = new THREE.Group(); hips.add(spine);
  const torso = part(new THREE.CapsuleGeometry(0.17, 0.44, 8, 16), mat);
  torso.position.y = 0.22; spine.add(torso);
  const chest = new THREE.Group(); chest.position.y = 0.38; spine.add(chest);
  const neck = new THREE.Group(); neck.position.y = 0.06; chest.add(neck);
  const head = part(new THREE.SphereGeometry(0.14, 20, 20), mat);
  head.position.y = 0.17; neck.add(head);
  const joints = {};
  // 腿（大腿胶囊 + 膝盖/踝关节组 + 小腿 + 脚）
  const makeLeg = (side) => {
    const hipJ = new THREE.Group(); hipJ.position.set(side * 0.1, 0, 0); hips.add(hipJ);
    const thigh = part(new THREE.CapsuleGeometry(0.085, 0.32, 8, 16), mat);
    thigh.position.y = -0.23; hipJ.add(thigh);
    const kneeJ = new THREE.Group(); kneeJ.position.y = -0.45; hipJ.add(kneeJ);
    const shin = part(new THREE.CapsuleGeometry(0.07, 0.3, 8, 16), mat);
    shin.position.y = -0.22; kneeJ.add(shin);
    const ankleJ = new THREE.Group(); ankleJ.position.y = -0.44; kneeJ.add(ankleJ);
    const foot = part(new THREE.BoxGeometry(0.16, 0.07, 0.24), mat);
    foot.position.set(0, -0.04, 0.06); ankleJ.add(foot);
    return { hipJ, kneeJ, ankleJ };
  };
  const leftLeg = makeLeg(-1), rightLeg = makeLeg(1);
  // 手臂（大臂胶囊 + 肘/腕关节组 + 小臂 + 手球）
  const makeArm = (side) => {
    const shoulderJ = new THREE.Group(); shoulderJ.position.set(side * 0.24, 0.33, 0); chest.add(shoulderJ);
    const upper = part(new THREE.CapsuleGeometry(0.065, 0.22, 8, 16), mat);
    upper.position.y = -0.14; shoulderJ.add(upper);
    const elbowJ = new THREE.Group(); elbowJ.position.y = -0.3; shoulderJ.add(elbowJ);
    const fore = part(new THREE.CapsuleGeometry(0.055, 0.2, 8, 16), mat);
    fore.position.y = -0.13; elbowJ.add(fore);
    const wristJ = new THREE.Group(); wristJ.position.y = -0.27; elbowJ.add(wristJ);
    const hand = part(new THREE.SphereGeometry(0.055, 12, 12), mat);
    hand.position.y = -0.05; wristJ.add(hand);
    return { shoulderJ, elbowJ, wristJ };
  };
  const leftArm = makeArm(-1), rightArm = makeArm(1);
  Object.assign(joints, {
    spine, neck,
    left_shoulder: leftArm.shoulderJ, left_elbow: leftArm.elbowJ, left_wrist: leftArm.wristJ,
    right_shoulder: rightArm.shoulderJ, right_elbow: rightArm.elbowJ, right_wrist: rightArm.wristJ,
    left_hip: leftLeg.hipJ, left_knee: leftLeg.kneeJ, left_ankle: leftLeg.ankleJ,
    right_hip: rightLeg.hipJ, right_knee: rightLeg.kneeJ, right_ankle: rightLeg.ankleJ,
  });
  // 关节白球（选中人物时显示，供点选微调；球只挂各自关节，不共用父组——
  // three 的 add 会先 removeFromParent，共挂会把球全堆到局部原点）
  const balls = [];
  for (const j of Object.values(joints)) {
    const ball = part(_jointGeo, _jointMat);
    ball.userData.jointName = null;
    ball.visible = false;
    j.add(ball);
    balls.push(ball);
  }
  root.userData = { joints, balls };
  return root;
}

export function setJointBallsVisible(group, visible) {
  for (const ball of group.userData.balls) ball.visible = visible;
}

export function applyPose(root, pose, offsets) {
  const angles = jointAngles(pose, offsets);
  for (const [name, joint] of Object.entries(root.userData.joints)) {
    const [x, y, z] = angles[name] || [0, 0, 0];
    joint.rotation.set(x, y, z);
  }
}

export function addMannequin(data) {
  const group = buildMannequin(data.color);
  group.position.set(data.position[0], data.position[1], data.position[2]);
  group.rotation.y = (data.rotation_y || 0) * Math.PI / 180;
  group.scale.setScalar(data.scale || 1);
  applyPose(group, data.pose, data.joints);
  state.scene.add(group);
  const labelEl = document.createElement('div');
  labelEl.className = 'char-label';
  labelEl.textContent = data.label || data.id;
  document.getElementById('viewport-wrap').appendChild(labelEl);
  state.mannequins.set(data.id, { group, data, labelEl });
  return group;
}

export function updateLabels() {
  for (const { group, labelEl } of state.mannequins.values()) {
    const v = new THREE.Vector3();
    group.children[0].getWorldPosition(v);   // 髋部
    v.y += 0.9;                               // 头顶上方
    v.project(state.camera);
    const x = (v.x * 0.5 + 0.5) * state.renderer.domElement.clientWidth;
    const y = (-v.y * 0.5 + 0.5) * state.renderer.domElement.clientHeight;
    labelEl.style.transform = `translate(-50%,-100%) translate(${x}px,${y}px)`;
    labelEl.style.display = v.z < 1 ? 'block' : 'none';
  }
}

// ============ 相机与景别 ============
import { shotDistance, ASPECTS, aspectRatio } from './core.js';

const SHOT_ORDER = ['远景', '全景', '中景', '近景', '特写'];
let currentAspect = '16:9';
let currentShotSize = '中景';

export function currentShot() { return currentShotSize; }

export function applyShotSize(size) {
  const distance = shotDistance(size);
  const dir = new THREE.Vector3().subVectors(state.camera.position, state.controls.target);
  if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1);
  dir.normalize();
  state.camera.position.copy(state.controls.target).addScaledVector(dir, distance);
  state.controls.update();
  currentShotSize = size;
  updateCameraHud();
}

export function updateCameraHud() {
  const dir = new THREE.Vector3().subVectors(state.camera.position, state.controls.target);
  const horiz = Math.sqrt(dir.x * dir.x + dir.z * dir.z);
  const azimuth = Math.round(Math.atan2(dir.x, dir.z) * 180 / Math.PI);
  const elevation = Math.round(Math.atan2(dir.y, horiz) * 180 / Math.PI);
  document.getElementById('camera-hud').textContent =
    `方位 ${azimuth}° · 俯仰 ${elevation}° · ${currentShotSize}`;
}

// 辅助线框 = 视口内「最大内接框」，按当前画幅计算
export function fitOverlay() {
  const el = document.getElementById('viewport');
  const ratio = aspectRatio(currentAspect);          // 宽/高
  const vr = el.clientWidth / el.clientHeight;
  const overlay = document.getElementById('frame-overlay');
  if (ratio > vr) {                                  // 目标比视口宽 → 限制宽度
    overlay.style.width = '100%';
    overlay.style.height = (100 * vr / ratio) + '%';
  } else {                                           // 目标比视口窄 → 限制高度
    overlay.style.height = '100%';
    overlay.style.width = (100 * ratio / vr) + '%';
  }
  overlay.style.inset = 'auto';
  overlay.style.left = '50%'; overlay.style.top = '50%';
  overlay.style.transform = 'translate(-50%,-50%)';
}

export function applyAspect(aspect) {
  currentAspect = aspect;
  fitOverlay();
}

export function initCameraPanel() {
  const chips = document.getElementById('shot-size-chips');
  for (const size of SHOT_ORDER) {
    const c = document.createElement('span');
    c.className = 'chip' + (size === '中景' ? ' active' : '');
    c.textContent = size;
    c.onclick = () => {
      chips.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
      c.classList.add('active');
      applyShotSize(size);
    };
    chips.appendChild(c);
  }
  const sel = document.getElementById('cam-aspect');
  for (const [name] of ASPECTS) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  }
  sel.value = currentAspect;
  sel.onchange = () => applyAspect(sel.value);
  const guides = document.getElementById('cam-guides');
  // 初始同步：checkbox 默认勾选，但 .on 类未挂 → 进页面就有辅助线
  document.getElementById('frame-overlay').classList.toggle('on', guides.checked);
  guides.onchange = (e) => {
    document.getElementById('frame-overlay').classList.toggle('on', e.target.checked);
  };
  fitOverlay();
  window.addEventListener('resize', fitOverlay);
  applyAspect(currentAspect);
}

// ============ 选中与地面拖拽 ============
export function pickAt(clientX, clientY) {
  const el = state.renderer.domElement;
  const r = el.getBoundingClientRect();
  pointer.x = ((clientX - r.left) / r.width) * 2 - 1;
  pointer.y = -((clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(pointer, state.camera);
  const targets = [];
  for (const { group } of state.mannequins.values()) targets.push(...group.children);
  for (const { group } of state.props.values()) targets.push(...group.children);
  const hits = raycaster.intersectObjects(targets, true);
  if (!hits.length) return null;
  let obj = hits[0].object;
  while (obj.parent && obj.parent.type !== 'Scene') obj = obj.parent;
  for (const [id, { group }] of state.mannequins) if (group === obj) return { kind: 'char', id };
  for (const [id, { group }] of state.props) if (group === obj) return { kind: 'prop', id };
  return null;
}

export function groundPoint(clientX, clientY) {
  const el = state.renderer.domElement;
  const r = el.getBoundingClientRect();
  pointer.x = ((clientX - r.left) / r.width) * 2 - 1;
  pointer.y = -((clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(pointer, state.camera);
  return raycaster.ray.intersectPlane(groundPlane, new THREE.Vector3());
}

export function onViewportDown(e) {
  const hit = pickAt(e.clientX, e.clientY);
  if (!hit) { select(null); return; }
  select(hit);
  state.controls.enabled = false;   // 命中对象时禁用轨道，避免拖拽与转相机打架
  const group = hit.kind === 'char' ? state.mannequins.get(hit.id).group : state.props.get(hit.id).group;
  const p = groundPoint(e.clientX, e.clientY);
  if (p) dragState = { group, offset: p.clone().sub(group.position) };
}

export function onViewportMove(e) {
  if (!dragState) return;
  const p = groundPoint(e.clientX, e.clientY);
  if (!p) return;
  const group = dragState.group;
  group.position.set(p.x - dragState.offset.x, 0, p.z - dragState.offset.z);
  // 数据回写由 data flow 层（Task 8）在 pointerup 时统一做
}

export function onViewportUp() {
  if (dragState) { dragState = null; /* Task 8 接 data flow 钩子 */ }
  state.controls.enabled = true;    // 空处按下不关轨道，这里恢复只是幂等兜底
}

// 接线（本任务的最小闭环；Task 7 替换 select stub，Task 8 替换临时木人为项目驱动）
const el = document.getElementById('viewport');
initScene();
initCameraPanel();
state.controls.addEventListener('change', updateCameraHud);
updateCameraHud();
const charA = newCharacter('A', '主角');
const charB = newCharacter('B', '配角');
charB.position = [1.5, 0, 0]; charB.color = '#3b82f6';
addMannequin(charA);
addMannequin(charB);  // 两个不同色木人
// Task 7 才会实现真正的选中面板，这里先放 stub 防止点击报错
function select(x) { console.log('selected:', x); }
el.addEventListener('pointerdown', onViewportDown);
window.addEventListener('pointermove', onViewportMove);
window.addEventListener('pointerup', onViewportUp);
function tick() {
  requestAnimationFrame(tick);
  state.controls.update();
  updateLabels();
  state.renderer.render(state.scene, state.camera);
}
tick();
window.addEventListener('resize', () => {
  state.camera.aspect = el.clientWidth / el.clientHeight;
  state.camera.updateProjectionMatrix();
  state.renderer.setSize(el.clientWidth, el.clientHeight);
});
