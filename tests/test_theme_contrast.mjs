/*
 * 主题配色的对比度护栏（2026-10-26）。
 *
 * 把"正文看得清"从**口头承诺**变成可执行断言：解析 core.css（浅色）与
 * theme-dark.css（深色）里的 token 值，按 WCAG 2.1 相对亮度算对比度。
 * 任何一次调色只要跌穿底线，这个测试立刻红 —— 深浅两套都算。
 *
 * 阈值：正文类 ≥ 4.5:1；次要文字 / 非文本（状态点、图标）≥ 3:1。
 * 实算数字见 docs/2026-10-26-dark-mode-design.md §3.6。
 *
 * 跑法：`node tests/test_theme_contrast.mjs`
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');

/** 从 CSS 文本里抓 `--name: <值>;`（只看第一处定义即可 —— 暗色文件里每个 token 只写一次）。 */
function tokenMap(cssText) {
  const map = new Map();
  for (const match of cssText.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;{}]+);/gi)) {
    const name = match[1].trim();
    if (!map.has(name)) map.set(name, match[2].trim());
  }
  return map;
}

function parseColor(value) {
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!hex) return null;
  let digits = hex[1];
  if (digits.length === 3) digits = digits.split('').map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(digits.slice(i, i + 2), 16) / 255);
}

function luminance(rgb) {
  const linear = rgb.map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(fg, bg) {
  const a = luminance(fg);
  const b = luminance(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** [可读名字, 前景 token, 背景 token, 最低要求] */
const PAIRS = [
  ['正文 / 画布', '--ui-color-text', '--ui-color-canvas', 4.5],
  ['正文 / 表面', '--ui-color-text', '--ui-color-surface', 4.5],
  ['次要文字 / 表面', '--ui-color-text-secondary', '--ui-color-surface', 4.5],
  ['弱化文字 / 表面', '--ui-color-text-muted', '--ui-color-surface', 3.0],
  ['品牌文字 / 表面', '--ui-color-brand', '--ui-color-surface', 4.5],
  ['强调底上的字 / 强调底', '--ui-color-on-accent', '--ui-color-brand', 4.5],
  ['强调底上的字 / 危险底', '--ui-color-on-accent', '--ui-color-danger', 4.5],
  ['强调底上的字 / 成功底', '--ui-color-on-accent', '--ui-color-success', 4.5],
  ['强调底上的字 / 警告底', '--ui-color-on-accent', '--ui-color-warning', 4.5],
  ['strong 按钮字 / strong 底', '--ui-color-on-strong', '--ui-color-strong', 4.5],
  ['成功文字 / 成功软底', '--ui-color-success-text', '--ui-color-success-soft', 4.5],
  ['警告文字 / 警告软底', '--ui-color-warning-text', '--ui-color-warning-soft', 4.5],
  ['危险文字 / 危险软底', '--ui-color-danger-text', '--ui-color-danger-soft', 4.5],
  ['信息文字 / 信息软底', '--ui-color-info-text', '--ui-color-info-soft', 4.5],
  ['成功点 / 表面', '--ui-color-success', '--ui-color-surface', 3.0],
  ['警告点 / 表面', '--ui-color-warning', '--ui-color-surface', 3.0],
  ['危险点 / 表面', '--ui-color-danger', '--ui-color-surface', 3.0],
];

const THEMES = [
  ['浅色', tokenMap(read('shared-ui/tokens/core.css'))],
  ['深色', tokenMap(read('shared-ui/tokens/theme-dark.css'))],
];

/* 浅色套里**既有**的可读性欠账：只登记、不改值。
   第 1 期的硬要求是「浅色外观与改动前肉眼无差异」，所以不能借"提对比度"顺手改浅色 ——
   要改浅色得单独提一次（那次专门做浅色可读性）。深色套没有这条豁免。 */
const LEGACY_LIGHT_EXCEPTIONS = new Map([
  ['浅色｜强调底上的字 / 危险底', {
    floor: 3.7,
    why: '白字 on #ef4444 是既有浅色外观（原 .ui-btn--danger）；深色已达标（#101419 on #f0857c = 7.35）',
  }],
  ['浅色｜强调底上的字 / 成功底', {
    floor: 2.5,
    why: '白字 on #10b981 是既有浅色外观（原 .ui-btn--success）；深色已达标（8.12）',
  }],
  ['浅色｜强调底上的字 / 警告底', {
    floor: 2.1,
    why: '白字 on #f59e0b 是既有浅色外观（浅色下没人把 warning 当实心按钮底，是历史遗留）；深色已达标（8.67）',
  }],
  ['浅色｜成功点 / 表面', {
    floor: 2.5,
    why: '#10b981 在浅色下既是状态点也是实心按钮底（既有值）；深色已达标（7.52）',
  }],
  ['浅色｜警告点 / 表面', {
    floor: 2.1,
    why: '#f59e0b 是既有浅色值，作状态点时在浅底上偏弱；深色已达标（8.03）',
  }],
]);

const report = [];
for (const [themeName, tokens] of THEMES) {
  for (const [label, fgToken, bgToken, floor] of PAIRS) {
    const key = `${themeName}｜${label}`;
    const exception = LEGACY_LIGHT_EXCEPTIONS.get(key);
    const effectiveFloor = exception ? exception.floor : floor;
    const fgRaw = tokens.get(fgToken);
    const bgRaw = tokens.get(bgToken);
    assert.ok(fgRaw, `${themeName}主题缺 ${fgToken}`);
    assert.ok(bgRaw, `${themeName}主题缺 ${bgToken}`);
    const fg = parseColor(fgRaw);
    const bg = parseColor(bgRaw);
    assert.ok(fg && bg, `${themeName} ${label}: token 必须是 #hex（收到 ${fgRaw} / ${bgRaw}）`);
    const ratio = contrast(fg, bg);
    report.push(
      `${themeName}｜${label}｜${ratio.toFixed(2)}:1（要求 ≥ ${effectiveFloor}）`
      + (exception ? ` ← 既有豁免：${exception.why}` : ''),
    );
    assert.ok(
      ratio >= effectiveFloor,
      `${themeName}主题「${label}」对比度只有 ${ratio.toFixed(2)}:1，低于 ${effectiveFloor}:1 —— ` +
        `${fgToken} on ${bgToken}（${fgRaw} / ${bgRaw}）`,
    );
  }
}

console.log(report.join('\n'));
console.log('\ntheme contrast: ok');
