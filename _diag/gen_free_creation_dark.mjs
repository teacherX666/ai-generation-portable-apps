// 深色层生成器 v2（2026-10-26）
// 相比 v1 的三点改进：
//   1) 扫**整个文件**（v1 只扫最后一层，于是散在早期层的 hover 规则漏了 —— 实测 10 条会在深色下露白）；
//   2) 只镜像"确实映射到了深色值"的规则（纯深色/品牌/渐变的规则原样跳过，不产生噪声）；
//   3) **保留 @media 上下文**（v1 会把媒体查询里的规则提到顶层，等于改了小屏行为）。
import fs from 'node:fs';

const FILE = 'portal/static/free-creation/styles.css';
const text = fs.readFileSync(FILE, 'utf8');

const VALUE_MAP = [
  ['#ffffff', '#171c23'], ['#fff', '#171c23'], ['white', '#171c23'],
  ['#f5f6f8', '#101419'], ['#f5f6f9', '#101419'], ['#fcfcfd', '#12161c'], ['#f7f8fa', '#1d242c'],
  ['rgba(252,252,253,.92)', 'rgba(23,28,35,.92)'],
  ['rgba(252,252,253,.96)', 'rgba(23,28,35,.96)'],
  ['rgba(255,255,255,.82)', 'rgba(255,255,255,.06)'],
  ['rgba(255,255,255,.78)', 'rgba(255,255,255,.06)'],
  ['#171820', 'var(--text)'], ['#1c1d26', 'var(--text)'],
  ['#555866', 'var(--muted-strong)'], ['#4d5060', 'var(--muted-strong)'],
  ['#8b8e9d', 'var(--muted)'], ['#8d90a2', 'var(--muted)'], ['#989baa', 'var(--muted)'],
  ['#686b78', 'var(--muted)'], ['#737684', 'var(--muted)'], ['#777a87', 'var(--muted)'], ['#777a88', 'var(--muted)'],
  ['#f2f3f5', 'var(--panel-strong)'], ['#eef1f6', 'var(--panel-strong)'], ['#edf1f6', 'var(--panel-strong)'],
  ['rgba(30,32,45,.16)', 'rgba(255,255,255,.16)'],
  ['rgba(30,32,45,.10)', 'rgba(255,255,255,.10)'],
  ['rgba(30,32,45,.09)', 'rgba(255,255,255,.10)'],
  ['rgba(30,32,45,.08)', 'rgba(255,255,255,.09)'],
  ['rgba(30,32,45,.07)', 'rgba(255,255,255,.08)'],
  ['rgba(30,32,45,.035)', 'rgba(255,255,255,.05)'],
  ['rgba(28,30,45,.045)', 'rgba(255,255,255,.06)'],
  ['rgba(35,40,60,.14)', 'rgba(0,0,0,.5)'],
  ['rgba(35,40,60,.11)', 'rgba(0,0,0,.42)'],
  ['rgba(35,40,60,.10)', 'rgba(0,0,0,.4)'],
  ['rgba(35,40,60,.06)', 'rgba(0,0,0,.3)'],
  ['rgba(35,40,60,.055)', 'rgba(0,0,0,.3)'],
  ['rgba(35,40,60,.05)', 'rgba(0,0,0,.28)'],
  ['rgba(35,40,60,.045)', 'rgba(0,0,0,.26)'],
  ['rgba(35,40,60,.035)', 'rgba(0,0,0,.24)'],
];

function mapValues(body) {
  let out = body;
  let touched = false;
  for (const [from, to] of VALUE_MAP) {
    const re = new RegExp(from.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + (from.startsWith('#') ? '(?![0-9a-fA-F])' : ''), 'g');
    const next = out.replace(re, to);
    if (next !== out) touched = true;
    out = next;
  }
  // 通用规则：**高不透明度的"白玻璃"**（rgba(255,255,255,α≥.5)）在浅色下是"比底更亮的面"，
  // 深色下必须变成低透明度的高光，否则就是一块块白斑 —— 实测漏了 21 处（.fc-popover .96、
  // .fc-toast .96、.fc-new-chat .76…，它们面积小、日常审计的"孤岛"阈值抓不到，是护栏的
  // 计数指标抓出来的）。映射保持单调：越接近纯白 → 高光越弱。
  out = out.replace(
    /rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*(0?\.\d+|1(?:\.0+)?)\s*\)/g,
    (match, a) => {
      const alpha = parseFloat(a);
      if (alpha < 0.5) return match; // 低透明度白本身就是深色主题里的高光，保留
      touched = true;
      const glass = Math.max(0.04, Math.min(0.14, (1 - alpha) * 0.3 + 0.04));
      return `rgba(255,255,255,${glass.toFixed(2)})`;
    },
  );
  // 通用规则二：浅色层里**偏暗的文字色**（亮度 < .25）在深色底上会糊 —— 换成
  // `light-dark(原值, 提亮档)`：浅色**逐字不变**，深色按 color-scheme 自动取提亮版，
  // 而且**保留色相**（紫/红这些品牌强调色不会被洗成灰）。
  out = out.replace(/(?<![\w-])(color\s*:\s*)(#[0-9a-fA-F]{3,8})(?![0-9a-fA-F])/g, (match, prefix, hex) => {
    const l = luminance(hex);
    if (l >= 0.25) return match;
    touched = true;
    if (l < 0.04) return `${prefix}light-dark(${hex}, var(--text))`;
    return `${prefix}light-dark(${hex}, ${lighten(hex, 0.55)})`;
  });
  return { out, touched };
}

/** sRGB 里朝白色混 alpha 比例（与 Portal 的 appColor 同一口径）。 */
function lighten(hex, ratio) {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  const ch = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  return '#' + ch.map((v) => Math.round(v * (1 - ratio) + 255 * ratio).toString(16).padStart(2, '0')).join('');
}

function luminance(hex) {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  const lin = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

// ── 先定位所有 @media 的作用区间（按大括号配对） ─────────────────────────
const mediaSpans = [];
for (const m of text.matchAll(/@media[^{]+\{/g)) {
  let depth = 1;
  let i = m.index + m[0].length;
  while (i < text.length && depth > 0) {
    if (text[i] === '{') depth += 1;
    else if (text[i] === '}') depth -= 1;
    i += 1;
  }
  mediaSpans.push({ start: m.index, end: i, prelude: m[0].slice(0, -1).trim() });
}
const mediaFor = (index) => mediaSpans.find((s) => index > s.start && index < s.end);

const darkSelector = (raw) => {
  const s = raw.trim();
  if (/^html\b/.test(s)) return s.replace(/^html/, 'html[data-theme="dark"]');
  if (/^:root\b/.test(s)) return s.replace(/^:root/, ':root[data-theme="dark"]');
  return `:root[data-theme="dark"] ${s}`;
};

const topRules = [];
const mediaRules = new Map(); // prelude → rules[]
let mirrored = 0;
for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
  const selector = m[1].trim().replace(/\s+/g, ' ');
  if (/^@|from$|to$|^\d+%$/.test(selector)) continue;
  if (!/(background|border|color|box-shadow|fill|stroke)\s*:/.test(m[2])) continue;
  const { out, touched } = mapValues(m[2]);
  if (!touched) continue;                       // 纯深色/品牌/渐变：跳过，不加噪声
  mirrored += 1;
  const block = `${selector.split(',').map(darkSelector).join(',\n')} {${out}}`;
  const media = mediaFor(m.index);
  if (media) {
    if (!mediaRules.has(media.prelude)) mediaRules.set(media.prelude, []);
    mediaRules.get(media.prelude).push(block);
  } else {
    topRules.push(block);
  }
}

const HEADER = `
/* ============================================================================
   深色模式层（2026-10-26 加入，v2 生成）
   这个应用的样式是**五层堆叠**、最后 500 行还用 226 条 !important 把浅色写死。
   本层把"声明了浅色值"的规则逐条镜像成 [data-theme="dark"] 作用域下的深色版本：
   同文件、更靠后、选择器权重更高 → 深浅切换时赢。
   * **浅色一行未动**：这些规则只在 <html data-theme="dark"> 时命中；
   * 纯品牌红/渐变/玻璃高光的规则**不镜像**（它们是内容，不是皮肤）；
   * @media 里的规则保留原媒体查询上下文。
   v1 只扫最后一层，漏了 10 条散在早期层的 hover（深色下悬停会露白），v2 改为全文件扫描。
   ============================================================================ */
`;

const VAR_BLOCK = `:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #101419;
  --bg-soft: #171c23;
  --panel: rgba(23, 28, 35, .86);
  --panel-solid: #171c23;
  --panel-strong: #1d242c;
  --line: rgba(255, 255, 255, .10);
  --line-strong: rgba(255, 255, 255, .18);
  --text: #e4e8ed;
  --muted: #9aa4b0;
  --muted-strong: #c3cad3;
  --shadow-xl: 0 30px 90px rgba(0, 0, 0, .55);
  --shadow-md: 0 14px 40px rgba(0, 0, 0, .4);
}
`;

const mediaBlocks = [...mediaRules.entries()]
  .map(([prelude, rules]) => `${prelude} {\n${rules.join('\n\n')}\n}`)
  .join('\n\n');

const out = text + HEADER + '\n' + VAR_BLOCK + '\n' + topRules.join('\n\n') + '\n\n' + mediaBlocks + '\n';
fs.writeFileSync(FILE, out, 'utf8');

console.log(`镜像规则 ${mirrored} 条（顶层 ${topRules.length} / 媒体查询内 ${mirrored - topRules.length}）`);
console.log(`媒体查询分组 ${mediaRules.size} 个`);
console.log(`文件 ${text.length} → ${out.length}`);
