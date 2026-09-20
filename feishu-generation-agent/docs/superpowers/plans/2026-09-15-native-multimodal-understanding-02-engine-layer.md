# 原生多模态理解层 — Plan 02：Gemini 引擎与理解层编排

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `GeminiNativeEngine`（httpx 直连 Gemini 原生 `:generateContent`）与 `NativeUnderstandingLayer`（大小闸门 → 缓存 → 重试 → 确定性校验 → 降级信号）。本计划不接 graph；层只负责"原生理解"，是否降级由调用方（Plan 03 的 graph 节点）决定。

**Architecture:** `engine.py` 定义 `NativeEngine` 协议与错误类型（不含 HTTP）；`gemini.py` 实现该协议；`layer.py` 做编排与降级判定，对外只暴露两个方法，失败时抛 `NativeUnavailable(reason)` 让调用方回退到既有抽帧路径。

**Tech Stack:** Python 3.12、httpx 0.28、pydantic v2、pytest（`asyncio_mode="auto"`）

**Spec:** `docs/superpowers/specs/2026-09-15-native-multimodal-understanding-design.md`
**前置:** Plan 01 必须已合入（`VideoEvidence`、`build_cache_key`、`validate_video_evidence`、`vision_cache` 迁移）

## Global Constraints

- 工作目录 `<agent>` = `C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent`；解释器 `<agent>\.venv\Scripts\python.exe`
- **仓库有 111 项既有未提交改动。禁止 `git checkout` / `git reset` / `git restore` / `git clean`。**
- **禁止裸 `git commit`**：index 里已有 65 个用户 staged 文件。一律 `git commit --only <paths>`。
- **`video_url` 协议永久禁用**：实测该中转会返回 HTTP 200 但静默丢弃视频（`prompt_tokens` 937 → 63，模型回 `UNSUPPORTED`）。只用 `inline_data`。
- 唯一可用端点：`POST {base}/v1beta/models/{model}:generateContent`
- **中转不支持 Gemini File API**（`/v1beta/files` 与 `/upload/v1beta/files` 均 `Server disconnected`）→ 只有 inline 一条通道
- inline 安全线 `native_video_max_inline_bytes = 15 * 1024 * 1024`
- 超时 300s；结构重试 3 次；传输错误重试 1 次
- 每条证据必须带 `engine_id='gemini_native'` + `schema_version='native_v1'`（留痕红线）
- httpx 客户端必须 `trust_env=False`（本机有 `socks4://` 代理变量，httpx 会直接抛 `Unknown scheme for proxy URL`）
- 不触碰统计功能实现

---

### Task 1: 新增原生理解层配置

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/config.py`（在 `claude_model` 之后插入字段；在 `asset_public_url` 之后插入两个 property）
- Test: `<agent>/tests/unit/test_native_config.py`（新建）

**Interfaces:**
- Produces（`Settings` 新成员）：
  - `native_understanding_enabled: bool = True`
  - `native_vision_model: str = "gemini-2.5-flash"`
  - `native_vision_api_key: SecretStr | None = None`
  - `native_vision_base_url: str | None = None`
  - `native_vision_timeout_seconds: float = 300.0`
  - `native_video_max_inline_bytes: int = 15728640`
  - `Settings.resolved_native_vision_api_key -> str`
  - `Settings.resolved_native_vision_base_url -> str`

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_config.py`：

```python
from feishu_generation_agent.config import Settings


def _settings(**overrides):
    base = dict(
        _env_file=None,
        claude_api_key="claude-key-value",
        claude_base_url="https://ai.t8star.org",
    )
    base.update(overrides)
    return Settings(**base)


def test_native_defaults():
    settings = _settings()
    assert settings.native_understanding_enabled is True
    assert settings.native_vision_model == "gemini-2.5-flash"
    assert settings.native_vision_timeout_seconds == 300.0
    assert settings.native_video_max_inline_bytes == 15 * 1024 * 1024


def test_native_credentials_fall_back_to_claude_channel():
    settings = _settings()
    assert settings.resolved_native_vision_api_key == "claude-key-value"
    assert settings.resolved_native_vision_base_url == "https://ai.t8star.org"


def test_native_credentials_can_be_overridden_independently():
    settings = _settings(
        native_vision_api_key="dedicated-key",
        native_vision_base_url="https://other.example/v2/",
    )
    assert settings.resolved_native_vision_api_key == "dedicated-key"
    assert settings.resolved_native_vision_base_url == "https://other.example/v2"


def test_native_credentials_empty_when_nothing_configured():
    settings = Settings(_env_file=None)
    assert settings.resolved_native_vision_api_key == ""
    assert settings.resolved_native_vision_base_url == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_config.py -v
```
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'native_understanding_enabled'`

- [ ] **Step 3: Write minimal implementation**

`<agent>/src/feishu_generation_agent/config.py`，在 `claude_model: str | None = None`（第 81 行）之后插入：

```python
    # 原生多模态理解层（v1：Gemini 原生视频/图片证据）。
    # 默认复用 claude_api_key / claude_base_url —— 实测同一把 t8star key
    # 既能跑 Claude 图片分析，也能原生吃 MP4。
    native_understanding_enabled: bool = True
    native_vision_model: str = "gemini-2.5-flash"
    native_vision_api_key: SecretStr | None = None
    native_vision_base_url: str | None = None
    native_vision_timeout_seconds: float = Field(default=300.0, gt=0.0)
    # Gemini inline 请求体上限约 20MB，base64 膨胀 4/3 → 原文件安全线 15MB。
    # 实测 21.3MB base64 能过，但那只是边界，不作为常规路径。
    native_video_max_inline_bytes: int = Field(
        default=15 * 1024 * 1024, ge=1
    )
```

在同一文件的 `asset_public_url` 方法之后插入：

```python
    @property
    def resolved_native_vision_api_key(self) -> str:
        value = self.native_vision_api_key or self.claude_api_key
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return value or ""

    @property
    def resolved_native_vision_base_url(self) -> str:
        value = self.native_vision_base_url or self.claude_base_url or ""
        return value.strip().rstrip("/")
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_config.py -v
```
Expected: PASS（4 passed）

- [ ] **Step 5: 回归：既有配置测试不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/config.py tests/unit/test_native_config.py
git commit --only src/feishu_generation_agent/config.py tests/unit/test_native_config.py -m "feat(native): 原生理解层配置项"
```

---

### Task 2: 提示词契约与 `NativeEngine` 协议

**Files:**
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/prompts.py`
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/engine.py`
- Test: `<agent>/tests/unit/test_native_prompts.py`（新建）

**Interfaces:**
- Consumes: Plan 01 的 `VideoEvidence` / `VisionDescription`
- Produces:
  - `PROMPT_VERSION = "native_v1"`
  - `VIDEO_PROMPT: str`、`IMAGE_PROMPT: str`
  - `TEXT_ONLY_BASELINE_TOKENS = 200`
  - `EngineVideoResult(evidence: VideoEvidence, prompt_tokens: int)`
  - `EngineImageResult(description: VisionDescription, prompt_tokens: int)`
  - `NativeEngineError(message, *, retryable: bool, status_code: int | None = None)`
  - `NativeEngine` Protocol：`name`、`model_name`、`understand_video(...)`、`understand_image(...)`

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_prompts.py`：

```python
from feishu_generation_agent.integrations.native_understanding.prompts import (
    IMAGE_PROMPT,
    PROMPT_VERSION,
    TEXT_ONLY_BASELINE_TOKENS,
    VIDEO_PROMPT,
)


def test_video_prompt_requires_the_timed_json_contract():
    for field in (
        "shots",
        "start",
        "end",
        "shot_size",
        "action",
        "camera",
        "transcript",
        "audio",
        "on_screen_text",
        "representative_timestamp",
        "summary",
        "kind",
        "uncertainties",
    ):
        assert field in VIDEO_PROMPT, field


def test_video_prompt_forbids_fabrication_and_requires_time_points():
    assert "不得推断" in VIDEO_PROMPT or "不要编造" in VIDEO_PROMPT
    assert "时间点" in VIDEO_PROMPT


def test_image_prompt_covers_every_vision_description_field():
    for field in (
        "subjects",
        "scene",
        "style",
        "composition",
        "characters",
        "actions",
        "visible_text",
        "colors",
        "probable_role",
        "uncertainties",
    ):
        assert field in IMAGE_PROMPT, field


def test_prompt_version_and_baseline_are_pinned():
    assert PROMPT_VERSION == "native_v1"
    assert TEXT_ONLY_BASELINE_TOKENS == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_prompts.py -v
```
Expected: FAIL — `ModuleNotFoundError: ...native_understanding.prompts`

- [ ] **Step 3: Write minimal implementation**

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/prompts.py`：

```python
"""原生理解层的提示词契约。

视频提示词产出的是"带时间点的结构化证据"，不是散文摘要 —— 这是本层
相对旧 3 帧方案的核心增量，也是 v2 成片返工诊断能复用同一 schema 的前提。
"""

PROMPT_VERSION = "native_v1"

# 实测：纯文本请求的 promptTokenCount 约 63；真正喂进视频是 937~5195。
# 中转存在"字段不认就静默丢包、照样返回 200"的行为（video_url 实测），
# 因此调用侧必须用这个基线做运行时确认，防止"假装看过视频"。
TEXT_ONLY_BASELINE_TOKENS = 200

VIDEO_PROMPT = """你是严格的视频证据转录工具。只描述视频中直接可见、可听的内容。

不得推断镜头之外的剧情、人物身份、品牌或动机；不要编造画面里没有的信息。

请按时间轴输出 JSON 对象，字段如下：
- shots: 数组，每个镜头一项，含 start（起秒）、end（止秒）、shot_size（景别）、
  action（该镜头内主体的连续动作，要写清动作如何开始、如何结束）、
  camera（运镜方式，如固定/缓慢推近/手持跟随/从左向右摇镜；必须结合帧间
  主体位移与画面边界变化判断，不得只写"有运镜"）
- transcript: 数组，每句台词或歌词一项，含 t（出现时间点，秒）与 text（原文，
  不要翻译、不要改写；听不清就整体省略该句）
- audio: 数组，背景音乐、音效、环境音等的文字描述
- on_screen_text: 数组，画面中实际出现的字幕、标题、贴纸文字，逐条抄录
- representative_timestamp: 最能代表这段视频的单个时间点（秒），必须落在某个镜头区间内
- summary: 中文概括这段视频能被后续生成任务直接参考的画面要点
- kind: 只能是 character、camera_movement、editing_style、scene_style、other 之一
- uncertainties: 数组，所有不确定的信息只能放这里，不得混入其他字段

时间点必须尽量精确到 0.1 秒。只返回符合上述结构的 JSON 对象，不要附加解释。"""

IMAGE_PROMPT = """你是严格的图片观察与转录工具。
1. 只描述图片中直接可见的内容，不补充图片之外的信息。
2. 不得推断未出现的剧情、品牌或人物身份。
3. visible_text 必须逐项抄录图片中实际可见的文字；看不清时不要猜测。
4. 所有不确定信息只能写入 uncertainties，不得混入其他字段。
5. 严格按给定结构返回结果，不要附加解释或原始响应。

请返回 JSON 对象，字段如下：
- subjects: 数组，画面主体
- scene: 场景描述
- style: 画风或视觉风格
- composition: 构图描述
- characters: 数组，画面中的人物及其外观特征
- actions: 数组，可见的动作
- visible_text: 数组，画面中实际可见的文字
- colors: 数组，主要色彩
- probable_role: 这张图作为参考素材最可能的用途
- uncertainties: 数组，不确定的信息"""
```

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/engine.py`：

```python
"""引擎无关的理解层协议。

刻意不含任何 HTTP：引擎实现（gemini.py）与编排（layer.py）分开，
便于用 fake engine 做完整的单测而不打网络。
"""

from dataclasses import dataclass
from typing import Protocol

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VideoEvidence,
    VisionDescription,
)


@dataclass(frozen=True, slots=True)
class EngineVideoResult:
    evidence: VideoEvidence
    prompt_tokens: int


@dataclass(frozen=True, slots=True)
class EngineImageResult:
    description: VisionDescription
    prompt_tokens: int


class NativeEngineError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class NativeEngine(Protocol):
    name: str
    model_name: str

    async def understand_video(
        self,
        asset: MediaAsset,
        *,
        duration: float,
    ) -> EngineVideoResult:
        raise NotImplementedError

    async def understand_image(self, asset: MediaAsset) -> EngineImageResult:
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_prompts.py -v
```
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/native_understanding/prompts.py src/feishu_generation_agent/integrations/native_understanding/engine.py tests/unit/test_native_prompts.py
git commit --only src/feishu_generation_agent/integrations/native_understanding/prompts.py src/feishu_generation_agent/integrations/native_understanding/engine.py tests/unit/test_native_prompts.py -m "feat(native): 提示词契约与引擎协议"
```

---

### Task 3: `GeminiNativeEngine`

**Files:**
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/gemini.py`
- Test: `<agent>/tests/unit/test_native_gemini_engine.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `EngineVideoResult` / `EngineImageResult` / `NativeEngineError` / `VIDEO_PROMPT` / `IMAGE_PROMPT` / `TEXT_ONLY_BASELINE_TOKENS`
- Produces:
  - `GeminiNativeEngine(*, base_url: str, api_key: str, model_name: str, timeout_seconds: float = 300.0, client: httpx.AsyncClient | None = None)`
  - 属性 `name = "gemini_native"`、`model_name`
  - `endpoint` property → `f"{base_url}/v1beta/models/{model_name}:generateContent"`

**行为规格：**
1. 请求体必须是 Gemini 原生结构：`contents[0].parts = [{"text": ...}, {"inline_data": {"mime_type": ..., "data": <base64>}}]`，外加 `generationConfig.responseMimeType = "application/json"`
2. **绝不使用 `video_url` / OpenAI 兼容 schema**（实测静默丢包）
3. `usageMetadata.promptTokenCount <= TEXT_ONLY_BASELINE_TOKENS` → 抛 `NativeEngineError(retryable=False)`，消息含 `silent_drop`
4. HTTP 429 / 5xx / `httpx.TransportError` / `TimeoutError` → `retryable=True`
5. HTTP 4xx（非 429）→ `retryable=False`
6. `candidates[0].content.parts[*].text` 拼接后 `json.loads`；解析失败或结构非法 → `NativeEngineError(retryable=True)`
7. `kind` 非法值（不在 `VideoReferenceKind` 内）→ 归一为 `OTHER`，不报错
8. `duration` 由调用方给出，引擎写入 `VideoEvidence.duration`；`engine_id` / `schema_version` 由 layer 填，引擎留空字符串

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_gemini_engine.py`：

```python
import base64
import json
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.native_understanding.engine import (
    NativeEngineError,
)
from feishu_generation_agent.integrations.native_understanding.gemini import (
    GeminiNativeEngine,
)


def _asset(tmp_path: Path, *, mime: str = "video/mp4", size: int = 1024) -> MediaAsset:
    path = tmp_path / ("clip.mp4" if mime.startswith("video") else "shot.png")
    path.write_bytes(b"x" * size)
    return MediaAsset(
        asset_id="asset-video-1",
        source_block_id="video-1",
        origin="feishu",
        file_token="tok",
        local_path=path,
        mime_type=mime,
        size=size,
        sha256="a" * 64,
    )


def _gemini_response(payload: dict, prompt_tokens: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": json.dumps(payload)}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "totalTokenCount": prompt_tokens + 50,
            },
        },
    )


_VIDEO_JSON = {
    "shots": [
        {
            "start": 0.0,
            "end": 2.8,
            "shot_size": "中景",
            "action": "女子跪在棺材边哭喊",
            "camera": "固定",
        }
    ],
    "transcript": [{"t": 11.2, "text": "Oh, Grandma?"}],
    "audio": ["背景音乐"],
    "on_screen_text": [],
    "representative_timestamp": 2.8,
    "summary": "哭丧后棺盖被打开",
    "kind": "scene_style",
    "uncertainties": [],
}


async def test_uses_gemini_native_inline_data_endpoint(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["auth"] = request.headers.get("authorization")
        return _gemini_response(_VIDEO_JSON, 937)

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await engine.understand_video(_asset(tmp_path), duration=18.08)

    assert seen["url"] == (
        "https://ai.t8star.org/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert seen["auth"] == "Bearer k"
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["text"].startswith("你是严格的视频证据转录工具")
    assert parts[1]["inline_data"]["mime_type"] == "video/mp4"
    assert base64.b64decode(parts[1]["inline_data"]["data"])
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    # 红线：绝不能出现 video_url / OpenAI 兼容字段
    flat = json.dumps(seen["body"])
    assert "video_url" not in flat
    assert "image_url" not in flat

    assert result.prompt_tokens == 937
    assert result.evidence.shots[0].action == "女子跪在棺材边哭喊"
    assert result.evidence.transcript[0].t == 11.2
    assert result.evidence.duration == 18.08


async def test_silent_drop_is_detected_via_prompt_tokens(tmp_path):
    """实测教训：video_url 那次中转返回 200 但视频被丢，prompt_tokens 只有 63。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(
            {
                "shots": [],
                "transcript": [],
                "audio": [],
                "on_screen_text": [],
                "representative_timestamp": 0.0,
                "summary": "UNSUPPORTED: 我看不到视频",
                "kind": "other",
                "uncertainties": [],
            },
            prompt_tokens=63,
        )

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NativeEngineError) as excinfo:
        await engine.understand_video(_asset(tmp_path), duration=18.08)
    assert excinfo.value.retryable is False
    assert "silent_drop" in str(excinfo.value)


async def test_unknown_kind_is_normalized_to_other(tmp_path):
    payload = dict(_VIDEO_JSON, kind="完全没听说过的类别")

    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(payload, 937)

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await engine.understand_video(_asset(tmp_path), duration=18.08)
    assert result.evidence.kind.value == "other"


async def test_server_error_is_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NativeEngineError) as excinfo:
        await engine.understand_video(_asset(tmp_path), duration=18.08)
    assert excinfo.value.retryable is True
    assert excinfo.value.status_code == 500


async def test_client_error_is_not_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NativeEngineError) as excinfo:
        await engine.understand_video(_asset(tmp_path), duration=18.08)
    assert excinfo.value.retryable is False


async def test_unparseable_body_is_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "not json at all"}]}}],
                "usageMetadata": {"promptTokenCount": 937},
            },
        )

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NativeEngineError) as excinfo:
        await engine.understand_video(_asset(tmp_path), duration=18.08)
    assert excinfo.value.retryable is True


async def test_image_path_returns_vision_description(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(
            {
                "subjects": ["蓝色纸船"],
                "scene": "小河",
                "style": "柔和插画",
                "composition": "居中",
                "characters": [],
                "actions": ["漂流"],
                "visible_text": [],
                "colors": ["蓝色"],
                "probable_role": "主体参考图",
                "uncertainties": [],
            },
            prompt_tokens=300,
        )

    engine = GeminiNativeEngine(
        base_url="https://ai.t8star.org",
        api_key="k",
        model_name="gemini-2.5-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await engine.understand_image(
        _asset(tmp_path, mime="image/png", size=64)
    )
    assert result.description.subjects == ["蓝色纸船"]
    assert result.description.probable_role == "主体参考图"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_gemini_engine.py -v
```
Expected: FAIL — `ModuleNotFoundError: ...native_understanding.gemini`

- [ ] **Step 3: Write minimal implementation**

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/gemini.py`：

```python
"""Gemini 原生引擎：httpx 直连 :generateContent。

为什么不用 LangChain 的 ChatGoogleGenerativeAI / OpenAI 兼容层：
本机 t8star 中转在 OpenAI 兼容 schema 下会把视频**静默丢弃** —— 实测
`video_url` 请求返回 HTTP 200、无任何错误，但 usage 只剩 63 个 prompt token，
模型回答"我看不到视频"。走原生 `inline_data` 才能拿到 937~5195 token 的真实理解。

另一个实测约束：该中转不支持 Gemini File API（/v1beta/files 与
/upload/v1beta/files 均直接断连），所以只有 inline 一条通道。
"""

import base64
import json
from typing import Any

import httpx

from feishu_generation_agent.domain.document import (
    MediaAsset,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
    VisionDescription,
)

from .engine import EngineImageResult, EngineVideoResult, NativeEngineError
from .prompts import IMAGE_PROMPT, TEXT_ONLY_BASELINE_TOKENS, VIDEO_PROMPT

_VALID_KINDS = {kind.value for kind in VideoReferenceKind}


class GeminiNativeEngine:
    name = "gemini_native"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        # trust_env=False：本机存在 socks4:// 代理变量，httpx 会因不认该 scheme
        # 直接抛 ValueError("Unknown scheme for proxy URL")。
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, trust_env=False
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1beta/models/{self.model_name}:generateContent"

    async def understand_video(
        self,
        asset: MediaAsset,
        *,
        duration: float,
    ) -> EngineVideoResult:
        data = asset.local_path.read_bytes()
        payload = await self._generate(
            prompt=VIDEO_PROMPT,
            mime_type=asset.mime_type,
            data=data,
            asset_id=asset.asset_id,
        )
        evidence = VideoEvidence(
            asset_id=asset.asset_id,
            engine_id="",
            schema_version="",
            duration=duration,
            shots=[
                VideoShot(
                    start=float(shot.get("start", 0.0)),
                    end=float(shot.get("end", 0.0)),
                    shot_size=str(shot.get("shot_size", "")),
                    action=str(shot.get("action", "")),
                    camera=str(shot.get("camera", "")),
                )
                for shot in payload.get("shots", [])
                if isinstance(shot, dict)
            ],
            transcript=[
                TranscriptLine(t=float(line.get("t", 0.0)), text=str(line.get("text", "")))
                for line in payload.get("transcript", [])
                if isinstance(line, dict)
            ],
            audio=[str(item) for item in payload.get("audio", [])],
            on_screen_text=[str(item) for item in payload.get("on_screen_text", [])],
            representative_timestamp=float(
                payload.get("representative_timestamp", 0.0)
            ),
            summary=str(payload.get("summary", "")),
            kind=_normalize_kind(payload.get("kind")),
            uncertainties=[str(item) for item in payload.get("uncertainties", [])],
        )
        return EngineVideoResult(
            evidence=evidence,
            prompt_tokens=int(payload["__prompt_tokens"]),
        )

    async def understand_image(self, asset: MediaAsset) -> EngineImageResult:
        data = asset.local_path.read_bytes()
        payload = await self._generate(
            prompt=IMAGE_PROMPT,
            mime_type=asset.mime_type,
            data=data,
            asset_id=asset.asset_id,
        )
        description = VisionDescription(
            asset_id="",
            subjects=[str(item) for item in payload.get("subjects", [])],
            scene=str(payload.get("scene", "")),
            style=str(payload.get("style", "")),
            composition=str(payload.get("composition", "")),
            characters=[str(item) for item in payload.get("characters", [])],
            actions=[str(item) for item in payload.get("actions", [])],
            visible_text=[str(item) for item in payload.get("visible_text", [])],
            colors=[str(item) for item in payload.get("colors", [])],
            probable_role=str(payload.get("probable_role", "")),
            uncertainties=[str(item) for item in payload.get("uncertainties", [])],
        )
        return EngineImageResult(
            description=description,
            prompt_tokens=int(payload["__prompt_tokens"]),
        )

    async def _generate(
        self,
        *,
        prompt: str,
        mime_type: str,
        data: bytes,
        asset_id: str,
    ) -> dict[str, Any]:
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        try:
            response = await self._client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout_seconds,
            )
        except (httpx.TransportError, TimeoutError) as exc:
            raise NativeEngineError(
                f"原生理解请求失败 asset_id={asset_id} cause={type(exc).__name__}",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise NativeEngineError(
                f"原生理解返回非 200 asset_id={asset_id} "
                f"status={response.status_code} body={response.text[:200]}",
                retryable=retryable,
                status_code=response.status_code,
            )

        try:
            envelope = response.json()
            prompt_tokens = int(
                envelope.get("usageMetadata", {}).get("promptTokenCount", 0)
            )
        except (ValueError, TypeError) as exc:
            raise NativeEngineError(
                f"原生理解响应不是合法 JSON asset_id={asset_id}",
                retryable=True,
            ) from exc

        if prompt_tokens <= TEXT_ONLY_BASELINE_TOKENS:
            # 静默丢包检测：中转可能返回 200 但把素材丢掉，只送了文本。
            raise NativeEngineError(
                f"silent_drop: 原生理解只收到文本 prompt_tokens={prompt_tokens} "
                f"asset_id={asset_id}；疑似素材被中转丢弃",
                retryable=False,
            )

        try:
            parts = envelope["candidates"][0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
            payload = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise NativeEngineError(
                f"原生理解输出不是合法 JSON asset_id={asset_id}",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise NativeEngineError(
                f"原生理解输出不是 JSON 对象 asset_id={asset_id}",
                retryable=True,
            )
        payload["__prompt_tokens"] = prompt_tokens
        return payload


def _normalize_kind(value: Any) -> VideoReferenceKind:
    if isinstance(value, str) and value in _VALID_KINDS:
        return VideoReferenceKind(value)
    return VideoReferenceKind.OTHER
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_gemini_engine.py -v
```
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/native_understanding/gemini.py tests/unit/test_native_gemini_engine.py
git commit --only src/feishu_generation_agent/integrations/native_understanding/gemini.py tests/unit/test_native_gemini_engine.py -m "feat(native): Gemini 原生引擎与静默丢包检测"
```

---

### Task 4: `NativeUnderstandingLayer`（大小闸门 / 缓存 / 重试 / 校验 / 降级信号）

**Files:**
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/layer.py`
- Test: `<agent>/tests/unit/test_native_layer.py`（新建）

**Interfaces:**
- Consumes: Plan 01 的 `build_cache_key` / `validate_video_evidence` / `EvidenceValidationError` / `ENGINE_ID_GEMINI_NATIVE` / `SCHEMA_VERSION_NATIVE_V1` / `KIND_IMAGE` / `KIND_VIDEO`；Task 2/3 的引擎协议
- Produces:
  - `NativeUnavailable(RuntimeError)`，带 `.reason: str`
  - `NativeUnderstandingLayer(engine, repository, settings, *, duration_probe=None)`
  - `await layer.understand_video(asset) -> VideoEvidence`
  - `await layer.understand_image(asset) -> VisionDescription`
  - `layer.enabled` property

**行为规格：**
1. `settings.native_understanding_enabled is False` 或 `engine is None` → `NativeUnavailable("disabled")`
2. 视频 `asset.size > settings.native_video_max_inline_bytes` → `NativeUnavailable("exceeds_inline_limit")`，**引擎零调用**
3. `duration_probe` 为 None 或抛异常 → `NativeUnavailable("duration_unavailable")`
4. 缓存命中（按 Plan 01 的新 key）→ **引擎零调用**，直接返回
5. 缓存未命中 → 调引擎；`NativeEngineError.retryable=True` 时最多重试到 3 次尝试；`retryable=False` 立即放弃
6. 拿到证据后跑 `validate_video_evidence`；`EvidenceValidationError` → `NativeUnavailable("validation_failed")`
7. 成功 → 写入缓存（带 `engine_id` / `schema_version` / `kind` 三列），证据回填 `engine_id='gemini_native'`、`schema_version='native_v1'`、`asset_id`
8. 图片失败一律 `NativeUnavailable`（调用方回退到 Claude）

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_layer.py`：

```python
from pathlib import Path

import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    MediaAsset,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
    VisionDescription,
)
from feishu_generation_agent.integrations.native_understanding.engine import (
    EngineImageResult,
    EngineVideoResult,
    NativeEngineError,
)
from feishu_generation_agent.integrations.native_understanding.layer import (
    NativeUnderstandingLayer,
    NativeUnavailable,
)
from feishu_generation_agent.storage.repository import Repository


class FakeEngine:
    name = "gemini_native"
    model_name = "gemini-2.5-flash"

    def __init__(self, *, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.video_calls = 0
        self.image_calls = 0

    def _evidence(self, asset: MediaAsset, duration: float) -> VideoEvidence:
        return VideoEvidence(
            asset_id="",
            engine_id="",
            schema_version="",
            duration=duration,
            shots=[
                VideoShot(start=0.0, end=2.8, shot_size="中景", action="哭喊", camera="固定")
            ],
            transcript=[TranscriptLine(t=1.0, text="Oh, Grandma?")],
            audio=["背景音乐"],
            on_screen_text=[],
            representative_timestamp=1.0,
            summary="女子哭丧",
            kind=VideoReferenceKind.SCENE_STYLE,
            uncertainties=[],
        )

    async def understand_video(self, asset, *, duration):
        self.video_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return EngineVideoResult(
            evidence=self._evidence(asset, duration), prompt_tokens=937
        )

    async def understand_image(self, asset):
        self.image_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return EngineImageResult(
            description=VisionDescription(
                asset_id="",
                subjects=["蓝色纸船"],
                scene="小河",
                style="插画",
                composition="居中",
                characters=[],
                actions=["漂流"],
                visible_text=[],
                colors=["蓝色"],
                probable_role="主体参考图",
                uncertainties=[],
            ),
            prompt_tokens=300,
        )


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


def _video_asset(tmp_path: Path, size: int = 1024, suffix: str = "a") -> MediaAsset:
    path = tmp_path / f"clip-{suffix}.mp4"
    path.write_bytes(b"x" * size)
    return MediaAsset(
        asset_id=f"asset-{suffix}",
        source_block_id="video-1",
        origin="feishu",
        file_token="tok",
        local_path=path,
        mime_type="video/mp4",
        size=size,
        sha256=suffix * 64,
    )


def _image_asset(tmp_path: Path) -> MediaAsset:
    path = tmp_path / "shot.png"
    path.write_bytes(b"png")
    return MediaAsset(
        asset_id="asset-img",
        source_block_id="image-1",
        origin="feishu",
        file_token="tok2",
        local_path=path,
        mime_type="image/png",
        size=3,
        sha256="c" * 64,
    )


async def _layer(tmp_path, *, engine, duration=18.08, **settings_overrides):
    settings = _settings(tmp_path, **settings_overrides)
    repository = await Repository.open(settings.business_db_path)
    layer = NativeUnderstandingLayer(
        engine=engine,
        repository=repository,
        settings=settings,
        duration_probe=lambda path: duration,
    )
    return layer, repository


async def test_video_success_fills_engine_and_schema_and_caches(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        asset = _video_asset(tmp_path)
        evidence = await layer.understand_video(asset)
        assert evidence.engine_id == "gemini_native"
        assert evidence.schema_version == "native_v1"
        assert evidence.asset_id == asset.asset_id
        assert engine.video_calls == 1
        assert await repository.list_vision_cache_engine_ids() == {"gemini_native": 1}
    finally:
        await repository.close()


async def test_second_call_hits_cache_and_skips_engine(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        asset = _video_asset(tmp_path)
        await layer.understand_video(asset)
        await layer.understand_video(asset)
        assert engine.video_calls == 1
    finally:
        await repository.close()


async def test_oversize_video_degrades_without_calling_engine(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(
        tmp_path, engine=engine, native_video_max_inline_bytes=100
    )
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path, size=2048))
        assert excinfo.value.reason == "exceeds_inline_limit"
        assert engine.video_calls == 0
    finally:
        await repository.close()


async def test_retryable_engine_error_retries_then_degrades(tmp_path):
    engine = FakeEngine(
        failures=[
            NativeEngineError("500", retryable=True, status_code=500),
            NativeEngineError("500", retryable=True, status_code=500),
            NativeEngineError("500", retryable=True, status_code=500),
        ]
    )
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path))
        assert excinfo.value.reason == "engine_failed"
        assert engine.video_calls == 3
    finally:
        await repository.close()


async def test_retryable_error_recovers_on_second_attempt(tmp_path):
    engine = FakeEngine(
        failures=[NativeEngineError("500", retryable=True, status_code=500)]
    )
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        evidence = await layer.understand_video(_video_asset(tmp_path))
        assert evidence.engine_id == "gemini_native"
        assert engine.video_calls == 2
    finally:
        await repository.close()


async def test_non_retryable_error_degrades_immediately(tmp_path):
    engine = FakeEngine(
        failures=[NativeEngineError("silent_drop", retryable=False)]
    )
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path))
        assert excinfo.value.reason == "engine_failed"
        assert engine.video_calls == 1
    finally:
        await repository.close()


async def test_duration_mismatch_degrades_with_validation_failed(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(tmp_path, engine=engine, duration=30.0)
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path))
        assert excinfo.value.reason == "validation_failed"
    finally:
        await repository.close()


async def test_disabled_layer_degrades(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(
        tmp_path, engine=engine, native_understanding_enabled=False
    )
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path))
        assert excinfo.value.reason == "disabled"
        assert engine.video_calls == 0
    finally:
        await repository.close()


async def test_missing_duration_probe_degrades(tmp_path):
    settings = _settings(tmp_path)
    repository = await Repository.open(settings.business_db_path)
    engine = FakeEngine()
    layer = NativeUnderstandingLayer(
        engine=engine, repository=repository, settings=settings
    )
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_video(_video_asset(tmp_path))
        assert excinfo.value.reason == "duration_unavailable"
        assert engine.video_calls == 0
    finally:
        await repository.close()


async def test_image_success_fills_asset_id(tmp_path):
    engine = FakeEngine()
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        description = await layer.understand_image(_image_asset(tmp_path))
        assert description.asset_id == "asset-img"
        assert description.subjects == ["蓝色纸船"]
    finally:
        await repository.close()


async def test_image_failure_degrades(tmp_path):
    engine = FakeEngine(failures=[NativeEngineError("boom", retryable=False)])
    layer, repository = await _layer(tmp_path, engine=engine)
    try:
        with pytest.raises(NativeUnavailable) as excinfo:
            await layer.understand_image(_image_asset(tmp_path))
        assert excinfo.value.reason == "engine_failed"
    finally:
        await repository.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_layer.py -v
```
Expected: FAIL — `ModuleNotFoundError: ...native_understanding.layer`

- [ ] **Step 3: Write minimal implementation**

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/layer.py`：

```python
"""原生理解层编排：大小闸门 → 缓存 → 重试 → 确定性校验 → 降级信号。

本层只做"原生理解"。是否降级到抽帧由调用方决定（graph 节点捕获
NativeUnavailable 后走既有 _analyze_video_reference 路径），
这样降级策略与 graph 解耦、可独立测试。
"""

from collections.abc import Callable
import logging
from pathlib import Path

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    MediaAsset,
    VideoEvidence,
    VisionDescription,
)
from feishu_generation_agent.storage.repository import Repository

from .engine import NativeEngine, NativeEngineError
from .schemas import (
    ENGINE_ID_GEMINI_NATIVE,
    KIND_IMAGE,
    KIND_VIDEO,
    SCHEMA_VERSION_NATIVE_V1,
    build_cache_key,
)
from .validation import EvidenceValidationError, validate_video_evidence

_MAX_ATTEMPTS = 3

_LOGGER = logging.getLogger(__name__)


class NativeUnavailable(RuntimeError):
    """原生理解不可用，调用方应降级到抽帧路径。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NativeUnderstandingLayer:
    def __init__(
        self,
        engine: NativeEngine | None,
        repository: Repository,
        settings: Settings,
        *,
        duration_probe: Callable[[Path], float] | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._settings = settings
        self._duration_probe = duration_probe

    @property
    def enabled(self) -> bool:
        return bool(self._settings.native_understanding_enabled) and (
            self._engine is not None
        )

    def _require_engine(self) -> NativeEngine:
        if not self.enabled or self._engine is None:
            raise NativeUnavailable("disabled", "原生理解层未启用或未配置引擎")
        return self._engine

    def _cache_key(self, asset: MediaAsset, *, kind: str) -> str:
        return build_cache_key(
            engine_id=ENGINE_ID_GEMINI_NATIVE,
            kind=kind,
            model_name=self._engine.model_name if self._engine else "",
            schema_version=SCHEMA_VERSION_NATIVE_V1,
            sha256=asset.sha256,
        )

    async def understand_video(self, asset: MediaAsset) -> VideoEvidence:
        engine = self._require_engine()

        limit = int(self._settings.native_video_max_inline_bytes)
        if asset.size > limit:
            raise NativeUnavailable(
                "exceeds_inline_limit",
                f"asset_id={asset.asset_id} size={asset.size} limit={limit}",
            )

        cache_key = self._cache_key(asset, kind=KIND_VIDEO)
        cached = await self._repository.get_vision_cache(cache_key)
        if cached is not None:
            return VideoEvidence.model_validate(cached)

        duration = self._probe_duration(asset)

        last_detail = ""
        for _ in range(_MAX_ATTEMPTS):
            try:
                result = await engine.understand_video(asset, duration=duration)
            except NativeEngineError as exc:
                last_detail = str(exc)
                if not exc.retryable:
                    raise NativeUnavailable("engine_failed", last_detail) from exc
                continue

            try:
                validated, issues = validate_video_evidence(
                    result.evidence, ffprobe_duration=duration
                )
            except EvidenceValidationError as exc:
                raise NativeUnavailable(
                    "validation_failed", "; ".join(exc.issues)
                ) from exc

            evidence = validated.model_copy(
                update={
                    "asset_id": asset.asset_id,
                    "engine_id": ENGINE_ID_GEMINI_NATIVE,
                    "schema_version": SCHEMA_VERSION_NATIVE_V1,
                }
            )
            await self._repository.save_vision_cache(
                cache_key,
                evidence,
                engine_id=ENGINE_ID_GEMINI_NATIVE,
                schema_version=SCHEMA_VERSION_NATIVE_V1,
                kind=KIND_VIDEO,
            )
            if issues:
                _LOGGER.warning(
                    "原生视频证据已夹紧/剔除部分时间轴 asset_id=%s issues=%s",
                    asset.asset_id,
                    issues,
                )
            return evidence

        raise NativeUnavailable("engine_failed", last_detail or "重试耗尽")

    async def understand_image(self, asset: MediaAsset) -> VisionDescription:
        engine = self._require_engine()

        cache_key = self._cache_key(asset, kind=KIND_IMAGE)
        cached = await self._repository.get_vision_cache(cache_key)
        if cached is not None:
            return VisionDescription.model_validate(cached)

        last_detail = ""
        for _ in range(_MAX_ATTEMPTS):
            try:
                result = await engine.understand_image(asset)
            except NativeEngineError as exc:
                last_detail = str(exc)
                if not exc.retryable:
                    raise NativeUnavailable("engine_failed", last_detail) from exc
                continue
            except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不打断整轮
                raise NativeUnavailable(
                    "engine_failed", f"{type(exc).__name__}: {exc}"
                ) from exc

            description = result.description.model_copy(
                update={"asset_id": asset.asset_id}
            )
            await self._repository.save_vision_cache(
                cache_key,
                description,
                engine_id=ENGINE_ID_GEMINI_NATIVE,
                schema_version=SCHEMA_VERSION_NATIVE_V1,
                kind=KIND_IMAGE,
            )
            return description

        raise NativeUnavailable("engine_failed", last_detail or "重试耗尽")

    def _probe_duration(self, asset: MediaAsset) -> float:
        if self._duration_probe is None:
            raise NativeUnavailable("duration_unavailable", "未注入 duration_probe")
        try:
            duration = float(self._duration_probe(asset.local_path))
        except Exception as exc:  # noqa: BLE001
            raise NativeUnavailable(
                "duration_unavailable", f"{type(exc).__name__}: {exc}"
            ) from exc
        if duration <= 0:
            raise NativeUnavailable(
                "duration_unavailable", f"duration={duration}"
            )
        return duration
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_layer.py -v
```
Expected: PASS（11 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/native_understanding/layer.py tests/unit/test_native_layer.py
git commit --only src/feishu_generation_agent/integrations/native_understanding/layer.py tests/unit/test_native_layer.py -m "feat(native): 理解层编排与降级信号"
```

---

### Task 5: 本计划总验收

- [ ] **Step 1: 跑本计划全部新增测试**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_config.py tests/unit/test_native_prompts.py tests/unit/test_native_gemini_engine.py tests/unit/test_native_layer.py -v
```
Expected: PASS（全部通过）

- [ ] **Step 2: 确认全链路不出现被禁用的协议字段**

Run:
```bash
.venv\Scripts\python.exe -c "import pathlib; root=pathlib.Path('src/feishu_generation_agent/integrations/native_understanding'); hits=[(p.name,i+1,l.strip()) for p in root.glob('*.py') for i,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if 'video_url' in l and 'not in' not in l and '禁用' not in l and '绝不' not in l]; print(hits)"
```
Expected: `[]`

- [ ] **Step 3: 回归：既有视觉与存储测试**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_vision.py tests/unit/test_storage.py tests/unit/test_config.py -v
```
Expected: PASS

- [ ] **Step 4: 确认工作区改动条目数没有减少**

Run:
```bash
git status --porcelain | Measure-Object -Line
```
Expected: ≥ 112。若变小，立即停止并上报。

- [ ] **Step 5: Task 5 无代码改动，无需 commit**

---

## Self-Review

**Spec 覆盖检查**

| Spec 章节 | 落地任务 |
|---|---|
| §4.1 `engine.py` / `gemini.py` / `prompts.py` | Task 2、Task 3 |
| §4.2 职责边界（引擎不含 HTTP 以外的编排） | Task 2、Task 3 |
| §6.1 新 cache key 用于读写 | Task 4 |
| §6.2 留痕红线 + 运行时 token 确认 | Task 3（`silent_drop`）、Task 4（回填 engine_id/schema_version） |
| §6.3 确定性校验接入 | Task 4 |
| §7.1 降级触发（大小 / 时长 / 传输 / 结构 / 校验） | Task 4 全部 5 条 |
| §7.2 错误分类（retryable 分流） | Task 3 |
| §7.3 约束（300s / 禁用 video_url / inline_data） | Task 1、Task 3 |
| §8.1 测试 2（>15MB 引擎零调用） | Task 4 |
| §8.1 测试 3（500 → 重试 → 降级） | Task 4 |
| §8.1 测试 10（token 基线确认） | Task 3 `test_silent_drop_is_detected_via_prompt_tokens` |

**未覆盖（Plan 03）**：graph 节点接线、`representative_timestamp` 抽帧、planner prompt 证据段、集成测试、付费冒烟脚本。

**Placeholder 扫描**：无 TBD / TODO / 未给出代码的步骤。

**类型一致性**：`NativeEngineError(message, *, retryable, status_code)` 在 Task 2 定义、Task 3/4 与测试中按该签名使用；`EngineVideoResult(evidence, prompt_tokens)` / `EngineImageResult(description, prompt_tokens)` 一致；`NativeUnavailable(reason, detail="")` 的 `.reason` 取值在 Task 4 实现与测试中断言一致（`disabled` / `exceeds_inline_limit` / `duration_unavailable` / `engine_failed` / `validation_failed`）；`Settings.resolved_native_vision_*` 在 Task 1 定义，Plan 03 组装处消费。
