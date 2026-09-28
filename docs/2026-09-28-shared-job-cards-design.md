# 任务卡片收拢为共享实现（seedance / nano-banana）

- 日期：2026-09-28
- 状态：待实施
- 范围：**B**（收拢两个 iframe 子应用的卡片实现）。C（Portal 与自由创作）本次只做设计预留，不实施。

## 1. 问题

用户反馈三件事，指向同一个根因：

1. 「图片生成第一个历史任务有五个属性，没去掉开始时间」—— 我改了 `nano-banana`，
   但用户看到的仍是旧版；且**同一个应用里不同任务的属性个数还不一样**。
2. 「后面的任务也各不相同」—— 详情弹窗的字段是条件拼装的：
   `var metaItems = [Seed, 运行耗时, 使用模型]; if (user) metaItems.push(用户)`。
   username 有值就 4 格、没值就 3 格。
3. 「界面很奇怪」—— 同一个应用有**两条互不相干的详情路径**：点任务是
   `Seed/运行耗时/使用模型/用户` 网格；点「活动」tab 是
   `status · 时间 · provider · model` 加两坨 JSON 原文。
4. 「是不是我的代码太多屎山了」—— 属实：`seedance`、`nano-banana`、`portal`、
   `portal/static/free-creation` **各写了一套卡片**，是同构复制后各自演化。典型后果：
   改一处另一处还是旧的；声称「照搬」实际没搬全。

## 2. 现状勘察

### 2.1 四套实现的规模

| 文件 | 行数 |
|---|---|
| `seedance/static/app.js` | 3382 |
| `nano-banana/static/app.js` | 3300 |
| `portal/static/app.js` | 3796 |
| `portal/static/free-creation/app.js` | 1763 |

两套子应用函数一一对应（`jobPreviewItems` / `jobPreviewSignature` / `mergeJobWithActivity` /
`isCancellableJob` / `jobDisplayStatus` / `renderJobsGrid` / `_buildJobTile` / `_buildJobThumb` /
`openJobDetail` / `_renderJobDetail` / `closeJobDetail` / `jobActivityId`），差别在**风格与演化**：
seedance 是 ES6（`const` / 箭头函数），nano-banana 是 ES5（`var` / `function`）；
`isCancellableJob` 一个在文件顶层、一个挂在 methods 里。

### 2.2 仓库里**已经**有共享机制，但覆盖不全

- 共享源：`shared-ui/`（含 `portal-ui-core.css`、`model-capabilities.js`、`styles/`、`tokens/`）
- 同步脚本：`shared-ui/sync-ui.sh` —— **只复制 CSS / tokens / styles，不复制 `model-capabilities.js`**
- 一致性测试：`tests/test_ui_core_structure.mjs` —— **只校验 CSS**，且只断言 CSS 内容

**实锤漂移**：`shared-ui/model-capabilities.js` 与 `seedance/static/model-capabilities.js`
哈希一致（`dfb8e87a…`），但 `nano-banana/static/model-capabilities.js` 是 `d3642976…` ——
**源在共享目录里，同步脚本不管它，测试也不查它，于是漂移无人发现。**
这正是「机制存在但形同虚设」的具体形态，也是用户所说「屎山」的成因之一。

### 2.3 已确认的决策（来自用户）

| 决策点 | 选择 |
|---|---|
| 共享机制 | **A**：共享源 + 同步脚本 + 一致性测试 |
| 共享源定位 | **B**：按「平台通用」设计接口，本次只接 seedance / nano-banana |
| 行为基线 | **A**：以 seedance 为主，但**修掉「字段时有时无」** |
| 合并深度 | **方案 1**：完整实现共享（DOM 构建也在共享源里） |
| 落地顺序 | **B2 先动 seedance**（它是基准，风险最低） |
| 同步脚本语言 | **Python 为主**（`.sh` 在 Windows 需要 Git Bash，保留给其它机器） |

## 3. 目标与非目标

**目标**

- 两个子应用的卡片实现收拢到单一源 `shared-ui/job-cards.js`，经同步脚本分发。
- 所有任务的详情弹窗**字段固定为四格**：`Seed / 运行耗时 / 使用模型 / 用户`；
  无用户名时显示 `—`，不再整格消失。
- 一致性测试覆盖新增的 JS（并补上 `model-capabilities.js` 这个漏网之鱼）。
- 接口按平台通用设计，使 C 阶段（Portal / 自由创作）能**接入**而不是重写。

**非目标**

- 本次不改 Portal 与自由创作的卡片行为（C 阶段）。
- 不引入构建工具链（客户端零构建是硬约束）。
- 不顺手重构卡片以外的代码。

## 4. 设计

### 4.1 文件与同步

```
shared-ui/job-cards.js              ← 单一源（新增）
shared-ui/sync-ui.sh                ← 扩展：连 JS 一起同步（保留给其它机器）
tools/sync-ui.py                    ← 新增：跨平台主力（Windows 直跑）
seedance/static/job-cards.js        ← 同步副本
nano-banana/static/job-cards.js     ← 同步副本
tests/test_job_cards_in_sync.mjs    ← 新增：一致性 + 契约测试
```

同步脚本维护一份**目标清单**，本次只含两个子应用；C 阶段往清单里加 `portal/static/`
与 `portal/static/free-creation/` 即可，脚本与测试结构不变。

### 4.2 共享接口（平台通用）

```js
JobCards.create({
  appPath: '/nano-banana',        // 不假设在 iframe 里；缺省从 location.pathname 推断
  thumbKind: 'image',             // 'image' 用 <img loading=lazy>；'video' 用 <video preload=metadata>
  openPreview: (items, index) => {},   // 图片查看器 / 视频播放器
  onCancel: (jobId, status) => {},
})
```

`JobCards` 是全局单例（`window.JobCards`，由 `<script>` 引入后自注册）。
`create(config)` 返回**绑定该 config 的实例**，实例上提供渲染与交互：

```js
var cards = JobCards.create({ appPath: '/nano-banana', thumbKind: 'image' });
cards.renderGrid(gridEl, jobs);      // 任务矩阵
cards.buildTile(job);                // 单个卡片元素
cards.renderDetail(bodyEl, job);     // 详情弹窗
```

**纯逻辑**（`isCancellableJob`、`jobDisplayStatus`、`mergeJobWithActivity`）
同时直接挂在 `JobCards` 上，不需要实例化即可使用 —— 这样只用到状态判定的调用点
不必先 `create()`。

### 4.3 字段固定

详情弹窗恒为四格，顺序与 seedance 一致：

```
Seed / 运行耗时 / 使用模型 / 用户
```

- `Seed`：取不到真实种子时显示 `随机`（后端尚未回写 actual seed）。
- `用户`：`username` 为空时显示 `—`，**整格不再消失**。
- `运行耗时`：运行中状态每秒刷新一次（需要 `data-meta="runtime"` 与定时器，
  关闭弹窗时清理）。

### 4.4 错误处理

- 注入回调缺失 → 降级到内置默认（不抛错）。
- 字段缺值 → 显示 `—`，不隐藏整格。
- `appPath` 未提供 → 从 `location.pathname` 推断，兼容独立模式。
- `job` 为 null / 缺 `job_id` → 提前返回，不渲染半个弹窗。

### 4.5 分批落地（每批单独提交）

| 批次 | 内容 | 验证 | 是否碰应用 |
|---|---|---|---|
| B1 | 建 `shared-ui/job-cards.js` + `tools/sync-ui.py` + 一致性测试 | 测试通过 | 否 |
| B2 | 接入 seedance：删本地实现，改调共享 API | 浏览器实测 + 测试 | 是 |
| B3 | 接入 nano-banana | 同上 | 是 |
| B4 | 补 `model-capabilities.js` 进同步与测试，修掉漂移 | 测试通过 | 是（仅该文件） |

每批用 `git commit --only <路径>` 提交，绝不 `git add -A`。

## 5. 验证策略

1. `node tests/test_job_cards_in_sync.mjs` —— 同步一致性与接口契约。
2. `python tests/run_tests_local.py` —— 既有回归。
3. `node --check` 两个 app.js 与共享源。
4. **浏览器实测**（B2 / B3 各一次）：新建任务 → 卡片出现 → 打开详情 →
   确认四格字段与耗时刷新 → 点「活动」tab 确认不再有第二套界面。
5. 实测判据：**进程提供的文件确实是最新版**（HTTP 取回后字符串检查），
   避免再出现「服务端是新的、用户看的是缓存的旧版」这类误判。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **并发写手**：多个会话同时在改同一文件（`CLAUDE.local.md` 已多次记录，含改动被卷进别人提交） | 每批改完立刻 `git status` 确认；只用 `git commit --only`；判断改动集用**文件 mtime**，不靠 `git status`（本机工作区长期是脏的） |
| 独立模式失效 | `appPath` 可推断，且不做任何「必须在 iframe 内」的假设 |
| 两个应用行为差异被抹掉（图片 vs 视频） | 缩略图与预览通过回调注入，**不统一**这两处 |
| 用户在浏览器看到缓存旧版 | 部署后先核实服务端文件，再让用户硬刷新；`.sh` 与 `.py` 双份同步脚本避免漏同步 |
| 改动面大、一次改崩两个应用 | 严格分批：B1 不碰应用；B2、B3 各自独立提交与验证 |

## 7. 未决事项

- **C 阶段范围**：Portal 内部其实还有三套（`_dmBuildHistCard` / `openVpJobDetail` /
  `openDetail`），且 Portal 与自由创作**没有 `TERMINAL_STATUSES`**，状态处理更原始。
  C 的详细设计需要单独立项。
- `Seed` 的真实值：需要后端把实际种子回写进任务记录，属独立需求（用户已表达
  「同样的提示词 + 种子应当复现同样结果」的期望）。本次只把字段位置留好。
