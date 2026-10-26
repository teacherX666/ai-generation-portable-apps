/*
 * 主题变量守门（2026-10-26）：各应用样式表里 `var(--x)` 用到的变量**必须有定义**。
 *
 * 为什么值得单独一条：变量名写错/忘了加 token 时 CSS **不报错** —— 只是静默变成
 * `inherit`/透明。深色模式全靠变量翻转，这类静默失效正是最容易漏的回归
 * （本轮就抓到过 `--surface-raised` 需要新加 token 的情况）。
 *
 * 口径：只查**没有 fallback** 的 `var(--x)`；`var(--x, 兜底)` 自带兜底，不算问题。
 * 定义来源 = 该文件自己 + `shared-ui/tokens/*`（由 sync-ui.sh 分发到各应用）。
 *
 * 跑法：`node tests/test_theme_variables.mjs`
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';

const TOKEN_FILES = [
  'shared-ui/tokens/core.css',
  'shared-ui/tokens/semantic.css',
  'shared-ui/tokens/legacy-aliases.css',
  'shared-ui/tokens/theme-dark.css',
];

//: [文件, 额外的定义来源]
//: 入口页的行内 style 可以用**同目录应用样式表**里定义的应用级别名（如 portal 的
//: `--surface-raised`），所以定义域要把它一起算进来，否则会误报。
const APP_FILES = [
  ['portal/static/styles.css', []],
  ['portal/static/free-creation/styles.css', []],
  ['portal/static/index.html', ['portal/static/styles.css']],
  // dino.js 是"JS 注入样式"那条路（猫咪衣柜/实验室彩蛋）：它不走样式表，CSS 回填与
  // 常规扫描都覆盖不到，2026-10-26 实测它整块是浅色（深色下白斑）。定义域算上 portal 样式表。
  ['portal/static/dino.js', ['portal/static/styles.css']],
  ['seedance/static/styles.css', []],
  ['nano-banana/static/styles.css', []],
  ['dreamina/static/styles.css', []],
  ['previz/static/styles.css', []],
  ['rag-assistant/static/index.html', []],
  ['drama-pipeline/static/styles.css', []],
  ['volcengine-portrait/static/index.html', ['portal/static/styles.css']],
  ['feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css', []],
  ['shared-ui/styles/components.css', []],
];

//: 由 JS 在运行时写到元素上（拖拽位移、ResizeObserver 量的高度…），静态看不到定义
const RUNTIME_DEFINED = new Set([
  '--portal-header-height',
  '--composer-width',
  '--preview-frame-width',
  '--preview-frame-height',
  '--preview-frame-size',
  '--tx',
  '--ty',
]);

const definitions = (css) => new Set([...css.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)].map((m) => m[1]));

const tokenDefs = new Set();
for (const file of TOKEN_FILES) {
  for (const name of definitions(fs.readFileSync(file, 'utf8'))) tokenDefs.add(name);
}

let checked = 0;
for (const [file, extraSources] of APP_FILES) {
  const text = fs.readFileSync(file, 'utf8');
  const own = definitions(text);
  for (const extra of extraSources) {
    for (const name of definitions(fs.readFileSync(extra, 'utf8'))) own.add(name);
  }
  // 只取没有 fallback 的用法：`var(--x)` 后面紧跟 `)`
  const used = new Set(
    [...text.matchAll(/var\(\s*(--[a-zA-Z0-9_-]+)\s*\)/g)].map((m) => m[1]),
  );
  const missing = [...used].filter(
    (name) => !own.has(name) && !tokenDefs.has(name) && !RUNTIME_DEFINED.has(name),
  );
  assert.deepEqual(
    missing.sort(),
    [],
    `${file} 里这些变量没有定义（深色模式会静默失效）：${missing.join(', ')}`,
  );
  checked += used.size;
}

console.log(`theme variables: ok（${APP_FILES.length} 个文件、${checked} 处无兜底 var() 引用；共享 token ${tokenDefs.size} 个）`);
