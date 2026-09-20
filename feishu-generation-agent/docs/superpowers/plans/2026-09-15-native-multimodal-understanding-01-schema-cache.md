# 原生多模态理解层 — Plan 01：证据 schema、确定性校验与缓存迁移

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地原生理解层的纯数据基础 —— `VideoEvidence` 证据模型、确定性校验（含片尾容差夹紧）、`vision_cache` 表的三列扩展与 `frame_v1` 幂等回填标记。本计划不接任何网络调用、不改 graph 行为。

**Architecture:** 证据模型放 `domain/document.py`（因为 `NormalizedDocument` 要持有它，且 domain 不得反向依赖 integrations）；引擎常量、cache key 构造、确定性校验放新增的 `integrations/native_understanding/` 包。缓存迁移沿用仓库既有的 `PRAGMA table_info` + `ALTER TABLE` 模式（见 `Repository._migrate_operations`），并用 `PRAGMA user_version` 把回填限定为**只执行一次**。

**Tech Stack:** Python 3.12、pydantic v2、aiosqlite、pytest（`asyncio_mode = "auto"`）

**Spec:** `docs/superpowers/specs/2026-09-15-native-multimodal-understanding-design.md`

## Global Constraints

- 工作目录：`C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent`（下称 `<agent>`）；解释器 `<agent>\.venv\Scripts\python.exe`
- **仓库有 111 项既有未提交改动。禁止 `git checkout` / `git reset` / `git restore` / `git clean`。**
- **禁止裸 `git commit`**：index 里已有 65 个用户 staged 文件。一律用 `git commit --only <paths>` 或先 `git status` 确认。
- 不触碰统计功能实现（CLAUDE.md 核心要求）
- `VisionDescription` / `VideoReferenceAnalysis` 字段**一个都不改**（spec D5）
- 尾部容差 `TAIL_TOLERANCE_SECONDS = 1.0`；时长容差 `DURATION_TOLERANCE_SECONDS = 0.5`
- cache key 格式固定：`{engine_id}:{kind}:{model_name}:{schema_version}:{sha256}`
- 旧缓存行**一行都不删**，只打标记

---

### Task 1: `VideoEvidence` 证据模型

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/domain/document.py`（在 `VideoReferenceAnalysis` 之后、`NormalizedDocument` 之前插入）
- Modify: `<agent>/src/feishu_generation_agent/domain/document.py`（`NormalizedDocument` 增加 `video_evidence` 字段）
- Test: `<agent>/tests/unit/test_native_evidence_schema.py`（新建）

**Interfaces:**
- Consumes: 无（本计划起点）
- Produces:
  - `VideoShot(start: float, end: float, shot_size: str = "", action: str = "", camera: str = "")`
  - `TranscriptLine(t: float, text: str)`
  - `VideoEvidence(asset_id, engine_id, schema_version, duration, shots, transcript, audio, on_screen_text, representative_timestamp, summary, kind, uncertainties)`
  - `NormalizedDocument.video_evidence: list[VideoEvidence]`（默认 `[]`）

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_evidence_schema.py`：

```python
import json

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    SourceType,
    TranscriptLine,
    VideoEvidence,
    VideoReferenceKind,
    VideoShot,
)


def _evidence() -> VideoEvidence:
    return VideoEvidence(
        asset_id="asset-video-1",
        engine_id="gemini_native",
        schema_version="native_v1",
        duration=18.08,
        shots=[
            VideoShot(
                start=0.0,
                end=2.8,
                shot_size="中景",
                action="女子跪在棺材边哭喊",
                camera="固定",
            )
        ],
        transcript=[TranscriptLine(t=11.2, text="Oh, Grandma?")],
        audio=["背景音乐", "环境音"],
        on_screen_text=[],
        representative_timestamp=2.8,
        summary="女子哭丧后棺盖被打开",
        kind=VideoReferenceKind.SCENE_STYLE,
        uncertainties=[],
    )


def test_video_evidence_round_trips_json():
    payload = _evidence().model_dump(mode="json")
    restored = VideoEvidence.model_validate(payload)
    assert restored == _evidence()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["duration"] == 18.08


def test_video_evidence_defaults_to_empty_on_normalized_document():
    document = NormalizedDocument(
        document_id="doc-1",
        title="t",
        revision=1,
        source_type=SourceType.DOCX,
        source_token="doc-1",
        blocks=[],
        text_view="",
        media_assets=[],
    )
    assert document.video_evidence == []
```

- [ ] **Step 2: Run test to verify it fails**

Run（workdir `<agent>`）:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_evidence_schema.py -v
```
Expected: FAIL — `ImportError: cannot import name 'TranscriptLine' from 'feishu_generation_agent.domain.document'`

- [ ] **Step 3: Write minimal implementation**

在 `<agent>/src/feishu_generation_agent/domain/document.py` 的 `VideoReferenceAnalysis` 定义之后插入：

```python
class VideoShot(BaseModel):
    """原生视频理解产出的单个镜头。"""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    shot_size: str = ""
    action: str = ""
    camera: str = ""


class TranscriptLine(BaseModel):
    """台词 / 歌词，带出现时间点（秒）。"""

    t: float = Field(ge=0)
    text: str


class VideoEvidence(BaseModel):
    """原生多模态引擎对参考视频的带时间点结构化证据。

    engine_id / schema_version 由构造方（原生引擎或抽帧降级）显式写入，
    缓存 key 与审核留痕都依赖它们区分 native_v1 与 frame_v1。
    """

    asset_id: str
    engine_id: str
    schema_version: str
    duration: float
    shots: list[VideoShot] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)
    audio: list[str] = Field(default_factory=list)
    on_screen_text: list[str] = Field(default_factory=list)
    representative_timestamp: float = 0.0
    summary: str = ""
    kind: VideoReferenceKind = VideoReferenceKind.OTHER
    uncertainties: list[str] = Field(default_factory=list)
```

在同一文件的 `NormalizedDocument` 中，`video_semantics` 字段之后追加：

```python
    video_evidence: list[VideoEvidence] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_evidence_schema.py -v
```
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/domain/document.py tests/unit/test_native_evidence_schema.py
git status --porcelain -- src/feishu_generation_agent/domain/document.py tests/unit/test_native_evidence_schema.py
git commit --only src/feishu_generation_agent/domain/document.py tests/unit/test_native_evidence_schema.py -m "feat(native): 新增 VideoEvidence 证据模型"
```

---

### Task 2: 引擎常量与 cache key 构造

**Files:**
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/__init__.py`
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/schemas.py`
- Test: `<agent>/tests/unit/test_native_cache_key.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `VideoEvidence`
- Produces:
  - `ENGINE_ID_GEMINI_NATIVE = "gemini_native"`
  - `ENGINE_ID_FRAME_V1 = "frame_v1"`
  - `SCHEMA_VERSION_NATIVE_V1 = "native_v1"`
  - `SCHEMA_VERSION_FRAME_V1 = "frame_v1"`
  - `KIND_IMAGE = "image"` / `KIND_VIDEO = "video"`
  - `build_cache_key(*, engine_id: str, kind: str, model_name: str, schema_version: str, sha256: str) -> str`

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_cache_key.py`：

```python
from feishu_generation_agent.integrations.native_understanding.schemas import (
    ENGINE_ID_FRAME_V1,
    ENGINE_ID_GEMINI_NATIVE,
    KIND_IMAGE,
    KIND_VIDEO,
    SCHEMA_VERSION_FRAME_V1,
    SCHEMA_VERSION_NATIVE_V1,
    build_cache_key,
)


def test_cache_key_carries_engine_model_schema_and_kind():
    key = build_cache_key(
        engine_id=ENGINE_ID_GEMINI_NATIVE,
        kind=KIND_VIDEO,
        model_name="gemini-2.5-flash",
        schema_version=SCHEMA_VERSION_NATIVE_V1,
        sha256="a" * 64,
    )
    assert key == f"gemini_native:video:gemini-2.5-flash:native_v1:{'a' * 64}"


def test_legacy_and_native_keys_never_collide():
    common = dict(model_name="m", sha256="b" * 64)
    legacy = build_cache_key(
        engine_id=ENGINE_ID_FRAME_V1,
        kind=KIND_IMAGE,
        schema_version=SCHEMA_VERSION_FRAME_V1,
        **common,
    )
    native = build_cache_key(
        engine_id=ENGINE_ID_GEMINI_NATIVE,
        kind=KIND_IMAGE,
        schema_version=SCHEMA_VERSION_NATIVE_V1,
        **common,
    )
    assert legacy != native
    assert legacy.startswith("frame_v1:image:")
    assert native.startswith("gemini_native:image:")


def test_cache_key_rejects_empty_segment():
    import pytest

    with pytest.raises(ValueError, match="model_name"):
        build_cache_key(
            engine_id=ENGINE_ID_GEMINI_NATIVE,
            kind=KIND_VIDEO,
            model_name="",
            schema_version=SCHEMA_VERSION_NATIVE_V1,
            sha256="c" * 64,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_cache_key.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'feishu_generation_agent.integrations.native_understanding'`

- [ ] **Step 3: Write minimal implementation**

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/__init__.py`（空文件）。

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/schemas.py`：

```python
"""原生理解层的引擎常量与缓存 key 契约。

cache key 故意把 engine_id / kind / model_name / schema_version 全部编进去：
旧 Claude 时代的 key（sha256:model:prompt_version）因此永远命中不到，
等于让存量缓存自然失效，同时一行都不用删。
"""

ENGINE_ID_GEMINI_NATIVE = "gemini_native"
ENGINE_ID_FRAME_V1 = "frame_v1"

SCHEMA_VERSION_NATIVE_V1 = "native_v1"
SCHEMA_VERSION_FRAME_V1 = "frame_v1"

KIND_IMAGE = "image"
KIND_VIDEO = "video"

_SEGMENTS = ("engine_id", "kind", "model_name", "schema_version", "sha256")


def build_cache_key(
    *,
    engine_id: str,
    kind: str,
    model_name: str,
    schema_version: str,
    sha256: str,
) -> str:
    values = {
        "engine_id": engine_id,
        "kind": kind,
        "model_name": model_name,
        "schema_version": schema_version,
        "sha256": sha256,
    }
    for name in _SEGMENTS:
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"cache key segment {name} must be a non-empty string")
        if ":" in value:
            raise ValueError(f"cache key segment {name} must not contain ':'")
    return ":".join(values[name] for name in _SEGMENTS)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_cache_key.py -v
```
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/native_understanding/
git commit --only src/feishu_generation_agent/integrations/native_understanding/ tests/unit/test_native_cache_key.py -m "feat(native): 引擎常量与 cache key 契约"
```

---

### Task 3: 确定性校验（含片尾容差夹紧）

**Files:**
- Create: `<agent>/src/feishu_generation_agent/integrations/native_understanding/validation.py`
- Test: `<agent>/tests/unit/test_native_evidence_validation.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `VideoEvidence` / `VideoShot` / `TranscriptLine`
- Produces:
  - `EvidenceValidationError(RuntimeError)`，带 `.issues: list[str]`
  - `TAIL_TOLERANCE_SECONDS = 1.0`、`DURATION_TOLERANCE_SECONDS = 0.5`
  - `validate_video_evidence(evidence: VideoEvidence, *, ffprobe_duration: float) -> tuple[VideoEvidence, list[str]]`

**行为规格（必须逐条实现）：**
1. `abs(evidence.duration - ffprobe_duration) > 0.5` → 抛 `EvidenceValidationError`
2. 剔除 `start > end` 或时间非有限（NaN/Inf）的 shot；剔除动作记进 issues
3. shot 的 `end` 超出 `ffprobe_duration`：溢出 ≤ 1.0s → **夹紧到 `ffprobe_duration`**（记 issues 但不抛）；溢出 > 1.0s → 剔除该 shot 并记 issues
4. 剔除后 `shots` 为空 → 抛 `EvidenceValidationError`
5. transcript 的 `t` 超出 `ffprobe_duration + 1.0` → 剔除并记 issues；≤ 容差内溢出 → 夹紧
6. `representative_timestamp` 不落在任何 shot 内 → 夹到最近 shot 的合法边界；若 `shots` 非空则结果必须落在某个 shot 内

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_native_evidence_validation.py`：

```python
import math

import pytest

from feishu_generation_agent.domain.document import (
    TranscriptLine,
    VideoEvidence,
    VideoShot,
)
from feishu_generation_agent.integrations.native_understanding.validation import (
    EvidenceValidationError,
    TAIL_TOLERANCE_SECONDS,
    validate_video_evidence,
)


def _evidence(
    *,
    duration: float = 18.08,
    shots: list[VideoShot] | None = None,
    transcript: list[TranscriptLine] | None = None,
    representative_timestamp: float = 2.8,
) -> VideoEvidence:
    return VideoEvidence(
        asset_id="asset-video-1",
        engine_id="gemini_native",
        schema_version="native_v1",
        duration=duration,
        shots=shots
        if shots is not None
        else [
            VideoShot(start=0.0, end=2.8, shot_size="中景", action="哭喊", camera="固定"),
            VideoShot(start=15.5, end=18.5, shot_size="特写", action="递出手机", camera="固定"),
        ],
        transcript=transcript
        if transcript is not None
        else [TranscriptLine(t=11.2, text="Oh, Grandma?")],
        audio=["背景音乐"],
        on_screen_text=[],
        representative_timestamp=representative_timestamp,
        summary="哭丧后棺盖被打开",
        uncertainties=[],
    )


def test_real_18s_output_tail_overshoot_is_clamped_not_rejected():
    """实测回归：真实成功输出末镜头 end=18.5，视频实际 18.08s。

    这条证据完全正确，绝不能被判不合格。
    """
    evidence, issues = validate_video_evidence(_evidence(), ffprobe_duration=18.08)
    last = evidence.shots[-1]
    assert last.end == pytest.approx(18.08)
    assert last.start == pytest.approx(15.5)
    assert any("夹紧" in issue for issue in issues)


def test_tail_overshoot_beyond_tolerance_drops_shot():
    evidence, issues = validate_video_evidence(
        _evidence(
            shots=[
                VideoShot(start=0.0, end=2.8, action="哭喊"),
                VideoShot(start=15.5, end=25.0, action="越界镜头"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [shot.action for shot in evidence.shots] == ["哭喊"]
    assert any("剔除" in issue for issue in issues)


def test_duration_mismatch_raises():
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_video_evidence(_evidence(duration=18.08), ffprobe_duration=30.0)
    assert any("duration" in issue for issue in excinfo.value.issues)


def test_all_shots_dropped_raises():
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_video_evidence(
            _evidence(shots=[VideoShot(start=5.0, end=1.0, action="时间倒挂")]),
            ffprobe_duration=18.08,
        )
    assert any("shots" in issue for issue in excinfo.value.issues)


def test_non_finite_timestamps_are_dropped():
    evidence, issues = validate_video_evidence(
        _evidence(
            shots=[
                VideoShot(start=0.0, end=2.8, action="正常"),
                VideoShot(start=math.nan, end=5.0, action="NaN"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [shot.action for shot in evidence.shots] == ["正常"]
    assert issues


def test_representative_timestamp_is_clamped_into_a_shot():
    evidence, _ = validate_video_evidence(
        _evidence(representative_timestamp=99.0),
        ffprobe_duration=18.08,
    )
    assert any(
        shot.start <= evidence.representative_timestamp <= shot.end
        for shot in evidence.shots
    )


def test_representative_timestamp_inside_shot_is_untouched():
    evidence, _ = validate_video_evidence(
        _evidence(representative_timestamp=1.5),
        ffprobe_duration=18.08,
    )
    assert evidence.representative_timestamp == pytest.approx(1.5)


def test_transcript_beyond_tolerance_is_dropped():
    evidence, issues = validate_video_evidence(
        _evidence(
            transcript=[
                TranscriptLine(t=11.2, text="保留"),
                TranscriptLine(t=99.0, text="越界"),
            ]
        ),
        ffprobe_duration=18.08,
    )
    assert [line.text for line in evidence.transcript] == ["保留"]
    assert issues


def test_tail_tolerance_constant_is_one_second():
    assert TAIL_TOLERANCE_SECONDS == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_evidence_validation.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'feishu_generation_agent.integrations.native_understanding.validation'`

- [ ] **Step 3: Write minimal implementation**

新建 `<agent>/src/feishu_generation_agent/integrations/native_understanding/validation.py`：

```python
"""证据确定性校验：不调用任何模型。

存在的理由是一次实测教训：真实成功的视频理解输出末镜头 end=18.5，
而视频实际时长 18.08s。模型对片尾的时间估算天然有零点几秒溢出，
若按严格 [0, duration] 校验，会把完全正确的证据判为不合格并降级。
因此引入尾部容差：容差内夹紧，容差外才判定时间轴不可信。
"""

import math

from feishu_generation_agent.domain.document import (
    TranscriptLine,
    VideoEvidence,
    VideoShot,
)

TAIL_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_SECONDS = 0.5


class EvidenceValidationError(RuntimeError):
    """证据时间轴不可信，必须降级到抽帧。"""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = list(issues)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_video_evidence(
    evidence: VideoEvidence,
    *,
    ffprobe_duration: float,
) -> tuple[VideoEvidence, list[str]]:
    issues: list[str] = []

    if not _finite(ffprobe_duration) or ffprobe_duration <= 0:
        raise EvidenceValidationError(["ffprobe duration 非法，无法校验证据时间轴"])

    if abs(evidence.duration - ffprobe_duration) > DURATION_TOLERANCE_SECONDS:
        raise EvidenceValidationError(
            [
                "duration 与 ffprobe 不一致："
                f"evidence={evidence.duration} ffprobe={ffprobe_duration}"
            ]
        )

    kept_shots: list[VideoShot] = []
    for shot in evidence.shots:
        if not _finite(shot.start) or not _finite(shot.end) or shot.start > shot.end:
            issues.append(
                f"剔除时间非法镜头：start={shot.start} end={shot.end} action={shot.action}"
            )
            continue
        if shot.end > ffprobe_duration:
            overshoot = shot.end - ffprobe_duration
            if overshoot > TAIL_TOLERANCE_SECONDS:
                issues.append(
                    f"剔除越界镜头：end={shot.end} 超出时长 {overshoot:.2f}s "
                    f"> 容差 {TAIL_TOLERANCE_SECONDS}s action={shot.action}"
                )
                continue
            shot = shot.model_copy(update={"end": ffprobe_duration})
            issues.append(
                f"片尾溢出 {overshoot:.2f}s 已在容差内夹紧至 {ffprobe_duration}s"
            )
        if shot.start > ffprobe_duration:
            issues.append(f"剔除起点越界镜头：start={shot.start} action={shot.action}")
            continue
        kept_shots.append(shot)

    if not kept_shots:
        raise EvidenceValidationError(
            issues + ["shots 全部被剔除，时间轴不可信，必须降级抽帧"]
        )

    kept_transcript: list[TranscriptLine] = []
    for line in evidence.transcript:
        if not _finite(line.t) or not isinstance(line.text, str):
            issues.append("剔除时间或文本非法的台词记录")
            continue
        if line.t > ffprobe_duration + TAIL_TOLERANCE_SECONDS:
            issues.append(f"剔除越界台词：t={line.t} 超出容差范围")
            continue
        if line.t > ffprobe_duration:
            line = line.model_copy(update={"t": ffprobe_duration})
            issues.append(f"台词时间点 {line.text!r} 夹紧至片尾 {ffprobe_duration}s")
        kept_transcript.append(line)

    timestamp = evidence.representative_timestamp
    if not _finite(timestamp) or not any(
        shot.start <= timestamp <= shot.end for shot in kept_shots
    ):
        nearest = min(
            kept_shots,
            key=lambda shot: min(
                abs(shot.start - (timestamp if _finite(timestamp) else 0.0)),
                abs(shot.end - (timestamp if _finite(timestamp) else 0.0)),
            ),
        )
        clamped = min(
            max(timestamp if _finite(timestamp) else nearest.start, nearest.start),
            nearest.end,
        )
        issues.append(
            f"代表帧时间点 {timestamp} 不在任何镜头内，已夹紧至 {clamped}"
        )
        timestamp = clamped

    validated = evidence.model_copy(
        update={
            "shots": kept_shots,
            "transcript": kept_transcript,
            "representative_timestamp": timestamp,
            "uncertainties": list(evidence.uncertainties) + issues,
        }
    )
    return validated, issues
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_evidence_validation.py -v
```
Expected: PASS（9 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/native_understanding/validation.py tests/unit/test_native_evidence_validation.py
git commit --only src/feishu_generation_agent/integrations/native_understanding/validation.py tests/unit/test_native_evidence_validation.py -m "feat(native): 证据确定性校验与片尾容差夹紧"
```

---

### Task 4: `vision_cache` 三列扩展与 `frame_v1` 幂等回填

**Files:**
- Modify: `<agent>/src/feishu_generation_agent/storage/repository.py`（`open()` 新增迁移调用；新增 `_migrate_vision_cache`；`save_vision_cache` 增可选维度参数）
- Test: `<agent>/tests/unit/test_vision_cache_migration.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `build_cache_key`
- Produces:
  - `Repository._migrate_vision_cache(connection)` → 加 `engine_id` / `schema_version` / `kind` 三列，并在 `PRAGMA user_version < 1` 时把存量行回填为 `frame_v1` / `frame_v1` / `image`，随后置 `user_version = 1`
  - `Repository.save_vision_cache(cache_key, description, *, engine_id=None, schema_version=None, kind=None)`（`description` 类型放宽为 pydantic `BaseModel`）
  - `Repository.list_vision_cache_engine_ids() -> dict[str, int]`（便于断言与运维查看）

**为什么必须用 `user_version` 而不是 `WHERE engine_id IS NULL`**：回填条件若只看 `IS NULL`，每次 `open()` 都会把**新写入但没带 engine_id 的行**误标成 `frame_v1`。用 `user_version` 把回填限定为只跑一次，才是真正幂等。

- [ ] **Step 1: Write the failing test**

新建 `<agent>/tests/unit/test_vision_cache_migration.py`：

```python
import aiosqlite
import pytest

from feishu_generation_agent.domain.document import VisionDescription
from feishu_generation_agent.storage.repository import Repository


def _description(scene: str) -> VisionDescription:
    return VisionDescription(
        asset_id="asset-1",
        subjects=[],
        scene=scene,
        style="未确认",
        composition="未确认",
        characters=[],
        actions=[],
        visible_text=[],
        colors=[],
        probable_role="视觉参考素材",
        uncertainties=[],
    )


async def test_migration_adds_columns_and_marks_legacy_rows(tmp_path):
    db = tmp_path / "business.sqlite3"
    # 先造一个"Claude 时代"的老库：只有三列，塞一行老 key
    connection = await aiosqlite.connect(db)
    await connection.executescript(
        """
        CREATE TABLE vision_cache (
          cache_key TEXT PRIMARY KEY,
          description_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    await connection.execute(
        "INSERT INTO vision_cache (cache_key, description_json, updated_at) "
        "VALUES (?, ?, ?)",
        ("legacy-sha:claude-3-5-sonnet:v1", '{"asset_id":"asset-1","scene":"老行"}', "t0"),
    )
    await connection.commit()
    await connection.close()

    repository = await Repository.open(db)
    try:
        cursor = await repository._connection.execute(
            "SELECT engine_id, schema_version, kind, description_json FROM vision_cache"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert len(rows) == 1
        assert rows[0][0] == "frame_v1"
        assert rows[0][1] == "frame_v1"
        assert rows[0][2] == "image"
        assert "老行" in rows[0][3]
    finally:
        await repository.close()


async def test_backfill_is_idempotent_and_does_not_clobber_new_rows(tmp_path):
    db = tmp_path / "business.sqlite3"
    first = await Repository.open(db)
    await first.save_vision_cache(
        "gemini_native:image:m:native_v1:" + "a" * 64,
        _description("原生新行"),
        engine_id="gemini_native",
        schema_version="native_v1",
        kind="image",
    )
    await first.close()

    # 再开一次：回填不能再跑，新行不能被改成 frame_v1
    second = await Repository.open(db)
    try:
        cursor = await second._connection.execute(
            "SELECT engine_id FROM vision_cache WHERE cache_key LIKE 'gemini_native:%'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == "gemini_native"
        cursor = await second._connection.execute("PRAGMA user_version")
        version = await cursor.fetchone()
        await cursor.close()
        assert int(version[0]) == 1
        counts = await second.list_vision_cache_engine_ids()
        assert counts == {"gemini_native": 1}
    finally:
        await second.close()


async def test_legacy_key_never_resolves(tmp_path):
    repository = await Repository.open(tmp_path / "business.sqlite3")
    try:
        assert await repository.get_vision_cache("old-sha:model:v1") is None
    finally:
        await repository.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_vision_cache_migration.py -v
```
Expected: FAIL — `AttributeError: 'Repository' object has no attribute 'list_vision_cache_engine_ids'`（第一个测试还会因 `engine_id` 列为 `None` 而断言失败）

- [ ] **Step 3: Write minimal implementation**

在 `<agent>/src/feishu_generation_agent/storage/repository.py` 的 `open()` 中，`_migrate_operations` 之后加一行：

```python
            await cls._migrate_vision_cache(connection)
```

在 `_migrate_operations` 之后新增（照抄既有 `PRAGMA table_info` 模式）：

```python
    @staticmethod
    async def _migrate_vision_cache(connection: aiosqlite.Connection) -> None:
        """给 vision_cache 加引擎维度，并把 Claude 时代的存量行标记为 frame_v1。

        回填用 user_version 门控：只看 `engine_id IS NULL` 会把每次新写入、
        未显式带 engine_id 的行也误标成 frame_v1，那样就不幂等了。
        """
        cursor = await connection.execute("PRAGMA table_info(vision_cache)")
        rows = await cursor.fetchall()
        await cursor.close()
        columns = {str(row[1]) for row in rows}
        for name in ("engine_id", "schema_version", "kind"):
            if name not in columns:
                await connection.execute(
                    f"ALTER TABLE vision_cache ADD COLUMN {name} TEXT"
                )

        cursor = await connection.execute("PRAGMA user_version")
        version_row = await cursor.fetchone()
        await cursor.close()
        version = int(version_row[0]) if version_row is not None else 0
        if version < 1:
            await connection.execute(
                """
                UPDATE vision_cache
                   SET engine_id = 'frame_v1',
                       schema_version = 'frame_v1',
                       kind = 'image'
                 WHERE engine_id IS NULL
                """
            )
            await connection.execute("PRAGMA user_version = 1")
```

把 `save_vision_cache` 替换为：

```python
    async def save_vision_cache(
        self,
        cache_key: str,
        description: BaseModel,
        *,
        engine_id: str | None = None,
        schema_version: str | None = None,
        kind: str | None = None,
    ) -> None:
        description_json = json.dumps(
            description.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._write(
            """
            INSERT INTO vision_cache (
              cache_key, description_json, updated_at,
              engine_id, schema_version, kind
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              description_json = excluded.description_json,
              updated_at = excluded.updated_at,
              engine_id = excluded.engine_id,
              schema_version = excluded.schema_version,
              kind = excluded.kind
            """,
            (
                cache_key,
                description_json,
                _now(),
                engine_id,
                schema_version,
                kind,
            ),
        )

    async def list_vision_cache_engine_ids(self) -> dict[str, int]:
        cursor = await self._connection.execute(
            """
            SELECT COALESCE(engine_id, '<null>') AS engine_id, COUNT(*)
            FROM vision_cache
            GROUP BY engine_id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row[0]): int(row[1]) for row in rows}
```

在文件顶部 import 区补上 `from pydantic import BaseModel`（若已存在则跳过）。校验一下现有 import：

```bash
.venv\Scripts\python.exe -c "import ast,sys; src=open('src/feishu_generation_agent/storage/repository.py',encoding='utf-8').read(); print('BaseModel' in src)"
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_vision_cache_migration.py -v
```
Expected: PASS（3 passed）

- [ ] **Step 5: 回归：存量缓存相关测试不得被打破**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_storage.py tests/unit/test_vision.py -v
```
Expected: PASS（全绿；若有失败，先修到全绿再进入下一步）

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/storage/repository.py tests/unit/test_vision_cache_migration.py
git commit --only src/feishu_generation_agent/storage/repository.py tests/unit/test_vision_cache_migration.py -m "feat(native): vision_cache 引擎维度迁移与 frame_v1 幂等回填"
```

---

### Task 5: 本计划总验收

**Files:** 无新增；只跑测试

- [ ] **Step 1: 跑本计划全部新增测试**

Run:
```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_native_evidence_schema.py tests/unit/test_native_cache_key.py tests/unit/test_native_evidence_validation.py tests/unit/test_vision_cache_migration.py -v
```
Expected: PASS（全部通过）

- [ ] **Step 2: 确认 domain / 引擎常量层没有引入 integrations → domain 的反向依赖**

Run:
```bash
.venv\Scripts\python.exe -c "import ast,pathlib; p=pathlib.Path('src/feishu_generation_agent/domain/document.py'); src=p.read_text(encoding='utf-8'); print('integrations' in src)"
```
Expected: `False`

- [ ] **Step 3: 确认工作区改动条目数没有减少（禁止回退既有未提交改动）**

Run:
```bash
git status --porcelain | Measure-Object -Line
```
Expected: 条目数 **≥ 112**（实施前的基线是 112 = 用户 111 项 + spec 1 项）。若变小，立即停止并上报。

- [ ] **Step 4: Task 5 无代码改动，无需 commit**

---

## Self-Review

**Spec 覆盖检查**

| Spec 章节 | 落地任务 |
|---|---|
| §5.1 视频证据 schema | Task 1 |
| §5.2 向后兼容（字段不改、有默认值） | Task 1（`video_evidence` 默认 `[]`） |
| §6.1 缓存表迁移 + 新 key 格式 | Task 2、Task 4 |
| §6.3 确定性校验（含尾部容差） | Task 3 |
| §8.1 测试 1（cache key 组成） | Task 2 |
| §8.1 测试 5（校验 / 实测 18.5 vs 18.08 回归） | Task 3 |
| §8.1 测试 6（迁移幂等） | Task 4 |
| §12 迁移原则：不删旧行 | Task 4（只 `UPDATE` 打标记） |
| §9 验收：既有未提交改动零回退 | Task 5 Step 3 |

**未覆盖（属于后续 plan，符合预期）**：`>15MB 直接降级`、重试与降级编排、引擎 HTTP 调用、planner prompt 证据段、集成测试 —— 均在 Plan 02 / Plan 03。

**Placeholder 扫描**：无 TBD / TODO / "稍后补充"。

**类型一致性**：`VideoEvidence` / `VideoShot` / `TranscriptLine` 命名在 Task 1 定义、Task 3 使用，一致；`build_cache_key` 参数名在 Task 2 定义、Task 4 测试中按 `engine_id/kind/model_name/schema_version/sha256` 使用，一致；`validate_video_evidence` 返回 `tuple[VideoEvidence, list[str]]` 在 Task 3 定义与测试中一致。
