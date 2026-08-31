import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { JOINTS, jointAngles, POSE_PRESETS, newCharacter, newId, PROP_TYPES, CHARACTER_COLORS, obbPenetration } from './core.js';

// ============ 全局状态 ============
const state = {
  scene: null, camera: null, renderer: null, controls: null,
  mannequins: new Map(),      // characterId -> {group, joints, labelEl, data}
  props: new Map(),           // propId -> {group, data, footprint}
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
  // 实体地面（渲染成图保留，提供落点与深度参照；编辑态网格线叠加其上）
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(200, 200),
    new THREE.MeshStandardMaterial({ color: 0x182338, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  state.scene.add(ground);
  const grid = new THREE.GridHelper(20, 20, 0x475569, 0x1e293b);
  state.grid = grid;
  state.scene.add(grid);
  // 地平线参考：编辑辅助件（渲染时随 hideEditHelpers 隐藏），
  // 取网格线同色 0x334155 且更细，避免与灰色道具混淆
  const horizon = new THREE.Mesh(
    new THREE.BoxGeometry(0.03, 0.03, 20),
    new THREE.MeshBasicMaterial({ color: 0x334155 }));
  horizon.position.set(0, 1.4, -9);
  state.horizon = horizon;
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
  const neck = new THREE.Group(); neck.position.y = 0.12; chest.add(neck);   // 世界 y≈1.45
  const head = part(new THREE.SphereGeometry(0.14, 20, 20), mat);
  head.position.y = 0.16; neck.add(head);                                    // 头中心≈1.61，头顶≈1.75
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
    const shoulderJ = new THREE.Group(); shoulderJ.position.set(side * 0.24, 0.10, 0); chest.add(shoulderJ);  // 世界 y≈1.43，低于头底
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
  // 存进 map 的对象必须与返回值是同一引用：resolveCollisions 用 other === rec 跳过自身，
  // 若返回另一个字面量，木人会与自己碰撞（0.4+0.4 穿透，被推飞 8×0.801m）
  const rec = { group, data, labelEl };
  state.mannequins.set(data.id, rec);
  return rec;
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
    // box/cylinder/cone 是单几何体：包成 Mesh 返回（addProp 的 group.add 需要 Object3D）
    case 'box': return new THREE.Mesh(new THREE.BoxGeometry(...params), _propMat);
    case 'cylinder': return new THREE.Mesh(new THREE.CylinderGeometry(...params), _propMat);
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
      return new THREE.Mesh(new THREE.ConeGeometry(r, h, 4), _propMat);
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
    case 'bench': { // 长椅：座面 + 两腿，params=[w, h, d]
      const [w, h, d] = params;
      const group = new THREE.Group();
      const seat = new THREE.Mesh(new THREE.BoxGeometry(w, 0.08, d), _propMat);
      seat.position.y = h; group.add(seat);
      const legGeo = new THREE.BoxGeometry(0.08, h, d * 0.9);
      for (const x of [-1, 1]) {
        const leg = new THREE.Mesh(legGeo, _propMat);
        leg.position.set(x * (w / 2 - 0.08), h / 2, 0);
        group.add(leg);
      }
      return group;
    }
    case 'tree': { // 树木：树干圆柱 + 树冠圆锥，params=[总高]
      const [h] = params;
      const group = new THREE.Group();
      const trunk = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.16, h * 0.5, 10), _propMat);
      trunk.position.y = h * 0.25; group.add(trunk);
      const foliage = new THREE.Mesh(new THREE.ConeGeometry(h * 0.45, h * 0.6, 10), _propMat);
      foliage.position.y = h * 0.5 + h * 0.3; group.add(foliage);
      return group;
    }
    default: return new THREE.BoxGeometry(...params);
  }
}

export function addProp(data) {
  const group = new THREE.Group();
  group.add(propGeometry(data.type));
  const box = new THREE.Box3().setFromObject(group);   // 局部坐标（group 尚未变换）
  const size = new THREE.Vector3(), center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const rec = { group, data, footprint: { hx: size.x / 2, hz: size.z / 2, cx: center.x, cz: center.z } };
  group.position.set(data.position[0], data.position[1], data.position[2]);
  group.rotation.y = (data.rotation_y || 0) * Math.PI / 180;
  const s = data.scale || [1, 1, 1];
  group.scale.set(s[0], s[1], s[2]);
  state.scene.add(group);
  state.props.set(data.id, rec);
  return rec;   // 注意：返回值是 rec（原为 group），调用方按 rec 使用（group 在 rec.group）
}

// ============ 碰撞体积（XZ 平面 OBB） ============
// 道具：addProp 构建时测局部 XZ 半宽与中心偏移（几何在 group 局部空间、未加变换前）
// 木人：固定半径 0.4（×scale），无中心偏移
// 门框：返回 null —— 顶梁底沿高于木人头顶，门洞本应可通行；碰撞豁免
function footprintOf(rec) {
  const ry = rec.group.rotation.y;
  if (rec.data && rec.data.type === 'door') return null;   // 门框不参与碰撞
  if (!rec.footprint) {   // 木人路径：数据流层没有 footprint，动态生成
    return { x: rec.group.position.x, z: rec.group.position.z,
             hx: 0.4 * Math.abs(rec.group.scale.x), hz: 0.4 * Math.abs(rec.group.scale.x),
             ry };
  }
  // 偏移向量先缩放后按 three.js Y 旋转（R_y: x'=x·c+z·s, z'=−x·s+z·c——
  // 与标准 2D 旋转互为镜像，实测 R_y(90°)·(0,0,−0.24)=(−0.24,0,0)）
  const sx = Math.abs(rec.group.scale.x), sz = Math.abs(rec.group.scale.z);
  const ox = (rec.footprint.cx || 0) * sx, oz = (rec.footprint.cz || 0) * sz;
  const cos = Math.cos(ry), sin = Math.sin(ry);
  return { x: rec.group.position.x + ox * cos + oz * sin,
           z: rec.group.position.z - ox * sin + oz * cos,
           hx: rec.footprint.hx * sx, hz: rec.footprint.hz * sz, ry };
}

function allActorRecs() {
  return [...state.mannequins.values(), ...state.props.values()];
}

export function resolveCollisions(rec, maxSteps = 8) {
  // 最小穿透轴一次性推出（多对象链式时迭代 ≤maxSteps 次）
  for (let step = 0; step < maxSteps; step++) {
    const me = footprintOf(rec);
    if (!me) return;                    // 自身无碰撞体（门框）→ 直接跳过
    let best = null;
    for (const other of allActorRecs()) {
      if (other === rec) continue;
      const of = footprintOf(other);
      if (!of) continue;                // 对方无碰撞体（门框）→ 不参与
      const r = obbPenetration(me, of);
      if (r && (!best || r.pen < best.pen)) best = r;
    }
    if (!best) return;
    rec.group.position.x += best.ux * best.sign * (best.pen + 0.001);
    rec.group.position.z += best.uz * best.sign * (best.pen + 0.001);
  }
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
    resolveCollisions(rec);   // 旋转改变足迹朝向，可能重新重叠
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
    resolveCollisions(rec);   // 缩放改变足迹尺寸，可能重新重叠
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

// ============ toast ============
let toastTimer = null;
function toast(msg, isErr) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('err', !!isErr);
  el.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = 'none'; }, 4000);
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
      ['改名', () => renameShot(shot.id)],
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

function renameShot(id) {
  const shot = project.shots.find((s) => s.id === id);
  if (!shot) return;
  const name = prompt('新镜头名：', shot.name);
  if (!name || !name.trim()) return;
  shot.name = name.trim().slice(0, 60);
  renderShotList();
  setDirty();
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
  const { projects, broken } = await api('api/projects');
  if (broken) toast(`有 ${broken} 个项目数据损坏，服务端已备份为 .broken 文件（可人工恢复）`, true);
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
    horizon: state.horizon ? state.horizon.visible : false,
    grid: state.grid ? state.grid.visible : false,
  };
  if (state.horizon) state.horizon.visible = false;
  if (state.grid) state.grid.visible = false;   // 成图无任何线条
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
  if (state.horizon) state.horizon.visible = hidden.horizon;
  if (state.grid) state.grid.visible = hidden.grid;
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
  // 先冲刷防抖保存：镜头可能刚创建、服务端项目里还没有它（否则存档 POST 会 400 镜头不存在）
  await saveNow();
  const quality = document.getElementById('cam-quality').value;
  const customW = Number(document.getElementById('cam-custom-width').value) || 0;
  const { width, height } = renderSizeFor(currentAspect, quality, customW);
  toast('渲染中…');
  const dataUrl = captureShot(width, height);
  if (!dataUrl) return;   // captureShot 内部已 toast 提示
  const thumb = await makeThumbnail(dataUrl);
  openAnnoModal({
    dataUrl,
    filenameBase: sanitizeFilename(`${shot.order + 1}-${shot.name}`),
    width, height,
    title: `${shot.order + 1} · ${shot.name} · ${width}×${height}`,
    srcType: 'image/png',
  });
  try {
    const fd = new FormData();
    fd.append('shot_id', shot.id);
    fd.append('render', dataUrlToBlob(dataUrl), 'render.png');
    if (thumb) fd.append('thumb', dataUrlToBlob(thumb), 'thumb.png');
    const res = await api(`api/projects/${project.id}/render`, { method: 'POST', body: fd });
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
  if (!annoSource) return toast('先渲染或上传一张图片', true);
  const a = document.createElement('a');
  a.href = annoSource.dataUrl;
  a.download = `${annoSource.filenameBase}.${annoSource.srcType === 'image/jpeg' ? 'jpg' : 'png'}`;
  a.click();
}
window.__downloadRender = downloadRender;

// ============ 图片标注（打框 + 标记） ============
const anno = {          // 当前快照的标注状态
  tool: 'rect', color: '#ef4444', items: [],   // items: {type,color,x,y,w,h,text}
  svg: null, active: false, drawing: null,     // drawing: {x0,y0,el}
  baseUrl: null,        // 渲染原始 data URL（合并前的底图）
};
let annoSource = null;  // 当前标注会话：{ dataUrl, filenameBase, srcType, width, height, title }

function annoReset(baseUrl) {
  anno.items = [];
  anno.baseUrl = baseUrl;
  anno.drawing = null;
  anno.svg.innerHTML = '';
}

function annoCoords(evt) {
  const r = anno.svg.getBoundingClientRect();
  const vw = anno.svg.viewBox.baseVal.width, vh = anno.svg.viewBox.baseVal.height;
  return { x: (evt.clientX - r.left) / r.width * vw, y: (evt.clientY - r.top) / r.height * vh };
}

export function initAnno() {
  anno.svg = document.getElementById('anno-layer');
  const $ = (id) => document.getElementById(id);
  $('btn-render-anno').onclick = () => {
    anno.active = !anno.active;
    $('anno-tools').hidden = !anno.active;
    $('anno-layer').style.display = anno.active ? 'block' : 'none';
    if (!anno.baseUrl && annoSource) annoReset(annoSource.dataUrl);
  };
  $('anno-rect').onclick = () => { anno.tool = 'rect'; $('anno-rect').classList.add('active'); $('anno-text').classList.remove('active'); };
  $('anno-text').onclick = () => { anno.tool = 'text'; $('anno-text').classList.add('active'); $('anno-rect').classList.remove('active'); };
  document.querySelectorAll('#anno-colors .swatch').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('#anno-colors .swatch').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      anno.color = b.dataset.color;
    };
  });
  $('anno-undo').onclick = () => { anno.items.pop(); renderAnno(); };
  $('anno-clear').onclick = () => { anno.items = []; renderAnno(); };
  $('anno-bake').onclick = () => bakeAnno();
  // SVG 指针事件
  anno.svg.addEventListener('pointerdown', (evt) => {
    if (!anno.active) return;
    if (anno.drawing) {                    // 上一笔未提交（指针在窗口外松开等）：丢弃残留临时矩形
      anno.drawing.el.remove();
      anno.drawing = null;
    }
    if (evt.button !== 0) return;          // 仅左键开始绘制
    const p = annoCoords(evt);
    if (anno.tool === 'rect') {
      const el = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      el.setAttribute('fill', anno.color);
      el.setAttribute('fill-opacity', '0.12');
      el.setAttribute('stroke', anno.color);
      anno.svg.appendChild(el);
      anno.drawing = { x0: p.x, y0: p.y, el };
    } else {
      const label = prompt('标注文字：');
      if (!label) return;
      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', p.x); t.setAttribute('y', p.y);
      t.setAttribute('font-size', Math.max(16, anno.svg.viewBox.baseVal.width / 36));
      t.setAttribute('fill', anno.color);
      t.textContent = label.trim().slice(0, 40);
      anno.svg.appendChild(t);
      anno.items.push({ type: 'text', color: anno.color, x: p.x, y: p.y, text: t.textContent });
    }
  });
  window.addEventListener('pointermove', (evt) => {
    if (!anno.drawing) return;
    const p = annoCoords(evt);
    const x = Math.min(p.x, anno.drawing.x0), y = Math.min(p.y, anno.drawing.y0);
    const w = Math.abs(p.x - anno.drawing.x0), h = Math.abs(p.y - anno.drawing.y0);
    anno.drawing.el.setAttribute('x', x); anno.drawing.el.setAttribute('y', y);
    anno.drawing.el.setAttribute('width', w); anno.drawing.el.setAttribute('height', h);
  });
  window.addEventListener('pointerup', () => annoEndDraw());
  window.addEventListener('pointercancel', () => annoEndDraw());
}

// 收尾一笔绘制：提交或丢弃。pointerup / pointercancel 共用——
// 指针在浏览器窗口外松开时 pointerup 不触发，靠 pointercancel 兜底防幽灵矩形
function annoEndDraw() {
  if (!anno.drawing) return;
  const el = anno.drawing.el;
  const w = +el.getAttribute('width'), h = +el.getAttribute('height');
  if (w === 0 && h === 0) {   // 纯单击（未拖拽）：移除临时矩形，不入栈
    el.remove();
  } else {
    anno.items.push({ type: 'rect', color: el.getAttribute('stroke'),
                      x: +el.getAttribute('x'), y: +el.getAttribute('y'),
                      w, h });
  }
  anno.drawing = null;
}

function renderAnno() {
  anno.svg.innerHTML = '';
  for (const it of anno.items) {
    if (it.type === 'rect') {
      const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      r.setAttribute('x', it.x); r.setAttribute('y', it.y);
      r.setAttribute('width', it.w); r.setAttribute('height', it.h);
      r.setAttribute('fill', it.color); r.setAttribute('fill-opacity', '0.12');
      r.setAttribute('stroke', it.color);
      anno.svg.appendChild(r);
    } else {
      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', it.x); t.setAttribute('y', it.y);
      t.setAttribute('font-size', Math.max(16, anno.svg.viewBox.baseVal.width / 36));
      t.setAttribute('fill', it.color);
      t.textContent = it.text;
      anno.svg.appendChild(t);
    }
  }
}

function bakeAnno() {
  if (!anno.baseUrl) return;
  if (!anno.items.length) return toast('还没有标注', true);
  const img = new Image();
  img.onload = () => {
    try {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const sx = img.naturalWidth / anno.svg.viewBox.baseVal.width;
      const sy = img.naturalHeight / anno.svg.viewBox.baseVal.height;
      for (const it of anno.items) {
        ctx.fillStyle = it.color;
        ctx.lineWidth = Math.max(3, img.naturalWidth / 400);
        if (it.type === 'rect') {
          ctx.strokeStyle = it.color;
          ctx.globalAlpha = 0.12; ctx.fillRect(it.x * sx, it.y * sy, it.w * sx, it.h * sy);
          ctx.globalAlpha = 1; ctx.strokeRect(it.x * sx, it.y * sy, it.w * sx, it.h * sy);
        } else {
          const size = Math.max(16, img.naturalWidth / 36);
          ctx.font = `700 ${size}px -apple-system, "PingFang SC", sans-serif`;
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = size / 5; ctx.strokeText(it.text, it.x * sx, it.y * sy);
          ctx.fillText(it.text, it.x * sx, it.y * sy);
        }
      }
      ctx.globalAlpha = 1;
      // 烘焙格式跟随源图：JPEG 照片用 JPEG 0.92，避免 PNG 膨胀 3-10 倍导致送画布 413
      const fmt = (annoSource && annoSource.srcType === 'image/jpeg') ? 'image/jpeg' : 'image/png';
      const baked = fmt === 'image/jpeg' ? c.toDataURL('image/jpeg', 0.92) : c.toDataURL('image/png');
      anno.baseUrl = baked;              // 合并结果成为新底图
      anno.items = []; anno.svg.innerHTML = '';
      annoSource.dataUrl = baked;        // 下载/送画布走标注版
      document.getElementById('render-img').src = baked;
      toast('标注已合并进图片 ✓');
    } catch (err) {
      toast('合并失败：图片过大或格式不支持', true);
    }
  };
  img.src = anno.baseUrl;
}

// 打开标注模态（渲染快照 / 上传图共用）：每次都是干净的标注会话
function openAnnoModal(src) {
  document.getElementById('render-img').src = src.dataUrl;
  anno.svg.setAttribute('viewBox', `0 0 ${src.width} ${src.height}`);
  annoReset(src.dataUrl);
  annoSource = src;
  // 关闭标注模式（每次打开都是干净的：默认矩形框工具 + 工具条隐藏）
  anno.active = false;
  anno.tool = 'rect';
  document.getElementById('anno-rect').classList.add('active');
  document.getElementById('anno-text').classList.remove('active');
  document.getElementById('anno-tools').hidden = true;
  document.getElementById('anno-layer').style.display = 'none';
  document.getElementById('render-title').textContent = src.title;
  document.getElementById('render-status').textContent = '';
  document.getElementById('render-modal').hidden = false;
}

// ============ 送画布 ============
export async function sendRenderToCanvas() {
  if (!annoSource) return toast('先渲染或上传一张图片', true);
  const filename = `${annoSource.filenameBase}.${annoSource.srcType === 'image/jpeg' ? 'jpg' : 'png'}`;
  const fd = new FormData();
  fd.append('file', dataUrlToBlob(annoSource.dataUrl), filename);
  fd.append('media_type', 'image');
  fd.append('kind', 'reference');
  try {
    const res = await fetch('/infinite-canvas/api/v1/assets', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).message || msg; } catch (_) { /* 非 JSON 响应 */ }
      throw new Error(msg);
    }
    document.getElementById('render-status').textContent = '已送画布 ✓';
    toast('已送画布 ✓ 去「无限画布」标签页查看');
  } catch (err) {
    document.getElementById('render-status').textContent = '送画布失败：' + err.message;
    toast('送画布失败：' + err.message + '（快照已存档，可下载）', true);
  }
}
window.__sendRender = sendRenderToCanvas;
window.__toCanvas = () => {
  // 顶栏按钮：直接送当前标注会话的图；没有则提示
  if (!annoSource) return toast('先渲染或上传一张图片', true);
  sendRenderToCanvas();
};

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
  const rec = hit.kind === 'char' ? state.mannequins.get(hit.id) : state.props.get(hit.id);
  const group = rec.group;
  const p = groundPoint(e.clientX, e.clientY);
  if (p) dragState = { group, rec, offset: p.clone().sub(group.position) };
}

export function onViewportMove(e) {
  if (!dragState) return;
  const p = groundPoint(e.clientX, e.clientY);
  if (!p) return;
  const group = dragState.group;
  group.position.set(p.x - dragState.offset.x, 0, p.z - dragState.offset.z);
  resolveCollisions(dragState.rec);
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
document.getElementById('btn-project-rename').onclick = () => {
  if (!project) return;
  const name = prompt('新项目名：', project.name);
  if (!name || !name.trim()) return;
  project.name = name.trim().slice(0, 60);
  setDirty();
  saveNow();
  loadProjectList();   // 刷新下拉文案
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
// 道具类型选择面板
function initPropPicker() {
  const picker = document.getElementById('prop-picker');
  for (const p of PROP_TYPES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = p.label;
    b.onclick = () => {
      const rec = addProp({ id: newId('prop-'), type: p.type, position: [0, 0, 0], rotation_y: 0, scale: [1, 1, 1] });
      resolveCollisions(rec);
      markDirty();
      picker.hidden = true;
    };
    picker.appendChild(b);
  }
  // 点外部关闭
  document.addEventListener('click', (e) => {
    if (picker.hidden) return;
    if (!picker.contains(e.target) && e.target !== document.getElementById('btn-add-prop')) {
      picker.hidden = true;
    }
  });
  // ESC 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !picker.hidden) picker.hidden = true;
  });
}
// 对象工具
document.getElementById('btn-add-char').onclick = () => {
  const c = newCharacter(String.fromCharCode(65 + state.mannequins.size), '人物' + (state.mannequins.size + 1));
  c.id = 'char-' + newId('');
  c.color = CHARACTER_COLORS[state.mannequins.size % CHARACTER_COLORS.length];
  const rec = addMannequin(c);
  resolveCollisions(rec);
  markDirty();
};
document.getElementById('btn-add-prop').onclick = () => {
  const picker = document.getElementById('prop-picker');
  if (picker.hidden) {   // 打开时锚定到按钮下方——锚右缘：400px 网格从右往左展开，窄视口不溢出
    const rect = document.getElementById('btn-add-prop').getBoundingClientRect();
    picker.style.left = '';
    picker.style.right = (window.innerWidth - rect.right) + 'px';
    picker.style.top = (rect.bottom + 6) + 'px';
  }
  picker.hidden = !picker.hidden;
};
initPropPicker();
// 图片标注上传入口（任意图片进标注画板）
document.getElementById('btn-anno-image').onclick = () => {
  document.getElementById('anno-file').click();
};
document.getElementById('anno-file').onchange = (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = '';   // 允许重复选同一文件
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) return toast('图片超过 20MB', true);
  if (!file.type.startsWith('image/')) return toast('请选择图片文件', true);
  if (file.type === 'image/svg+xml') return toast('暂不支持 SVG 图片', true);
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      // 尺寸守卫：烘焙到 canvas 需要内存，超限直接拒绝
      if (img.naturalWidth > 8192 || img.naturalHeight > 8192
          || img.naturalWidth * img.naturalHeight > 64e6) {
        return toast('图片尺寸过大（最长边 ≤8192 且总像素 ≤6400 万）', true);
      }
      openAnnoModal({
        dataUrl: reader.result,
        filenameBase: sanitizeFilename(file.name.replace(/\.[^.]+$/, '')) || '标注图',
        width: img.naturalWidth,
        height: img.naturalHeight,
        title: `${file.name} · ${img.naturalWidth}×${img.naturalHeight}`,
        srcType: file.type,
      });
    };
    img.onerror = () => toast('图片读取失败', true);
    img.src = reader.result;
  };
  reader.onerror = () => toast('文件读取失败', true);
  reader.readAsDataURL(file);
};
// Task 9/10 挂载点
document.getElementById('btn-render').onclick = () => window.__renderShot && window.__renderShot();
document.getElementById('btn-to-canvas').onclick = () => window.__toCanvas && window.__toCanvas();
document.getElementById('btn-render-download').onclick = () => window.__downloadRender && window.__downloadRender();
document.getElementById('btn-render-canvas').onclick = () => window.__sendRender && window.__sendRender();
document.getElementById('btn-render-close').onclick = () => {
  document.getElementById('render-modal').hidden = true;
  anno.active = false;
  document.getElementById('anno-tools').hidden = true;
  document.getElementById('anno-layer').style.display = 'none';
};
initAnno();
// 调试钩子：headless 冒烟验证与排障用
window.__previzDebug = state;
