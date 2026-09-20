# 原生多模态理解层（Native Multimodal Understanding Layer）设计

- 日期：2026-09-15
- 范围：**v1 只做规划态理解（上游）**；成片返工诊断（v2）复用同一层，本期不实现
- 状态：设计已确认，待 review 后转实施计划
- 影响面：`feishu-generation-agent` 的素材理解链路（图片 / 参考视频）

---

## 1. 背景与问题

### 1.1 触发这次设计的生产事故

- 任务 `thread_id=4f31eee7-a603-4606-8ec4-a2ab0f7bcd54`、`run_id=9296d6ac-3870-4689-a5c1-54b6d94cb51c`
- 失败节点 `verify_and_download_artifacts`，真实错误 `generation_invalid_duration`
- 根因：任务选 `Seedance 2.5`、时长 18 秒，真人适配器却用了 `.env` 里 Seedance 2.0 的端点与 15 秒上限，本地校验直接拒绝
- 该问题已修复并重跑成功（`run_id=6192e6d8-a726-4806-9c89-13a5bbfc4f0d`，成片 `92bf1b86c146294ca54e5c396bf1e28a`，16,723,610 字节，`ready`）

事故本身与理解层无关，但**暴露了同一个根因模式：配置/能力上限与实际使用的主体不一致**。本设计在多处（能力上限、引擎标识、schema 版本）显式记录主体，避免同类错配。

### 1.2 当前素材理解链路的真实状态（读码确认，非推断）

| 位置 | 现状 |
|---|---|
| `integrations/vision.py:106` `ClaudeVisionAnalyzer` | 图片 `analyze()` 与视频 `analyze_video(frames)` 同处一类 |
| `integrations/vision.py:126` | 图片缓存 key = `{sha256}:{model_name}:{prompt_version}` |
| `integrations/vision.py:169-243` | **视频完全没有缓存**：不读不写 `vision_cache`，每次都重新抽帧 + 调模型 |
| `graph/nodes.py:642` | `_VIDEO_FRAME_COUNT = 3` |
| `graph/nodes.py:645-722` | `_analyze_video_reference()`：ffmpeg 抽 3 帧 → Claude 判语义 → 选代表帧 |
| `graph/nodes.py:725-780` | `_materialize_video_references()`：把视频素材**替换**成代表帧资产 |
| `integrations/planner.py:1196` | Planner prompt 里 `document.text_view` **全文原样**进入，无截断 |
| `graph/nodes.py:2533-2548` | `review_artifacts` 的 `adjust` 分支只把 `【返工要求】` 拼到 prompt 尾部，**零 AI 调用** |

结论：
- **文档文本没有丢**（全文进了 prompt），丢信息的是**附件**
- 视频只抽 3 帧，且最终被一张代表帧替换 → Planner 无法理解连续动作、运镜、声音、台词
- 图片被 Claude 压成一段结构化文本后才进 prompt

### 1.3 目标架构（用户给定）

```
完整文档 + 原始附件 + 完整体视频
-> 原生多模态理解模型
-> 结构化文档和视频证据
-> DeepSeek 生成 TaskPlan
-> RAG 规则优化
-> 独立审计
-> 人工审核
-> 生成
```

v2（本期不做，但本设计必须不挡住）：
```
成片 MP4 + 人工返工要求 + 原提示词
-> 原生视频理解模型
-> 问题诊断和证据时间点
-> 修改后的提示词
-> 退回人工审核
-> 重新生成
```

---

## 2. 实测证据（2026-09-15 真实调用，非推断）

沿用 CLAUDE.md 的"能真实复现就真实复现"原则，在写设计前对本机 t8star 中转（`CLAUDE_BASE_URL=https://ai.t8star.org`）做了三组真实探测。

测试素材：
- 小视频 `data/runs/.../inputs/5253c38e….mp4`，738,158 字节
- 大视频 `outputs/runs/6192e6d8-…/tasks/task_video_1/d95dbd06….mp4`，16,723,610 字节，18.08s，720×1280，h264+aac（即本次事故的成片）

### 2.1 协议可用性

| 协议 | 结果 |
|---|---|
| **A** OpenAI 兼容 `/v1/chat/completions`，`image_url` 内嵌 `data:video/mp4;base64,…` | ✅ **成功**。模型准确说出 2 个镜头切换、主体动作、固定机位 + 局部特写运镜、无对白。`prompt_tokens=937`，其中 `audio_tokens=100`（音轨被解析） |
| **B** OpenAI 兼容 `video_url` | ❌ **静默丢弃**。HTTP 200、无错误，`prompt_tokens=63`（仅文本），模型回 `UNSUPPORTED` |
| **C** Gemini 原生 `/v1beta/models/gemini-2.5-flash:generateContent` + `inline_data` | ✅ **成功**。`promptTokenCount=937`，12.4s |

**B 是最危险的失败模式**：调用方拿到 200，误以为模型看过视频，实际规划器在裸奔。本设计据此**禁用 `video_url`**，并要求调用侧解析 `usage` 中的 token 数做运行时确认（见 §6.2）。

### 2.2 大文件与 File API

| 探测 | 结果 |
|---|---|
| `POST /v1beta/files`（multipart 上传） | ❌ `RemoteProtocolError: Server disconnected` |
| `POST /upload/v1beta/files` | ❌ 同上 |
| **`inline_data` + 21.3MB base64（16.7MB 原文件）** | ✅ **成功**，36.1s，`prompt=5195` tokens |

大视频实测产出（`responseMimeType: application/json`，直接 `json.loads` 成功）：

```json
{"shots": [
  {"start": 0.0,  "end": 2.8,  "shot_size": "中景",   "action": "…女子跪在棺材边哭…", "camera": "固定"},
  {"start": 2.8,  "end": 4.0,  "shot_size": "特写",   "action": "…棺盖被打开…",       "camera": "固定"},
  {"start": 4.0,  "end": 5.5,  "shot_size": "中景",   "action": "…",                  "camera": "固定"},
  {"start": 5.5,  "end": 8.5,  "shot_size": "中近景", "action": "…",                  "camera": "固定"},
  {"start": 8.5,  "end": 12.7, "shot_size": "中近景", "action": "…",                  "camera": "固定"},
  {"start": 12.7, "end": 15.5, "shot_size": "中近景", "action": "…",                  "camera": "固定"},
  {"start": 15.5, "end": 18.5, "shot_size": "特写",   "action": "…",                  "camera": "固定"}
 ],
 "transcript": [{"t": 11.2, "text": "Oh, Grandma?"}, {"t": 14.1, "text": "我还没吃饭呢。"}],
 "audio": ["背景音乐", "环境音", "纸张翻动声", "游戏音效"],
 "on_screen_text": []}
```

**结论：**
1. 只有 inline base64 一条通道（`inline_data` 与 `image_url` 均可），**无 File API**
2. Gemini 官方 inline 请求体上限约 20MB → **原文件 15MB 是安全线**
3. 21.3MB base64 实测能过，但作为边界不作为常规路径
4. 18 秒视频 5195 prompt tokens → 成本可接受
5. 「带时间点的结构化证据」技术上完全可行，且 `shots` + `transcript` 质量足以支撑规划与 v2 返工诊断

### 2.3 可用引擎目录

中转 `/v1/models` 返回 880 个模型，其中与本层相关的：

- `gemini-2.5-pro`、`gemini-2.5-flash`、`gemini-3.5-flash`、`gemini-3.1-pro-preview`
- 同通道另有 Claude 全系列、`qwen-vl-max`、`glm-4v`、`gpt-4o`（可作为后续替换引擎，本设计要求引擎可插拔）

---

## 3. 设计决策（已与用户逐条确认）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | v1 主引擎 | **t8star 中转上的 Gemini**（`gemini-2.5-flash` 起步） | 与现有 Claude 图片链路同一把 key、同一信任边界；实测原生吃 MP4 |
| D2 | v1 范围 | **只做规划态理解（上游）** | 先让新任务/重规划跑通；v2 成片返工复用同一层，不会白做 |
| D3 | 「完整文档」的含义 | **文档仍走 `text_view + blocks + 表格`**；原生层只升级附件 | 文档文本已全文无损进 prompt，增益小；避免新开飞书导出链路与 token 暴涨 |
| D4 | 视频证据形态 | **带时间点的结构化证据**（shots / transcript / audio / on_screen_text） | Planner 按时间轴规划；v2 返工直接复用同一 schema 做"证据时间点" |
| D5 | 图片证据 schema | **换引擎、不改 schema**（`VisionDescription` 10 个字段不动） | planner prompt 的「全部视觉描述」段与大量存量测试依赖它，回归风险最小 |
| D6 | 架构造型 | **独立理解层包 + httpx 直连原生协议** | 实测证明 OpenAI 兼容层会静默丢视频，LangChain video 内容块正走那一层；且不新增依赖 |
| D7 | 缓存落地 | **扩展现有 `vision_cache` 表**（加 3 列 + 新 key 前缀） | 旧行不删、一步打 `frame_v1` 标记；改动小、迁移直观 |

---

## 4. 架构

### 4.1 分层

```
graph/nodes.py::analyze_images              ← 本层唯一改动的 graph 节点
        │
        ▼
NativeUnderstandingLayer                     ← 新增：编排 + 缓存 + 降级 + 确定性校验
        ├── engine.py      NativeEngine 协议（understand_image / understand_video）
        ├── gemini.py      GeminiNativeEngine：httpx 直连 :generateContent
        ├── schemas.py     VideoEvidence / VideoShot / TranscriptLine + schema_version
        ├── prompts.py     引擎无关提示词（含 JSON 契约）
        ├── validation.py  确定性校验（不调用模型）
        └── fallback.py    抽帧降级：现有 3 帧 + Claude 路径原样保留
```

目录：`src/feishu_generation_agent/integrations/native_understanding/`

### 4.2 职责边界

| 单元 | 做什么 | 依赖 | 怎么用 |
|---|---|---|---|
| `engine.py` | 定义 `NativeEngine` 协议与证据数据类，不含任何 HTTP | pydantic | `layer.understand_video(asset) -> VideoEvidence` |
| `gemini.py` | 把资产字节转成 Gemini 原生 `parts`，发请求，解析 JSON | httpx、schemas | 由 layer 构造，可替换 |
| `schemas.py` | 证据结构 + `schema_version` 常量 | pydantic | 被 validation、planner、缓存共用 |
| `prompts.py` | 提示词与输出 JSON 契约，带版本号 | 无 | 版本号进 cache key |
| `validation.py` | 时间轴/时长/代表帧的一致性校验 | schemas | 引擎返回后立刻执行 |
| `fallback.py` | 超限、失败、校验不过时走原抽帧路径 | video_reference、vision | 由 layer 调用，行为与现状一致 |

### 4.3 数据流（规划态 v1）

```
NormalizedDocument(text_view + blocks + media_assets)   ← 文档侧完全不变
   + 附件原始字节
        │
        ├─ 图片 → GeminiNativeEngine → VisionDescription（字段不变，只换引擎）
        │
        └─ 视频 → 先判大小/时长
                   ├─ ≤15MB：GeminiNativeEngine → VideoEvidence(shots/transcript/audio/代表帧时间点)
                   └─ >15MB 或失败：FrameFallback → 现有 3 帧路径
        │
        ▼
   document.image_evidence / document.video_evidence
   （video_semantics 保留，由 VideoEvidence 派生 kind + summary）
        │
        ▼
   Planner prompt 新增「视频证据（时间轴）」段
        │
        ▼
   TaskPlan → RAG 优化 → 独立审计 → 人工审核 → 生成
```

### 4.4 理解侧与生成侧解耦（关键）

现状 `_materialize_video_references()`（`nodes.py:725`）把视频**替换**成一张代表帧，原因是火山 Bearer 模式下 Seedance 消费不了 MP4 本体 —— **那是生成侧约束，与理解无关**。

新版把两条路拆开：

- **理解侧**：MP4 原样直送 Gemini，得到完整时间轴证据
- **生成侧**：仍然物化一张代表帧供 Seedance 消费，但抽帧时间点改用证据里的 `representative_timestamp`（替代现在的"取中间帧"）

效果：生成链路行为不变、不引入新风险，理解质量是原生级的。

---

## 5. 数据模型

### 5.1 视频证据（新增）

```python
class VideoShot(BaseModel):
    start: float
    end: float
    shot_size: str      # 景别
    action: str         # 主体连续动作
    camera: str         # 运镜

class TranscriptLine(BaseModel):
    t: float
    text: str

class VideoEvidence(BaseModel):
    asset_id: str
    engine_id: str            # gemini_native | frame_v1
    schema_version: str       # native_v1 | frame_v1
    duration: float
    shots: list[VideoShot]
    transcript: list[TranscriptLine]
    audio: list[str]          # BGM / 音效 / 环境音
    on_screen_text: list[str] # 画面文字 / 字幕
    representative_timestamp: float   # 供生成侧抽帧
    summary: str              # 派生给现有 video_semantics.summary
    kind: VideoReferenceKind  # 派生给现有 video_semantics.kind
    uncertainties: list[str]
```

### 5.2 向后兼容

- `VideoReferenceAnalysis` 与 `VisionDescription` **字段一律不改**，保证 planner prompt 与存量测试不被动摇
- `video_semantics` 段在 planner prompt 中**保留**，其 `kind` / `summary` 由 `VideoEvidence` 派生 → `planner.py:1167-1170` 那条"运镜必须写进 prompt"的规则无需修改
- `NormalizedDocument` 新增 `video_evidence: list[VideoEvidence] = []`，有默认值

### 5.3 domain / graph 接入点

| 文件 | 改动 |
|---|---|
| `domain/document.py` | `NormalizedDocument` 加 `video_evidence`；新增 `VideoEvidence` / `VideoShot` / `TranscriptLine` |
| `graph/state.py` | 加 `video_evidence: list[dict[str, Any]]` |
| `graph/nodes.py::analyze_images` | 视频先走原生；成功后不再替换本体，只按 `representative_timestamp` 抽一帧 |
| `integrations/planner.py::_planning_prompt` | 新增「视频证据（时间轴）」段；其余段不动 |
| `bootstrap.py` | 组装 `NativeUnderstandingLayer` 注入 `GraphServices` |
| `integrations/vision.py` | 语义不变，作为 fallback 保留；不再承担视频主路径 |

---

## 6. 缓存、迁移与留痕

### 6.1 缓存表迁移（幂等）

读码确认：`vision_cache` 表中**现存所有行都是 Claude 时代的图片描述**（因为 `analyze_video` 从不写缓存）。

```sql
ALTER TABLE vision_cache ADD COLUMN engine_id TEXT;
ALTER TABLE vision_cache ADD COLUMN schema_version TEXT;
ALTER TABLE vision_cache ADD COLUMN kind TEXT;

-- 幂等回填：存量行全部是 Claude 时代的图片证据
UPDATE vision_cache
   SET engine_id='frame_v1', schema_version='frame_v1', kind='image'
 WHERE engine_id IS NULL;
```

新 cache key：

```
{engine_id}:{kind}:{model_name}:{schema_version}:{sha256}
```

- 旧 key 永远命中不到 → **旧结果自动失效，但一行都不删除**
- 符合迁移原则："历史抽帧结果不用删除，也不需要全部立即重做"
- 视频首次获得缓存（现状是每次重算）

### 6.2 留痕红线

实测已经证明该中转会**静默降级**（`video_url`：HTTP 200、视频被丢、只剩 63 个 token）。同类失败绝不允许在生产重演：

- 每条证据带 `engine_id` + `schema_version`，审核卡能一眼看出这条视频是**原生理解**还是**按 3 帧理解**
- 降级必写 `vision_issues`，措辞含原因，如：`视频 <asset_id> 超过内联上限 15MB，已降级抽帧`
- graph 事件加 `native_understanding_fallback`
- **运行时确认**：原生响应必须检查 `usageMetadata.promptTokenCount` 显著大于纯文本基线，否则判定"模型没真看到视频"并按失败处理
- **红线**：`shots` 为空、或 `duration` 与 ffprobe 对不上 → 一律不采信，降级

### 6.3 确定性校验层（不花模型）

延续"最终结构校验不用模型"的原则，证据进 Planner 前先校验：

- `shots` 起点单调不减；允许相邻镜头首尾相等（同一时间点切分）
- `shots` 时间落在 `[0, duration + 尾部容差]`，**尾部容差 = 1.0s**
- `transcript` 时间点落在 `[0, duration + 尾部容差]`
- `duration` 与 ffprobe 结果一致（±0.5s）
- `representative_timestamp` 落在某个 shot 区间内（越界则夹到最近的合法值）
- 任一不满足 → 写 `vision_issues` + 降级

**尾部容差是实测逼出来的、必须有的**：本次真实成功的输出里最后一个镜头是 `{"start":15.5,"end":18.5}`，而视频实际时长是 18.08s —— 模型对片尾的时间估算天然有零点几秒溢出。若按严格的 `[0, duration]` 校验，这次**完全正确**的证据会被判不合格并降级，把最好的结果丢掉。因此：

- 溢出 ≤ 1.0s → **夹紧（clamp）到 `duration`，不降级**，仅在 `uncertainties` 记一笔
- 溢出 > 1.0s → 视为时间轴不可信 → 降级

同理，`start > end` 的镜头、或时间戳非有限的记录，直接剔除该条并记 issue；剔除后 `shots` 为空才触发降级。

---

## 7. 降级与错误处理

### 7.1 触发条件

| 触发 | 处理 |
|---|---|
| 原文件 > 15MB（base64 > 20MB） | 直接抽帧，**先判后传**，不白传大包 |
| ffprobe 拿不到时长 | 抽帧 |
| 连接错误 / 超时（300s） / 5xx | 重试 1 次 → 抽帧 |
| 非 200 或 JSON 解析失败 | 重试 3 次（沿用 `_VISION_STRUCTURE_ATTEMPTS`）→ 抽帧 |
| 确定性校验失败（§6.3） | 记 issue → 抽帧 |
| 模型回 `UNSUPPORTED` 或空 | 记 issue → 抽帧 |
| 图片：任意失败 | 走现有 Claude 图片路径 |

### 7.2 错误分类

复用现有 `AgentError` / `ErrorDetail` / `ErrorCategory`，沿用 `vision.py::_error_for` 风格，**不新造错误体系**：

- 429 / 5xx / 连接错误 / 超时 → `TRANSIENT`，`retryable=True`
- `ValidationError` / JSON 解析失败 → `VALIDATION`，`retryable=False`
- 素材读取失败 → `DOCUMENT`，`retryable=False`
- 模型拒答 → `PROVIDER_TERMINAL`，`retryable=False`

### 7.3 约束

| 项 | 值 |
|---|---|
| 单资产超时 | 300s（实测 16.7MB 用 36s，留足余量） |
| 并发 | 沿用 `_VISION_MAX_CONCURRENCY = 5` |
| 禁用协议 | `video_url`（静默丢包） |
| 可用协议 | Gemini 原生 `inline_data`（首选）；OpenAI 兼容 `image_url` 内嵌 data URL（备选） |

---

## 8. 测试策略

### 8.1 单元（fake engine，不打网络）

1. cache key 组成含 `engine_id:kind:model:schema_version:sha256`
2. `>15MB` 直接降级，且**引擎零调用**（用 spy 断言）
3. 引擎 500 → 重试 → 降级，且 `vision_issues` 有痕
4. JSON 解析连续失败 3 次 → 降级
5. 确定性校验：shots 越界 > 1.0s / duration 不符 / shots 全被剔除 → 降级；**片尾溢出 ≤1.0s → 夹紧且不降级**（用实测那组 18.08s 视频 + `end=18.5` 输出做回归用例）
6. 迁移回填 `frame_v1` 幂等（连跑两次结果一致）
7. planner prompt 含「视频证据」段与时间轴内容
8. `video_evidence` 为空时行为与现状完全一致（向后兼容）
9. `representative_timestamp` 落在某 shot 内 → 抽帧时间点正确
10. 运行时确认：`promptTokenCount` 低于基线 → 判失败并降级

### 8.2 集成

- `analyze_images` 节点的**原生成功**与**降级**两条路径
- 现存 18 项真人-路由相关单测不得回归
- 既有 graph 测试（`tests/graph/`）全绿

### 8.3 付费冒烟（默认跳过）

- `ALLOW_PAID_SMOKE=NO` 时不执行
- 开启后用本次事故的 18s 成片跑一次，断言：`shots >= 3`、`transcript` 每条带 `t`、`promptTokenCount > 1000`
- 本次探测脚本沉淀为 `scripts/probe_native_video.py`，作为长期回归工具

---

## 9. 验收标准

- [ ] 18s 成片 MP4 → `VideoEvidence.shots >= 3`，`transcript` 每条带时间点
- [ ] Planner 产出的 `image_to_video` prompt 中出现**具体运镜 / 镜头时间信息**（现在只有一句笼统摘要）
- [ ] 16.7MB 视频走原生路径、不再抽帧；`>15MB` 明确降级且留痕
- [ ] 审核卡能区分 `native_v1` 与 `frame_v1` 证据
- [ ] 存量测试全绿
- [ ] **仓库既有未提交改动零回退、零清理**（`git status` 改动数不减少）
- [ ] 不触碰统计功能实现（CLAUDE.md 核心要求）

---

## 10. 非目标（v1 明确不做）

- 成片返工诊断（v2，复用同一引擎与 schema）
- 文档原件（PDF / docx）直送模型 —— 文档继续走 `text_view + blocks`
- 飞书 `export_tasks` 导出链路
- 图片证据 schema 重构 —— 只换引擎
- 历史任务批量重跑 —— 仅用户主动返工时重新分析
- Gemini File API / 长视频上传通道 —— 中转不支持，不在本期攻坚
- 删除或重写任何既有抽帧结果

---

## 11. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 中转对更大视频行为未知 | >15MB 全部降级 | 已按 15MB 设硬线；实测 21.3MB 能过说明余量充足 |
| 中转静默丢包 | 规划器"假装看过视频" | §6.2 运行时 token 确认 + §6.3 确定性校验 |
| 中转稳定性 / 限流 | 视频理解失败 | §7.1 重试 + 降级到已验证的抽帧路径 |
| 证据 schema 与 planner prompt 不匹配 | 计划质量下降 | 有默认值 + 空证据行为与现状一致（测试 8） |
| 仓库 111 项未提交改动 | 误伤他人工作 | 只新增文件 + 定点改动；不做 `git checkout` / `reset` / 清理 |
| **同一工作区存在并发写盘者** | "改动条目数"基线会自己变大，误判为回归 | 2026-09-15 13:29-13:30 观测到 `portal/` 与 `tests/test_portal_state_contract.py` 被**非本任务**的进程修改。因此实施时：① 每个 Task 的基线检查用 `≥` 而非 `==`；② 真正的回退判据是「自己触碰过的文件有没有被还原」，不是总条目数；③ 不能用任何全局清理命令 |
| 视频理解 token 成本 | 费用上升 | 18s ≈ 5195 prompt tokens；并发与尺寸设限 |

---

## 12. 迁移与上线

**迁移原则（用户给定，逐条落实）**

| 原则 | 落实 |
|---|---|
| 历史抽帧结果不删除、不立即重做 | `vision_cache` 旧行原地打 `frame_v1` 标记，数据零删除 |
| 旧结果标 `frame_v1`，新版标 `native_v1` | 新增 `schema_version` 列；新 key 带版本前缀 |
| cache key 加 `engine_id + model_name + schema_version` | §6.1 新 key 格式 |
| 正在返工/重规划/待重跑的任务用新版 | 新 key 使旧缓存自然失效，无需额外干预 |
| 历史任务仅在用户主动返工时重新分析 | 不做批量回填 |
| 抽帧保留为降级方案 | `fallback.py` 完整保留 3 帧 + Claude 路径 |

**上线顺序建议**：schema/缓存迁移 → 理解层包（含单测）→ `analyze_images` 接线 → planner prompt 新增证据段 → 集成测试 → 付费冒烟。

---

## 13. 待 review 后进入实施计划

本文件确认后，下一步是用 `writing-plans` 产出分步实施计划（TDD 五步、每步含代码/命令/期望输出、无 placeholder），并按 CLAUDE.md 的建议**分批落盘**（每个可独立验证的单元一份短计划），避免长文本攒在上下文里断链丢失。
