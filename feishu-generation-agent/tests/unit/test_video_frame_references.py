"""参考视频：**默认原样直传上游**（role=reference_video），抽帧只作失败兜底。

2026-10-08 P0 口径反转（用户原话：「当我手动把参考视频加进去后，视频直接不分主次了，
强行切镜，然后把视频主角换成参考视频中明确不要参考的主角」）：

- 上游方舟多参考模式**本来就支持** `video_url + role=reference_video`
  （`integrations/seedance.py`），但旧实现有**两处互相矛盾**的摘除逻辑：
  `seedance.py` 的 `_strip_non_image_references`（静默摘掉）与 `graph/nodes.py` 的
  `_replace_video_references_with_frames`（**默认**把视频换成第 1 张抽帧静帧、
  role 改写为 `reference_image`）。后者把运动参考阉割成静帧参考，而静帧携带的
  **内容**（小红书水印 + 不要参考的紫海绵块）被当成普通参考图吸收进成片。
- 现在只有一条路径：`_normalize_video_references` 把视频规范成 `reference_video`
  直传；只有**上游明确拒绝**时才走 `_video_references_to_frames` 降级（带可见事件）。

历史事故（仍然必须守住）：2026-09-24 摘了视频但任务还引用它 →
`generation_asset_mapping_mismatch` → 节点全 completed、零产物、`last_error: null`。
所以**任何摘除/替换素材的动作都必须同时保证 `reference_images` 与 `assets` 集合一致**。
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import GenerationTask, TaskPlan
from feishu_generation_agent.graph import nodes as nodes_module
from feishu_generation_agent.graph.nodes import (
    _normalize_video_references,
    _task_assets,
    _unusable_reference_message,
    _VIDEO_FRAME_COUNT,
    _video_references_to_frames,
    execute_selected_tasks,
)

_VIDEO_ID = "upload-8127ef4d694e4cc1848f3335902511e6"
_FRAME_ID = f"{_VIDEO_ID}-frame"
_PROMPT = "镜头 1（0-5s）：@图片1 停在台上，参考 @视频1 的运镜节奏。"


class _Repository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def append_event(self, run_id, node, status, summary) -> None:
        self.events.append((node, status, summary))

    def summaries(self) -> str:
        return "\n".join(summary for _, _, summary in self.events)


class _FileStore:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def save_input(self, run_id: str, filename: str, content: bytes):
        path = self.tmp_path / filename
        path.write_bytes(content)
        mime_type = (
            "video/mp4" if filename.endswith(".mp4") else "image/jpeg"
        )
        return SimpleNamespace(
            local_path=path,
            mime_type=mime_type,
            size=len(content),
            sha256=sha256(content).hexdigest(),
            width=64,
            height=64,
        )


def _services(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repository=_Repository(),
        file_store=_FileStore(tmp_path),
    )


def _video_asset(tmp_path: Path) -> MediaAsset:
    path = tmp_path / f"{_VIDEO_ID}.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16)
    return MediaAsset(
        asset_id=_VIDEO_ID,
        source_block_id="local-upload",
        origin="local_upload",
        local_path=path,
        mime_type="video/mp4",
        size=path.stat().st_size,
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


def _image_asset(tmp_path: Path, asset_id: str = "image-1") -> MediaAsset:
    path = tmp_path / f"{asset_id}.png"
    path.write_bytes(b"fictional-image")
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="feishu",
        local_path=path,
        mime_type="image/png",
        size=path.stat().st_size,
        sha256=sha256(asset_id.encode()).hexdigest(),
        width=1440,
        height=2560,
    )


def _document(*assets: MediaAsset) -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc-video-ref",
        title="参考视频",
        revision=3,
        source_type=SourceType.DOCX,
        source_token="doc-video-ref",
        blocks=[
            DocumentBlock(
                block_id="block-1",
                parent_id="page",
                block_type="text",
                order=0,
                path=["page", "block-1"],
                text="参考视频里的运镜。",
            )
        ],
        text_view="[block:block-1] 参考视频里的运镜。",
        media_assets=list(assets),
    )


def _task(references: list[tuple[str, str, int]]) -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-01",
            "task_type": "image_to_video",
            "title": "红色巴士",
            "source_block_ids": ["block-1"],
            "user_intent": "生成短片",
            "prompt": _PROMPT,
            "reference_images": [
                {"asset_id": asset_id, "role": role, "order": order}
                for asset_id, role, order in references
            ],
            "reference_mode": "multi_reference",
            "aspect_ratio": "9:16",
            "duration": 14,
            "resolution": "720p",
        }
    )


def _stub_extraction(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False):
    """替掉 ffmpeg 抽帧（本机 ffmpeg 不在 PATH 上，真跑会被 skip 掉）。

    写的是**真 JPEG**（不是随便几个字节）：有些用例走的是真 `FileStore`，它会校验
    媒体内容（`unsupported or invalid media content`）。
    """
    from PIL import Image

    calls: list[tuple[Path, int]] = []

    def fake(video_path: Path, frame_count: int, output_dir: Path) -> list[Path]:
        calls.append((Path(video_path), frame_count))
        if fail:
            raise RuntimeError("ffprobe is not available")
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(frame_count):
            path = output_dir / f"frame-{index + 1:02d}.jpg"
            Image.new("RGB", (320, 240), (index * 20, 40, 90)).save(
                path, format="JPEG"
            )
            paths.append(path)
        return paths

    monkeypatch.setattr(nodes_module, "extract_video_frames", fake)
    return calls


# ---------------------------------------------------------------------------
# ① 默认路径：视频**原样直传上游**（role=reference_video），不抽帧、不动提示词
# ---------------------------------------------------------------------------


async def test_video_reference_is_kept_as_reference_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_extraction(monkeypatch)
    video = _video_asset(tmp_path)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    services = _services(tmp_path)

    normalized = _normalize_video_references(task, document, [])

    # 默认路径不抽帧、不写事件、不改提示词。
    assert calls == []
    assert services.repository.events == []
    assert [
        (reference.asset_id, reference.role)
        for reference in normalized.reference_images
    ] == [("image-1", "reference_image"), (_VIDEO_ID, "reference_video")]
    assert normalized.prompt == _PROMPT
    assert "@视频1" in normalized.prompt
    # 关键不变量：素材列表与任务引用**必须一致**（供应商适配层就是这么校验的，
    # 2026-09-24 的生产事故正是这里对不上 → 零产物空跑）。
    assets = _task_assets(normalized, document, [])
    assert {asset.asset_id for asset in assets} == {
        reference.asset_id for reference in normalized.reference_images
    }
    assert [asset.mime_type for asset in assets] == ["image/png", "video/mp4"]


async def test_frame_alias_is_mapped_back_to_the_video(tmp_path: Path) -> None:
    """历史契约里写的抽帧别名要归回原视频（否则 `_task_assets` 直接 KeyError）。"""
    video = _video_asset(tmp_path)
    document = _document(video)
    task = _task([(_FRAME_ID, "reference_image", 1)])

    normalized = _normalize_video_references(task, document, [])

    assert [
        (reference.asset_id, reference.role)
        for reference in normalized.reference_images
    ] == [(_VIDEO_ID, "reference_video")]
    assert _task_assets(normalized, document, [])


async def test_duplicate_video_reference_is_deduplicated(tmp_path: Path) -> None:
    """同一个视频被引用两次（视频 + 它的抽帧别名）→ 只留一条，否则上游拒单。"""
    video = _video_asset(tmp_path)
    document = _document(video)
    task = _task(
        [
            (_VIDEO_ID, "reference_video", 1),
            (_FRAME_ID, "reference_image", 2),
        ]
    )

    normalized = _normalize_video_references(task, document, [])

    assert [
        (reference.asset_id, reference.role, reference.order)
        for reference in normalized.reference_images
    ] == [(_VIDEO_ID, "reference_video", 1)]
    assert _task_assets(normalized, document, [])


async def test_task_without_video_references_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_extraction(monkeypatch)
    image = _image_asset(tmp_path)
    document = _document(image)
    task = _task([("image-1", "reference_image", 1)])

    normalized = _normalize_video_references(task, document, [])

    assert calls == []
    assert normalized is task


# ---------------------------------------------------------------------------
# ② 失败兜底：只有**明确降级**时才抽帧，且必须留可见痕迹
# ---------------------------------------------------------------------------


async def test_explicit_degrade_replaces_video_with_a_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_extraction(monkeypatch)
    video = _video_asset(tmp_path)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    services = _services(tmp_path)

    rewritten, created = await _video_references_to_frames(
        task,
        document,
        [],
        "run-1",
        services,
        {},
        reason="上游报 video_url 不支持",
    )

    assert calls and calls[0][1] == _VIDEO_FRAME_COUNT
    assert [reference.asset_id for reference in rewritten.reference_images] == [
        "image-1",
        _FRAME_ID,
    ]
    assert [reference.order for reference in rewritten.reference_images] == [1, 2]
    assert [reference.role for reference in rewritten.reference_images] == [
        "reference_image",
        "reference_image",
    ]
    assert [reference.asset_id for reference in created] == [_FRAME_ID]
    assert created[0].mime_type.startswith("image/")
    assert created[0].local_path.is_file()
    # 降级后立刻把引用落到抽帧产物上（否则又是「素材与引用不一致」空跑）。
    assets = _task_assets(rewritten, document, created)
    assert {asset.asset_id for asset in assets} == {
        reference.asset_id for reference in rewritten.reference_images
    }
    # 提示词里的视频 token 要落到抽帧画面上。
    assert "@视频1" not in rewritten.prompt
    assert "@图片2" in rewritten.prompt
    # **降级必须留可见痕迹**：事件里写清原因与被换掉的素材。
    summary = services.repository.summaries()
    assert "参考视频被上游拒绝" in summary
    assert "已降级为抽帧画面" in summary
    assert "上游报 video_url 不支持" in summary
    assert _FRAME_ID in summary


async def test_replaced_references_keep_contiguous_unique_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视频夹在中间时，替换后 order 仍从 1 连续、无重复（以前踩过重复 order 触发 400）。"""
    _stub_extraction(monkeypatch)
    video = _video_asset(tmp_path)
    document = _document(
        _image_asset(tmp_path, "image-1"),
        _image_asset(tmp_path, "image-2"),
        video,
    )
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
            ("image-2", "reference_image", 3),
        ]
    )
    services = _services(tmp_path)

    rewritten, created = await _video_references_to_frames(
        task, document, [], "run-1", services, {}, reason="上游拒绝参考视频"
    )

    orders = [reference.order for reference in rewritten.reference_images]
    assert orders == [1, 2, 3]
    assert [reference.asset_id for reference in rewritten.reference_images] == [
        "image-1",
        _FRAME_ID,
        "image-2",
    ]
    assert len({reference.asset_id for reference in rewritten.reference_images}) == 3


async def test_same_video_is_extracted_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_extraction(monkeypatch)
    video = _video_asset(tmp_path)
    document = _document(video)
    services = _services(tmp_path)
    cache: dict[str, Any] = {}

    for _ in range(2):
        task = _task([(_VIDEO_ID, "reference_video", 1)])
        await _video_references_to_frames(
            task, document, [], "run-1", services, cache, reason="上游拒绝参考视频"
        )

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ③ 抽帧失败 → 沿用「摘掉 + 事件」，并触发第 2 条
# ---------------------------------------------------------------------------


async def test_extraction_failure_drops_video_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_extraction(monkeypatch, fail=True)
    video = _video_asset(tmp_path)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    services = _services(tmp_path)

    rewritten, created = await _video_references_to_frames(
        task, document, [], "run-1", services, {}, reason="上游拒绝参考视频"
    )

    assert created == []
    # 老行为：摘掉引用（而不是留在任务里让供应商报「素材与引用不一致」）。
    assert [reference.asset_id for reference in rewritten.reference_images] == [
        "image-1"
    ]
    assert [reference.order for reference in rewritten.reference_images] == [1]
    assert "参考视频抽帧失败" in services.repository.summaries()
    # 还剩可用参考图 → 照常生成，不算「零参考图」。
    assert (
        _unusable_reference_message(
            rewritten, [_image_asset(tmp_path)], had_references=True
        )
        is None
    )


async def test_extraction_failure_with_no_other_reference_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_extraction(monkeypatch, fail=True)
    video = _video_asset(tmp_path)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    services = _services(tmp_path)

    rewritten, created = await _video_references_to_frames(
        task, document, [], "run-1", services, {}, reason="上游拒绝参考视频"
    )

    assert rewritten.reference_images == []
    reason = _unusable_reference_message(
        rewritten, [], had_references=True
    )
    assert reason is not None
    assert "参考素材整理后没有可用参考图" in reason


def test_text_to_video_without_references_is_not_blocked() -> None:
    """纯文生视频（本来就没有参考素材）不能被「零参考图」这条规则误伤。"""
    task = _task([])

    assert _unusable_reference_message(task, [], had_references=False) is None


def test_missing_asset_while_others_survive_is_reported(tmp_path: Path) -> None:
    """整理只摘掉其中一张、任务却还引用着它时，也要给可读原因（不是供应商黑话）。"""
    task = _task(
        [
            ("image-1", "reference_image", 1),
            ("image-2", "reference_image", 2),
        ]
    )

    reason = _unusable_reference_message(
        task, [_image_asset(tmp_path, "image-1")], had_references=True
    )

    assert reason is not None
    assert "image-2" in reason


# ---------------------------------------------------------------------------
# ④ 整理后零参考图 → 运行明确失败（不是静默 completed）
# ---------------------------------------------------------------------------


class _ImageOnlyGenerator:
    """只收图片素材的通道（真人类通道那类），声明自己不支持参考视频。"""

    provider = "seedance"
    supports_video_references = False

    def __init__(self) -> None:
        self.seen: list[GenerationTask] = []

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        self.seen.append(task)
        raise AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message="故意中止（测试用）",
                technical_detail="cause=stop_for_test",
                retryable=False,
            )
        )


@pytest.mark.asyncio
async def test_channel_without_video_support_degrades_with_a_visible_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """通道**确定性地**不支持视频引用时 → 走同一条降级路径，且事件里看得见。

    这是「两处矛盾逻辑合并成一条」的验收：旧实现里 `seedance.py` 的
    `_strip_non_image_references` 会悄悄摘掉视频、事件里什么都没有；现在由执行层统一
    决策（可判定 → 降级 + 写事件），适配层只负责明确报错。
    """
    _stub_extraction(monkeypatch)
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    video = _video_asset(tmp_path)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    generator = _ImageOnlyGenerator()
    services = replace(
        fake_services,
        video_generator=generator,
        seedance_video_generator=None,
        seedance_video_generators={},
        aiport_video_generator=None,
    )
    config = {"configurable": {"thread_id": "thread-video-frame-failure"}}

    await execute_selected_tasks(
        _state(document, task), config, services=services
    )

    assert generator.seen, "应当照常提交（降级后重试）"
    submitted = generator.seen[0]
    roles = [reference.role for reference in submitted.reference_images]
    assert roles == ["reference_image", "reference_image"]
    assert [reference.asset_id for reference in submitted.reference_images] == [
        "image-1",
        f"{_VIDEO_ID}-frame",
    ]
    # 降级必须留可见痕迹（事件 + 原因）。
    summaries = " ".join(
        str(event.get("summary", ""))
        for event in await services.repository.list_events(
            "run-video-frame-failure"
        )
    )
    assert "参考视频被上游拒绝" in summaries
    assert "当前生成通道不支持参考视频" in summaries
    assert "已降级为抽帧画面" in summaries
    assert f"{_VIDEO_ID}-frame" in summaries



class _DurationLimitedGenerator:
    """上游接受视频引用、但**视频太长**（实测错误原文见下）的假供应商。"""

    provider = "seedance"
    supports_video_references = True
    reference_video_max_seconds = 30.0

    def __init__(self) -> None:
        self.seen: list[GenerationTask] = []

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        self.seen.append(task)
        if len(self.seen) == 1:
            # 2026-10-08 真实上游原文（run a632f45f，参考视频 43.7s）：
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.PROVIDER_TERMINAL,
                    message=(
                        "生成服务拒绝了请求：The parameter `content[2]` specified "
                        "in the request is not valid: the parameter video duration "
                        "(seconds) specified in the request must be less than or "
                        "equal to 30.2 for model doubao-seedance-2-5 in r2"
                    ),
                    technical_detail="cause=submit_invalidparameter",
                    retryable=False,
                )
            )
        raise AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message="故意中止（测试用）",
                technical_detail="cause=stop_for_test",
                retryable=False,
            )
        )


@pytest.mark.asyncio
async def test_over_long_reference_video_is_trimmed_before_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """参考视频超出上游时长上限 → **先裁短重传**（保留运动），不是直接抽帧。

    用户诉求就是「运动参考」；抽帧会把运动丢掉。所以修复梯子的第一级必须是裁剪，
    只有裁剪不可行时才退到抽帧。
    """
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    monkeypatch.setattr(
        nodes_module, "probe_video_duration", lambda _path: 43.7
    )
    extracted = _stub_extraction(monkeypatch)

    def fake_trim(video_path: Path, max_seconds: float, output_path: Path):
        assert abs(max_seconds - 30.0) < 1e-6
        output_path.write_bytes(b"trimmed-mp4-bytes")
        return output_path

    monkeypatch.setattr(nodes_module, "trim_video", fake_trim)

    video = _video_asset(tmp_path)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    generator = _DurationLimitedGenerator()
    services = replace(
        fake_services,
        video_generator=generator,
        seedance_video_generator=None,
        seedance_video_generators={},
        aiport_video_generator=None,
        file_store=_FileStore(tmp_path),
    )
    config = {"configurable": {"thread_id": "thread-video-frame-failure"}}

    await execute_selected_tasks(
        _state(document, task), config, services=services
    )

    assert len(generator.seen) == 2, "第一次被拒后应当带着裁短的视频重试一次"
    retried = generator.seen[1]
    assert [
        (reference.asset_id, reference.role, reference.order)
        for reference in retried.reference_images
    ] == [
        ("image-1", "reference_image", 1),
        (f"{_VIDEO_ID}-trim", "reference_video", 2),
    ]
    # 裁剪路径**不抽帧**（抽帧会丢运动，是第二级才用的）。
    assert extracted == []
    # 提示词里的 @视频1 位置不变（仍是同类型的第 1 个视频），不需要重映射。
    assert "@视频1" in retried.prompt
    summaries = " ".join(
        str(event.get("summary", ""))
        for event in await services.repository.list_events(
            "run-video-frame-failure"
        )
    )
    assert "已裁剪到 30s 后重传" in summaries
    assert "保留运动参考" in summaries
    assert f"{_VIDEO_ID}-trim" in summaries


class _RejectingVideoGenerator:
    """上游**明确拒绝**参考视频的假供应商（唯一允许触发降级的场景）。"""

    provider = "seedance"
    supports_video_references = True

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        raise AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=f"上游拒绝了参考视频 video_url（task_id={task.task_id}）",
                technical_detail="cause=video_reference_not_supported",
                retryable=False,
            )
        )


def _state(document: NormalizedDocument, task: GenerationTask) -> dict[str, Any]:
    plan = TaskPlan(tasks=[task], document_summary="参考视频")
    return {
        "run_id": "run-video-frame-failure",
        "thread_id": "thread-video-frame-failure",
        "status": "running",
        "normalized_document": document.model_dump(mode="json"),
        "media_assets": [
            asset.model_dump(mode="json") for asset in document.media_assets
        ],
        "approved_plan": plan.model_dump(mode="json"),
        "approved_tasks": [task.model_dump(mode="json")],
        "storyboard_assets": [],
        "execution_records": [],
        "artifacts": [],
        "last_error": None,
    }


@pytest.mark.asyncio
async def test_run_fails_visibly_when_the_video_cannot_be_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """上游拒绝视频 + 抽帧也失败 → 运行 failed、零产物，但原因可读（含 last_error）。

    绝不允许「节点全 completed、零产物、`last_error: null`」（2026-09-24 实测），
    也不允许悄悄把视频任务变成纯文生视频。
    """
    _stub_extraction(monkeypatch, fail=True)
    # 只测执行循环这一段：计划校验另有覆盖。
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    video = _video_asset(tmp_path)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    services = replace(
        fake_services,
        video_generator=_RejectingVideoGenerator(),
        seedance_video_generator=None,
        seedance_video_generators={},
        aiport_video_generator=None,
    )
    config = {"configurable": {"thread_id": "thread-video-frame-failure"}}

    result = await execute_selected_tasks(
        _state(document, task), config, services=services
    )

    assert result["status"] == "failed"
    assert result["artifacts"] == []
    assert result["execution_records"][0]["status"] == "failed"
    message = result["execution_records"][0]["error"]["message"]
    assert isinstance(message, str) and message
    # 用户必须能看到原因：`last_error` 不能是 null。
    assert result["last_error"] is not None
    assert result["last_error"]["message"] == message
    # 降级尝试本身要留痕迹（抽帧失败也要说清楚）。
    summaries = " ".join(
        str(event.get("summary", ""))
        for event in await services.repository.list_events(
            "run-video-frame-failure"
        )
    )
    assert "参考视频" in summaries
    assert "抽帧失败" in summaries


@pytest.mark.asyncio
async def test_zero_artifact_message_falls_back_to_a_readable_sentence() -> None:
    from feishu_generation_agent.domain.artifact import ExecutionRecord
    from feishu_generation_agent.graph.nodes import _zero_artifact_message

    assert _zero_artifact_message(
        [ExecutionRecord(task_id="t", provider="seedance", status="failed")]
    ) == "本次执行没有生成任何产物，请查看运行事件了解原因"
    assert _zero_artifact_message(
        [
            ExecutionRecord(
                task_id="t",
                provider="seedance",
                status="failed",
                error={"message": "参考素材整理后没有可用参考图"},
            )
        ]
    ) == "参考素材整理后没有可用参考图"
