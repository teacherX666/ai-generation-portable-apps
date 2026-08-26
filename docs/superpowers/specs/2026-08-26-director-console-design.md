# 导演台（Director Console）设计

日期：2026-08-26
状态：待用户审阅

## 目标

在整体 Portal 的**右侧边栏**加一个「导演台」面板，给使用者一种 agent 式体验：输入信息 → 选择要用的 skill → 输出处理后的提示词 / 图片。第一批 skill：

1. **提示词优化**（DeepSeek）：把粗糙描述润色成高质量提示词
2. **提示词扩写**（DeepSeek）：一句话扩写成详细场景描述
3. **文生图**（火山方舟 Seedream）：提示词 → 图片

交互形态：**单步 + 可串联**——提示词类结果一键填入文生图输入框（不自动触发，用户点「生成」才出图）。

## 方案对比与推荐

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 新子应用 `director/`（推荐） | 独立 app.py + 端口 8895/8896，apps.json 注册，portal 右侧栏原生 Vue 组件调用 `/director/*` | 符合项目模式（每应用单 app.py）；`POST /api/jobs` 已在 portal 统计白名单 → 发 `X-Job-Id` 即自动计数；portal 后端零改动；后续加 skill（视频/参考图）只改 director |
| B. portal/app.py 内嵌端点 | 不新增端口，但 portal/app.py 已 2000+ 行，需把 DeepSeek/Ark 逻辑塞进聚合器 | 职责混杂、统计/代理/生成耦合，否决 |
| C. 复用现有子应用 API | 前端编排 nano-banana 出图 + seedance 优化提示词 | 用户已选直连方舟；nano-banana 无优化端点、部分 t8star key 已禁用、串联体验割裂，否决 |

## 架构

```
portal (9090)                          director (8895, 由 portal 拉起)
├── static/index.html                  ├── app.py            ← stdlib ThreadingHTTPServer
│   ├── 右侧边栏容器（tab-panels 之外，可折叠）│   ├── SKILL.md         ← 提示词优化/扩写系统提示
│   └── v-scope="DirectorApp()"        ├── providers.json    ← 方舟 base_url/model/比例档（committed）
├── static/app.js  DirectorApp()       └── state/secrets.json← ark/deepseek key（gitignored）
├── static/styles.css  右侧栏布局
└── apps.json  director AppSpec 条目   → portal 负责拉起进程 + /director/* 反代
```

- **进程拉起**：portal `start_all` 按 apps.json 逐个 Popen（`cwd=director`，env 注入 `CORS=1` + `PORT`），director 与 nano-banana 同款 stdlib 引擎。**plist 无需改动**。
- **反代**：`/director/*` → 127.0.0.1:8895（portal 按 apps.json 名字路由，与现有子应用一致）。
- **统计（核心红线）**：director 的 `POST /api/jobs` 天然命中 portal `_is_job_request` 白名单（`/api/jobs` 前缀已在列），创建任务时响应 200 且带 `X-Job-Id` 头 → 用量自动按「张」登记，**portal/app.py 无需任何统计改动**。提示词优化/扩写是 DeepSeek 调用、不出图，不计入用量（与 seedance `/api/optimize-prompt` 不计的现状一致）。

## 组件设计

### director/app.py（新子应用）

端点：

| 端点 | 方法 | 行为 |
|---|---|---|
| `/api/config` | GET | 返回模型名、9 档比例（复用飞书 agent `IMAGE_ASPECT_RATIOS`：1:1/4:3/3:4/16:9/9:16/3:2/2:3/21:9/9:21）、默认数量 |
| `/api/optimize-prompt` | POST | 入参 `{text, mode: refine\|expand, style?}`；DeepSeek chat（SKILL.md 作 system，含「非交互模式」指令）→ 出 `{prompt, raw}`；超时 120s |
| `/api/jobs` | POST | 入参 `{prompt, aspect_ratio, count}`；校验比例档与 count(1-4)；调方舟 `POST /images/generations`（同步接口，提交即终态）→ 图片落 `outputs/` → **200 + `X-Job-Id` 头** + job JSON |
| `/api/jobs/{id}` | GET | 状态与结果 URL（`/outputs/xxx.png`） |
| `/outputs/*` | GET | serve_file（图片；预留 Range 支持） |

- 密钥：`state/secrets.json`（gitignored）存 `ark_api_key`、`deepseek_api_key`；`providers.json`（committed）存 base_url、模型名、默认值——沿用 nano-banana 的 providers/secrets 双文件模式，**不把 key 写进 app.py 常量**。
- 端口：生产 8895、测试 8896（`os.environ.get("PORT", "8895")`）。不涉及 Windows release zip（导演台是 Portal 服务端面板），无 100 端口窗口要求。
- 错误处理：上游非 200 时把方舟/DeepSeek 的 message 翻译成中文错误返给前端；job 失败状态 = `failed` + `error` 字段。

### portal 前端（右侧栏）

- **index.html**：在 `tab-*` 面板之外新增右侧栏容器（`<aside class="director-sidebar">`），内含 skill 下拉（优化/扩写/文生图）、输入 textarea、参数区（风格、比例、数量）、执行按钮、结果区（提示词 + 复制 + 「填入文生图」按钮 / 图片网格 + blob 下载）。可折叠：折叠按钮收成竖条，展开恢复。
- **app.js**：`DirectorApp()` 组件（PetiteVue，与 DreaminaApp 同款写法）。串联逻辑 = 把优化/扩写结果写入文生图输入框，**不自动提交**。所有 `api()` 调用检查 `res.ok`（项目铁律，禁止乐观更新）。图片下载复用 `_blobDownload`（自签证书下 `<a download>` 直连会失败）。
- **styles.css**：Portal 布局加右侧列（`grid-template-columns: ... minmax(0,1fr) 320px` 变体或 fixed + 内容区 margin），右侧栏 `position: sticky`、折叠过渡动画；窄屏（<1200px）右侧栏变悬浮层。iframe 类 tab 面板内容区自动收窄，不影响现有面板。

## 数据流

1. 用户选 skill（优化/扩写）→ 输入信息 → `POST /director/api/optimize-prompt` → 结果区展示提示词 → 「填入文生图」把结果放进文生图输入框
2. 用户切到文生图 skill → 选比例/数量 → 点「生成」→ `POST /director/api/jobs` → 轮询 `GET /api/jobs/{id}` → 图片网格展示（Seedream 同步返回，轮询一次即终态，轮询逻辑保留以兼容未来异步 provider）

## 错误处理

- 前端：所有请求 `res.ok` 检查；失败显示后端返回的中文错误（如「方舟鉴权失败：InvalidApiKey」「DeepSeek 超时」）
- 后端：DeepSeek 120s 超时；方舟同步调用带超时与重试（429 时等 2s 重试 1 次）；job 状态机 pending → done/failed；异常均落 job.error 并透传

## 测试

1. **单元测试**（`tests/test_director_*.py`，pytest + fake DeepSeek/方舟）：optimize-prompt 参数校验与模式分流、jobs 比例档校验（非法比例 400）、X-Job-Id 响应头存在、方舟错误翻译、secrets 缺失时的友好报错
2. **前端**：右侧栏折叠/串联填写的 PetiteVue 行为用现有 node 测试方式（参考 `tests/test_seedance_topic_render.mjs`）做最小冒烟
3. **live 实测**（部署后）：真实跑一次「优化 → 文生图」，出一张真图验证端到端（接受少量出图费用，符合「真实复现优先」原则）；确认统计页「导演台」当日计数 +1

## 部署

- `portal/apps.json` 加 director 条目；portal 三件套（index.html/app.js/styles.css）+ 新 `director/` 目录
- 重启仅需 `launchctl kickstart -k gui/$(id -u)/com.ai-portal`（plist 不变）。**Portal 重启会终止全部子应用与进行中任务**，执行前与使用者协调、确认无进行中任务
- director 密钥由管理员填入 `director/state/secrets.json`（与 nano-banana secrets 同款，gitignored）

## 统计核对（核心红线）

- 出图走 `POST /api/jobs` + `X-Job-Id` → 白名单已覆盖，自动按「张」计数
- 提示词优化/扩写不计用量（无 X-Job-Id、路径不在白名单）——与 seedance 行为一致
- `apps.json` 条目带 `metrics: ["images"]`、`unit_label: "张"`、`stats_combine: "images_or_seconds"`，统计页自动出现「导演台」分组
