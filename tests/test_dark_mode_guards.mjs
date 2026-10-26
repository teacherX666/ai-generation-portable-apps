/*
 * 深色模式的源码护栏（2026-10-26）。
 *
 * 守住四件"改错了也看不出来"的事：
 *   1. 每个入口页的 theme-boot 必须排在**第一个样式表之前** —— 否则深色用户会先看到白闪；
 *   2. 每个应用自己的 `ui/theme-boot.js` 必须与共享源一致（sync-ui.sh 分发漏了就失效）；
 *   3. theme-dark.css 必须覆盖**每一个** token —— 少一个就有一处白天色漏进夜里；
 *   4. 各应用样式表里的"裸白表面"只许减少（迁移期预算制，第 1 期目标是全部归零）。
 *
 * 跑法：`node tests/test_dark_mode_guards.mjs`（仓库根目录，与 test_ui_core_structure.mjs 同款）。
 * 设计依据：docs/2026-10-26-dark-mode-design.md §2.3 / §3.3 / §3.6 / §5.1。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');

/* ── 1. 入口页：boot 在第一个样式表之前 ───────────────────────────────── */

const ENTRY_PAGES = [
  ['portal/static/index.html', '/ui/theme-boot.js'],
  ['portal/static/login.html', '/ui/theme-boot.js'],
  ['portal/static/free-creation/index.html', '/ui/theme-boot.js'],
  ['seedance/static/index.html', 'ui/theme-boot.js'],
  ['nano-banana/static/index.html', 'ui/theme-boot.js'],
  ['rag-assistant/static/index.html', 'ui/theme-boot.js'],
  ['drama-pipeline/static/index.html', 'ui/theme-boot.js'],
  ['dreamina/static/index.html', '/ui/theme-boot.js'],
  ['volcengine-portrait/static/index.html', '/ui/theme-boot.js'],
  ['previz/static/index.html', '/ui/theme-boot.js'],
  ['feishu-generation-agent/src/feishu_generation_agent/web/static/index.html', 'static/ui/theme-boot.js'],
];

for (const [entry, src] of ENTRY_PAGES) {
  const html = read(entry);
  const bootAt = html.search(new RegExp(`<script[^>]+src=["']${src.replaceAll('/', '\\/')}(?:\\?[^"']*)?["']`));
  assert.ok(bootAt >= 0, `${entry} 必须引入 ${src}`);
  const cssAt = html.search(/<link[^>]+rel=["']stylesheet["']/);
  assert.ok(cssAt >= 0, `${entry} 应该至少有一个样式表`);
  assert.ok(
    bootAt < cssAt,
    `${entry} 的 theme-boot 必须排在第一个样式表之前 —— 否则深色下会白闪一帧`,
  );
}

/* 无限画布是**编译产物**（Vite），它自带一套等价的引导脚本（读自己的 zustand 持久化
   store 并给 <html> 打 `dark` 类）。第 1 期刻意不接 Portal 主题（内部 antd/Tailwind
   留给第 2 期），所以这里断言的是"它自己那份引导仍然在、且在样式表之前"，而不是
   硬塞一个 theme-boot 进去。见设计 §3.8。 */
const canvasEntry = read('infinite-canvas/web/index.html');
assert.match(
  canvasEntry,
  /localStorage\.getItem\("infinite-canvas:theme_store"\)/,
  '无限画布自带的主题引导不能丢（第 2 期接 Portal 主题前，它就是它的防闪白）',
);
assert.match(
  canvasEntry,
  /documentElement\.classList\.toggle\("dark"/,
  '无限画布的引导必须给 <html> 打 dark 类（Tailwind 的暗色开关）',
);

/* ── 2. 每个 ui/ 里的 theme-boot.js 必须与共享源一致 ───────────────────── */

const sharedBoot = read('shared-ui/theme-boot.js');
const BOOT_TARGETS = [
  'portal/static/ui',
  'seedance/static/ui',
  'nano-banana/static/ui',
  'dreamina/static/ui',
  'rag-assistant/static/ui',
  'volcengine-portrait/static/ui',
  'drama-pipeline/static/ui',
  'previz/static/ui',
  'feishu-generation-agent/src/feishu_generation_agent/web/static/ui',
];
for (const target of BOOT_TARGETS) {
  assert.equal(
    read(`${target}/theme-boot.js`),
    sharedBoot,
    `${target}/theme-boot.js 与 shared-ui/theme-boot.js 不一致 —— 跑 shared-ui/sync-ui.sh`,
  );
  assert.equal(
    read(`${target}/tokens/theme-dark.css`),
    read('shared-ui/tokens/theme-dark.css'),
    `${target}/tokens/theme-dark.css 与共享源不一致 —— 跑 shared-ui/sync-ui.sh`,
  );
}

/* ── 3. theme-dark.css 必须覆盖每一个 token ───────────────────────────── */

const dark = read('shared-ui/tokens/theme-dark.css');
const REQUIRED_DARK_TOKENS = [
  '--ui-color-canvas',
  '--ui-color-surface',
  '--ui-color-surface-soft',
  '--ui-color-surface-sunken',
  '--ui-color-surface-raised',
  '--ui-color-border',
  '--ui-color-border-strong',
  '--ui-color-text',
  '--ui-color-text-secondary',
  '--ui-color-text-muted',
  '--ui-color-strong',
  '--ui-color-strong-hover',
  '--ui-color-strong-border',
  '--ui-color-on-strong',
  '--ui-color-on-accent',
  '--ui-color-brand',
  '--ui-color-brand-hover',
  '--ui-color-brand-soft',
  '--ui-color-brand-ring',
  '--ui-color-success',
  '--ui-color-success-soft',
  '--ui-color-success-text',
  '--ui-color-success-border',
  '--ui-color-danger',
  '--ui-color-danger-soft',
  '--ui-color-danger-text',
  '--ui-color-danger-border',
  '--ui-color-warning',
  '--ui-color-warning-soft',
  '--ui-color-warning-text',
  '--ui-color-warning-border',
  '--ui-color-info-soft',
  '--ui-color-info-text',
  '--ui-color-info-border',
  '--ui-shadow-sm',
  '--ui-shadow-md',
  '--ui-shadow-lg',
];
for (const token of REQUIRED_DARK_TOKENS) {
  assert.match(
    dark,
    new RegExp(`\\${token}\\s*:`),
    `theme-dark.css 必须覆盖 ${token} —— 缺一个就有一处白天色漏进夜里`,
  );
}
assert.match(
  dark,
  /:root\[data-theme="dark"\]\s*\{[^}]*color-scheme:\s*dark/s,
  '暗色下必须声明 color-scheme: dark（原生滚动条 / 日期选择器 / 下拉都靠它）',
);

/* core.css 必须给每个"暗色会覆盖的"原语一个浅色值，否则浅色下别名解析成空 */
const core = read('shared-ui/tokens/core.css');
for (const token of ['--ui-color-on-accent', '--ui-color-success-border', '--ui-color-danger-border', '--ui-color-warning-border', '--ui-color-info-border']) {
  assert.match(core, new RegExp(`\\${token}\\s*:\\s*#`), `core.css 必须定义 ${token} 的浅色值`);
}

/* ── 4. 入口必须真的加载暗色 token ────────────────────────────────────── */

assert.match(
  read('shared-ui/portal-ui-core.css'),
  /@import url\("tokens\/theme-dark\.css"\)/,
  'portal-ui-core.css 必须 @import theme-dark.css —— 否则"改了 token 但没人加载"',
);

/* ── 5. 裸白表面预算：迁移期只许减少 ─────────────────────────────────── */

//: 白底：纯白 / 浅灰白 / **高不透明度**的半透明白（如顶栏那条 `rgba(255,255,255,.92)`）。
//: 低透明度的 `rgba(255,255,255,.06)` 不算 —— 那是深色层里的"玻璃高光"，本来就是要留在暗色里的
//: （`(?<!["'])` 跳过属性选择器里的字面量：`.x [style*="background: #fff"]` 是在匹配别人写死的行内样式）。
const SURFACE_WHITE = /(?<!["'])background(?:-color)?\s*:\s*(?:#fff\b|#ffffff\b|white\b|#f8fafc\b|#f3f6fa\b|#eef2f7\b|#f9fafb\b|#fafafa\b|#f1f5f9\b|#f5f6f8\b|#f5f6f9\b|#fcfcfd\b|#f7f8fa\b|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*,\s*(?:0?\.[7-9]\d*|1(?:\.0+)?)\s*\))/gi;
//: 第 1 期结束时这些预算**全部为 0**；迁一个模块就把对应数字调小。
const SURFACE_BUDGET = {
  'portal/static/styles.css': 0,
  'portal/static/free-creation/styles.css': 65,
  'seedance/static/styles.css': 11,
  'nano-banana/static/styles.css': 12,
  'dreamina/static/styles.css': 6,
  'drama-pipeline/static/styles.css': 0,
  'previz/static/styles.css': 0,
  'feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css': 14,
};
for (const [file, budget] of Object.entries(SURFACE_BUDGET)) {
  const count = (read(file).match(SURFACE_WHITE) || []).length;
  assert.ok(
    count <= budget,
    `${file} 的裸白表面从 ${budget} 涨到 ${count} —— 新写的表面请用 var(--ui-bg-*) 系列 token`,
  );
}

console.log('dark mode guards: ok');
