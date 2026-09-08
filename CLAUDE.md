# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！

！！！优先级：时间 >> token！！！用户要的是尽快修好、一次到位。宁可多花
token 做 live 实测 / 并行探查 / 端到端验证把问题一锤定音，也不要为省 token
靠猜测反复打补丁、反复让用户重启验证。定位生产问题时，能真实复现就真实复现
（哪怕产生少量出图费用），不要用推断代替证据。

## 本机专属内容在 CLAUDE.local.md

运行状态快照（端口表、plist 环境变量、数据布局）、排障教训、上游同步记录等
**易变/本机专属内容在 `CLAUDE.local.md`（gitignored，不进仓库）**。
协作分支合并不覆盖它；新克隆的机器没有该文件也不影响理解仓库结构。

## 长任务防卡死：产出优先落盘，别攒在上下文

**症状**：写实施 plan / 长文档 / 多步方案时，会陷入"复述状态→摸信息→再复述"的循环，API
每断一次就丢工作、从头开始，用户看到"一直在工作但没产出"。

**根因**：`superpowers:writing-plans` 这类 skill 强制走 announce → 摸信息 → self-review
→ execution handoff 一串前置流程；内容全在上下文里、没落磁盘，断链就丢。系统 reminder
和 auto-mode 又会不断触发我复述状态而不是往下写。

**修复方式（对任何多轮长产出都适用）**：

1. **绕开 skill 的流程壳，直接 Write 文件**。skill 里的**格式指引**（TDD 五步、每步含
   代码 / 命令 / 期望输出、无 placeholder）值得保留；**流程指引**（announce、摸信息、
   self-review、handoff）跳过——它们不落盘、断链即丢。
2. **文件一落盘 = 一个 checkpoint**。下次续写从磁盘读，不依赖上下文。写完一个文件立刻
   commit 的心态：宁可多份短 plan，不要一份憋大的。
3. **分批产出而不是一次全写**。3 份 plan 就 3 次 Write，不要合成一份大 plan——大 plan
   写到一半断链是灾难，短 plan 断了只丢一份。
4. **信息够就写，不追求穷尽**。plan 的完整性靠"格式对了 + 决策对了"，不靠"摸到所有
   行号"。行号写错执行者能自己修，决策错了返工代价大得多。
5. **不要每轮口头解释"我在做什么"**。用户看到 Write 工具在跑就知道，反复复述是噪音。

## 项目定位

这是一个部署在**服务机**（用户本机 Mac）上的多子应用聚合平台，聚合 Seedance / Nano Banana / Dreamina / Volcengine Portrait 等 AI 生成能力，统一 Portal 前端 + 反向代理暴露给使用者。

**当前部署**：服务机通过局域网 HTTPS（`https://<局域网IP>:9090`，自签证书）向公司同事提供服务，同事只需浏览器即可使用，不需要在自己电脑上安装任何环境。**IP 每周会变**（DHCP），以启动日志 / 顶部标题栏 LAN 显示 / 页面顶部横幅为准，勿在文档写死具体 IP。

**后续演进方向**：可能迁移到公网服务器，让外部客户通过域名访问。因此设计上应尽量避免绑死「本机路径 / 本机 IP / 单机 launchd」这类假设——涉及主机名、证书、端口、路径的代码要留出配置化的余地，方便日后切换到域名 + 反向代理 + 正式证书的部署形态。

## Running

```bash
# Start everything (Portal + all sub-apps) on port 9090
./Start\ All.command

# Or manually:
cd portal && python3 app.py
```

Portal binds `0.0.0.0:9090` and auto-launches sub-apps on their fixed ports. Individual sub-apps can also run standalone:

```bash
cd seedance && python3 app.py    # port 8787
cd nano-banana && python3 app.py # port 8797
cd dreamina && python3 app.py    # port 8888
```

当前后端主要使用 stdlib（`http.server`、`threading`、`concurrent.futures`、`subprocess`），但**不是硬约束**。项目部署模型：用户本机（Mac）作为唯一后端服务器，公司其他电脑通过局域网浏览器访问，不分发后端代码给客户端。因此可以按需引入 pip 库；新加依赖时先与用户确认，并装到 launchd 使用的解释器（`/opt/homebrew/bin/python3.12`）下。客户端侧（浏览器里跑的 HTML/JS/CSS）才需要「无环境依赖」——不能引入构建工具链。

## Architecture

```
portal/           → Unified SPA + reverse proxy (port 9090)
├── app.py        → ThreadingHTTPServer: serves static/, proxies /<app>/* to sub-apps,
│                    tracks usage stats (by_ip), polls job completion, watchdog health loop
├── apps.json     → 子应用注册表（name / port_env / port_default / mount / 统计指标）
├── ark_errors.py → Ark 错误中文翻译表（seedance/nano-banana/volcengine-portrait 共享）
├── error_explainer.py → 规则未命中时的 doubao-seed 模型兜底解释（三级降级）
├── daily_report.py → 飞书日报（usage.json.by_user → CSV + AI 洞察 + 卡片）
├── cat_skins/    → 猫咪皮肤系统（概念库/实验服务/皮肤生成）
├── certs/        → 自签证书（IP 变化自动重生）
└── static/
    ├── index.html → 全部 tab；tab 按钮与面板硬编码，加子应用要手工改；面板 id 必须是 "tab-" + data-tab
    ├── app.js    → 各 tab 的 Vue 组件（生成表单/轮询/历史）
    ├── styles.css
    └── js/       → portal-{api,analytics,utils,shell,enhancements,rag-interceptor,module-registry}.js
                    （导航壳/帮助按钮/模块帮助 MODULE_HELP 配置在 enhancements.js）

seedance/         → Video generation (Seedance 2.0 via T8Star or Volcengine Ark)
├── app.py        → Full app: HTTP handler, job runner (ThreadPoolExecutor), file upload/download, archive system
├── providers.json → Provider configs (base_url, models, defaults per provider)
└── static/       → Standalone UI (used when running without Portal)

nano-banana/      → Image generation (T8Star OpenAI-style or Gemini)
├── app.py        → Same pattern as seedance
├── providers.json
└── static/

dreamina/         → Image/video via Dreamina CLI wrapper
├── app.py        → Wraps `dreamina` CLI tool, manages login/env, polls submit_id for results
├── config.json   → Runtime config (port, max_concurrent, poll intervals)
└── static/

previz/           → 分镜布局：浏览器 3D 素模摆放（14 关节木人 + 6 道具 + 景别五档相机），
│                   多镜头项目存档，渲染快照可下载 / 一键送画布（POST /infinite-canvas/api/v1/assets）
├── app.py        → stdlib 后端：项目 CRUD（state/projects/{id}/project.json）+ 渲染存档 + export.zip；无模型调用、不发 X-Job-Id
├── static/       → 纯静态前端：vendor three r170（ES module + import map，无构建链）；core.js 为 node 可测纯逻辑
└── tests/        → 后端 unittest + core.js node:test
```

> 生产子应用大多经 `*_ENGINE=fastapi` 环境变量跑 uvicorn `app_fastapi.py`（经 `_LegacyBridge`
> 桥接原 stdlib handle_* 处理器）；具体哪个应用跑哪个引擎见 CLAUDE.local.md 端口表。

## Key Patterns

**Sub-app structure**: Each sub-app is a single `app.py` with:
- `FALLBACK_PROVIDERS` dict (seedance/nano-banana) or `DEFAULT_CONFIG` (dreamina)
- `VALUE_FIELDS` set defining which form fields are extracted
- `run_job()` → spawns `run_one()` per concurrency slot via ThreadPoolExecutor
- `JOBS` dict (in-memory) holding all job state; not persisted across restarts
- `Handler` class extending `SimpleHTTPRequestHandler` with REST endpoints
- `/api/config` returns providers, models, key hint
- `/api/jobs` POST creates jobs, GET returns status
- Archives stored as `.seedance`/`.nanobanana`/`.dreamina` zip files in `archives/`

**Portal proxy**: `_proxy()` 全程流式转发（`shutil.copyfileobj`，64KB 块），**不缓冲响应体**。任务 id 由子应用通过 **`X-Job-Id` 响应头**上报，Portal 据此登记用量。登记的三个必要条件：POST 路径命中 `_is_job_request` 白名单、响应状态 200/201、`X-Job-Id` 非空。

> 早期实现是「读完整 body 提取 job_id」，因长任务阻塞代理线程已改掉。新增子应用若不发 `X-Job-Id`，统计会**静默不计数**（功能全正常、数字永远是 0）。**取消接口刻意不发 X-Job-Id（不计数）；重试接口发（新任务重新计费）。**

**Provider system** (seedance, nano-banana): `providers.json` defines available providers with `base_url`, `models[]`, `defaults{}`. Frontend `bindProviderSwitch()` rebuilds model dropdown and updates URL on provider change.

**Output naming**: When `output_name` is set, files are named `{name}-{index}.ext` for multi-concurrency or `{name}.ext` for single runs. Empty means timestamp-based auto-naming.

**Environment detection**: Portal sets `CORS=1` env var on sub-apps. Sub-apps check this to skip auto-opening browser and to add CORS headers.

## Important Constraints

- **Never overwrite git history** — always create new commits, never amend/force-push
- **第三方库按需使用** — 后端跑在用户本机，不分发；引入 pip 库前确认并装到 `/opt/homebrew/bin/python3.12`。客户端浏览器代码仍需零构建依赖
- **Jobs are in-memory** — restarting kills running tasks; coordinate with users before restart
- **Frontend changes are instant** — Portal serves with `Cache-Control: no-cache, no-store, must-revalidate`, clients get new version on refresh without restart
- **Backend changes require restart** — which terminates all sub-app processes and running jobs
- **CLAUDE.local.md 是 gitignored 的个人文件** — 更新状态快照/教训时改它，不要动共享的 CLAUDE.md（协作分支会提交各自的 CLAUDE.md 改动，合并不覆盖 local 文件）

## File Conventions

- `state/` — runtime JSON (usage, presets, activity logs); gitignored
- `outputs/` — generated files; gitignored
- `archives/` — user-saved presets as zip; gitignored, may contain API keys
- `logs/` — startup/debug logs; gitignored
- `providers.json` — provider/model configuration; committed
- Each app has exactly one `app.py` (no module splitting)

## 外部 API 参考

### 火山方舟私域虚拟人像 Asset API

- 端点：`https://ark.cn-beijing.volcengineapi.com/?Action={Action}&Version=2024-01-01`
- 鉴权：AK/SK **SigV4**（非 Bearer），Service=`ark`、Version=`2024-01-01`、Region=`cn-beijing`
- 所有请求 POST + `Content-Type: application/json`
- 10 个 Action：CreateAssetGroup / CreateAsset / GetAsset / ListAssets / ListAssetGroups / GetAssetGroup / UpdateAsset / UpdateAssetGroup / DeleteAsset / DeleteAssetGroup
- 素材状态：Processing（继续轮询）/ Active（可用）/ Failed
- ListAssets `Filter` 有效字段：GroupIds、GroupType、Statuses、Name（模糊）— **不含 AssetType**
- 图片限制：jpeg/png/webp/bmp/tiff/gif/heic；宽高比 (0.4, 2.5)；尺寸 (300, 6000)px；<30MB
- 视频生成引用：`asset://<asset_ID>`，多图 content 数组顺序 = text 在前 + image_url 依次 role=`reference_image`；prompt 用「图片1」「图片2」指代
- IAM 权限：`ark:*Asset*`
- 详细 body/response 字段：见项目内 `docs/` 或 `volcengine-portrait/` 实现

### Ark Files API（临时图片）

- `POST https://ark.cn-beijing.volces.com/api/v3/files`，Bearer Token，multipart，`purpose=user_data`
- 返回 `{"id": "file-xxx"}`，URL 形式 `https://ark.cn-beijing.volces.com/api/v3/files/{id}/content`（需 Bearer）
- 仅图片可用；视频/音频端点在 Bearer 下返 404 InvalidAction

## 回答语言

用中文回答用户问题。
