// core.js — 纯逻辑模块：无 DOM、无 three 依赖；node 单测与浏览器共用。
// 所有 3D 常量与默认值集中在这里，改参数只动这一个文件。

// —— 关节（顺序即骨骼层级参考，数值 [x,y,z] 欧拉角，radians）——
export const JOINTS = [
  'spine', 'neck',
  'left_shoulder', 'left_elbow', 'left_wrist',
  'right_shoulder', 'right_elbow', 'right_wrist',
  'left_hip', 'left_knee', 'left_ankle',
  'right_hip', 'right_knee', 'right_ankle',
];

const Z = [0, 0, 0];
export const POSE_PRESETS = {
  // 起始值（实施 Task 5 时对照视口微调符号与幅度，但结构不许变）
  站立:  { spine: Z, neck: Z, left_shoulder: Z, left_elbow: Z, left_wrist: Z,
          right_shoulder: Z, right_elbow: Z, right_wrist: Z,
          left_hip: Z, left_knee: Z, left_ankle: Z,
          right_hip: Z, right_knee: Z, right_ankle: Z },
  坐姿:  { spine: [-0.12, 0, 0], neck: [0.08, 0, 0],
          left_shoulder: Z, left_elbow: [-0.5, 0, 0], left_wrist: Z,
          right_shoulder: Z, right_elbow: [-0.5, 0, 0], right_wrist: Z,
          left_hip: [-1.35, 0, 0], left_knee: [1.5, 0, 0], left_ankle: [-0.15, 0, 0],
          right_hip: [-1.35, 0, 0], right_knee: [1.5, 0, 0], right_ankle: [-0.15, 0, 0] },
  挥手:  { spine: Z, neck: Z, left_shoulder: Z, left_elbow: Z, left_wrist: Z,
          right_shoulder: [0, 0, 2.4], right_elbow: [0, 0, 0.4], right_wrist: Z,
          left_hip: Z, left_knee: Z, left_ankle: Z,
          right_hip: Z, right_knee: Z, right_ankle: Z },
  指向:  { spine: Z, neck: Z, left_shoulder: Z, left_elbow: Z, left_wrist: Z,
          right_shoulder: [2.3, 0, 0.4], right_elbow: [-0.15, 0, 0], right_wrist: Z,
          left_hip: Z, left_knee: Z, left_ankle: Z,
          right_hip: Z, right_knee: Z, right_ankle: Z },
  蹲下:  { spine: [0.25, 0, 0], neck: [0.1, 0, 0],
          left_shoulder: [0.4, 0, 0], left_elbow: [-0.9, 0, 0], left_wrist: Z,
          right_shoulder: [0.4, 0, 0], right_elbow: [-0.9, 0, 0], right_wrist: Z,
          left_hip: [-1.9, 0, 0], left_knee: [1.9, 0, 0], left_ankle: [0.3, 0, 0],
          right_hip: [-1.9, 0, 0], right_knee: [1.9, 0, 0], right_ankle: [0.3, 0, 0] },
  行走:  { spine: [0.06, 0, 0], neck: Z,
          left_shoulder: [0.5, 0, 0], left_elbow: [-0.3, 0, 0], left_wrist: Z,
          right_shoulder: [-0.5, 0, 0], right_elbow: [-0.3, 0, 0], right_wrist: Z,
          left_hip: [-0.55, 0, 0], left_knee: [0.5, 0, 0], left_ankle: Z,
          right_hip: [0.55, 0, 0], right_knee: [-0.4, 0, 0], right_ankle: Z },
};

export function jointAngles(pose, offsets) {
  const base = POSE_PRESETS[pose] || POSE_PRESETS['站立'];
  const out = {};
  for (const j of JOINTS) {
    const b = base[j] || Z;
    const o = (offsets && offsets[j]) || Z;
    out[j] = [b[0] + o[0], b[1] + o[1], b[2] + o[2]];
  }
  return out;
}

// —— 画幅（与导演台同款 9 档）——
export const ASPECTS = [
  ['1:1', 1], ['4:3', 4 / 3], ['3:4', 3 / 4], ['16:9', 16 / 9], ['9:16', 9 / 16],
  ['3:2', 3 / 2], ['2:3', 2 / 3], ['21:9', 21 / 9], ['9:21', 9 / 21],
];
export function aspectRatio(name) {
  const hit = ASPECTS.find(([n]) => n === name);
  return hit ? hit[1] : 16 / 9;
}

// —— 景别五档（相机距离，米；1.8m 身高、fov 50° 的起始值）——
export const SHOT_SIZES = { 远景: 12, 全景: 6, 中景: 3, 近景: 1.6, 特写: 0.8 };
export function shotDistance(size) {
  return SHOT_SIZES[size] !== undefined ? SHOT_SIZES[size] : SHOT_SIZES['中景'];
}

// —— 道具（type → 几何参数，Task 7 使用）——
export const PROP_TYPES = [
  { type: 'box', label: '箱子', geo: ['box', [0.6, 0.6, 0.6]] },
  { type: 'cylinder', label: '圆柱', geo: ['cylinder', [0.25, 0.25, 0.9]] },
  { type: 'door', label: '门框', geo: ['door', [1.0, 2.1, 0.16]] },
  { type: 'steps', label: '台阶', geo: ['steps', [0.5, 0.15, 0.3]] },
  { type: 'mountain', label: '山形', geo: ['cone', [0.7, 0.8, 4]] },
  { type: 'table', label: '桌子', geo: ['table', [1.2, 0.75, 0.8]] },
  { type: 'wall', label: '墙壁', geo: ['box', [3.0, 2.4, 0.25]] },
  { type: 'pillar', label: '柱子', geo: ['box', [0.4, 3.0, 0.4]] },
  { type: 'bench', label: '长椅', geo: ['bench', [1.6, 0.45, 0.45]] },
  { type: 'tree', label: '树木', geo: ['tree', [1.8]] },
];

export const CHARACTER_COLORS = ['#f59e0b', '#3b82f6', '#22c55e', '#a855f7'];

// 仅供未显式赋色的默认创建循环用；场景内创建者（Task 5/7 的 +人物按钮）应显式赋色
let _colorIndex = 0;

export function newId(prefix) {
  return prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  // 不用 crypto.randomUUID：纯 HTTP 浏览器下不可用（画布上游同步教训）
}

export function newCharacter(id, label) {
  return {
    id, label: label || id, color: CHARACTER_COLORS[_colorIndex++ % CHARACTER_COLORS.length],
    position: [0, 0, 0], rotation_y: 0, scale: 1.0,
    pose: '站立', joints: {},
  };
}

export function newShot(name, order) {
  return {
    id: newId('s_'), name: name || `镜头${order + 1}`, order,
    aspect: '16:9',
    camera: { position: [0, 3.2, 12], target: [0, 1.0, 0], fov: 50,
              shot_size: '中景', azimuth: 0, elevation: 15 },
    characters: [], props: [], thumbnail: '', render: '', notes: '',
  };
}

export function emptyProject(name) {
  return {
    id: newId('p_'), name: name || '未命名项目',
    created_at: '', updated_at: '', created_by_ip: '', shots: [],
  };
}

export function renderSizeFor(aspect, quality, customWidth) {
  const long = quality === 'hd' ? 2560 : Number(customWidth) > 0 ? Number(customWidth) : 1920;
  const r = aspectRatio(aspect); // 宽/高
  let w, h;
  if (r >= 1) { w = long; h = Math.round(long / r); }
  else { h = long; w = Math.round(long * r); }
  return { width: w - (w % 2), height: h - (h % 2) }; // 取偶数，规避视频编码对齐问题
}

export function sanitizeFilename(name) {
  return String(name || 'shot').replace(/[^\w一-鿿-]+/g, '_').slice(0, 60) || 'shot';
}

export function dataUrlToBlob(dataUrl) {
  const [head, b64] = String(dataUrl).split(',', 2);
  const mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/png';
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}
