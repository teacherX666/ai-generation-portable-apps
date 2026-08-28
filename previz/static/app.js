import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { JOINTS, jointAngles, POSE_PRESETS, newCharacter, newId, PROP_TYPES, CHARACTER_COLORS } from './core.js';

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
  state.camera.position.set(0, 3.2, 12);
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
  markDirty();
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
  markDirty();
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

// ============ 道具 ============
const _propMat = new THREE.MeshStandardMaterial({ color: 0x64748b, roughness: 0.85 });

function propGeometry(type) {
  const def = PROP_TYPES.find((p) => p.type === type) || PROP_TYPES[0];
  const [kind, params] = def.geo;
  switch (kind) {
    case 'box': return new THREE.BoxGeometry(...params);
    case 'cylinder': return new THREE.CylinderGeometry(...params);
    case 'door': { // 门框：三根柱拼装（顶梁 + 左右柱），params=[w,h,d]
      const [w, h, d] = params;
      const g = new THREE.BoxGeometry(w, d, d);        // 顶梁
      const side = new THREE.BoxGeometry(d, h, d);
      const group = new THREE.Group();
      const top = new THREE.Mesh(g, _propMat); top.position.y = h - d / 2; group.add(top);
      const l = new THREE.Mesh(side, _propMat); l.position.set(-w / 2, h / 2 - d / 2, 0); group.add(l);
      const r = new THREE.Mesh(side, _propMat); r.position.set(w / 2, h / 2 - d / 2, 0); group.add(r);
      return group;
    }
    case 'steps': { // 台阶：三层渐窄
      const [w, h, d] = params;
      const group = new THREE.Group();
      for (let i = 0; i < 3; i++) {
        const s = new THREE.Mesh(new THREE.BoxGeometry(w * (1 - i * 0.2), h, d), _propMat);
        s.position.set(0, h / 2 + i * h, -i * d * 0.8);
        group.add(s);
      }
      return group;
    }
    case 'cone': { // 山形
      const [r, h] = params;
      return new THREE.ConeGeometry(r, h, 4);
    }
    case 'table': { // 桌子：桌面 + 四腿
      const [w, h, d] = params;
      const group = new THREE.Group();
      const top = new THREE.Mesh(new THREE.BoxGeometry(w, 0.06, d), _propMat);
      top.position.y = h; group.add(top);
      const legGeo = new THREE.BoxGeometry(0.07, h, 0.07);
      for (const [x, z] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
        const leg = new THREE.Mesh(legGeo, _propMat);
        leg.position.set(x * (w / 2 - 0.05), h / 2, z * (d / 2 - 0.05));
        group.add(leg);
      }
      return group;
    }
    default: return new THREE.BoxGeometry(...params);
  }
}

export function addProp(data) {
  const group = new THREE.Group();
  group.add(propGeometry(data.type));
  group.position.set(data.position[0], data.position[1], data.position[2]);
  group.rotation.y = (data.rotation_y || 0) * Math.PI / 180;
  const s = data.scale || [1, 1, 1];
  group.scale.set(s[0], s[1], s[2]);
  state.scene.add(group);
  state.props.set(data.id, { group, data });
  return group;
}

// ============ 对象面板 ============
const _JOINT_LABELS = {
  spine: '躯干', neck: '脖子', left_shoulder: '左肩', left_elbow: '左肘', left_wrist: '左腕',
  right_shoulder: '右肩', right_elbow: '右肘', right_wrist: '右腕',
  left_hip: '左髋', left_knee: '左膝', left_ankle: '左踝',
  right_hip: '右髋', right_knee: '右膝', right_ankle: '右踝',
};

export function select(kindAndId) {
  state.selected = kindAndId;
  const detail = document.getElementById('object-detail');
  const empty = document.getElementById('object-empty');
  const isChar = kindAndId && kindAndId.kind === 'char';
  const isProp = kindAndId && kindAndId.kind === 'prop';
  detail.hidden = !kindAndId;
  empty.hidden = !!kindAndId;
  // 关节白球只显示给选中的木人（Task 5 契约：setJointBallsVisible）
  for (const [id, { group }] of state.mannequins) {
    setJointBallsVisible(group, !!(isChar && kindAndId.id === id));
  }
  if (!kindAndId) { renderJointRows(null); return; }
  const rec = isChar ? state.mannequins.get(kindAndId.id) : state.props.get(kindAndId.id);
  if (!rec) return;
  const d = rec.data;
  document.getElementById('obj-type').textContent = isChar ? `人物 ${d.id}` : `道具·${(PROP_TYPES.find((p) => p.type === d.type) || {}).label || d.type}`;
  document.getElementById('obj-label').value = d.label || '';
  document.getElementById('obj-label').disabled = !isChar;   // 道具标签不可改（改动会被丢弃）
  document.getElementById('row-color').hidden = !isChar;
  if (isChar) {
    const sel = document.getElementById('obj-color');
    sel.innerHTML = '';
    for (const c of CHARACTER_COLORS) {
      const o = document.createElement('option');
      o.value = c; o.textContent = c; o.style.color = c;
      sel.appendChild(o);
    }
    sel.value = d.color;
    document.getElementById('pose-row').hidden = false;
    const poseSel = document.getElementById('obj-pose');
    poseSel.innerHTML = '';
    for (const name of Object.keys(POSE_PRESETS)) {
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      poseSel.appendChild(o);
    }
    poseSel.value = d.pose;
    renderJointRows(rec);
  } else {
    document.getElementById('pose-row').hidden = true;
    renderJointRows(null);   // 清掉上一个木人的关节滑块，避免残留面板
  }
  document.getElementById('obj-rotation').value = d.rotation_y || 0;
  document.getElementById('obj-rotation-val').textContent = (d.rotation_y || 0) + '°';
  document.getElementById('obj-scale').value = isChar ? (d.scale || 1) : ((d.scale || [1, 1, 1])[0]);
  document.getElementById('obj-scale-val').textContent = isChar ? String(d.scale || 1) : String((d.scale || [1, 1, 1])[0]);
}

function renderJointRows(rec) {
  const wrap = document.getElementById('joint-rows');
  wrap.innerHTML = '';
  if (!rec) return;
  wrap.innerHTML = '<div class="muted">关节微调（弧度）</div>';
  const d = rec.data;
  for (const j of JOINTS) {
    const row = document.createElement('div');
    row.className = 'joint-row';
    row.innerHTML = `<label>${_JOINT_LABELS[j] || j}</label><input type="range" min="-3" max="3" step="0.02" value="0">`;
    const input = row.querySelector('input');
    const off = (d.joints && d.joints[j]) || [0, 0, 0];
    input.value = off[0];
    input.oninput = () => {
      if (!d.joints) d.joints = {};
      d.joints[j] = [Number(input.value), 0, 0];   // v1 只调 X 轴（前后摆）
      applyPose(rec.group, d.pose, d.joints);
      markDirty();
    };
    wrap.appendChild(row);
  }
}

// 面板事件绑定（接线块调用一次）
export function initObjectPanel() {
  const $ = (id) => document.getElementById(id);
  $('obj-label').onchange = (e) => {
    const s = state.selected;
    if (!s || s.kind !== 'char') return;
    const d = state.mannequins.get(s.id).data;
    d.label = e.target.value;
    state.mannequins.get(s.id).labelEl.textContent = d.label || d.id;
    markDirty();
  };
  $('obj-color').onchange = (e) => {
    const s = state.selected;
    if (!s || s.kind !== 'char') return;
    const rec = state.mannequins.get(s.id);
    rec.data.color = e.target.value;
    // Task 5 重构后 jointBalls Group 已删除、球挂在关节下且初始不可见；
    // 换色遍历：除关节白球（_jointMat）外的所有 mesh 换新材质
    rec.group.traverse((o) => {
      if (o.isMesh && o.material !== _jointMat) {
        o.material = new THREE.MeshStandardMaterial({ color: e.target.value, roughness: 0.75 });
      }
    });
    markDirty();
  };
  $('obj-rotation').oninput = (e) => {
    const s = state.selected;
    if (!s) return;
    const rec = s.kind === 'char' ? state.mannequins.get(s.id) : state.props.get(s.id);
    rec.data.rotation_y = Number(e.target.value);
    rec.group.rotation.y = Number(e.target.value) * Math.PI / 180;
    document.getElementById('obj-rotation-val').textContent = e.target.value + '°';
    markDirty();
  };
  $('obj-scale').oninput = (e) => {
    const s = state.selected;
    if (!s) return;
    const v = Number(e.target.value);
    const rec = s.kind === 'char' ? state.mannequins.get(s.id) : state.props.get(s.id);
    if (s.kind === 'char') {
      rec.data.scale = v;
      rec.group.scale.setScalar(v);
    } else {
      rec.data.scale = [v, v, v];
      rec.group.scale.set(v, v, v);
    }
    document.getElementById('obj-scale-val').textContent = String(v);
    markDirty();
  };
  $('obj-pose').onchange = (e) => {
    const s = state.selected;
    if (!s || s.kind !== 'char') return;
    const rec = state.mannequins.get(s.id);
    rec.data.pose = e.target.value;
    applyPose(rec.group, rec.data.pose, rec.data.joints);
    markDirty();
  };
  $('btn-obj-delete').onclick = () => {
    const s = state.selected;
    if (!s) return;
    if (s.kind === 'char') removeMannequin(s.id);
    else removeProp(s.id);
    select(null);
    markDirty();
  };
  // 面板 tab 切换
  $('ptab-object').onclick = () => switchPanel('object');
  $('ptab-camera').onclick = () => switchPanel('camera');
}

function switchPanel(which) {
  document.getElementById('ptab-object').classList.toggle('active', which === 'object');
  document.getElementById('ptab-camera').classList.toggle('active', which === 'camera');
  document.getElementById('panel-object').hidden = which !== 'object';
  document.getElementById('panel-camera').hidden = which !== 'camera';
}

export function removeMannequin(id) {
  const rec = state.mannequins.get(id);
  if (!rec) return;
  state.scene.remove(rec.group);
  rec.labelEl.remove();
  state.mannequins.delete(id);
}

export function removeProp(id) {
  const rec = state.props.get(id);
  if (!rec) return;
  state.scene.remove(rec.group);
  state.props.delete(id);
}

// markDirty：对象面板改动入口，Task 8 起转调数据流层 setDirty
function markDirty() { setDirty(); }

// ============ 项目数据流 ============
import { emptyProject, newShot, sanitizeFilename, dataUrlToBlob, renderSizeFor } from './core.js';

let project = null;        // 当前项目 JSON（内存态）
let activeShotId = null;
let saveTimer = null;
let lastRender = null;     // Task 9 渲染结果 {dataUrl, shotId, width, height}

// toast：Task 10 实现真实版本，这里先挂占位
window.__previzToast = null;
function toast(msg, isErr) {
  if (window.__previzToast) { window.__previzToast(msg, isErr); return; }
  console.warn('toast:', msg);
}

export function setDirty() {
  const el = document.getElementById('save-state');
  el.textContent = '未保存…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 2000);
}
window.__markDirty = setDirty;

export function currentShotData() {
  return project && project.shots.find((s) => s.id === activeShotId) || null;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

function round3(v) { return Math.round(v * 1000) / 1000; }

function serializeSceneInto(shot) {
  shot.characters = [...state.mannequins.values()].map((rec) => {
    const d = rec.data;
    return {
      ...d,
      position: [round3(rec.group.position.x), round3(rec.group.position.y), round3(rec.group.position.z)],
    };
  });
  shot.props = [...state.props.values()].map((rec) => {
    const d = rec.data;
    return { ...d, position: [round3(rec.group.position.x), 0, round3(rec.group.position.z)] };
  });
  const dir = new THREE.Vector3().subVectors(state.camera.position, state.controls.target);
  const horiz = Math.sqrt(dir.x * dir.x + dir.z * dir.z);
  shot.camera = {
    position: [round3(state.camera.position.x), round3(state.camera.position.y), round3(state.camera.position.z)],
    target: [round3(state.controls.target.x), round3(state.controls.target.y), round3(state.controls.target.z)],
    fov: state.camera.fov,
    shot_size: currentShot(),
    azimuth: Math.round(Math.atan2(dir.x, dir.z) * 180 / Math.PI),
    elevation: Math.round(Math.atan2(dir.y, horiz) * 180 / Math.PI),
  };
  shot.aspect = currentAspect;
}

function clearSceneActors() {
  for (const id of [...state.mannequins.keys()]) removeMannequin(id);
  for (const id of [...state.props.keys()]) removeProp(id);
}

function loadShotIntoScene(shot) {
  clearSceneActors();
  for (const c of shot.characters) {
    const base = newCharacter(c.id, c.label);
    addMannequin({ ...base, ...c });
  }
  for (const p of shot.props) addProp({ ...p });
  state.camera.position.set(...(shot.camera.position || [0, 3.2, 12]));
  state.controls.target.set(...(shot.camera.target || [0, 1, 0]));
  state.camera.fov = shot.camera.fov || 50;
  state.camera.updateProjectionMatrix();
  state.controls.update();
  currentShotSize = shot.camera.shot_size || '中景';
  currentAspect = shot.aspect || '16:9';
  const sel = document.getElementById('cam-aspect');
  if (sel.value !== currentAspect) sel.value = currentAspect;
  applyAspect(currentAspect);
  document.querySelectorAll('#shot-size-chips .chip').forEach((c) =>
    c.classList.toggle('active', c.textContent === currentShotSize));
  updateCameraHud();
  select(null);
  document.getElementById('shot-notes').value = shot.notes || '';
  renderShotList();
}

export function switchShot(id) {
  const old = currentShotData();
  if (old && old.id !== id) serializeSceneInto(old);
  activeShotId = id;
  const shot = currentShotData();
  if (!shot) {   // 新项目还没有镜头：清空场景即可
    clearSceneActors();
    renderShotList();
    return;
  }
  loadShotIntoScene(shot);
}

async function saveNow() {
  clearTimeout(saveTimer);
  const shot = currentShotData();
  if (shot) serializeSceneInto(shot);
  if (!project) return;
  try {
    await api('api/projects/' + project.id, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(project),
    });
    document.getElementById('save-state').textContent = '已保存 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  } catch (err) {
    document.getElementById('save-state').textContent = '保存失败';
    toast('保存失败：' + err.message, true);
  }
}

export function renderShotList() {
  const wrap = document.getElementById('shot-items');
  wrap.innerHTML = '';
  if (!project) return;
  const shots = [...project.shots].sort((a, b) => a.order - b.order);
  for (const shot of shots) {
    const item = document.createElement('div');
    item.className = 'shot-item' + (shot.id === activeShotId ? ' active' : '');
    const img = document.createElement('img');
    img.src = shot.thumbnail
      ? `api/files/${project.id}/${shot.thumbnail}`
      : 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="160" height="52"><rect width="160" height="52" fill="%230f172a"/></svg>';
    item.appendChild(img);
    const name = document.createElement('div');
    name.className = 'shot-name';
    name.textContent = `${shot.order + 1} · ${shot.name}`;
    item.appendChild(name);
    const actions = document.createElement('div');
    actions.className = 'shot-actions';
    for (const [label, fn] of [
      ['复制', () => duplicateShot(shot.id)],
      ['↑', () => moveShot(shot.id, -1)],
      ['↓', () => moveShot(shot.id, 1)],
      ['删', () => deleteShot(shot.id)],
    ]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      actions.appendChild(b);
    }
    item.appendChild(actions);
    item.onclick = () => switchShot(shot.id);
    wrap.appendChild(item);
  }
}

function duplicateShot(id) {
  const src = project.shots.find((s) => s.id === id);
  if (!src) return;
  serializeSceneInto(src);   // 深拷贝前序列化，确保副本拿到最新机位
  const copy = JSON.parse(JSON.stringify(src));
  copy.id = newId('s_');
  copy.name = src.name + ' 副本';
  copy.render = ''; copy.thumbnail = '';
  project.shots.splice(project.shots.indexOf(src) + 1, 0, copy);
  reorder();
  switchShot(copy.id);
  setDirty();
}

function deleteShot(id) {
  if (project.shots.length <= 1) return toast('至少保留一个镜头', true);
  if (!confirm('删除这个镜头？')) return;
  const idx = project.shots.findIndex((s) => s.id === id);
  project.shots.splice(idx, 1);
  reorder();
  switchShot(project.shots[Math.min(idx, project.shots.length - 1)].id);
  setDirty();
}

function moveShot(id, delta) {
  const idx = project.shots.findIndex((s) => s.id === id);
  const target = idx + delta;
  if (target < 0 || target >= project.shots.length) return;
  const [shot] = project.shots.splice(idx, 1);
  project.shots.splice(target, 0, shot);
  reorder();
  renderShotList();
  setDirty();
}

function reorder() {
  project.shots.forEach((s, i) => { s.order = i; });
}

export async function loadProjectList() {
  const { projects } = await api('api/projects');
  const sel = document.getElementById('project-select');
  sel.innerHTML = '';
  for (const p of projects) {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = `${p.name}（${p.shot_count} 镜头）`;
    sel.appendChild(o);
  }
  if (!projects.length) {
    await createProject('未命名项目');
    return;
  }
  const target = project ? project.id : projects[0].id;
  await loadProject(target || projects[0].id);
}

export async function loadProject(id) {
  if (saveTimer) saveNow();   // saveNow 内部会 clearTimeout 并同步序列化；PUT 的 id+body 在 fetch 时已快照，项目被替换后仍安全写旧 id
  try {
    project = await api('api/projects/' + id);
  } catch (err) {
    toast('项目读取失败：' + err.message + '（若数据损坏，服务端已自动备份）', true);
    return loadProjectList();
  }
  const sel = document.getElementById('project-select');
  sel.value = id;
  switchShot(project.shots[0] ? project.shots[0].id : null);
  document.getElementById('save-state').textContent = '已保存';
}

export async function createProject(name) {
  const p = await api('api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  await loadProjectList();
  await loadProject(p.id);
  return p;
}

// ============ 渲染管线 ============
export function captureShot(width, height) {
  if (!state.renderer) return toast('3D 视口不可用', true);
  const renderer = state.renderer, camera = state.camera;
  const oldSize = new THREE.Vector2();
  renderer.getSize(oldSize);
  const oldPR = renderer.getPixelRatio();
  const oldAspect = camera.aspect;
  let hidden;
  try {
    hidden = hideEditHelpers();
    renderer.setPixelRatio(1);
    renderer.setSize(width, height, false);   // false：不改 canvas CSS 尺寸
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.render(state.scene, camera);
    const dataUrl = renderer.domElement.toDataURL('image/png');
    return dataUrl;
  } finally {
    // 任何异常（含超大缓冲分配失败）都必须恢复原状态
    if (hidden) restoreEditHelpers(hidden);
    camera.aspect = oldAspect;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(oldPR);
    renderer.setSize(oldSize.x, oldSize.y, false);
    renderer.render(state.scene, camera);   // 恢复后立刻重渲一帧，避免 setSize 重分配造成的黑闪
  }
}

// 【ADAPT】Task 5 重构后 jointBalls Group 已不存在（球挂在关节下、经 setJointBallsVisible 切换）
function hideEditHelpers() {
  const hidden = {
    balls: [],
    labels: [],
    overlay: document.getElementById('frame-overlay').classList.contains('on'),
  };
  for (const rec of state.mannequins.values()) {
    hidden.balls.push(rec.group.userData.balls.map((b) => b.visible));
    setJointBallsVisible(rec.group, false);
    hidden.labels.push(rec.labelEl.style.display);
    rec.labelEl.style.display = 'none';
  }
  document.getElementById('frame-overlay').classList.remove('on');
  return hidden;
}

function restoreEditHelpers(hidden) {
  let i = 0;
  for (const rec of state.mannequins.values()) {
    const flags = hidden.balls[i] || [];
    rec.group.userData.balls.forEach((b, j) => { b.visible = !!(flags[j] ?? true); });
    rec.labelEl.style.display = hidden.labels[i] || '';
    i++;
  }
  if (hidden.overlay) document.getElementById('frame-overlay').classList.add('on');
}

function makeThumbnail(dataUrl, w = 320) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const h = Math.max(1, Math.round(w * img.height / img.width));
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(img, 0, 0, w, h);
      resolve(c.toDataURL('image/jpeg', 0.82));
    };
    img.onerror = () => resolve(null);   // 加载失败不卡死「渲染中…」，调用方跳过缩略图
    img.src = dataUrl;
  });
}

export async function renderShot() {
  const shot = currentShotData();
  if (!shot) return toast('先选择一个镜头', true);
  const quality = document.getElementById('cam-quality').value;
  const customW = Number(document.getElementById('cam-custom-width').value) || 0;
  const { width, height } = renderSizeFor(currentAspect, quality, customW);
  toast('渲染中…');
  const dataUrl = captureShot(width, height);
  if (!dataUrl) return;   // captureShot 内部已 toast 提示
  const thumb = await makeThumbnail(dataUrl);
  document.getElementById('render-img').src = dataUrl;
  document.getElementById('render-title').textContent = `${shot.order + 1} · ${shot.name} · ${width}×${height}`;
  document.getElementById('render-status').textContent = '';
  document.getElementById('render-modal').hidden = false;
  lastRender = { dataUrl, shotId: shot.id, width, height };
  try {
    const fd = new FormData();
    fd.append('shot_id', shot.id);
    fd.append('render', dataUrlToBlob(dataUrl), 'render.png');
    if (thumb) fd.append('thumb', dataUrlToBlob(thumb), 'thumb.png');
    const res = await api(`api/projects/${project.id}/shots/${shot.id}/render`, { method: 'POST', body: fd });
    shot.render = String(res.render_url).split('/').pop();
    shot.thumbnail = res.thumbnail || shot.thumbnail;
    document.getElementById('render-status').textContent = '已存档 ✓';
    renderShotList();
  } catch (err) {
    document.getElementById('render-status').textContent = '存档失败：' + err.message + '（预览与下载仍可用）';
  }
}
window.__renderShot = renderShot;

export async function downloadRender() {
  if (!lastRender) return toast('先渲染一张快照', true);
  const shot = project.shots.find((s) => s.id === lastRender.shotId) || currentShotData();
  const name = shot ? sanitizeFilename(`${shot.order + 1}-${shot.name}`) : 'shot';
  const a = document.createElement('a');
  a.href = lastRender.dataUrl;
  a.download = `${name}.png`;
  a.click();
}
window.__downloadRender = downloadRender;

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
  if (dragState) {
    const shot = currentShotData();
    if (shot) serializeSceneInto(shot);
    dragState = null;
    setDirty();
  }
  state.controls.enabled = true;    // 空处按下不关轨道，这里恢复只是幂等兜底
}

// 页面关闭前强制保存。未用 keepalive（64KB body 上限对多镜头项目 JSON 可能不够）——
// 通常 2s 防抖已经存过，这里是兜底，浏览器可能中止该请求，损失上限 = 最后 2s 编辑
window.addEventListener('pagehide', () => {
  if (saveTimer) { clearTimeout(saveTimer); saveNow(); }
});

// 接线（Task 8 起：项目驱动数据流）
const _glProbe = document.createElement('canvas');
if (!_glProbe.getContext('webgl2')) {
  const el = document.getElementById('webgl-error');
  el.hidden = false;
  el.innerHTML = `<div><h2>无法使用 3D 视口</h2>
    <p>当前浏览器不支持 WebGL2（可能被禁用或显卡驱动问题）。</p>
    <p>建议：换用最新版 Chrome / Edge，或在系统设置里开启「硬件加速」。</p></div>`;
} else {
  const el = document.getElementById('viewport');
  initScene();
  initCameraPanel();
  initObjectPanel();
  switchPanel('object');
  state.controls.addEventListener('change', () => { updateCameraHud(); markDirty(); });
  updateCameraHud();
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
  // 项目加载入口（WebGL 检测已前置到接线块顶部）
  function initApp() {
    loadProjectList().catch((err) => toast('加载项目列表失败：' + err.message, true));
  }
  initApp();
  // 相机面板剩余接线：选「自定义宽度」才显示宽度输入行
  document.getElementById('cam-quality').onchange = (e) => {
    document.getElementById('custom-width-row').hidden = e.target.value !== 'custom';
  };
}
// 顶部工具栏：项目 / 镜头 / 导出
document.getElementById('project-select').onchange = (e) => loadProject(e.target.value);
document.getElementById('btn-project-new').onclick = () => createProject('未命名项目');
document.getElementById('btn-project-delete').onclick = async () => {
  if (!project) return;
  if (!confirm(`删除项目「${project.name}」？不可恢复`)) return;
  await api('api/projects/' + project.id, { method: 'DELETE' });
  toast('项目已删除');
  project = null;
  await loadProjectList();
};
document.getElementById('btn-shot-new').onclick = () => {
  const shot = newShot('镜头' + (project.shots.length + 1), project.shots.length);
  project.shots.push(shot);
  switchShot(shot.id);
  setDirty();
};
document.getElementById('shot-notes').onchange = (e) => {
  const shot = currentShotData();
  if (shot) { shot.notes = e.target.value; setDirty(); }
};
document.getElementById('btn-export').onclick = async () => {
  if (!project) return;
  await saveNow();   // 导出前冲刷防抖窗口内的编辑（相机/景别/画幅等）
  const shots = project.shots.filter((s) => s.render);
  if (!shots.length) return toast('还没有渲染过快照，先渲染再导出', true);
  await _blobDownload(`api/projects/${project.id}/export.zip`, sanitizeFilename(project.name) + '-分镜快照.zip');
};

async function _blobDownload(url, filename) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) {
    toast('下载失败：' + err.message, true);
  }
}
// 对象工具
document.getElementById('btn-add-char').onclick = () => {
  const c = newCharacter(String.fromCharCode(65 + state.mannequins.size), '人物' + (state.mannequins.size + 1));
  c.id = 'char-' + newId('');
  c.color = CHARACTER_COLORS[state.mannequins.size % CHARACTER_COLORS.length];
  addMannequin(c);
  markDirty();
};
document.getElementById('btn-add-prop').onclick = () => {
  addProp({ id: newId('prop-'), type: 'box', position: [0, 0, 0], rotation_y: 0, scale: [1, 1, 1] });
  markDirty();
};
// Task 9/10 挂载点
document.getElementById('btn-render').onclick = () => window.__renderShot && window.__renderShot();
document.getElementById('btn-to-canvas').onclick = () => window.__toCanvas && window.__toCanvas();
document.getElementById('btn-render-download').onclick = () => window.__downloadRender && window.__downloadRender();
document.getElementById('btn-render-canvas').onclick = () => window.__sendRender && window.__sendRender();
document.getElementById('btn-render-close').onclick = () => {
  document.getElementById('render-modal').hidden = true;
};
