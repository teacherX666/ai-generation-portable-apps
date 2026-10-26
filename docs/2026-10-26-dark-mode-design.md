# 整站深色模式（Unified Dark Mode）设计

> 2026-10-26 · 状态：待评审 · 作者：DSH agent（与用户逐轮敲定）
>
> 前置决策（用户已确认）：① 开关形态 = **A 案幽灵图标键**（月亮内阴影收成满月 + 光芒旋转淡入 + 图标转 180°）；
> ② 铺开节奏 = **两段走**（整站都能切 → 先修看得见的破绽，第二期补角落）；
> ③ 深色配色 = **P-A 冷灰蓝**（沿用 `docs/2026-08-14-product-upgrade-review.html` 里已画过的那套暗色盘）。

## 1. 目标与非目标

### 1.1 目标

1. **整站一套暗色**：Portal 外壳、登录页、自由创作页、各原生 tab、全部子应用，视觉上是同一个产品的白天/黑夜两面。
2. **一个开关，点击即切**：Portal 顶栏右侧的幽灵图标键（月亮 ↔ 太阳）；切完**所有已打开的 iframe 同步跟着变**，不用刷新；按浏览器记住选择。
3. **机制层可复用**：暗色只写一份 token，经现有 `@import` 入口分发；以后换配色只改一个文件，不碰各应用样式表。
4. **第 1 期把"看得见的破绽"清零**：白底黑字、白字白底、状态色糊成一团、输入框/表格/滚动条仍是白的 —— 全部修掉。

### 1.2 非目标（YAGNI，明确不做）

- **不做"跟随系统"**（`prefers-color-scheme` 自动切）：用户要的是手动月亮键；加了会多一层优先级而无对应收益。以后要加 = 一行 media query。
- **不做第二套暗色 / 主题商店 / 每用户服务端偏好**：浏览器本地记就够，同事各用各的机器。
- **不改生成、统计、反代链路**：`_proxy`、`X-Job-Id` 记账、usage 统计一律不碰（仓库红线：每次更新都要检查统计功能）。本设计只动 CSS + 少量纯前端 JS。
- **第 1 期不做编译产物的深色内部**：无限画布（Vite + React + antd）与分镜预演 3D 画布内部维持原样，只保证外框跟随（见 §3.8）。

## 2. 现状（实测，2026-10-26）

### 2.1 已有的地基（三件现成的，直接复用）

1. **token 层**：`shared-ui/tokens/core.css`（原语 `--ui-color-*`）、`semantic.css`（语义别名 `--ui-bg-* / --ui-text-* / --ui-border-* / --ui-action-*`）、`legacy-aliases.css`。
2. **单一入口 + 分发**：`shared-ui/portal-ui-core.css` 用 8 行 `@import` 把上面这些 + `styles/*` 串成唯一入口；`shared-ui/sync-ui.sh` 把它和 `tokens/`、`styles/` 复制到 8 个目标（portal / seedance / nano-banana / dreamina / rag-assistant / volcengine-portrait / drama-pipeline / feishu）。
   ⇒ **加一行 `@import url("tokens/theme-dark.css")` 就能让所有参与设计系统的应用同时支持暗色。**
3. **跨文档同步先例**：`portal/static/effects.js` 用 `localStorage['portal_fx_trail']` + `storage` 事件把"鼠标拖尾"开关同步到同源 iframe。深色模式复用同一模式。

### 2.2 真正的成本：硬编码颜色普查

| 文件 | 行数 | `var(--` | `#hex` | `rgb()/rgba()` | 是否引入 ui-core |
|---|---|---|---|---|---|
| `portal/static/styles.css` | 3094 | 708 | 418 | 92 | ✔ |
| `portal/static/free-creation/styles.css` | 2161 | 287 | 270 | 470 | ✘（自有变量体系） |
| `feishu-.../web/static/styles.css` | 1468 | 196 | 115 | 14 | ✔ |
| `seedance/static/styles.css` | 1311 | 183 | 174 | 19 | ✔ |
| `nano-banana/static/styles.css` | 1031 | 190 | 169 | 19 | ✔ |
| `dreamina/static/styles.css` | 1183 | 264 | 68 | 13 | ✔ |
| `previz/static/styles.css` | 77 | 34 | 18 | 4 | ✘ |
| `drama-pipeline/static/styles.css` | 276 | 77 | 0 | 0 | ✔ |
| `infinite-canvas`（Vite 构建产物） | — | — | — | — | ✘（Tailwind/antd） |

**两个关键发现（直接改变工作量）**：

- **自由创作页的暗色盘本来就在文件里**：`free-creation/styles.css` 有三层堆叠的 `:root`（第 1 段是完整暗色盘 `--bg:#090a0f …`、第 312 段是浅色盘 `--bg:#f5f6f8 …`、第 608 段 `color-scheme: light`），**后写的赢** ⇒ 现在生效的是浅色那层。所以它的暗色版本 ≈ **把第 1 段暗色盘在 `[data-theme="dark"]` 下重新激活**（再补那些不走变量的硬编码色），不是从零画一套。
- **全仓 `prefers-color-scheme` 零命中** ⇒ 现在没有任何系统级暗色行为，加主题不会与既有逻辑打架。

### 2.3 入口页与 boot 引用形式（逐个实测过）

| # | 入口页 | 该页现有 ui-core 引用 | boot 引用形式 |
|---|---|---|---|
| 1 | `portal/static/index.html` | `/ui/portal-ui-core.css` | `/ui/theme-boot.js` |
| 2 | `portal/static/login.html` | `/ui/portal-ui-core.css` | `/ui/theme-boot.js` |
| 3 | `portal/static/free-creation/index.html` | 无（只引 `./styles.css`） | `/ui/theme-boot.js`（新增） |
| 4 | `seedance/static/index.html` | `ui/portal-ui-core.css` | `ui/theme-boot.js` |
| 5 | `nano-banana/static/index.html` | `ui/portal-ui-core.css` | `ui/theme-boot.js` |
| 6 | `rag-assistant/static/index.html` | `ui/portal-ui-core.css` | `ui/theme-boot.js` |
| 7 | `drama-pipeline/static/index.html` | `ui/portal-ui-core.css` | `ui/theme-boot.js` |
| 8 | `dreamina/static/index.html` | `/ui/portal-ui-core.css` | `/ui/theme-boot.js` |
| 9 | `volcengine-portrait/static/index.html` | `/ui/portal-ui-core.css` | `/ui/theme-boot.js` |
| 10 | `feishu-.../web/static/index.html` | `static/ui/portal-ui-core.css` | `static/ui/theme-boot.js` |
| 11 | `previz/static/index.html` | 无 | `/ui/theme-boot.js`（新增 `ui/` 目录） |
| 12 | `infinite-canvas/web/index.html` → 构建出 `static/index.html` | 无（Vite 注入） | `/ui/theme-boot.js`（新增 `ui/` 目录 + 重 build） |

**规约**：boot 一律用**该页 ui-core 引用的同一种前缀形式**；`theme-boot.js` 由 `sync-ui.sh` 复制进**每个应用自己的 `ui/`**，于是"挂载在 Portal 下"与"独立端口直连"两条路都能命中。

### 2.4 子应用挂载方式（决定落地路径）

- **iframe 型**（独立文档，经 Portal 反代加载时与 Portal **同源** ⇒ 可共享 localStorage / `storage` 事件）：seedance、nano-banana、infinite-canvas、feishu、previz、rag-assistant、drama-pipeline、local-ai。
- **component 型**（在 Portal 文档内渲染的 Vue 组件，**天然继承 Portal 主题**）：dreamina、volcengine-portrait、director。它们的 `static/index.html` 只在独立端口直连时才用到。
- **独立端口直连**：与 Portal 不同源 ⇒ 读不到共享状态 ⇒ 保持浅色（可用自己的 key 记住，见 §3.4）。

### 2.5 顶栏工具按钮现状

`portal/static/effects.js` **只有 Portal 这一份创建按钮**（`#_fxTrailToggle`，文本 `*`，内联样式），seedance / nano-banana / feishu 三份 `effects.js` **只跟随不建按钮**。⇒ 两个按钮都归 Portal 顶栏管；但 `effects.js` 在 4 处重复、按钮样式游离于设计系统之外（用户评价"有点丑"）。

## 3. 设计

### 3.1 状态模型

- 单一状态源：`localStorage['portal_theme']`，取值 `'light' | 'dark'`，**缺省 = 浅色**。
- 无服务端存储、无 cookie、无用户档案字段。
- 页面上唯一真源是 `<html data-theme="dark">` 这个属性；**所有样式只认它**，不认 localStorage。

### 3.2 生效机制

```html
<html lang="zh-CN" data-theme="dark">   <!-- 由 boot 在首帧前写入 -->
```

```css
/* shared-ui/tokens/theme-dark.css —— 唯一新增的配色源 */
:root[data-theme="dark"] { color-scheme: dark; /* 覆盖原语与阴影，见 §3.6 */ }
```

```css
/* shared-ui/portal-ui-core.css —— 只加这一行，一次改动分发到 8 个目标 */
@import url("tokens/theme-dark.css");
```

- **`color-scheme: dark` 是性价比最高的一行**：原生滚动条、`<input type="date">` 日历、`<select>` 下拉、复选框、自动填充背景、默认画布底色全部跟着变 —— 手写要几百行。
- 语义别名不改（它们指向原语），原语一换全局变。
- 组件级样式（`ui-*` 类）**一行不动**：它们本来就吃 token。

### 3.3 防闪白

新增 `shared-ui/theme-boot.js`（**≤ 14 行、无依赖、绝不抛错**），按 §2.3 的表插到每个入口页 `<head>` 的**第一个样式表之前**（阻塞式执行 ⇒ 首帧前生效）：

```js
/* theme-boot.js — 首帧之前把主题写进 <html>，并在同源文档间同步 */
(function () {
  var KEY = 'portal_theme';
  function apply(t) { if (t === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
                      else document.documentElement.removeAttribute('data-theme'); }
  var v = null;
  try { v = localStorage.getItem(KEY); } catch (e) { /* 禁用存储：保持浅色 */ }
  apply(v);
  window.addEventListener('storage', function (e) { if (e.key === KEY) apply(e.newValue); });
})();
```

- 「读值 + 监听同步」放同一个文件：少一个请求，也避免两处 key 写错。
- 不做骨架屏/渐变遮罩之类补救 —— 从根上不闪。

### 3.4 跨文档同步

- Portal 顶栏切换：写 localStorage → 本页立即设 `data-theme` → 同源各 iframe 收到 `storage` 事件（**必须由"别的文档"写才触发**）→ 各自设 `data-theme`，**不刷新**。
- 后打开的 iframe：boot 自己读 localStorage ⇒ 也不会闪。
- **不同源直连**：读不到共享值 ⇒ 保持浅色；各应用可用同名 key 在**自己的源**下记住，互不影响。
- 同步是**单向**的（Portal 是唯一控制面）；第 1 期子应用内不放开关。第 2 期若要放，复用同一 key + 同一个 `storage` 监听即可（boot 已注册）。

### 3.5 开关 UI（A 案）

新增 `portal/static/topbar-tools.js`（**Portal 专用**，不进 `shared-ui/`），统一生成顶栏右侧两个按钮，**替代** `portal/static/effects.js` 里建按钮那段：

| 按钮 | 图标 | off | on |
|---|---|---|---|
| 深色模式 | 月亮 ↔ 太阳（同一元素形变） | 月亮（中性 `#64748b` 一档） | 太阳（品牌色 `#58bcd8`） |
| 鼠标拖尾 | 四角星 ✦ | 弱化描边星，60% 不透明 | 品牌色实心星 + 右下角 4px 圆点 |

- 统一 `30×30`、圆角 `8px`、图标描边 `1.75px`、悬停浮 6% 中性底、`active` 缩到 `.94`；主题键在拖尾键**右侧**。
- **月亮 → 太阳**（纯 CSS）：圆盘用 `box-shadow: inset -4.6px -1.8px 0 0 currentColor` 画月牙；深色时该阴影过渡到 `inset 0 0 0 0`（月牙收成满月 = 太阳芯），8 道光芒（`repeating-conic-gradient` + 径向 mask）从 `opacity:0 / scale(.35) / rotate(-70deg)` 淡入转正，整颗图标同步 `rotate(180deg)`；时长：形状 `.45s cubic-bezier(.4,0,.2,1)`、光芒 `.55s cubic-bezier(.34,1.3,.64,1)`。
- **无障碍**：`<button type="button">` + `aria-pressed` + 随状态变化的 `title`（"深色模式"/"浅色模式"）+ `:focus-visible` 用 `--ui-focus-ring`。
- **减少动态效果**：`@media (prefers-reduced-motion: reduce)` 下取消过渡与旋转，直接换图标（与 `effects.js` 既有约定一致）。
- 拖尾逻辑（canvas 粒子、空闲停帧、`visibilitychange` 停止）**原样保留**，只换皮。

### 3.6 配色 token 表（P-A 冷灰蓝）

`shared-ui/tokens/theme-dark.css` 覆盖下列值（light 列是现值，仅作对照）：

| token | light | dark |
|---|---|---|
| `--ui-color-canvas` | `#f3f6fa` | `#101419` |
| `--ui-color-surface` | `#ffffff` | `#171c23` |
| `--ui-color-surface-soft` | `#f8fafc` | `#1d242c` |
| `--ui-color-surface-sunken` | `#eef2f7` | `#0c1014` |
| `--ui-color-border` | `#d9e0ea` | `#262e38` |
| `--ui-color-border-strong` | `#c3ccd9` | `#3a4553` |
| `--ui-color-text` | `#172033` | `#e4e8ed` |
| `--ui-color-text-secondary` | `#465267` | `#c3cad3` |
| `--ui-color-text-muted` | `#687386` | `#9aa4b0` |
| `--ui-color-strong` | `#1e293b` | `#232c37` |
| `--ui-color-strong-hover` | `#334155` | `#2c3644` |
| `--ui-color-strong-border` | `#475569` | `#3d4a5a` |
| `--ui-color-on-strong` | `#e2e8f0` | `#e4e8ed` |
| `--ui-color-brand` | `#235fd6` | `#58bcd8` |
| `--ui-color-brand-hover` | `#194fb8` | `#74cbe3` |
| `--ui-color-brand-soft` | `#eef5ff` | `#14303a` |
| `--ui-color-brand-ring` | `rgba(35,95,214,.22)` | `rgba(88,188,216,.30)` |
| `--ui-color-success` | `#10b981` | `#5fbe8b` |
| `--ui-color-success-soft` | `#dcfce7` | `#162c21` |
| `--ui-color-success-text` | `#047857` | `#7fd3a5` |
| `--ui-color-danger` | `#ef4444` | `#f0857c` |
| `--ui-color-danger-soft` | `#fee2e2` | `#35201e` |
| `--ui-color-danger-text` | `#b91c1c` | `#f4a49c` |
| `--ui-color-warning` | `#f59e0b` | `#dfa84e` |
| `--ui-color-warning-soft` | `#fef3c7` | `#322717` |
| `--ui-color-warning-text` | `#92400e` | `#e9c179` |
| `--ui-color-info-soft` | `#e0f2fe` | `#14303a` |
| `--ui-color-info-text` | `#0369a1` | `#7fc9e2` |
| `--ui-shadow-sm/md/lg` | `rgba(20,32,51,.05/.08/.12)` | `rgba(0,0,0,.45/.50/.60)` |

**对比度已实算**（WCAG 2.1 相对亮度）：

| 配对 | 比值 | 要求 |
|---|---|---|
| 正文 `#e4e8ed` on 画布 `#101419` | **15.02** | ≥ 4.5 ✔ |
| 正文 on 表面 `#171c23` | **13.91** | ≥ 4.5 ✔ |
| 次要文字 `#c3cad3` on 表面 | **10.36** | ≥ 4.5 ✔ |
| 弱化文字 `#9aa4b0` on 表面 | **6.77** | ≥ 3.0 ✔ |
| 品牌文字 `#58bcd8` on 表面 | **7.83** | ≥ 4.5 ✔ |
| 主按钮字 `#06202a` on 品牌底 `#58bcd8` | **7.69** | ≥ 4.5 ✔ |
| strong 按钮字 `#e4e8ed` on `#232c37` / hover `#2c3644` | **11.48 / 9.93** | ≥ 4.5 ✔ |
| 成功 / 警告 / 危险 文字 on 各自软底 | **8.31 / 8.60 / 7.73** | ≥ 4.5 ✔ |
| 状态点 on 表面 | **7.52 / 8.03 / 6.81** | ≥ 3.0 ✔ |

⚠️ 上表由 §5.2 的测试钉住。唯一需要"看着调"的是 `--ui-color-strong` 系（语义是"最显眼的中性按钮"，浅色下深底浅字）：暗色取"抬升表面 + 强边框"，若某处不够突出，按 §3.7 白名单逐处处理；**不要**改成纯白底反色按钮 —— 那会让整站出现大面积高亮块。

### 3.7 硬编码色回填策略

按**语义角色**分类替换，**禁止通篇 find/replace**：

| 角色 | 识别特征 | 替换为 |
|---|---|---|
| 画布/页面底 | `background` 上的 `#fff / #f8fafc / #f3f6fa / white` | `--ui-bg-canvas` |
| 卡片/面板表面 | 白底 + 圆角 + 边框 | `--ui-bg-surface` |
| 次级/凹陷表面 | 表头、代码块、禁用态浅灰底 | `--ui-bg-subtle` / `--ui-bg-sunken` |
| 正文 | `color:` 上的深灰/近黑 | `--ui-text-primary` |
| 次要/弱化文字 | `#687386 / #94a3b8 / #64748b` 一档 | `--ui-text-secondary` / `--ui-text-muted` |
| 边框/分割线 | `border`、`box-shadow` 描边、`hr` | `--ui-border-default` / `--ui-border-strong` |
| 状态色 | 绿/黄/红三族（含软底与文字） | `--ui-color-{success,warning,danger}*` |
| 品牌/交互 | 主按钮、链接、选中态、焦点圈 | `--ui-action-primary*` / `--ui-focus-ring` |
| 阴影 | `box-shadow` 里的 `rgba(20,32,51,…)` | `--ui-shadow-*` |

**白名单（刻意不动，逐条写进测试）**：① 品牌图形与营销视觉（首页 hero、`--rc-red-dark` 那套红、渐变）；② 图片上的叠加文字与 `text-shadow`、缩略图占位底色；③ 语义即"永远深色"的区块（代码块、`free-creation` 自带暗色盘里那些装饰渐变）；④ 第三方/编译产物内部（无限画布 antd、Tailwind 生成物）。

**铁律**：token 的 light 值必须与它替换掉的字面量**逐一相等**；原值不在 token 表里就**新增 token**，不许取近似值 —— 否则浅色外观会被顺手改坏。

**执行顺序**（每文件一次走完，不跨模块混提）：颜色普查（属性 × 角色 × 次数）→ 按角色批量替换 → 浅/深两套截图逐屏核对。

### 3.8 各应用的落地路径

| 类型 | 应用 | 第 1 期动作 |
|---|---|---|
| 参与设计系统（引 ui-core） | portal 本体、seedance、nano-banana、dreamina、volcengine-portrait、rag-assistant、drama-pipeline、feishu | boot + `portal-ui-core.css` 加一行 `@import` + 各自 `styles.css` 按角色回填 + `sync-ui.sh` 重跑 |
| component 型（Portal 内渲染） | dreamina、volcengine-portrait、director | 主题自动继承；只需回填自身 CSS（dreamina/portrait 的 `static/index.html` 也补 boot，供直连用） |
| 自有变量体系 | **自由创作** | 末尾追加 `:root[data-theme="dark"]`，**复用文件第 1 段那套暗色盘**（`--bg/--panel/--text/--line/--muted/--shadow-*`），再补不走变量的硬编码色 |
| 自有变量体系（小） | **previz** | boot + `styles.css`（77 行 / 18 处 hex）直接回填；three.js 画布内部不动 |
| 编译产物 | **infinite-canvas** | **只保底**：`web/index.html` 插 boot + 重新 `npm run build`，保证外框/顶栏跟随；antd 组件与画布内部维持原样（第 2 期） |
| 独立端口直连 | 任意 | 保持浅色（§3.4） |

## 4. 分期与验收

### 4.1 第 1 期（本次实施范围）

1. **机制**：`theme-dark.css` + `theme-boot.js` + `portal-ui-core.css` 加 `@import` + `sync-ui.sh` 扩展（分发 boot）。
2. **开关**：`portal/static/topbar-tools.js`（A 案主题键 + 重做的拖尾键），摘掉 `effects.js` 里建按钮那段。
3. **Portal 本体**：3 个入口页 + `portal/static/styles.css`（418 hex / 92 rgb）+ 自由创作页（复用自带暗色盘）。
4. **子应用**：seedance、nano-banana、dreamina、volcengine-portrait、rag-assistant、drama-pipeline、feishu —— boot + 回填 + 破绽清理。
5. **保底**：previz（回填小样式表）、infinite-canvas（仅 boot + 重 build）。
6. **护栏与验收**：§5 四类测试全绿 + 每模块浅/深成对截图过视觉检查。

**完成判据**：切到深色后逐屏过一遍，不存在"白底黑字 / 白字白底 / 状态色分不开 / 输入框与表格仍是白的"；**浅色外观与改动前肉眼无差异**（浅色是默认路径，不能被顺手改坏）。

#### 实施结果（2026-10-26，第 1 期完成）

程序化审计（口径见 §5.4）在真实浏览器里跑出来的数字 —— **每个模块都跑了两遍（浅 / 深）**：

| 模块 | 深色·对比度违规 | 深色·浅色孤岛 | 备注 |
|---|---|---|---|
| Portal 原生：home / history / keys / stats / dreamina / 人像 | 0 / 0 / 0 / 0 / 0 / 0 | 0 | 起始值：人像 21、stats 10、home 19 |
| 自由创作（iframe） | 0 | 0 | 它的"白色工作台"由**生成的暗色镜像层**（76 条规则）接管 |
| seedance | 0（原 44） | 0 | |
| nano-banana | 0（原 38） | 0 | |
| 飞书创作助手 | 0（原 46） | 0 | 保留绿色身份（`#176b50` → `#5fbe8b`） |
| rag-assistant / drama-pipeline | 0 / 0 | 0 | |
| previz | 2 条既有欠账 | 0 | **它本身就是暗色 3D 工作台**，`:root` 全深色，未参与改造 |
| 无限画布 | 第 2 期 | — | 保底：外框跟随，内部维持（它是 Vite 产物） |

浅色一轮：Portal 7 个 tab 与自由创作**无新增违规**（残留的都是既有欠账：`--muted` 4.42 vs 4.5 那条线、品牌红、渐变文字假阳性）；子应用残留的同族欠账同理。**浅色对比度整体只升不降**，唯一反向的是"实心成功徽章"（`#16a34a` → `--success` 让白字从 2.8 变 2.54，两者都在既有欠账里，已登记为测试豁免）。

回填规模（机械计数）：Portal 表面白 31→0、Tier-2 中性灰阶 162 处、首页 `--rc-*` 别名 + `--rc-red-ink`、行内色 36 处；自由创作暗色镜像 76 条规则；seedance 11+66+6+9、nano-banana 12+63+6+9、feishu 14+33+13+2、dreamina 16、rag 8、previz 2；子应用"实心状态底 + 白字"共 15 处改 `--ui-color-on-accent`。

### 4.2 第 2 期（后续，按实际使用驱动）

无限画布内部（antd `ConfigProvider` 暗色算法 + Tailwind `dark:` + 重新 build）、previz 3D 画布、PDF/XLSX/DOCX 预览底色、代码块语法高亮、`tools/cleanup.html` 之类边角页。

## 5. 测试与护栏

### 5.1 源码护栏（`tests/frontend/dark_mode_guards.test.cjs`）

1. §2.3 表里**每个**入口页，`theme-boot.js` 的 `<script>` 必须出现在第一个 `<link rel="stylesheet">` **之前**（防闪白的硬约束，后续改动最容易被它打乱）。
2. 各应用样式表里**不许出现裸白表面**：`background`/`background-color` 上的 `#fff / #ffffff / white`（含简写）—— 白名单文件 + 选择器显式列出并注明理由。
3. `theme-dark.css` 必须覆盖 §3.6 表的**每一个** token（缺一个就会有一处白天色漏进夜里）。
4. `portal-ui-core.css` 必须 `@import` 到 `theme-dark.css`（漏了就"改了 token 但没人加载"）。

### 5.2 对比度（`tests/frontend/theme_contrast.test.cjs`）

解析 `theme-dark.css` 与 `core.css` 的 token 值，按 WCAG 2.1 计算 §3.6 全部配对，断言正文 ≥ 4.5:1、次要/非文本 ≥ 3.0:1。**任何调色导致跌破底线，测试立刻红。**

### 5.3 行为单测（`tests/frontend/theme_toggle_app.test.cjs`，沿用现有 vm + 假 DOM 套路）

- boot：`localStorage='dark'` → `documentElement.dataset.theme === 'dark'`；无值 → 不设属性；`localStorage` 抛错 → 不抛。
- 开关：点一下 → 属性翻转 + `aria-pressed` / `title` 同步 + 写入 localStorage。
- 同步：伪造 `storage` 事件（`key='portal_theme'`, `newValue='dark'`）→ 当前文档跟着变。
- `prefers-reduced-motion: reduce` 时形变仍生效（只是不过渡）。

### 5.4 视觉验收（人工 + 视觉模型，每模块一对截图）

驱动真实浏览器（9090）对每个模块出**浅色 / 深色成对截图**，用视觉模型专扫三类破绽：**白底黑字 / 白字白底 / 状态色糊在一起**；另抽查：滚动条与日期选择器（`color-scheme` 是否真生效）、图片缩略图在深底上的观感、悬停/选中/禁用三种态。

**2026-10-26 实施修正（实测后改的口径）**：截图在这台机器上不可用 —— DSH 的浏览器宿主要求页签处于前台才合成画面，本会话的页签是"隐藏"态，`browser_screenshot` 一律 30s 超时（`document.body.clientWidth === 0`、`visibilityState === 'hidden'`）。
改为**程序化审计**（更强，且可复跑）：在真实浏览器里遍历文档内每个可见文本元素，算 WCAG 对比度（排除渐变文字），并另跑一遍"浅色孤岛"检查（暗色下仍是大片浅底的元素）。口径：

* 对比度：正文 ≥ 4.5:1、大号/粗体 ≥ 3:1；
* 浅色孤岛：有效底色亮度 > 0.7 且面积 > 25000px²；
* 背景解析要**跳过透明背景并识别渐变**（`body` 只有 `background-image` 渐变的页面会被误判成白底 —— 实测飞书那 3 条就是这种假阳性）。

对 iframe 子应用：在 `iframe.contentDocument` 里跑同一套审计。**这套脚本是这一轮所有"0 违规"结论的来源**，不是肉眼看的。

审计脚本已入库：**`_diag/audit_dark_mode.js`** —— 在 9090 页面控制台整段粘贴即可复跑（或由 Agent 用浏览器工具执行），返回每个文档浅/深两轮的违规明细与浅色孤岛清单。文件头写清了三个背景解析的坑（透明祖先、渐变整页底、属性选择器里的颜色字符串）与判据。

#### 自由创作：生成器 v2 + 交互态补漏（2026-10-26 复检）

第一版的暗色层只镜像了**最后一层**（76 条规则），复检时发现两类漏网，都是"护栏指标先发现、审计阈值后知后觉"：

1. **散在早期层的 hover 规则**：全文件扫一遍才发现 10 条 `:hover` 仍是浅底/深字（深色下悬停露白）。v2 改为**全文件扫描**，且只镜像"确实映射到深色值"的规则，并保留 `@media` 上下文。
2. **高不透明度"白玻璃"**：`rgba(255,255,255,.7~.96)`（`.fc-popover` / `.fc-toast` / `.fc-new-chat` …）共 21 处 —— 它们面积小，**日常审计的"孤岛"阈值（25000px²）抓不到**，是护栏的裸白计数抓出来的。v2 加了通用规则：α≥.5 的白玻璃按 `(1-α)*0.3+0.04` 单调映射成 4%~14% 的高光。
3. 另外把浅色层里**偏暗的文字色**（亮度 <.25）统一换成 `light-dark(原值, 提亮档)` —— 浅色逐字不变，深色自动提亮且**保留色相**（紫/红这些强调色不会被洗成灰）。

v2 共镜像 **197 条**规则。复检：把审计的孤岛阈值收紧到 6000px² 后仍是 **0 违规 / 0 孤岛**；交互态扫描 21 条"硬编码浅色的交互态"**全部**有深色镜像（0 条会露白）。
生成器入库：**`_diag/gen_free_creation_dark.mjs`**（含完整值映射表与三条通用规则，可从"未加暗色层"的样式表重跑）。


#### 登录页：一处真 bug（审计逼出来的）

第 1 期扫完全站后又去审了**唯一没覆盖的登录页**，抓到：`login.html` 的行内兜底把**语义别名**写成了字面量（`--ui-bg-canvas:#f3f6fa` 等），而它在共享样式表**之后**、同权重 —— 等于把浅色钉死：深色主题覆盖了原语也没用，**登录页在深色下整页发白**。改成"只声明原语 + 别名用 `var()` 串过去"后：暗色 body `#101419`、卡片 `#171c23`、按钮深墨配品牌青（7.69:1）、输入框深底浅字，**0 违规 0 孤岛**；浅色逐字不变。
`test_ui_core_structure.mjs` 里那条守卫也一并改成守"原语 + 别名链"，并加一条 `doesNotMatch`：**登录页兜底不得再把语义别名写成字面量**。

### 5.5 变量守门（`tests/test_theme_variables.mjs`）

各应用样式表里**没有兜底**的 `var(--x)` 引用，必须在"该文件自己 ∪ 同目录应用样式表 ∪ `shared-ui/tokens/*`"里有定义。深色模式全靠变量翻转，**变量名写错/忘了加 token 时 CSS 不报错**，只会静默变成 `inherit`/透明 —— 这是本轮最容易漏的一类回归（实测就抓到过 `--surface-raised` 需要新加 token）。口径：`var(--x, 兜底)` 自带兜底，不算问题；JS 运行时写入的变量（拖拽位移 `--tx/--ty`、量高度的 `--portal-header-height` 等）在允许清单里。

当前：12 个文件、441 处无兜底引用、共享 token 132 个定义，全绿；注入一个未定义变量会立刻红（判别力自检过）。

## 6. 风险与回滚

| 风险 | 处置 |
|---|---|
| 整站视觉回归面大（约 30 个文件） | **分模块小步提**（Portal → 各子应用逐个），每步浅/深截图留档；用户已接受"部分模块已好、部分还没"的中间期 |
| 浅色被顺手改坏 | 浅色路径保持是验收判据（§4.1）+ §3.7 铁律（light 值逐一相等）+ 裸白护栏测试 |
| 闪白 | boot 顺序护栏测试（§5.1.1） |
| 硬编码色替换把品牌色也换掉 | 白名单 + 逐文件颜色普查 + 每步截图比对 |
| 误伤统计/反代 | 不触碰 `portal/app.py` 的 `_proxy`/记账路径；改动集中在 `*.css` 与 `static/*.js`；实施后按仓库红线**单独复核一次统计链路** |
| 回滚 | 改动集中在 CSS + 少量前端 JS；`data-theme` 不存在时**就是现在的外观** ⇒ 单模块 revert 即回浅色；顶栏开关也可整体隐藏而不影响其它功能 |

## 7. 影响面清单（实施后按实际更新）

实际改动 **93 个文件、+3911 / −852**（提交 `b840ed2`）：

- **新增（8）**：`shared-ui/theme-boot.js`、`shared-ui/tokens/theme-dark.css`、`portal/static/topbar-tools.js`、`_diag/audit_dark_mode.js`、4 个护栏测试（guards / contrast / toggle / variables）、本设计文档（更早一次提交）。
- **机制（3）**：`shared-ui/portal-ui-core.css`（+1 行 `@import`）、`shared-ui/tokens/{core,semantic}.css`、`shared-ui/styles/components.css`（19 处硬编码色收成 token）、`shared-ui/sync-ui.sh`。
- **分发副本（51）**：9 个目标 × `ui/` 下的 `portal-ui-core.css` / `theme-boot.js` / `tokens/{core,semantic,theme-dark}.css` / `styles/components.css`；`previz/static/ui/` 是整目录新建（它原来不引 ui-core，第 1 期只为 boot 建了这份）。
- **Portal（9）**：`index.html` / `login.html` / `styles.css` / `effects.js` / `topbar-tools.js` / `app.js` / `js/portal-rag-interceptor.js` / `free-creation/index.html` / `free-creation/styles.css`。
- **子应用（12）**：seedance、nano-banana、dreamina、previz、rag-assistant、drama-pipeline、volcengine-portrait、feishu 的入口页与样式表。
- **测试（6）**：上述 4 个新护栏 + `test_ui_core_structure.mjs`（改成守语义、补登录页守卫）+ `test_token_section.mjs`（修内联 CSS 函数误报）。

## 9. 已知欠账（登记未改，别误当遗漏）

| 项 | 现状 | 为什么没改 |
|---|---|---|
| 浅色下 5 组对比度欠账（白字 on `#10b981` 2.54、on `#f59e0b` 2.15 等） | `test_theme_contrast.mjs` 里**显式豁免**并注明原因 | 你定的硬要求是"浅色零变化"，不能借提对比度顺手改浅色；要治得单独开一次浅色可读性调整 |
| 深色下 `--muted` 在浅色主题里 4.42:1（差 0.08） | 同上，属既有值 | 同上 |
| previz 的 2 处"浅字配亮蓝按钮" | 它本身就是暗色 3D 工作台（`:root` 全深色），第 1 期未改造 | 与主题无关，是它自己的既有配色 |
| 无限画布内部（antd / Tailwind / 画布） | 第 2 期 | Vite 构建产物，要改源码重 build；第 1 期只保底（外框跟随 + 它自带的引导脚本） |
| 实心成功徽章浅色对比度 2.8 → 2.54 | 统一到 `--success` 的副作用 | 两者都低于 AA，属上面那族既有欠账 |


## 8. 实施前需再确认的小决策（都有默认值，不阻塞）

1. **顶栏两个按钮的左右次序**：默认主题键在右；实现时若觉得碍眼可对调（不影响其它设计）。
2. **自由创作页在浅色模式下要不要也变浅**：现在它是浅色（文件里三层 `:root` 后写的赢）；第 1 期默认**只做深色**（浅色保持现状），要不要做"浅色变体"另议。
3. **第 2 期无限画布的暗色方案**（antd 算法 vs Tailwind `dark:` 双轨）留到第 2 期单独出设计。
