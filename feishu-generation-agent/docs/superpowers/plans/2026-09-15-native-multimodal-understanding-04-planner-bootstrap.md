# 原生多模态理解层 — Plan 04：Planner 证据段、bootstrap 组装与端到端验收

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `VideoEvidence` 真正影响规划结果 —— Planner prompt 新增「视频证据（时间轴）」段并强制把镜头/运镜写进生成提示词；`bootstrap.py` 组装并注入理解层；提供默认跳过的付费冒烟脚本与端到端验收清单。

**Architecture:** Planner 侧只**新增**一段，不改任何既有段落（`视频参考语义=` 段保留，由证据派生的 `kind`/`summary` 继续支撑既有的"运镜必须写进 prompt"规则）。bootstrap 复用已有的 `provider_http`（`httpx.AsyncClient(trust_env=False)`，line 335 创建 / 634 关闭），避免多出一个不受管理的连接池。

**Tech Stack:** Python 3.12、httpx、pydantic v2、pytest

**Spec:** `docs/superpowers/specs/2026-09-15-native-multimodal-understanding-design.md`
**前置:** Plan 01 + 02 + 03 必须已合入

## Global Constraints

- 工作目录 `<agent>` = `C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent`；解释器 `<agent>\.venv\Scripts\python.exe`
- **仓库有 111 项既有未提交改动。禁止 `git checkout` / `git reset` / `git restore` / `git clean`。**
- **禁止裸 `git commit`**：一律 `git commit --only <paths>`。
- Planner prompt 的既有段落**一个字都不删**；`video_evidence` 为空时产出必须与改造前逐字一致
- 冒烟脚本默认**不发任何付费请求**，只有 `ALLOW_PAID_SMOKE=YES` 时才真跑
- 复用 `provider_http`，不新建第二个连接池
- 不触碰统计功能实现

---

### Task 1: Planner prompt 新增「视频证据（时间轴）」段

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/integrations/planner.py`（`_planning_prompt` 的 video 分支，约 1146-1209 行）
- Test: `<agent>/tests/unit/test_planner_video_evidence.py`（新建）

**Interfaces:**
- Consumes: Plan 01 `NormalizedDocument.video_evidence`
- Produces: video 模式的 prompt 中出现 `视频证据（时间轴）=` 段；image 模式**不出现**

**行为规格：**
1. video 模式且 `document.video_evidence` 非空 → prompt 含 `视频证据（时间轴）={` 段，用现有 `_compact_json` 序列化
2. video 模式但证据为空 → 输出 `视频证据（时间轴）=[]`（与既有 `视频参考语义=[]` 写法一致）
3. **image 模式绝不出现该段**（图片模式不能混入视频指令，见 `test_planning_prompt_mode.py` 记录的真实故障）
4. 新增一条指令，要求把镜头的 `start`/`end` 与 `camera` 写进 `image_to_video` 任务的 prompt

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_planner_video_evidence.py`：

```python
"""视频证据（时间轴）必须进入 planner prompt，否则原生理解白做。

同时守住既有教训：图片模式绝不能混入视频指令 —— 用户提示词离生成更近，
冲突时会压过 system prompt 里的图片模板骨架（见 test_planning_prompt_mode.py）。
"""

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    NormalizedDocument,
    SourceType,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
)
from feishu_generation_agent.integrations.planner import DeepSeekPlanner


class _Model:
    def bind(self, **_kwargs):
        return self


def _document(*, with_evidence: bool) -> NormalizedDocument:
    evidence = (
        [
            VideoEvidence(
                asset_id="video-1-frame",
                engine_id="gemini_native",
                schema_version="native_v1",
                duration=18.08,
                shots=[
                    VideoShot(
                        start=0.0, end=2.8, shot_size="中景",
                        action="女子跪在棺材边哭喊", camera="固定",
                    ),
                    VideoShot(
                        start=15.5, end=18.08, shot_size="特写",
                        action="老人递出手机", camera="缓慢推近",
                    ),
                ],
                transcript=[TranscriptLine(t=11.2, text="Oh, Grandma?")],
                audio=["背景音乐", "纸张翻动声"],
                on_screen_text=["第一章"],
                representative_timestamp=15.5,
                summary="女子哭丧后棺盖被打开，最后递出手机",
                kind=VideoReferenceKind.CAMERA_MOVEMENT,
                uncertainties=[],
            )
        ]
        if with_evidence
        else []
    )
    return NormalizedDocument(
        document_id="doc-1",
        title="短片需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="tok",
        blocks=[
            DocumentBlock(
                block_id="b1", parent_id=None, block_type="text",
                order=0, path=["b1"], text="编号 1：女子哭丧",
            )
        ],
        text_view="[block:b1] 编号 1：女子哭丧",
        media_assets=[],
        video_evidence=evidence,
    )


def _prompt(*, with_evidence: bool, mode: str = "video") -> str:
    planner = DeepSeekPlanner(_Model())
    return planner._planning_prompt(
        _document(with_evidence=with_evidence), [], None, mode=mode
    )


def test_video_mode_includes_timed_evidence():
    prompt = _prompt(with_evidence=True)
    assert "视频证据（时间轴）=" in prompt
    assert "缓慢推近" in prompt
    assert "Oh, Grandma?" in prompt
    assert "纸张翻动声" in prompt
    assert '"start":0.0' in prompt.replace(" ", "")


def test_video_mode_without_evidence_keeps_placeholder():
    prompt = _prompt(with_evidence=False)
    assert "视频证据（时间轴）=[]" in prompt


def test_video_mode_keeps_existing_semantics_section():
    prompt = _prompt(with_evidence=True)
    # 既有段落一个字都不能少 —— 它支撑"运镜必须写进 prompt"的既有规则
    assert "视频参考语义=" in prompt
    assert "全部视觉描述=" in prompt
    assert "序列化表格及后代 blocks=" in prompt


def test_instruction_requires_writing_camera_and_timeline_into_prompt():
    prompt = _prompt(with_evidence=True)
    instructions = prompt.split("TaskPlan JSON Schema=")[0]
    assert "视频证据" in instructions
    assert "运镜" in instructions
    assert "时间点" in instructions or "时间轴" in instructions


def test_image_mode_never_mentions_video_evidence():
    prompt = _prompt(with_evidence=True, mode="image")
    assert "视频证据" not in prompt
    assert "视频参考语义" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_planner_video_evidence.py -v
```
Expected: FAIL — `assert '视频证据（时间轴）=' in prompt`

- [ ] **Step 3: Write minimal implementation**

`<agent>/src/feishu_generation_agent/integrations/planner.py` 的 `_planning_prompt` 中，video 分支里 `video_semantics` 变量计算之后追加：

```python
        video_evidence = [
            evidence.model_dump(mode="json")
            for evidence in document.video_evidence
        ]
```

在同一 video 分支返回的列表里，把

```python
                (
                    f"视频参考语义={_compact_json(video_semantics)}"
                    if video_semantics
                    else "视频参考语义=[]"
                ),
```

替换为

```python
                (
                    f"视频参考语义={_compact_json(video_semantics)}"
                    if video_semantics
                    else "视频参考语义=[]"
                ),
                (
                    "视频证据（时间轴）给出参考视频的镜头切分、每个镜头的连续动作与运镜、"
                    "台词出现时间点、声音与画面文字。规划时必须把这些证据落到具体镜头上："
                    "在 image_to_video 任务的 prompt 中写出镜头的起止时间点与运镜方式，"
                    "让连续动作按证据的顺序和节奏展开；有台词或声音证据时必须体现对应的"
                    "节奏与口型时机。不得把证据概括成一句笼统描述。"
                ),
                (
                    f"视频证据（时间轴）={_compact_json(video_evidence)}"
                    if video_evidence
                    else "视频证据（时间轴）=[]"
                ),
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_planner_video_evidence.py -v
```
Expected: PASS（5 passed）

- [ ] **Step 5: 回归：既有 planner prompt 测试不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_planner.py tests/unit/test_planning_prompt_mode.py tests/unit/test_planning_prompt_snapshot_mode.py tests/unit/test_planner_prompts.py -v
```
Expected: PASS（全绿）

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/integrations/planner.py tests/unit/test_planner_video_evidence.py
git commit --only src/feishu_generation_agent/integrations/planner.py tests/unit/test_planner_video_evidence.py -m "feat(native): planner prompt 新增视频证据时间轴段"
```

---

### Task 2: `bootstrap.py` 组装并注入理解层

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/bootstrap.py`（import 区；`provider_http` 创建之后组装 layer；`GraphServices(...)` 传入）
- Test: `<agent>/tests/unit/test_native_bootstrap_wiring.py`（新建）

**Interfaces:**
- Consumes: Plan 02 `GeminiNativeEngine` / `NativeUnderstandingLayer`；Plan 03 `GraphServices.native_understanding`；Plan 03 Task 1 `probe_video_duration`
- Produces: `bootstrap` 内部按条件构造的 `native_understanding` 实例，组装规则抽成可单测的纯函数 `build_native_understanding_layer(...) -> NativeUnderstandingLayer | None`

**组装规则（必须逐条实现）：**
1. `native_understanding_enabled` 为 False → 返回 None
2. `resolved_native_vision_api_key` 或 `resolved_native_vision_base_url` 为空 → 返回 None（**不抛异常**：没配就静默退回抽帧，与 `vision_model` 的处理方式一致）
3. 否则返回 `NativeUnderstandingLayer`，`duration_probe=probe_video_duration`，客户端复用传入的 `provider_http`

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_bootstrap_wiring.py`：

```python
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.bootstrap import build_native_understanding_layer
from feishu_generation_agent.config import Settings
from feishu_generation_agent.integrations.native_understanding.gemini import (
    GeminiNativeEngine,
)
from feishu_generation_agent.integrations.native_understanding.layer import (
    NativeUnderstandingLayer,
)
from feishu_generation_agent.storage.repository import Repository


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )
    base.update(overrides)
    return Settings(**base)


async def test_returns_none_when_disabled(tmp_path):
    settings = _settings(
        tmp_path,
        claude_api_key="k",
        claude_base_url="https://ai.t8star.org",
        native_understanding_enabled=False,
    )
    repository = await Repository.open(settings.business_db_path)
    try:
        assert (
            build_native_understanding_layer(
                settings, repository, httpx.AsyncClient(trust_env=False)
            )
            is None
        )
    finally:
        await repository.close()


async def test_returns_none_without_credentials(tmp_path):
    settings = _settings(tmp_path)  # 没有任何 key
    repository = await Repository.open(settings.business_db_path)
    try:
        assert (
            build_native_understanding_layer(
                settings, repository, httpx.AsyncClient(trust_env=False)
            )
            is None
        )
    finally:
        await repository.close()


async def test_builds_layer_with_shared_client_and_ffprobe_probe(tmp_path):
    settings = _settings(
        tmp_path,
        claude_api_key="k",
        claude_base_url="https://ai.t8star.org",
    )
    repository = await Repository.open(settings.business_db_path)
    client = httpx.AsyncClient(trust_env=False)
    try:
        layer = build_native_understanding_layer(settings, repository, client)
        assert isinstance(layer, NativeUnderstandingLayer)
        assert layer.enabled is True
        engine = layer._engine
        assert isinstance(engine, GeminiNativeEngine)
        assert engine._client is client  # 复用，不新建连接池
        assert engine.model_name == "gemini-2.5-flash"
        assert engine.endpoint == (
            "https://ai.t8star.org/v1beta/models/gemini-2.5-flash:generateContent"
        )
        assert layer._duration_probe is not None
    finally:
        await client.aclose()
        await repository.close()


async def test_layer_reads_duration_via_injected_probe(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        claude_api_key="k",
        claude_base_url="https://ai.t8star.org",
    )
    repository = await Repository.open(settings.business_db_path)
    client = httpx.AsyncClient(trust_env=False)
    try:
        layer = build_native_understanding_layer(settings, repository, client)
        called: dict = {}

        def fake_probe(path):
            called["path"] = path
            return 18.08

        monkeypatch.setattr(layer, "_duration_probe", fake_probe)
        assert layer._probe_duration(type("A", (), {"local_path": Path("x.mp4")})()) == pytest.approx(18.08)
        assert called["path"] == Path("x.mp4")
    finally:
        await client.aclose()
        await repository.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_bootstrap_wiring.py -v
```
Expected: FAIL — `ImportError: cannot import name 'build_native_understanding_layer' from 'feishu_generation_agent.bootstrap'`

- [ ] **Step 3: Write minimal implementation**

`<agent>/src/feishu_generation_agent/bootstrap.py` 的 import 区追加：

```python
from feishu_generation_agent.integrations.native_understanding.gemini import (
    GeminiNativeEngine,
)
from feishu_generation_agent.integrations.native_understanding.layer import (
    NativeUnderstandingLayer,
)
from feishu_generation_agent.integrations.video_reference import (
    probe_video_duration,
)
```

在同文件模块级（`bootstrap` 函数之外）新增：

```python
def build_native_understanding_layer(
    settings: Settings,
    repository: Repository,
    provider_http: httpx.AsyncClient,
) -> NativeUnderstandingLayer | None:
    """组装原生多模态理解层。

    没配 key / base_url 时返回 None 而不是抛异常 —— 与 vision_model 的处理
    一致：缺失能力应当静默退回抽帧，不该阻断整个 agent。
    """
    if not settings.native_understanding_enabled:
        return None
    api_key = settings.resolved_native_vision_api_key
    base_url = settings.resolved_native_vision_base_url
    if not api_key or not base_url:
        return None
    return NativeUnderstandingLayer(
        engine=GeminiNativeEngine(
            base_url=base_url,
            api_key=api_key,
            model_name=settings.native_vision_model,
            timeout_seconds=settings.native_vision_timeout_seconds,
            # 复用 provider_http（trust_env=False），不新建连接池；
            # 单次请求的 timeout 由引擎在 post() 上显式覆盖（默认 5s 太短）。
            client=provider_http,
        ),
        repository=repository,
        settings=settings,
        duration_probe=probe_video_duration,
    )
```

在该文件的 `GraphServices(...)` 构造中（约 558 行起）追加一个关键字参数：

```python
            native_understanding=build_native_understanding_layer(
                settings, repository, provider_http
            ),
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_bootstrap_wiring.py -v
```
Expected: PASS（4 passed）

- [ ] **Step 5: 回归：bootstrap 相关测试不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/integration/test_restart_recovery.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/bootstrap.py tests/unit/test_native_bootstrap_wiring.py
git commit --only src/feishu_generation_agent/bootstrap.py tests/unit/test_native_bootstrap_wiring.py -m "feat(native): bootstrap 组装并注入原生理解层"
```

---

### Task 3: 付费冒烟脚本（默认不发请求）

**Files:**
- Create: `<agent>/scripts/probe_native_video.py`
- Test: 无（脚本自身默认行为即安全；用一个 subprocess 测试确认默认不发请求）

**Interfaces:**
- Consumes: 环境变量 `ALLOW_PAID_SMOKE`、`.env` 的 `CLAUDE_BASE_URL` / `CLAUDE_API_KEY`
- Produces: 命令行脚本，退出码 0 表示"跳过或通过"，1 表示断言失败

**行为规格：**
1. 不带 `--video` 或 `ALLOW_PAID_SMOKE != YES` → 打印 `SKIPPED: ...` 且退出码 0，**绝不发起网络请求**
2. 带 `--video <path>` 且 `ALLOW_PAID_SMOKE=YES` → 用 `GeminiNativeEngine` 跑一次，断言 `prompt_tokens > 200`、`len(shots) >= 2`、`duration > 0`，并打印证据 JSON
3. 端点固定为 `{base}/v1beta/models/{model}:generateContent`，禁用 `video_url`

- [ ] **Step 1: 写脚本**

新建 `<agent>/scripts/probe_native_video.py`：

```python
"""原生视频理解付费冒烟：默认什么都不做，只有显式打开才发请求。

用法（Windows）：
    $env:ALLOW_PAID_SMOKE="YES"
    .venv\\Scripts\\python.exe scripts\\probe_native_video.py --video <path.mp4>

不传 --video 或没开 ALLOW_PAID_SMOKE 时打印 SKIPPED 并以 0 退出，
确保 CI / 日常回归不会意外产生费用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feishu_generation_agent.config import Settings  # noqa: E402
from feishu_generation_agent.domain.document import MediaAsset  # noqa: E402
from feishu_generation_agent.integrations.native_understanding.gemini import (  # noqa: E402
    GeminiNativeEngine,
)
from feishu_generation_agent.integrations.video_reference import (  # noqa: E402
    probe_video_duration,
)

MIN_SHOTS = 2


async def _run(video: Path, model: str) -> int:
    settings = Settings()
    base_url = settings.resolved_native_vision_base_url
    api_key = settings.resolved_native_vision_api_key
    if not base_url or not api_key:
        print("SKIPPED: CLAUDE_BASE_URL / CLAUDE_API_KEY 未配置")
        return 0

    duration = probe_video_duration(video)
    asset = MediaAsset(
        asset_id="smoke-native-video",
        source_block_id="smoke",
        origin="smoke",
        file_token=None,
        local_path=video,
        mime_type="video/mp4",
        size=video.stat().st_size,
        sha256="0" * 64,
    )
    engine = GeminiNativeEngine(
        base_url=base_url, api_key=api_key, model_name=model
    )
    try:
        result = await engine.understand_video(asset, duration=duration)
    finally:
        await engine._client.aclose()

    evidence = result.evidence
    print(f"duration={duration:.2f}s prompt_tokens={result.prompt_tokens}")
    print(f"shots={len(evidence.shots)} transcript={len(evidence.transcript)}")
    print(json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, indent=2))

    failures = []
    if result.prompt_tokens <= 200:
        failures.append(
            f"prompt_tokens={result.prompt_tokens} 未超过文本基线，疑似静默丢包"
        )
    if len(evidence.shots) < MIN_SHOTS:
        failures.append(f"shots={len(evidence.shots)} < {MIN_SHOTS}")
    if duration <= 0:
        failures.append("duration 非法")
    if failures:
        print("FAIL: " + "; ".join(failures))
        return 1
    print("PASS: 原生视频理解可用")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument("--model", default="gemini-2.5-flash")
    args = parser.parse_args()

    if os.environ.get("ALLOW_PAID_SMOKE", "NO").strip().upper() != "YES":
        print("SKIPPED: ALLOW_PAID_SMOKE != YES（避免产生费用）")
        return 0
    if args.video is None:
        print("SKIPPED: 未提供 --video")
        return 0
    if not args.video.is_file():
        print(f"FAIL: 视频不存在 {args.video}")
        return 1
    return asyncio.run(_run(args.video, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 验证默认行为确实不发请求**

Run:
```bash
.venv\Scripts\python.exe scripts\probe_native_video.py
```
Expected: 输出 `SKIPPED: ALLOW_PAID_SMOKE != YES（避免产生费用）`，退出码 0

Run:
```powershell
$env:ALLOW_PAID_SMOKE="YES"; .venv\Scripts\python.exe scripts\probe_native_video.py; Remove-Item Env:\ALLOW_PAID_SMOKE
```
Expected: 输出 `SKIPPED: 未提供 --video`，退出码 0（**仍然不发请求**）

- [ ] **Step 3: 真跑一次付费冒烟（仅在用户明确同意时）**

Run（用事故那条 18s 成片）:
```powershell
$env:ALLOW_PAID_SMOKE="YES"
.venv\Scripts\python.exe scripts\probe_native_video.py --video "outputs\runs\6192e6d8-a726-4806-9c89-13a5bbfc4f0d\tasks\task_video_1\d95dbd06b86f95f4d41cccd68a1791422c1b40dffe55ca6f1f9e20d6af19cc60.mp4"
Remove-Item Env:\ALLOW_PAID_SMOKE
```
Expected: `PASS`，`shots >= 2`，`prompt_tokens` 约 5000 量级（实测基线 5195）

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_native_video.py
git commit --only scripts/probe_native_video.py -m "test(native): 原生视频理解付费冒烟脚本（默认跳过）"
```

---

### Task 4: 端到端验收

- [ ] **Step 1: 全量单测 + 集成测试**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit tests/graph tests/integration -q
```
Expected: 全绿；与改造前的失败集合相比**不得新增失败**。若改造前就有失败，先记录基线再比对。

- [ ] **Step 2: 前端测试（防止误伤）**

Run:
```bash
node --test tests/frontend
```
Expected: 全绿

- [ ] **Step 3: 逐条核对 spec §9 验收标准**

| 验收项 | 核对方式 | 结果 |
|---|---|---|
| 18s 成片 → `shots >= 3`、`transcript` 带时间点 | Task 3 Step 3 冒烟输出 | ☐ |
| planner prompt 出现具体运镜/镜头时间信息 | `test_planner_video_evidence.py` + 人工看一次真实 prompt | ☐ |
| 16.7MB 视频走原生；`>15MB` 明确降级且留痕 | `test_oversize_video_degrades_without_calling_engine` + 日志确认 warning | ☐ |
| 审核卡能区分 `native_v1` 与 `frame_v1` | `repository.list_vision_cache_engine_ids()` 输出 | ☐ |
| 存量测试全绿 | Step 1 / Step 2 | ☐ |
| 既有未提交改动零回退 | Step 4 | ☐ |
| 未触碰统计功能 | `git diff --stat` 人工确认无 portal 统计相关文件 | ☐ |

- [ ] **Step 4: 确认工作区改动条目数没有减少**

Run:
```bash
git status --porcelain | Measure-Object -Line
```
Expected: ≥ 112（基线 = 用户 111 项 + spec 1 项 + 本系列 plan 4 项 + 新增代码/测试文件）。若变小，立即停止并上报。

- [ ] **Step 5: 迁移标记确认（运维可见性）**

Run:
```bash
.venv\Scripts\python.exe -c "import asyncio,sys; sys.path.insert(0,'src'); from pathlib import Path; from feishu_generation_agent.storage.repository import Repository; async def m():\n r=await Repository.open(Path('data/agent.sqlite3')); print(await r.list_vision_cache_engine_ids()); await r.close()\n asyncio.run(m())"
```
Expected: 出现 `frame_v1` 计数（历史 Claude 图片行）与后续产生的 `gemini_native` 计数，两者可区分

- [ ] **Step 6: 重启服务后确认健康**

由用户执行（会中断运行中任务）：
```powershell
# 见 restart-dsh-web.ps1 / 既有重启流程；确认 8765 健康
Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" -UseBasicParsing | Select-Object StatusCode
```
Expected: 200

- [ ] **Step 7: Task 4 无代码改动，无需 commit**

---

## Self-Review

**Spec 覆盖检查**

| Spec 章节 | 落地任务 |
|---|---|
| §4.3 Planner prompt 新增证据段 | Task 1 |
| §5.3 `bootstrap.py` 组装 | Task 2 |
| §8.3 付费冒烟（默认跳过、18s 成片断言） | Task 3 |
| §9 全部验收标准 | Task 4 |
| §12 上线顺序（schema → 层 → 接线 → prompt → 集成 → 冒烟） | Plan 01→02→03→04 顺序 |
| §11 风险：仓库未提交改动误伤 | 每个 Task 的 Step 4 / Task 4 Step 4 |

**Placeholder 扫描**：无 TBD / TODO / 未给出代码的步骤。Task 4 的表格是验收清单而非实现步骤，允许留空勾选框。

**类型一致性**：`build_native_understanding_layer(settings, repository, provider_http)` 在 Task 2 定义并在同任务测试中按三位置参数调用；`layer._engine` / `layer._duration_probe` / `layer._probe_duration` 与 Plan 02 实现一致；`GeminiNativeEngine(base_url, api_key, model_name, timeout_seconds, client)` 与 Plan 02 签名一致；`probe_video_duration` 与 Plan 03 Task 1 一致。

**遗留提醒**：Task 3 Step 3 会产生真实费用（约一次 18s 视频 ≈ 5000 prompt tokens），只有在用户明确同意后才执行。
