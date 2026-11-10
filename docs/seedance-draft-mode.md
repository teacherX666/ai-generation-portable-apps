# Seedance 2.5 样片模式（Draft）：官方规则与接入要点

> 2026-10-15 接入自由创作时整理。规则来源：官方文档的 **Markdown 版**
> （`https://ark.volcengine.com/region:cn-beijing/docs/seedance-2-5.md`，任意文档页地址后加 `.md`）。
> 常规网页抓取会在 50000 字符处截断、正好截掉 Step2 参数表 —— **查方舟文档请直接抓 `.md`**。

## 一句话

样片模式是**两段式**：第一段用 480P 快速试错（`draft: true`），第二段基于第一段的**任务 ID**
出 1080P 成片。仅 Seedance 2.5 支持（2.0 系列 ✗）。

## Step1：生成 Draft 视频

- 请求体加 `"draft": true`。
- **只允许 480p**，设置其他分辨率会报错。
- 返回的任务 `id` 就是 Draft 任务 ID，**有效期 7 天**（从 `created_at` 起算）。
- 计费：480p Draft 的 token 用量与单价，**与正常生成 480p 视频一致** —— 省钱来自
  「480p 单价 vs 1080p 单价」，不是打折。

## Step2：基于 Draft 出成片

`content` 里放样片引用：

```json
{ "model": "doubao-seedance-2-5-260628",
  "content": [{ "type": "draft_task", "draft_task": { "id": "<Step1 的任务 id>" } }],
  "resolution": "1080p" }
```

**id 必须是 Step1 返回的任务 `id`，不是 `upstreamTaskId`**（官方原文点名的坑）。

参数分三类（官方 Step2 参数表）：

| 类型 | 参数 | 规则 |
|---|---|---|
| 取值固定 | `model` | 必须与创建 Draft 时一致 |
| | `draft` | 可不传或 `false` |
| | `resolution` | **必须传，且仅支持 `1080p`** |
| 自动复用（**禁止传入**） | 提示词 `content.text`、参考图片/视频/音频、`duration`、`ratio`、`seed`、`generate_audio`、`omni_reference_task_type` | 「即使传入值与 Draft 阶段使用的一致，也会触发报错」 |
| 可重新指定 | `return_last_frame`、`output_format`、`watermark`、`service_tier`、`execution_expires_after`、`priority`、`callback_url`、`safety_identifier` | 不传就用模型默认值，**不会**沿用 Draft 的取值 |

计费：两段独立计费。Step2 的 token 单价**取决于 Step1 是否包含输入视频**；算 token 用量时
输入视频时长按 Step1 的算，Draft 视频不计入。

## ⚠️ 实测踩到的坑（2026-10-15，真机）

第一段 480p 成功；第二段按「重新指定分辨率」的直觉发了 `720p`，被方舟**同步拒**：

```
InvalidParameter: the parameter resolution specified in the request is not valid for model
doubao-seedance-2-5 in draft_task final stage; received: "720p"
```

教训：**「可重新指定」≠「随便指定」**。官方那句话（「部分参数可重新指定」）在 Step2 表里
其实是**只有 1080p 一个合法值**。当时凭「宣传口径 480P→高清成片」推断成 720p，白打了一发。

## 本仓库的接线位置

- `seedance/providers.json`：2.5 的 `capabilities.draft = true`；分辨率声明补上 `1080p`
  （官方能力表里 2.5 = 480p / 720p / 1080p，漏了它，本地 `validate_model_capabilities`
  会把合法的成片请求拦下来）。
  - **「2.5 没有 1080p」是旧信息**：2026-08-07 的官方能力矩阵确实只写 480p/720p（仓库里
    `d37d5eb` 就是照它配的），现在官方已支持 —— 教程能力表写 `1080p（10bit 位深）`，
    价格表也单列了 1080p 档。所以给 2.5 放开 1080p 既是真实能力，也意味着**单条贵 2.5 倍**。
    **2026-10-19 已实测钉死**（`_diag/live_test_seedance25_1080p.py`，普通路径、非样片）：
    5 秒 16:9、无参考素材 → 产出 `1920x1080 / 121 帧 / 7.3MB`，245,025 tokens
    ≈ **18.87 元**（官方价格表示例 18.71 元，对得上）。
  - 因此分辨率选项上给 1080p 加了价格提示（`resolutionOptionLabel`，只标 2.5 这一档 ——
    不同模型同档价位差很多，没把握的模型不许瞎标）。
- `seedance/app.py`：
  - `VALUE_FIELDS` 加 `draft` / `draft_task_id`；
  - `build_payload`：有 `draft_task_id` 时**短路返回**只含 `draft_task` + `resolution=1080p`
    的最小体（不发素材，避免白跑上传、也避免撞「禁止传入」）；`draft=1` 时把分辨率就地钉成 480p；
  - `validate_model_capabilities`：非 2.5 拒绝样片模式；成片阶段分辨率非 1080p **提交时就 400**。
- `portal/static/free-creation/app.js`：云端模型组里一个伪模型（`seedance-2.5-draft`，
  `modelId` 仍是真实 id），提交时带 `draft=1`；样片结果出来后预览区出现
  「用这条样片出成片（1080p）」按钮（见 `finalizeDraft()`）。
- 测试：`tests/test_seedance_draft_mode.py`（21 条）、`tests/test_free_creation_draft_mode.mjs`（10 条）。

## ⚠️ 坑 2：「样片窗口关掉之后再也回不去」（2026-10-20）

用户原话：「就是如果一开始没有决定用1080，后面没办法返回那个页面了」。

**根因在卡片的数据来源，不在窗口本身**：

| 卡片来源 | 下发的字段 | 有 `draft` 吗 |
|---|---|---|
| `/api/jobs`（任务列表，内存任务） | job_id/status/model/prompt/results/… | **没有**（为瘦身刻意不下发整个 `params`） |
| `/api/activity`（活动列表，持久化历史） | id/job_id/status/title/… | **没有**（刻意不下发 `request`） |

于是 `isDraftJob(卡片)` 恒为 `false` → 点样片卡片只会打开**通用详情弹窗** → 关掉二级窗口
之后没有入口能回去（连「样片」角标也不会出现）。

**修法（前后端一起）**：
1. 前端：卡片点击先走 `_openCardJob → _resolveCardJob` —— 快照里没有 `draft` 键时
   **补取一次详情**（先 `/api/jobs/<id>`，再 `/api/activity/<activityId>`）再判类型；
   取不到就退回通用详情（不猜、不卡住）。这样**不用重启后端就恢复了返回入口**。
2. 后端：`/api/jobs` 列表项与 `/api/activity` 摘要各加一个 `draft` 布尔字段
   （`values_are_draft` / `activity_record_draft`），角标因此能正常显示，且卡片点击
   不再需要额外那一次请求。

**教训（值得复用）**：**测试要用真实的数据形状**。第一版测试拿的是「详情接口的 job」
（带 `params`），而真实卡片是「列表接口的形状」（没 `params`）—— 断言全绿、真机全废。
现在 `tests/test_seedance_draft_mode_ui.mjs` 里两种形状都覆盖（`listShaped()` 会剥掉
`params`/`request`/`restore`），并把判别力固定成脚本（把卡片点击改回旧写法 → 必红）。
