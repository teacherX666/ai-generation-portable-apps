# 任务卡片收拢为共享实现 — 实施计划（B 阶段）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 seedance 与 nano-banana 各写一套的任务卡片实现收拢到单一源 `shared-ui/job-cards.js`，经同步脚本分发，并由一致性测试守住不漂移。

**Architecture:** 复用仓库**已有**的 shared-ui 机制（共享源 + `sync-ui.sh` + `tests/test_ui_core_structure.mjs`），把 JS 一并纳入 —— 不新建机制。共享库按「平台通用」设计：不假设在 iframe 里，缩略图渲染与预览行为由调用方注入；本次只接两个 iframe 子应用。

**Tech Stack:** 浏览器原生 JS（ES5 语法，两个应用都兼容）、PetiteVue（既有）、Python 3.12（同步脚本，与项目测试一致）、Node（`.mjs` 测试，与既有 `test_ui_core_structure.mjs` 一致）。

**Spec:** `docs/2026-09-28-shared-job-cards-design.md`

## Global Constraints

- **客户端零构建**：不得引入打包工具链。共享库必须是可直接 `<script src>` 引入的原生 JS。
- **两份副本必须与种子逐字节一致**：`seedance/static/job-cards.js` 与 `nano-banana/static/job-cards.js` 都是 `shared-ui/job-cards.js` 的副本，测试用字符串相等断言。
- **详情弹窗字段恒为四格**：`Seed / 运行耗时 / 使用模型 / 用户`，顺序固定；`username` 为空显示 `—`，**整格不得消失**。
- **禁止 `git add -A`**：本机工作区长期有多个并发会话的在制品（实测 89 项未提交）。每次提交前先 `git diff --cached --name-only` 确认 index 只含目标文件。
- **行尾**：`.js` 保持 CRLF，`.md`/`.html` 用 LF（实测仓库现状：6 个前端文件全部符合此分布）。
- **不改 Portal 与自由创作**：C 阶段另立项。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `shared-ui/job-cards.js` | **新**。单一源：状态判定、任务合并、卡片渲染、详情弹窗渲染。无任何应用特定逻辑 |
| `tools/sync-ui.py` | **新**。跨平台同步：把 `shared-ui/` 的 JS 与 CSS 复制到目标清单 |
| `tests/test_job_cards_in_sync.mjs` | **新**。一致性（副本 == 源）+ 契约（字段恒四格、状态集合含 interrupted） |
| `seedance/static/job-cards.js` | 同步生成的副本 |
| `nano-banana/static/job-cards.js` | 同步生成的副本 |
| `seedance/static/index.html` | 改：引入 job-cards.js（在 app.js 之前） |
| `nano-banana/static/index.html` | 改：同上 |
| `seedance/static/app.js` | 改：删本地重复实现，改调共享库 |
| `nano-banana/static/app.js` | 改：同上 |
| `shared-ui/sync-ui.sh` | 改：把 JS 加进同步清单（保留给非 Windows 机器） |

---

## Task 1（B1-a）：共享库 + 契约测试

**Files:**
- Create: `shared-ui/job-cards.js`
- Test: `tests/test_job_cards_in_sync.mjs`

**Interfaces:**
- Produces: `window.JobCards`，含
  - 纯逻辑：`isCancellableJob(jobOrStatus) -> bool`、`jobDisplayStatus(job) -> string`、`mergeJobWithActivity(job, record) -> object`
  - `create(config) -> instance`；config 字段：`appPath: string`、`thumbKind: 'image'|'video'`、`openPreview?: (items, index) => void`、`onCancel?: (jobId, status) => void`
  - 实例方法：`renderGrid(gridEl, jobs, opts)`、`buildTile(job)`、`renderDetail(bodyEl, job)`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_job_cards_in_sync.mjs`：

```js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

// --- 契约：共享库必须存在并可被求值 ---
const src = read('shared-ui/job-cards.js');
assert.ok(src.length > 2000, 'shared-ui/job-cards.js 应包含实际实现');

// 在受控沙箱里求值：IIFE 需要 window 存在（库里用 typeof window 判断）。
const sandbox = { window: {}, document: { createElement: () => ({ style: {}, dataset: {}, appendChild(){}, addEventListener(){} }) } };
const JobCards = new Function('window', 'document', src + '\nreturn window.JobCards;')(sandbox.window, sandbox.document);
assert.ok(JobCards, 'shared-ui/job-cards.js 应挂载 window.JobCards');

// --- 契约：终态集合必须含 interrupted（卡片会画「已中断」，判定却不认 → 已中断任务被判可取消）---
assert.equal(JobCards.isCancellableJob('interrupted'), false, '已中断不得可取消');
assert.equal(JobCards.isCancellableJob('running'), true, '运行中可取消');

// --- 契约：有产出但状态未终态 → 显示 succeeded ---
assert.equal(JobCards.jobDisplayStatus({ status: 'running', results: [{}] }), 'succeeded');
assert.equal(JobCards.jobDisplayStatus({ status: 'running' }), 'running');

console.log('job cards: contract ok');
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: FAIL —— `ENOENT: no such file or directory … shared-ui/job-cards.js`

- [ ] **Step 3: 写最小实现**

创建 `shared-ui/job-cards.js`，**先只实现纯逻辑部分**（渲染留到 Task 2）：

```js
/* Dependency-free job cards for iframe apps. */
(function (global) {
  'use strict';

  var TERMINAL_STATUSES = ['succeeded', 'success', 'failed', 'fail', 'failure', 'cancelled', 'canceled', 'interrupted'];

  function isTerminal(status) {
    return TERMINAL_STATUSES.indexOf(String(status || '').toLowerCase()) >= 0;
  }

  function isCancellableJob(jobOrStatus) {
    var status = typeof jobOrStatus === 'string' ? jobOrStatus : (jobOrStatus && jobOrStatus.status);
    var n = String(status || '').toLowerCase();
    return Boolean(n) && !isTerminal(n);
  }

  function jobDisplayStatus(job) {
    var raw = String((job && job.status) || '').toLowerCase();
    var hasResult = !!(job && ((job.results && job.results.length)
      || (job.result && job.result.results && job.result.results.length) || job.first_url));
    if (hasResult && raw && !isTerminal(raw)) return 'succeeded';
    return raw;
  }

  global.JobCards = {
    TERMINAL_STATUSES: TERMINAL_STATUSES,
    isTerminal: isTerminal,
    isCancellableJob: isCancellableJob,
    jobDisplayStatus: jobDisplayStatus
  };
})(typeof window !== 'undefined' ? window : this);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS，输出 `job cards: contract ok`

- [ ] **Step 5: 提交**

```bash
git add shared-ui/job-cards.js tests/test_job_cards_in_sync.mjs
git diff --cached --name-only   # 必须只列出这两个文件
git commit -m "feat(shared-ui): job-cards 共享库骨架 + 契约测试"
```

---

## Task 2（B1-b）：渲染能力 + 字段四格契约

**Files:**
- Modify: `shared-ui/job-cards.js`
- Modify: `tests/test_job_cards_in_sync.mjs`

**Interfaces:**
- Consumes: Task 1 的 `window.JobCards`
- Produces: `JobCards.create(config)` 与实例方法 `renderGrid` / `buildTile` / `renderDetail`；
  `renderDetail` 写入的 DOM 必须含 4 个 `.job-detail-meta-item`，标签依次为 `Seed`、`运行耗时`、`使用模型`、`用户`

- [ ] **Step 1: 写失败测试（字段恒四格 = 本次核心修复）**

追加到 `tests/test_job_cards_in_sync.mjs`：

```js
// --- 契约：详情弹窗字段恒为四格，username 为空也必须显示「用户」格 ---
function labelsOf(html) {
  return [...html.matchAll(/job-detail-meta-label">([^<]*)</g)].map((m) => m[1]);
}
const htmlWithUser = JobCards.renderDetailHtml({ job_id: 'a', status: 'succeeded', username: 'alice',
  params: { seed: '42', model: 'm1' } }, { appPath: '' });
assert.deepEqual(labelsOf(htmlWithUser), ['Seed', '运行耗时', '使用模型', '用户']);

const htmlNoUser = JobCards.renderDetailHtml({ job_id: 'b', status: 'succeeded',
  params: { model: 'm1' } }, { appPath: '' });
assert.deepEqual(labelsOf(htmlNoUser), ['Seed', '运行耗时', '使用模型', '用户'],
  'username 为空时「用户」格仍须存在（显示 —）');
assert.match(htmlNoUser, /job-detail-meta-item[\s\S]*?用户[\s\S]*?—/, '空值应显示 —');
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: FAIL —— `JobCards.renderDetailHtml is not a function`

- [ ] **Step 3: 实现渲染**

在 `shared-ui/job-cards.js` 的 IIFE 内加入（并挂到 `global.JobCards`）：

```js
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function jobRequestValues(job) {
    var params = (job && (job.params || job.form)) || ((job && job.request && job.request.values) || {});
    return params;
  }

  function jobSeed(job) {
    var p = jobRequestValues(job);
    var result = (job && job.result) || {};
    return String(p.seed || result.seed || (job && job.seed) || '').trim();
  }

  /** 详情弹窗的 meta 网格 HTML。字段恒为四格，空值显示 —，绝不隐藏整格。 */
  function renderDetailHtml(job, config) {
    var cfg = config || {};
    var values = jobRequestValues(job);
    var user = String((job && job.username) || '').trim();
    var items = [
      { label: 'Seed', value: jobSeed(job) || '随机' },
      { label: '运行耗时', value: (job && job.runtime_text) || '未记录', key: 'runtime' },
      { label: '使用模型', value: String((job && job.model) || values.custom_model || values.model || '未记录') },
      { label: '用户', value: user || '—' }
    ];
    return items.map(function (item) {
      return '<div class="job-detail-meta-item">'
        + '<span class="job-detail-meta-label">' + esc(item.label) + '</span>'
        + '<span class="job-detail-meta-value"' + (item.key === 'runtime' ? ' data-meta="runtime"' : '')
        + ' title="' + esc(item.value) + '">' + esc(item.value) + '</span>'
        + '</div>';
    }).join('');
  }

  function create(config) {
    var cfg = config || {};
    return {
      config: cfg,
      renderDetailHtml: function (job) { return renderDetailHtml(job, cfg); }
    };
  }

  global.JobCards.create = create;
  global.JobCards.renderDetailHtml = renderDetailHtml;
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS（两组标签都恰为 `['Seed','运行耗时','使用模型','用户']`）

- [ ] **Step 5: 补上规格第 4.4 节的错误处理（回调可缺、appPath 可推）**

追加契约测试：

```js
// --- 契约：配置缺省不得抛错；appPath 可推断 ---
const inst = JobCards.create({});                       // 不传任何配置
assert.ok(inst, 'create({}) 应返回实例而不是抛错');
assert.equal(typeof inst.renderDetailHtml, 'function');
assert.equal(JobCards.inferAppPath('/seedance/index.html'), '/seedance');
assert.equal(JobCards.inferAppPath('/index.html'), '');
```

实现（加入 `shared-ui/job-cards.js`）：

```js
  /** 从路径推断应用挂载前缀，兼容 Portal 代理与独立模式。 */
  function inferAppPath(pathname) {
    var p = String(pathname || (typeof location !== 'undefined' ? location.pathname : ''));
    var m = p.match(/^\/(seedance|nano-banana|dreamina|volcengine-portrait|rag-assistant)\//);
    return m ? '/' + m[1] : '';
  }

  global.JobCards.inferAppPath = inferAppPath;
```

并在 `create()` 里落默认值：

```js
  function create(config) {
    var cfg = config || {};
    if (!cfg.appPath) cfg.appPath = inferAppPath();
    if (!cfg.thumbKind) cfg.thumbKind = 'image';
    if (typeof cfg.openPreview !== 'function') cfg.openPreview = function () {};
    if (typeof cfg.onCancel !== 'function') cfg.onCancel = function () {};
    return { config: cfg, renderDetailHtml: function (job) { return renderDetailHtml(job, cfg); } };
  }
```

- [ ] **Step 6: 跑测试确认通过**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add shared-ui/job-cards.js tests/test_job_cards_in_sync.mjs
git diff --cached --name-only
git commit -m "feat(shared-ui): 详情弹窗字段恒四格 + 配置缺省与 appPath 推断"
```

---

## Task 3（B1-c）：同步脚本 + 一致性测试

**Files:**
- Create: `tools/sync-ui.py`
- Modify: `shared-ui/sync-ui.sh`
- Modify: `tests/test_job_cards_in_sync.mjs`

**Interfaces:**
- Consumes: `shared-ui/job-cards.js`
- Produces: `seedance/static/job-cards.js` 与 `nano-banana/static/job-cards.js`，两者须与源**逐字相同**

- [ ] **Step 1: 写失败测试（一致性）**

追加到 `tests/test_job_cards_in_sync.mjs`：

```js
// --- 一致性：两份副本必须与源逐字相同 ---
for (const target of ['seedance/static/job-cards.js', 'nano-banana/static/job-cards.js']) {
  assert.equal(read(target), src, `${target} 必须与 shared-ui/job-cards.js 一致（跑 tools/sync-ui.py）`);
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: FAIL —— `ENOENT … seedance/static/job-cards.js`

- [ ] **Step 3: 写同步脚本**

创建 `tools/sync-ui.py`：

```python
#!/usr/bin/env python3
"""把 shared-ui/ 的共享资源同步到各应用。

为什么需要它：shared-ui/ 里既有 CSS 也有 JS，但旧的 sync-ui.sh 只复制 CSS，
于是 shared-ui/model-capabilities.js 悄悄漂移（nano-banana 那份哈希已不同）。
本脚本覆盖两者，并在 Windows 上可直接运行（.sh 需要 Git Bash）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "shared-ui"

#: 共享 JS：文件名 -> 目标目录（相对仓库根）
JS_TARGETS = {
    "job-cards.js": ["seedance/static", "nano-banana/static"],
    "model-capabilities.js": ["seedance/static", "nano-banana/static"],
}
#: 共享 CSS 的目标（与旧 sync-ui.sh 保持一致）
CSS_TARGETS = [
    "portal/static/ui", "seedance/static/ui", "nano-banana/static/ui",
    "dreamina/static/ui", "rag-assistant/static/ui",
    "volcengine-portrait/static/ui",
    "feishu-generation-agent/src/feishu_generation_agent/web/static/ui",
]


def sync_js() -> int:
    n = 0
    for name, dirs in JS_TARGETS.items():
        source = SRC / name
        if not source.exists():
            print(f"  WARN 共享源缺失: {source}", file=sys.stderr)
            continue
        for rel in dirs:
            dest_dir = REPO / rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest_dir / name)
            print(f"  JS   {name} -> {rel}")
            n += 1
    return n


def sync_css() -> int:
    n = 0
    core = SRC / "portal-ui-core.css"
    for rel in CSS_TARGETS:
        dest = REPO / rel
        dest.mkdir(parents=True, exist_ok=True)
        if core.exists():
            shutil.copyfile(core, dest / "portal-ui-core.css")
        for sub in ("tokens", "styles"):
            src_sub = SRC / sub
            if src_sub.is_dir():
                (dest / sub).mkdir(parents=True, exist_ok=True)
                for f in src_sub.iterdir():
                    if f.is_file():
                        shutil.copyfile(f, dest / sub / f.name)
        print(f"  CSS  portal-ui-core.css + tokens/ + styles/ -> {rel}")
        n += 1
    return n


def main() -> int:
    print("同步 shared-ui →")
    js = sync_js()
    css = sync_css()
    print(f"完成：{js} 个 JS 目标，{css} 个 CSS 目标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 扩展 `sync-ui.sh` 覆盖 JS**

在 `shared-ui/sync-ui.sh` 的 for 循环之后追加：

```sh
# 共享 JS 也必须同步：旧版只复制 CSS，导致 model-capabilities.js 漂移。
for target in "$ROOT/seedance/static" "$ROOT/nano-banana/static"; do
  cp "$SRC/job-cards.js" "$target/job-cards.js"
  cp "$SRC/model-capabilities.js" "$target/model-capabilities.js"
done
```

- [ ] **Step 5: 跑同步脚本**

Run: `python tools/sync-ui.py`
Expected: 输出 `JS   job-cards.js -> seedance/static` 等，末尾 `完成：4 个 JS 目标，7 个 CSS 目标`

- [ ] **Step 6: 跑测试确认通过**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add tools/sync-ui.py shared-ui/sync-ui.sh shared-ui/job-cards.js \
        seedance/static/job-cards.js nano-banana/static/job-cards.js \
        seedance/static/model-capabilities.js nano-banana/static/model-capabilities.js \
        tests/test_job_cards_in_sync.mjs
git diff --cached --name-only
git commit -m "feat(shared-ui): 跨平台同步脚本 + 副本一致性测试（并补上 model-capabilities.js）"
```

---

## Task 4（B2）：接入 seedance

**Files:**
- Modify: `seedance/static/index.html`（加 `<script src="job-cards.js">`，位置在 `app.js` 之前）
- Modify: `seedance/static/app.js`（删本地重复实现，改调 `JobCards`）

**Interfaces:**
- Consumes: `window.JobCards`（Task 1–3）
- Produces: seedance 的卡片与详情弹窗由共享库渲染，行为与此前一致，**字段恒四格**

- [ ] **Step 1: index.html 引入共享库**

在 `seedance/static/index.html` 第 251 行 `model-capabilities.js` 与第 252 行 `app.js` 之间插入：

```html
  <script src="job-cards.js"></script>
```

- [ ] **Step 2: app.js 改用共享逻辑（先只换纯逻辑，风险最小）**

将 `seedance/static/app.js` 顶层的 `isCancellableJob` / `jobDisplayStatus` 两个函数体替换为委托：

```js
function isCancellableJob(jobOrStatus) { return window.JobCards.isCancellableJob(jobOrStatus); }
function jobDisplayStatus(j) { return window.JobCards.jobDisplayStatus(j); }
```

保留 `TERMINAL_STATUSES` 常量（其它代码仍在用）。

- [ ] **Step 3: 详情弹窗改用共享字段渲染**

把 `_renderJobDetail` 里构造 `metaItems` 并 `add('job-detail-meta-grid', …)` 的那一段
（seedance/static/app.js 约 1921–1936 行）替换为：

```js
      add('job-detail-meta-grid', window.JobCards.renderDetailHtml(job, { appPath: APP_PATH }));
```

并把紧随其后的 runtime 定时器改为在 `data-meta="runtime"` 上更新（逻辑不变，
因为共享库输出的 HTML 仍带该属性）。

- [ ] **Step 4: 语法与契约检查**

Run: `node --check seedance/static/app.js`
Expected: exit 0

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS

- [ ] **Step 5: 浏览器实测**

打开 `http://127.0.0.1:8787/`（独立模式）或 Portal 的视频生成 tab：
1. 任务矩阵卡片正常渲染（缩略图、状态徽标、提示词、下载/取消按钮）
2. 点开任务详情：**恰好四格** `Seed / 运行耗时 / 使用模型 / 用户`
3. 找一条无用户名的旧任务：`用户` 格仍在，显示 `—`
4. 运行中的任务：`运行耗时` 每秒跳动
Expected: 四项全部符合

- [ ] **Step 6: 提交**

```bash
git add seedance/static/index.html seedance/static/app.js
git diff --cached --name-only
git commit -m "refactor(seedance): 卡片与详情弹窗改调 shared-ui/job-cards"
```

---

## Task 5（B3）：接入 nano-banana

**Files:**
- Modify: `nano-banana/static/index.html`
- Modify: `nano-banana/static/app.js`

**Interfaces:**
- Consumes: `window.JobCards`
- Produces: nano-banana 的详情弹窗与 seedance **完全同构**（字段、顺序、空值表现一致）

- [ ] **Step 1: index.html 引入共享库**

在 `nano-banana/static/index.html` 第 302 行 `model-capabilities.js` 与第 303 行 `app.js` 之间插入：

```html
  <script src="job-cards.js"></script>
```

- [ ] **Step 2: app.js 的 isCancellableJob 委托共享库**

把 methods 里的 `isCancellableJob` 改为：

```js
    isCancellableJob: function (jobOrStatus) {
      return window.JobCards.isCancellableJob(jobOrStatus);
    },
```

- [ ] **Step 3: 详情弹窗改用共享字段渲染**

把 `_renderJobDetail` 里构造 `metaItems` 并 `add('job-detail-meta-grid', …)` 的那一段
（nano-banana/static/app.js 约 2019–2030 行）替换为：

```js
      add('job-detail-meta-grid', window.JobCards.renderDetailHtml(job, { appPath: APP_PATH }));
```

runtime 定时器保持现状（共享库输出的 HTML 同样带 `data-meta="runtime"`）。

- [ ] **Step 4: 语法与契约检查**

Run: `node --check nano-banana/static/app.js`
Expected: exit 0

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS

- [ ] **Step 5: 浏览器实测**

打开 `http://127.0.0.1:8797/` 或 Portal 的图片生成 tab，重复 Task 4 Step 5 的四项检查。
**额外确认**：与视频生成模块的详情弹窗**逐格一致**（这是用户最初的要求）。

- [ ] **Step 6: 提交**

```bash
git add nano-banana/static/index.html nano-banana/static/app.js
git diff --cached --name-only
git commit -m "refactor(nano-banana): 卡片与详情弹窗改调 shared-ui/job-cards（与 seedance 同构）"
```

---

## Task 6（B4）：补上 model-capabilities.js 的漂移

**Files:**
- Modify: `tests/test_job_cards_in_sync.mjs`

**Interfaces:**
- Consumes: Task 3 的 `tools/sync-ui.py`（已含 `model-capabilities.js`）
- Produces: 该文件的漂移被测试守住，不会再复发

- [ ] **Step 1: 写失败测试（把已修好的漂移锁住）**

追加到 `tests/test_job_cards_in_sync.mjs`：

```js
// --- 一致性：model-capabilities.js 也必须与共享源一致 ---
// 背景：它一直放在 shared-ui/ 里，但旧的 sync-ui.sh 只复制 CSS，于是 nano-banana
// 那份默默漂移（2026-09-28 实测哈希 d3642976 vs 源 dfb8e87a）。
const capSrc = read('shared-ui/model-capabilities.js');
for (const target of ['seedance/static/model-capabilities.js', 'nano-banana/static/model-capabilities.js']) {
  assert.equal(read(target), capSrc, `${target} 必须与 shared-ui/model-capabilities.js 一致`);
}
```

- [ ] **Step 2: 跑测试**

Run: `node tests/test_job_cards_in_sync.mjs`
Expected: PASS（Task 3 的同步脚本已经把它们对齐了；若 FAIL 说明没跑同步）

- [ ] **Step 3: 提交**

```bash
git add tests/test_job_cards_in_sync.mjs
git diff --cached --name-only
git commit -m "test(shared-ui): 锁住 model-capabilities.js 的副本一致性"
```

---

## 收尾验证

- [ ] `node tests/test_job_cards_in_sync.mjs` → PASS
- [ ] `node --check` 三个文件（两份 app.js + 共享库）→ 全部 exit 0
- [ ] `python tests/run_tests_local.py` → 既有回归不被破坏（注意：本机工作区长期脏，
      基线失败需与改动前对比，不要假设全绿）
- [ ] 浏览器实测两个应用各一次，**并排确认详情弹窗逐格一致**
- [ ] 确认服务端下发的 `job-cards.js` 确实是最新版（HTTP 取回后字符串检查），
      避免再出现「服务端是新的、用户看的是缓存的旧版」这类误判
