# 返工（adjust）提示词净化设计

- 日期：2026-09-15（初稿） / 2026-09-16（按实现修订）
- 范围：`feishu-generation-agent` 的 `review_artifacts` 节点 `adjust` 分支（成片返工）
- 状态：**已实现（路 B）**；审批阶段的 `plan_requirements` 返工（路 A）仍未处理
- 影响面：重跑选中任务时的提示词生成方式；不改重跑的调度、审批与产物落盘

---

## 1. 背景与问题

### 1.1 现状（读码确认，非推断）

`graph/nodes.py`，`decision.action == "adjust"` 分支的旧实现：

```python
existing = task.prompt
if marker in existing:
    existing = existing.split(marker, 1)[0].rstrip()   # ← 撕掉历史要求
prompt = f"{existing}\n{marker}{feedback}"
if len(prompt) > SEEDANCE_PROMPT_MAX_CHARS:
    raise _validation_error("...提示词过长")            # ← 超长直接失败
```

### 1.2 三个缺陷（按严重程度）

1. **历史返工要求被覆盖（最严重）**。追加新要求前先把已有的 `【返工要求】`
   段**整段删掉**，于是「上一轮刚修好的问题」在下一轮必然复发：

   | 轮次 | 用户意见 | 提示词里实际留下的 |
   |---|---|---|
   | 第 1 次 | 手不要僵 | 手不要僵 |
   | 第 2 次 | 背景太暗 | ~~手不要僵~~ + 背景太暗 |

   这与「每次返工都在净化提示词、说过的绝不再犯」的目标**正好相反**。

2. **无 AI 参与，只是字符串拼接**。返工要求以原始中文挂在提示词尾部，生成模型
   要自己猜怎么把「不要露手机屏幕」融合进画面描述。拼接不等于执行。

3. **拼超长就硬失败**。提示词接近 1500 字符时追加返工要求会直接抛错，**整个返工
   无法进行**，且错误只说「提示词过长」，用户没有补救路径。

### 1.3 目标

- 历次返工要求**只累积、永不覆盖**——已修复的问题绝不再现
- 返工要求由 AI **融合**进提示词，而不是拼接（真正的「净化」）
- 提示词天然受 1500 字符约束，**不再因为超长而失败**
- 任何失败（模型不可用、超时、结果不合契约）**回退到安全拼接**，返工永远不因为
  「优化不可用」而失败
- `@图片N` / `@视频N` / `@音频N` 引用令牌必须原样保留——丢失会导致下游
  `_task_assets` 解析失败

## 2. 实现

### 2.1 状态与提示词正文解耦（`domain/plan.py`）

`GenerationTask` 新增两个字段，用 `SkipJsonSchema` 挡在给规划模型的 JSON Schema
之外（系统管理字段，模型不需要产出），但不影响 `model_dump` 持久化：

| 字段 | 含义 |
|---|---|
| `rework_requirements: list[str]` | 历次返工要求，**只增不减** |
| `rework_base_prompt: str \| None` | 首次返工前的提示词，冻结不变 |

提示词正文会被 AI 重写，所以历史要求**不能只存在正文里**；清单是唯一真相源。

### 2.2 纯逻辑模块（`integrations/rework_prompt.py`）

不依赖图与网络，可单独测试：

- `merge_requirements(*groups)`：按顺序累加、去重、忽略空白。**唯一的累加入口，
  调用方永不做覆盖。**
- `split_legacy_requirements(prompt)`：把老 run 写在正文里的 `【返工要求】` 段拆出来，
  保证升级后历史要求不凭空丢失。
- `is_acceptable_fusion(fused, base_prompt=...)`：融合结果验收——非空、不超长、
  **不含返工标记**、**素材令牌多重集与原文完全一致**。
- `build_fallback_prompt(base, requirements)`：兜底拼接，包含**全部**要求；
  空间不足时优先牺牲原始画面描述，连要求都放不下时从**最旧**的条目开始丢
  （最新的诉求优先保住）。**永不超长、永不抛错。**

### 2.3 AI 融合（`integrations/planner.py`）

`DeepSeekPlanner.fuse_rework_prompt(original_prompt, requirements) -> str | None`：

- 输入永远是「**冻结的 base + 全部历史要求**」，不是「上一轮的成品 + 新要求」——
  避免逐轮改写导致的语义漂移与信息递减
- 系统指令明确要求：保留全部画面信息、素材令牌一个都不能增删、每条返工要求都必须
  融入且不得遗漏、新旧冲突以更新者为准、不输出标记、≤1500 字符
- 融合模型按需派生并关闭思考（改写任务不需要推理预算）
- 任何异常、空返回、非文本 → 返回 `None`，调用方回退拼接

### 2.4 节点接线（`graph/nodes.py`）

```
legacy_base, legacy_requirements = split_legacy_requirements(task.prompt)
base_prompt = task.rework_base_prompt or legacy_base
requirements = merge_requirements(task.rework_requirements, legacy_requirements, [feedback])
prompt, truncated = await _rework_prompt_for_task(services, base_prompt, requirements)
```

`_rework_prompt_for_task` 先试 AI 融合并通过 `is_acceptable_fusion` 验收；不通过则
回退 `build_fallback_prompt`。截断时在该任务的 `warnings` 追加一条提示
（**历史要求仍完整保留在字段里**，不会因截断丢失）。

## 3. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_rework_prompt.py`（17） | 累积不丢、去重、老格式兼容、兜底永不超长、融合契约拒绝（丢/加令牌、超长、标记泄漏、空） |
| `tests/unit/test_planner_rework_fusion.py`（6） | 融合返回值、异常/空返回降级、请求携带全部要求、构造期 bind 次数不变 |
| `tests/graph/test_artifact_rerun.py`（5） | **第二次返工保留第一次要求**、融合被采纳且无标记、融合抛错回退、擅自加令牌回退 |

核心回归：`test_second_rework_keeps_first_requirement` 在实现前**实测失败**
（提示词里只剩 `【返工要求】背景太暗`），实现后通过。

## 4. 不在范围内

- **审批阶段返工（路 A）**：`human_approval` 拒绝后回 `plan_requirements`，
  `planner_feedback` 每轮被覆盖，且上一版计划**没有**传给规划模型 → 同样会忘事。
  需要单独修。
- 成片返工**诊断**（看视频判断哪里要改）——属原生多模态理解的 v2
- 批量一次调用（现在每个目标任务调一次融合）
- 前端展示 `rework_requirements` / `warnings`

## 5. 已决策

| 决策点 | 结论 |
|---|---|
| 超长是否改为截断 + 标记（而非硬失败） | 接受。返工被阻断比截断更糟；截断只影响原始画面描述，历史要求仍完整保留 |
| 追溯字段位置 | 放在 `GenerationTask` 上（`rework_requirements` / `rework_base_prompt`），随计划一起持久化并可见 |
| 是否加「不用 AI」开关 | 不需要。融合失败已自动回退安全拼接，等价于开关关闭 |