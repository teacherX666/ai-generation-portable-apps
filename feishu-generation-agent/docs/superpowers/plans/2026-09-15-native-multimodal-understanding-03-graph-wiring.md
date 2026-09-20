# 原生多模态理解层 — Plan 03：抽帧工具与 graph 接线

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan 02 的理解层接进 graph —— 参考视频**原生优先、抽帧兜底**；原生成功时按证据给出的 `representative_timestamp` 抽代表帧（而非现在的"取中间帧"），把 `VideoEvidence` 挂到 document 与 state 上。

**Architecture:** 改动集中在 `_materialize_video_references()`：先试 `services.native_understanding`，抛 `NativeUnavailable` 就回落到既有的 `_analyze_video_reference()`（3 帧 + Claude，行为完全不变）。生成侧仍然把视频替换成图片资产（火山 Bearer 模式吃不了 MP4 本体），只是抽帧时间点改由证据决定。

**Tech Stack:** Python 3.12、ffmpeg/ffprobe（外部二进制）、pytest（`asyncio_mode="auto"`）

**Spec:** `docs/superpowers/specs/2026-09-15-native-multimodal-understanding-design.md`
**前置:** Plan 01 + Plan 02 必须已合入

## Global Constraints

- 工作目录 `<agent>` = `C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent`；解释器 `<agent>\.venv\Scripts\python.exe`
- **仓库有 111 项既有未提交改动。禁止 `git checkout` / `git reset` / `git restore` / `git clean`。**
- **禁止裸 `git commit`**：一律 `git commit --only <paths>`。
- **本机 ffmpeg/ffprobe 不在 PATH**（实在 `C:\ProgramData\chocolatey\bin\`），因此既有抽帧测试在本机是 SKIPPED。**本计划所有接线测试必须用 monkeypatch，不得依赖 ffmpeg 存在**，否则等于没测。
- 不改 `VisionDescription` / `VideoReferenceAnalysis` 字段
- 抽帧路径行为必须与现状完全一致（存量 `test_video_reference.py` 的断言不得修改）
- `services` 可能是不含 `native_understanding` 属性的 `SimpleNamespace`（存量测试就是这么构造的）→ **必须用 `getattr(services, "native_understanding", None)`**
- 不触碰统计功能实现

---

### Task 1: 暴露时长探测、新增按时间点抽帧，并让抽帧测试真正跑起来

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/integrations/video_reference.py`（`_video_duration` 保留，新增公开 `probe_video_duration`；新增 `extract_video_frame_at`）
- Test: `<agent>/tests/unit/test_video_frame_at.py`（新建）

**Interfaces:**
- Consumes: 模块内既有 `_binary` / `_video_duration`
- Produces:
  - `probe_video_duration(video_path: Path) -> float`
  - `extract_video_frame_at(video_path: Path, timestamp: float, output_path: Path) -> Path`
  - `ffmpeg_available() -> bool`（用项目自己的 `_binary` 解析，替代 `shutil.which`）

**为什么需要 `ffmpeg_available()`**：本机 ffmpeg 不在 PATH，`shutil.which("ffmpeg")` 返回 None，导致既有抽帧测试直接 SKIPPED —— 而应用本身因为 `_binary` 有 chocolatey 回退，跑得好好的。测试的跳过条件比应用的可运行条件更严，等于**测试永远不跑**。这个 helper 让跳过条件与真实运行条件一致。

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_video_frame_at.py`：

```python
import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_generation_agent.integrations.video_reference import (
    extract_video_frame_at,
    ffmpeg_available,
    probe_video_duration,
)


def test_ffmpeg_available_agrees_with_binary_resolution():
    from feishu_generation_agent.integrations.video_reference import _binary

    try:
        _binary("ffmpeg")
        _binary("ffprobe")
    except RuntimeError:
        assert ffmpeg_available() is False
    else:
        assert ffmpeg_available() is True


def test_extract_video_frame_at_builds_seek_command(monkeypatch, tmp_path):
    recorded: dict = {}

    def fake_run(command, **kwargs):
        recorded["command"] = command
        output = Path(command[-1])
        output.write_bytes(b"jpeg-bytes")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference._binary",
        lambda name: name,
    )
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference.subprocess.run",
        fake_run,
    )

    output = extract_video_frame_at(
        tmp_path / "sample.mp4", 3.25, tmp_path / "out.jpg"
    )

    command = recorded["command"]
    assert command[0] == "ffmpeg"
    assert "-ss" in command
    assert command[command.index("-ss") + 1] == "3.250"
    assert output.is_file()


def test_probe_video_duration_parses_ffprobe_stdout(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "18.08\n", "")

    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference._binary",
        lambda name: name,
    )
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference.subprocess.run",
        fake_run,
    )
    assert probe_video_duration(tmp_path / "sample.mp4") == pytest.approx(18.08)


def test_probe_video_duration_rejects_garbage(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "not-a-number\n", "")

    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference._binary",
        lambda name: name,
    )
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.video_reference.subprocess.run",
        fake_run,
    )
    with pytest.raises(RuntimeError):
        probe_video_duration(tmp_path / "sample.mp4")


@pytest.mark.skipif(
    not ffmpeg_available(),
    reason="ffmpeg/ffprobe not resolvable (same rule the app uses)",
)
def test_extract_video_frame_at_really_extracts(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        timeout=60,
    )
    duration = probe_video_duration(video)
    assert duration == pytest.approx(2.0, abs=0.3)
    output = extract_video_frame_at(video, 1.0, tmp_path / "frame.jpg")
    assert output.is_file() and output.stat().st_size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_video_frame_at.py -v
```
Expected: FAIL — `ImportError: cannot import name 'ffmpeg_available' from 'feishu_generation_agent.integrations.video_reference'`

- [ ] **Step 3: Write minimal implementation**

在 `<agent>/src/feishu_generation_agent/integrations/video_reference.py` 末尾追加，并把文件顶部 `import shutil` 保留（`_binary` 仍用它）：

```python
def ffmpeg_available() -> bool:
    """用应用自己的二进制解析规则判断 ffmpeg/ffprobe 是否可用。

    不要用 shutil.which：本机 ffmpeg 装在 chocolatey 目录、不在 PATH，
    shutil.which 返回 None 会让测试永远 SKIPPED，而应用其实跑得动。
    测试的跳过条件必须与真实运行条件一致。
    """
    try:
        _binary("ffmpeg")
        _binary("ffprobe")
    except RuntimeError:
        return False
    return True


def probe_video_duration(video_path: Path) -> float:
    """公开的时长探测（原生理解层用作 duration_probe）。"""
    return _video_duration(video_path)


def extract_video_frame_at(
    video_path: Path,
    timestamp: float,
    output_path: Path,
) -> Path:
    """在指定时间点抽一帧 JPEG。

    原生视频理解会给出 representative_timestamp（真正能代表这段视频的
    时间点），比原来"取中间帧"更准。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _binary("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no frame")
    return output_path
```

同时把 `<agent>/tests/unit/test_video_reference.py` 里两处跳过条件从 `shutil.which(...)` 改为 `ffmpeg_available()`：

```python
# 顶部 import 增加
from feishu_generation_agent.integrations.video_reference import (
    extract_video_frames,
    ffmpeg_available,
)

# 两处装饰器改为
@pytest.mark.skipif(
    not ffmpeg_available(),
    reason="ffmpeg/ffprobe not resolvable (same rule the app uses)",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_video_frame_at.py tests/unit/test_video_reference.py -v
```
Expected: PASS 且 **`test_video_reference.py` 的 2 个测试不再 SKIPPED**（本机 ffmpeg 可解析）。若仍 SKIPPED，停下来查 `ffmpeg_available()` 的解析路径。

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/video_reference.py tests/unit/test_video_frame_at.py tests/unit/test_video_reference.py
git commit --only src/feishu_generation_agent/integrations/video_reference.py tests/unit/test_video_frame_at.py tests/unit/test_video_reference.py -m "feat(native): 按时间点抽帧与可用性探测，修正抽帧测试跳过条件"
```

---

### Task 2: `GraphServices` / `AgentState` 增加原生理解字段

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/graph/nodes.py`（`GraphServices` 加字段）
- Modify: `<agent>/src/feishu_generation_agent/graph/state.py`（`AgentState` 加字段）
- Test: `<agent>/tests/unit/test_native_services_field.py`（新建）

**Interfaces:**
- Produces:
  - `GraphServices.native_understanding: Any | None = None`（默认 None → 存量构造点零改动）
  - `AgentState.video_evidence: list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_services_field.py`：

```python
from feishu_generation_agent.graph.nodes import GraphServices


def test_graph_services_accepts_native_understanding_and_defaults_to_none():
    fields = GraphServices.__dataclass_fields__
    assert "native_understanding" in fields
    assert fields["native_understanding"].default is None


def test_agent_state_declares_video_evidence():
    from feishu_generation_agent.graph.state import AgentState

    assert "video_evidence" in AgentState.__annotations__
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_services_field.py -v
```
Expected: FAIL — `assert 'native_understanding' in fields`

- [ ] **Step 3: Write minimal implementation**

`<agent>/src/feishu_generation_agent/graph/nodes.py`：在 `GraphServices` 的最后一个字段之后追加（注意该 dataclass 是 `frozen=True, slots=True`，带默认值的字段必须排在所有无默认值字段之后）：

```python
    # 原生多模态理解层（Gemini 原生视频/图片证据）。
    # 为 None 时视频走既有抽帧路径，行为与改造前完全一致。
    native_understanding: Any | None = None
```

`<agent>/src/feishu_generation_agent/graph/state.py`：在 `vision_issues: list[str]` 之后追加：

```python
    video_evidence: list[dict[str, Any]]
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_services_field.py -v
```
Expected: PASS（2 passed）

- [ ] **Step 5: 回归：GraphServices 构造点不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_real_person_routing.py tests/unit/test_video_model_routing.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/graph/nodes.py src/feishu_generation_agent/graph/state.py tests/unit/test_native_services_field.py
git commit --only src/feishu_generation_agent/graph/nodes.py src/feishu_generation_agent/graph/state.py tests/unit/test_native_services_field.py -m "feat(native): GraphServices/AgentState 增加原生理解字段"
```

---

### Task 3: `_materialize_video_references` 原生优先、抽帧兜底

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/graph/nodes.py`（import 区加 `extract_video_frame_at` 与 `NativeUnavailable`；新增 `_materialize_native_frame`；改写 `_materialize_video_references`）
- Test: `<agent>/tests/unit/test_native_video_materialize.py`（新建，**不依赖 ffmpeg**）

**Interfaces:**
- Consumes: Plan 01 `VideoEvidence`；Plan 02 `NativeUnderstandingLayer` / `NativeUnavailable`；Task 1 `extract_video_frame_at`；Task 2 `services.native_understanding`
- Produces:
  - `_materialize_native_frame(document, services, video, evidence) -> tuple[MediaAsset | None, VideoReferenceAnalysis | None]`
  - `_materialize_video_references(document, services)` 返回值新增 `video_evidence`

**行为规格：**
1. `getattr(services, "native_understanding", None)` 为 None → 直接走既有 `_analyze_video_reference` 路径（存量行为）
2. 有 layer 时先调 `layer.understand_video(video)`；`NativeUnavailable` 或任何异常 → 记 warning，回落到既有路径
3. 原生成功 → 用 `evidence.representative_timestamp` 抽帧（**不是中间帧**），产出 `{asset_id}-frame` 图片资产替换原视频
4. 由证据派生 `VideoReferenceAnalysis(kind=evidence.kind, summary=evidence.summary, uncertainties=evidence.uncertainties, representative_frame_index=1)` —— 保证 planner 既有的"运镜必须写进 prompt"规则继续可用
5. `video_evidence` 累积所有原生成功的证据；降级的视频不进 `video_evidence`（**留痕红线：不能把"没原生看过"伪装成原生证据**）
6. `text_view` 里 `[video:x]` → `[image:x-frame]` 的替换逻辑保持不变

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_video_materialize.py`：

```python
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceAnalysis,
    VideoReferenceKind,
    VideoShot,
)
from feishu_generation_agent.graph import nodes
from feishu_generation_agent.graph.nodes import _materialize_video_references
from feishu_generation_agent.integrations.native_understanding.layer import (
    NativeUnavailable,
)
from feishu_generation_agent.storage.files import FileStore


class _FakeNativeLayer:
    def __init__(self, *, evidence=None, failure: Exception | None = None) -> None:
        self.evidence = evidence
        self.failure = failure
        self.calls = 0

    async def understand_video(self, asset):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.evidence


class _FakeVideoVisionAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze_video(self, asset, frames):
        self.calls += 1
        return VideoReferenceAnalysis(
            asset_id=asset.asset_id,
            kind=VideoReferenceKind.CHARACTER,
            summary="降级路径：人物形象参考",
            representative_frame_index=2,
            uncertainties=[],
        )


def _document(tmp_path: Path) -> tuple[NormalizedDocument, MediaAsset]:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fake-mp4-bytes")
    media = MediaAsset(
        asset_id="video-1",
        source_block_id="video-file",
        origin="feishu_video",
        file_token="file-token",
        local_path=video,
        mime_type="video/mp4",
        size=video.stat().st_size,
        sha256="video-sha",
    )
    document = NormalizedDocument(
        document_id="doc-video",
        title="参考视频测试",
        revision=3,
        source_type=SourceType.DOCX,
        source_token="doc-video",
        blocks=[
            DocumentBlock(
                block_id="video-file",
                parent_id="page",
                block_type="file",
                order=0,
                path=["page", "video-file"],
                text="",
            )
        ],
        text_view="[video:video-1]",
        media_assets=[media],
    )
    return document, media


def _evidence() -> VideoEvidence:
    return VideoEvidence(
        asset_id="video-1",
        engine_id="gemini_native",
        schema_version="native_v1",
        duration=18.08,
        shots=[
            VideoShot(start=0.0, end=2.8, shot_size="中景", action="哭喊", camera="固定"),
            VideoShot(start=15.5, end=18.08, shot_size="特写", action="递出手机", camera="缓慢推近"),
        ],
        transcript=[TranscriptLine(t=11.2, text="Oh, Grandma?")],
        audio=["背景音乐"],
        on_screen_text=[],
        representative_timestamp=15.5,
        summary="女子哭丧后棺盖被打开，最后递出手机",
        kind=VideoReferenceKind.SCENE_STYLE,
        uncertainties=[],
    )


def _services(tmp_path: Path, **extra):
    return SimpleNamespace(
        file_store=FileStore(
            tmp_path / "data", tmp_path / "outputs", max_bytes=10 * 1024 * 1024
        ),
        vision_analyzer=_FakeVideoVisionAnalyzer(),
        **extra,
    )


async def test_native_path_extracts_frame_at_evidence_timestamp(
    tmp_path, monkeypatch
):
    recorded: dict = {}

    def fake_extract(video_path, timestamp, output_path):
        recorded["timestamp"] = timestamp
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"jpeg-bytes")
        return output_path

    monkeypatch.setattr(nodes, "extract_video_frame_at", fake_extract)

    document, _ = _document(tmp_path)
    layer = _FakeNativeLayer(evidence=_evidence())
    services = _services(tmp_path, native_understanding=layer)

    updated = await _materialize_video_references(document, services)

    # 关键：抽帧时间点来自证据，不是"取中间帧"
    assert recorded["timestamp"] == pytest.approx(15.5)
    assert layer.calls == 1
    assert services.vision_analyzer.calls == 0  # 原生成功就不该再调 Claude

    assert updated.media_assets[0].asset_id == "video-1-frame"
    assert updated.media_assets[0].mime_type.startswith("image/")
    assert "[image:video-1-frame]" in updated.text_view
    assert "[video:video-1]" not in updated.text_view

    # video_semantics 由证据派生，既有 planner 规则继续可用
    assert len(updated.video_semantics) == 1
    assert updated.video_semantics[0].kind == VideoReferenceKind.SCENE_STYLE
    assert "递出手机" in updated.video_semantics[0].summary

    # 结构化证据保留给 planner
    assert len(updated.video_evidence) == 1
    assert updated.video_evidence[0].engine_id == "gemini_native"
    assert [shot.camera for shot in updated.video_evidence[0].shots] == ["固定", "缓慢推近"]


async def test_native_unavailable_falls_back_to_frame_extraction(
    tmp_path, monkeypatch
):
    def fake_extract_frames(video_path, frame_count, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(frame_count):
            path = output_dir / f"frame-{index + 1:02d}.jpg"
            path.write_bytes(b"jpeg-bytes")
            paths.append(path)
        return paths

    monkeypatch.setattr(nodes, "extract_video_frames", fake_extract_frames)

    document, _ = _document(tmp_path)
    layer = _FakeNativeLayer(
        failure=NativeUnavailable("engine_failed", "500")
    )
    services = _services(tmp_path, native_understanding=layer)

    updated = await _materialize_video_references(document, services)

    assert layer.calls == 1
    assert services.vision_analyzer.calls == 1  # 回落到既有抽帧 + Claude
    assert updated.media_assets[0].asset_id == "video-1-frame"
    assert updated.video_semantics[0].kind == VideoReferenceKind.CHARACTER
    # 留痕红线：降级的视频不得伪装成原生证据
    assert updated.video_evidence == []


async def test_native_unexpected_exception_also_falls_back(tmp_path, monkeypatch):
    def fake_extract_frames(video_path, frame_count, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(frame_count):
            path = output_dir / f"frame-{index + 1:02d}.jpg"
            path.write_bytes(b"jpeg-bytes")
            paths.append(path)
        return paths

    monkeypatch.setattr(nodes, "extract_video_frames", fake_extract_frames)

    document, _ = _document(tmp_path)
    layer = _FakeNativeLayer(failure=RuntimeError("unexpected"))
    services = _services(tmp_path, native_understanding=layer)

    updated = await _materialize_video_references(document, services)
    assert services.vision_analyzer.calls == 1
    assert updated.video_evidence == []


async def test_services_without_native_attribute_keeps_legacy_behaviour(
    tmp_path, monkeypatch
):
    def fake_extract_frames(video_path, frame_count, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(frame_count):
            path = output_dir / f"frame-{index + 1:02d}.jpg"
            path.write_bytes(b"jpeg-bytes")
            paths.append(path)
        return paths

    monkeypatch.setattr(nodes, "extract_video_frames", fake_extract_frames)

    document, _ = _document(tmp_path)
    services = _services(tmp_path)  # 没有 native_understanding 属性

    updated = await _materialize_video_references(document, services)

    assert services.vision_analyzer.calls == 1
    assert updated.media_assets[0].asset_id == "video-1-frame"
    assert updated.video_evidence == []
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_video_materialize.py -v
```
Expected: FAIL — `AttributeError: module 'feishu_generation_agent.graph.nodes' has no attribute 'extract_video_frame_at'`

- [ ] **Step 3: Write minimal implementation**

`<agent>/src/feishu_generation_agent/graph/nodes.py` 的 import 区，先把 `domain.document` 的导入块（第 30-41 行那一组）补上 `VideoEvidence`：

```python
from feishu_generation_agent.domain.document import (
    MediaAsset,
    NormalizedDocument,
    PlanningPromptSnapshot,
    RequirementRequest,
    VideoEvidence,
    VideoReferenceAnalysis,
    VideoReferenceKind,
    VisionDescription,
    IngestIssueSeverity,
    build_planning_prompt_snapshot,
    resolve_ingest_issue_records,
)
```

再把

```python
from feishu_generation_agent.integrations.video_reference import (
    ExtractedVideoFrame,
    extract_video_frames,
)
```

改为

```python
from feishu_generation_agent.integrations.video_reference import (
    ExtractedVideoFrame,
    extract_video_frame_at,
    extract_video_frames,
)
from feishu_generation_agent.integrations.native_understanding.layer import (
    NativeUnavailable,
)
```

在 `_analyze_video_reference` 之后、`_materialize_video_references` 之前插入新函数：

```python
async def _materialize_native_frame(
    document: NormalizedDocument,
    services: GraphServices,
    video: MediaAsset,
    evidence: VideoEvidence,
) -> tuple[MediaAsset | None, VideoReferenceAnalysis | None]:
    """按原生证据给出的代表帧时间点抽一帧，落成可被 Seedance 消费的图片资产。

    仍然替换视频本体，因为火山 Bearer 模式下 Seedance 吃不了 MP4 ——
    那是生成侧约束，与理解无关。区别只在抽帧时间点由证据决定。
    """
    try:
        with tempfile.TemporaryDirectory(
            prefix="feishu-native-frame-"
        ) as work_dir:
            output_path = Path(work_dir) / "frame.jpg"
            await asyncio.to_thread(
                extract_video_frame_at,
                video.local_path,
                evidence.representative_timestamp,
                output_path,
            )
            frame_content = output_path.read_bytes()
            stored = services.file_store.save_input(
                document.document_id,
                f"{video.asset_id}-frame.jpg",
                frame_content,
            )
            frame_asset = MediaAsset(
                asset_id=f"{video.asset_id}-frame",
                source_block_id=video.source_block_id,
                origin="feishu_video_frame",
                file_token=None,
                local_path=stored.local_path,
                mime_type=stored.mime_type,
                size=stored.size,
                sha256=stored.sha256,
                width=stored.width,
                height=stored.height,
            )
            insight = VideoReferenceAnalysis(
                asset_id=frame_asset.asset_id,
                kind=evidence.kind,
                summary=evidence.summary,
                representative_frame_index=1,
                uncertainties=list(evidence.uncertainties),
            )
            return frame_asset, insight
    except Exception:
        _LOGGER.warning(
            "原生证据抽帧失败，保留原始视频素材 video=%s",
            video.asset_id,
            exc_info=True,
        )
        return None, None
```

把 `_materialize_video_references` 整体替换为：

```python
async def _materialize_video_references(
    document: NormalizedDocument,
    services: GraphServices,
) -> NormalizedDocument:
    video_assets = [
        asset
        for asset in document.media_assets
        if asset.mime_type.startswith("video/")
    ]
    if not video_assets:
        return document

    native_layer = getattr(services, "native_understanding", None)
    replacements: dict[str, MediaAsset] = {}
    semantics: list[VideoReferenceAnalysis] = list(document.video_semantics)
    evidence_items: list[VideoEvidence] = list(document.video_evidence)

    for video in video_assets:
        evidence: VideoEvidence | None = None
        if native_layer is not None:
            try:
                evidence = await native_layer.understand_video(video)
            except NativeUnavailable as exc:
                _LOGGER.warning(
                    "原生视频理解不可用，降级抽帧 video=%s reason=%s detail=%s",
                    video.asset_id,
                    exc.reason,
                    exc.detail,
                )
            except Exception:
                _LOGGER.warning(
                    "原生视频理解异常，降级抽帧 video=%s",
                    video.asset_id,
                    exc_info=True,
                )

        if evidence is not None:
            frame_asset, insight = await _materialize_native_frame(
                document, services, video, evidence
            )
            if frame_asset is not None:
                evidence_items.append(evidence)
        else:
            frame_asset, insight = await _analyze_video_reference(
                services,
                document.document_id,
                video,
            )

        if frame_asset is not None:
            replacements[video.asset_id] = frame_asset
        if insight is not None:
            semantics.append(insight)

    if not replacements:
        return document.model_copy(
            update={
                "video_semantics": semantics,
                "video_evidence": evidence_items,
            }
        )

    media_assets: list[MediaAsset] = []
    text_view = document.text_view
    for asset in document.media_assets:
        replacement = replacements.get(asset.asset_id)
        if replacement is None:
            media_assets.append(asset)
            continue
        media_assets.append(replacement)
        text_view = text_view.replace(
            f"[video:{asset.asset_id}]",
            f"[image:{replacement.asset_id}]",
        )

    for video in video_assets:
        replacement = replacements.get(video.asset_id)
        if replacement is None:
            continue
        marker = f"[image:{replacement.asset_id}]"
        if marker not in text_view:
            text_view = f"{text_view}\n{marker}"

    return document.model_copy(
        update={
            "media_assets": media_assets,
            "text_view": text_view,
            "video_semantics": semantics,
            "video_evidence": evidence_items,
        }
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_video_materialize.py tests/unit/test_video_reference.py -v
```
Expected: PASS（4 + 2 passed）。**注意 `test_video_reference.py` 的存量断言必须原样通过、不得修改。**

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/graph/nodes.py tests/unit/test_native_video_materialize.py
git commit --only src/feishu_generation_agent/graph/nodes.py tests/unit/test_native_video_materialize.py -m "feat(native): 参考视频原生优先、抽帧兜底"
```

---

### Task 4: `analyze_images` 把 `video_evidence` 带进 state

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/graph/nodes.py`（`analyze_images` 的两个 return 分支）
- Test: `<agent>/tests/unit/test_native_analyze_images_state.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `document.video_evidence`
- Produces: `AgentState["video_evidence"]` 在 `analyze_images` 之后可用（**两个 return 分支都要带**，包括 `vision_analyzer is None` 的早退分支）

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_analyze_images_state.py`：

```python
import json

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    SourceType,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
)
from feishu_generation_agent.graph import nodes


class _FakeLayer:
    def __init__(self, evidence: VideoEvidence) -> None:
        self.evidence = evidence

    async def understand_video(self, asset):
        return self.evidence


def _document_with_video_evidence() -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc-1",
        title="t",
        revision=1,
        source_type=SourceType.DOCX,
        source_token="doc-1",
        blocks=[],
        text_view="no video here",
        media_assets=[],
        video_evidence=[
            VideoEvidence(
                asset_id="video-1",
                engine_id="gemini_native",
                schema_version="native_v1",
                duration=5.0,
                shots=[VideoShot(start=0.0, end=5.0, action="走", camera="推近")],
                summary="s",
                kind=VideoReferenceKind.CAMERA_MOVEMENT,
            )
        ],
    )


async def _run(monkeypatch, tmp_path, *, vision_analyzer):
    document = _document_with_video_evidence()
    captured: dict = {}

    async def fake_materialize(doc, services):
        return doc

    async def fake_planning_mode(run_id, services, state):
        return "video"

    monkeypatch.setattr(nodes, "_materialize_video_references", fake_materialize)
    monkeypatch.setattr(nodes, "_planning_mode_for_run", fake_planning_mode)

    async def fake_run_node(state, name, services, operation):
        captured["result"] = await operation()
        return captured["result"]

    monkeypatch.setattr(nodes, "_run_node", fake_run_node)

    services = type("S", (), {"vision_analyzer": vision_analyzer})()
    state = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "normalized_document": document.model_dump(mode="json"),
        "vision_descriptions": [],
        "vision_issues": [],
    }
    # config 必须带上与 state["thread_id"] 一致的 thread_id，
    # 否则 _ensure_thread_id 会抛 "The workflow thread is invalid"
    # （它读的是 config["configurable"]["thread_id"]）。
    await nodes.analyze_images(
        state, {"configurable": {"thread_id": "thread-1"}}, services=services
    )
    return captured["result"]


async def test_video_evidence_reaches_state_even_without_vision_analyzer(
    monkeypatch, tmp_path
):
    result = await _run(monkeypatch, tmp_path, vision_analyzer=None)
    assert len(result["video_evidence"]) == 1
    assert result["video_evidence"][0]["engine_id"] == "gemini_native"
    assert json.dumps(result["video_evidence"][0], ensure_ascii=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_analyze_images_state.py -v
```
Expected: FAIL — `KeyError: 'video_evidence'`

- [ ] **Step 3: Write minimal implementation**

在 `<agent>/src/feishu_generation_agent/graph/nodes.py` 的 `analyze_images` 中，把 `vision_analyzer is None` 的早退分支改为：

```python
        if services.vision_analyzer is None:
            return {
                "vision_descriptions": [],
                "vision_issues": [],
                "video_evidence": [
                    _json_model(item) for item in document.video_evidence
                ],
                "normalized_document": document_json,
                "media_assets": document_json["media_assets"],
            }
```

把函数末尾的成功分支改为：

```python
        return {
            "vision_descriptions": [
                _json_model(description) for description in descriptions
            ],
            "vision_issues": issues,
            "video_evidence": [
                _json_model(item) for item in document.video_evidence
            ],
            "normalized_document": document_json,
            "media_assets": document_json["media_assets"],
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_analyze_images_state.py -v
```
Expected: PASS

- [ ] **Step 5: 回归：graph 测试不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/graph -v
```
Expected: PASS（全绿）

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/graph/nodes.py tests/unit/test_native_analyze_images_state.py
git commit --only src/feishu_generation_agent/graph/nodes.py tests/unit/test_native_analyze_images_state.py -m "feat(native): analyze_images 透传 video_evidence"
```

---

### Task 5: 本计划总验收

- [ ] **Step 1: 跑本计划全部新增测试**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_video_frame_at.py tests/unit/test_native_services_field.py tests/unit/test_native_video_materialize.py tests/unit/test_native_analyze_images_state.py -v
```
Expected: PASS（全部通过）

- [ ] **Step 2: 确认存量抽帧行为未被改写**

Run:
```bash
git diff -- tests/unit/test_video_reference.py
```
Expected: 只有 skipif 条件与 import 变化，**断言部分零改动**

- [ ] **Step 3: 确认 `native_understanding` 为 None 时链路与改造前等价**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_video_reference.py tests/graph -v
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
| §4.4 理解侧与生成侧解耦 | Task 3（原生吃 MP4，生成侧仍抽帧） |
| §4.4 抽帧时间点改用 `representative_timestamp` | Task 1（`extract_video_frame_at`）、Task 3 |
| §5.2 `video_semantics` 保留并由证据派生 | Task 3（`kind` / `summary` 派生） |
| §5.3 `domain` / `graph` 接入点 | Task 2、Task 3、Task 4 |
| §7.1 降级：任意失败 → 抽帧 | Task 3（三条兜底测试） |
| §6.2 留痕红线：降级不伪装成原生证据 | Task 3 `test_native_unavailable_falls_back_to_frame_extraction` |
| §8.1 测试 8（`video_evidence` 为空时行为与现状一致） | Task 3 `test_services_without_native_attribute_keeps_legacy_behaviour` |

**未覆盖（Plan 04）**：planner prompt 新增视频证据段、`bootstrap.py` 组装 layer、付费冒烟脚本、端到端验收。

**Placeholder 扫描**：无 TBD / TODO / 未给出代码的步骤。

**类型一致性**：`evidence.representative_timestamp` 在 Plan 01 定义、Task 3 消费；`NativeUnavailable.reason` / `.detail` 在 Plan 02 定义、Task 3 按 `.reason` / `.detail` 记录；`extract_video_frame_at(video_path, timestamp, output_path) -> Path` 在 Task 1 定义、Task 3 按三参数调用；`VideoEvidence` 从 `feishu_generation_agent.domain.document` 导入，与 Plan 01 一致。
