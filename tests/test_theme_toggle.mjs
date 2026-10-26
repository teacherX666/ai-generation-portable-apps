/*
 * 深色模式的**行为**护栏（2026-10-26）：防闪白脚本真的按 localStorage 生效、
 * 真的会在同源文档之间同步；顶栏那对按钮的接线没被改坏。
 *
 * 行为部分用 `node:vm` + 手搓假 DOM 跑真实的 `shared-ui/theme-boot.js`（不是源文本断言 ——
 * 源文本断言抓不到"属性名写错/抛异常"这类错）。按钮部分用源文本断言，成本低且够用。
 *
 * 跑法：`node tests/test_theme_toggle.mjs`
 * 设计依据：docs/2026-10-26-dark-mode-design.md §3.3 / §3.4 / §3.5 / §5.3。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = process.cwd();
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');
const BOOT = read('shared-ui/theme-boot.js');

/** 跑一遍真实的 theme-boot.js，返回可观测的假 DOM。 */
function runBoot({ stored = null, storageThrows = false } = {}) {
  const attributes = new Map();
  const listeners = new Map();
  const documentElement = {
    getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
    setAttribute: (name, value) => attributes.set(name, String(value)),
    removeAttribute: (name) => attributes.delete(name),
  };
  const win = {
    addEventListener: (name, fn) => {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(fn);
    },
  };
  const localStorage = {
    getItem: () => {
      if (storageThrows) throw new Error('storage disabled');
      return stored;
    },
    setItem: () => {},
  };
  vm.runInNewContext(BOOT, {
    document: { documentElement },
    window: win,
    localStorage,
    console,
  });
  return {
    isDark: () => attributes.get('data-theme') === 'dark',
    fire: (name, event) => (listeners.get(name) || []).forEach((fn) => fn(event)),
    listenerCount: (name) => (listeners.get(name) || []).length,
  };
}

/* ── 行为：首帧前按 localStorage 决定主题 ─────────────────────────────── */
{
  assert.equal(runBoot({ stored: 'dark' }).isDark(), true, '存了 dark 就必须在首帧前设上 data-theme');
  assert.equal(runBoot({ stored: 'light' }).isDark(), false, '存了 light 就不设 data-theme（浅色是默认路径）');
  assert.equal(runBoot({ stored: null }).isDark(), false, '没存过 = 浅色');

  const broken = runBoot({ storageThrows: true });
  assert.equal(broken.isDark(), false, 'localStorage 不可用（隐私模式）时保持浅色');
}

/* ── 行为：storage 事件同步（Portal 切 → iframe 跟） ──────────────────── */
{
  const boot = runBoot({ stored: null });
  assert.ok(boot.listenerCount('storage') >= 1, 'boot 必须注册 storage 监听（跨文档同步靠它）');
  boot.fire('storage', { key: 'portal_theme', newValue: 'dark' });
  assert.equal(boot.isDark(), true, '别人把主题改成 dark，本页要跟着变（不刷新）');
  boot.fire('storage', { key: 'portal_theme', newValue: 'light' });
  assert.equal(boot.isDark(), false, '改回 light 同样要跟着变');
  boot.fire('storage', { key: 'portal_theme', newValue: 'dark' });
  boot.fire('storage', { key: 'portal_fx_trail', newValue: 'on' });
  assert.equal(boot.isDark(), true, '别的 key 不能影响主题');
}

/* ── 接线：顶栏按钮 ──────────────────────────────────────────────────── */
const tools = read('portal/static/topbar-tools.js');
assert.match(tools, /portal-tool--theme/, '必须有深色模式按钮');
assert.match(tools, /portal-tool--trail/, '必须有鼠标拖尾按钮（原 `*` 的重做）');
assert.match(tools, /aria-pressed/, '两个按钮都要带 aria-pressed（状态可被读屏感知）');
assert.match(tools, /title\s*=\s*dark \? "浅色模式" : "深色模式"/, '主题按钮的 title 要随状态描述"点下去会变成什么"');
assert.match(tools, /writeItem\(THEME_KEY/, '点了必须把选择记下来');
assert.match(tools, /window\.__portalFxTrail/, '拖尾按钮要复用 effects.js 的逻辑，不能各写一份');
assert.match(
  tools,
  /host\.insertBefore\(trailBtn, themeBtn\)/,
  '排布：拖尾在左、主题在右（2026-10-26 用户选定）',
);

const effects = read('portal/static/effects.js');
assert.doesNotMatch(
  effects,
  /_fxTrailToggle/,
  '拖尾按钮的 DOM 已经搬到 topbar-tools.js —— effects.js 里不该再自己造按钮',
);
assert.match(effects, /window\.__portalFxTrail\s*=/, 'effects.js 必须暴露拖尾开关 API');
assert.match(effects, /toggle:\s*function/, 'API 要能 toggle');

const portalEntry = read('portal/static/index.html');
assert.match(
  portalEntry,
  /<script[^>]+src="\/effects\.js[^"]*"><\/script>\s*<script[^>]+src="\/topbar-tools\.js/,
  'topbar-tools.js 必须排在 effects.js 之后加载',
);

/* ── 样式：形变与降级 ─────────────────────────────────────────────────── */
const portalCss = read('portal/static/styles.css');
assert.match(
  portalCss,
  /:root\[data-theme="dark"\]\s+\.portal-tool__disc\s*\{[^}]*box-shadow:\s*inset 0 0 0 0 currentColor/s,
  '月亮收成满月（内阴影归零）这条规则不能丢',
);
assert.match(
  portalCss,
  /:root\[data-theme="dark"\]\s+\.portal-tool__rays\s*\{[^}]*opacity:\s*1/s,
  '太阳光芒在深色下要淡入',
);
assert.match(
  portalCss,
  /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\.portal-tool__rays/,
  '减少动态效果时两个按钮的动效都要降级',
);
assert.doesNotMatch(
  portalCss,
  /\.portal-tool\s*\{[^}]*background:\s*#/s,
  '按钮颜色必须走 token（裸色值会在深色下露馅）',
);

console.log('theme toggle: ok');
