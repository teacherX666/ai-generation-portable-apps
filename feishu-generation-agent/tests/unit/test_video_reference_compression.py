"""参考视频**体积超限自动压缩**：修复梯子第一级（2026-10-10）。

背景（用户原话）：「是视频太大了，能不能帮我自动缩小，下次遇到类似的问题也能解决」。

实测根因（零成本复现，`_diag_repro_input_too_large.py`）：用户看到的
`生成失败：Seedance 2.5：参考素材无法用于生成（generation_input_too_large）`
**不是上游错误**，是本机适配层自己的闸门 —— `SeedanceVideoGenerator` 的
`_validate_submission` 里 `file_stat.st_size > self._max_input_bytes`（默认 32 MiB）
→ `cause=input_too_large`，且**发生在任何上游请求之前**（复现时上游请求数 = 0）。
量的是**单个参考素材文件的字节数**，与分辨率/码率/时长无关。
用户那条参考视频是 35,710,093 B = 34.06 MiB，只越线 2.06 MB。

本文件覆盖的验收点：
1. 未超限 → **一个字都不动**（不重编码、不写事件）；
2. 超限 → 压缩后重传（**保留完整时长与帧率**，不抽帧）；
3. 顺序：压缩（第一级）→ 裁短（第二级）→ 抽帧（第三级）；
4. 迭代收敛与轮数上限（`_VIDEO_COMPRESS_MAX_ROUNDS`）；
5. 压缩不动 → 如实失败（体积问题**不许**顺着梯子退化成裁短/抽帧）；
6. `reference_images` 与 `assets` 集合始终一致（否则 `generation_asset_mapping_mismatch`）；
7. 每一级都留可见事件，且事件里带**具体数字**（体积/分辨率/码率）；
8. 压缩失败**绝不静默**（事件 + 原始错误浮出来）。
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
from feishu_generation_agent.integrations import video_reference
from feishu_generation_agent.integrations.video_reference import (
    CompressedVideo,
    VideoInfo,
    compress_video,
    probe_video_info,
)

_VIDEO_ID = "upload-8127ef4d694e4cc1848f3335902511e6"
_COMPRESSED_ID = f"{_VIDEO_ID}-small"
_TRIMMED_ID = f"{_VIDEO_ID}-trim"
_FRAME_ID = f"{_VIDEO_ID}-frame"
_PROMPT = "镜头 1（0-5s）：@图片1 停在台上，参考 @视频1 的运镜节奏。"
_RUN_ID = "run-video-size-compression"
_LIMIT = 1024  # 测试用的极小闸门，方便造超限


# ---------------------------------------------------------------------------
# 基础设施（与 test_video_frame_references.py 同一套最小桩）
# ---------------------------------------------------------------------------


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
            width=320,
            height=240,
        )


def _video_asset(tmp_path: Path, size: int = 4096) -> MediaAsset:
    path = tmp_path / f"{_VIDEO_ID}.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x01" * (size - 16))
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
        document_id="doc-video-size",
        title="参考视频体积",
        revision=3,
        source_type=SourceType.DOCX,
        source_token="doc-video-size",
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


def _state(document: NormalizedDocument, task: GenerationTask) -> dict[str, Any]:
    plan = TaskPlan(tasks=[task], document_summary="参考视频")
    return {
        "run_id": _RUN_ID,
        "thread_id": _RUN_ID,
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


def _stub_extraction(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """替掉抽帧：压缩/裁短路径**不该**走到抽帧，真走了调用列表里就有记录。"""
    calls: list[Path] = []

    def fake(video_path: Path, frame_count: int, output_dir: Path) -> list[Path]:
        from PIL import Image

        calls.append(Path(video_path))
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


def _fake_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    width: int = 640,
    height: int = 480,
    duration: float = 12.0,
    bitrate_bps: int = 2_400_000,
    out_width: int = 320,
    out_height: int = 240,
    out_bitrate_bps: int = 640_000,
    codec: str = "h264",
    out_codec: str = "h264",
):
    """把 `probe_video_info` 换掉：压缩产物按文件名后缀给出"压缩后"的参数。

    体积一律取**真实文件大小**（压缩轮的收敛判断依赖它）。
    """

    def fake(path: Path) -> VideoInfo:
        real = Path(path)
        compressed = real.stem.endswith(("-small", "-trim", "-small-trim"))
        return VideoInfo(
            width=out_width if compressed else width,
            height=out_height if compressed else height,
            fps=30.0,
            duration=duration,
            bitrate_bps=out_bitrate_bps if compressed else bitrate_bps,
            size_bytes=real.stat().st_size,
            has_audio=True,
            codec=out_codec if compressed else codec,
        )

    monkeypatch.setattr(nodes_module, "probe_video_info", fake)
    return fake


def _fake_compress(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sizes: list[int] | None = None,
    raises: bool = False,
    out_width: int = 320,
    out_height: int = 240,
    out_kbps: int = 640,
    out_codec: str = "h264",
):
    """替换 `compress_video`：记录每一轮的档位，按 `sizes` 依次给出压缩后体积。"""
    calls: list[dict[str, Any]] = []
    plan = list(sizes or [])
    # 源视频帧数（`_compress_video_asset` 会先数一次，用来自检"没有抽帧"）——
    # 这里的视频是假字节，真 ffprobe 数不出来，所以一并桩掉。
    monkeypatch.setattr(
        nodes_module, "count_decoded_frames", lambda _path: 300
    )

    def fake(
        video_path,
        max_bytes,
        output_path,
        *,
        max_long_side=None,
        budget_ratio=0.92,
        crf=None,
        **kwargs,
    ):
        calls.append(
            {
                "max_long_side": max_long_side,
                "max_bytes": max_bytes,
                "budget_ratio": budget_ratio,
                "crf": crf,
                "kwargs": kwargs,
            }
        )
        if raises:
            raise RuntimeError("ffmpeg exploded")
        size = plan[len(calls) - 1] if len(calls) <= len(plan) else plan[-1]
        output_path.write_bytes(b"\x02" * size)
        return CompressedVideo(
            path=Path(output_path),
            info=VideoInfo(
                width=out_width,
                height=out_height,
                fps=30.0,
                duration=12.0,
                bitrate_bps=out_kbps * 1000,
                size_bytes=size,
                has_audio=True,
                codec=out_codec if crf is None else "h264",
            ),
            video_kbps=out_kbps,
            max_long_side=max_long_side,
        )

    monkeypatch.setattr(nodes_module, "compress_video", fake)
    return calls


def _record_trim(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []

    def fake(video_path: Path, max_seconds: float, output_path: Path):
        calls.append(max_seconds)
        output_path.write_bytes(b"\x03" * 256)
        return output_path

    monkeypatch.setattr(nodes_module, "trim_video", fake)
    return calls


class _LocalSizeGateGenerator:
    """模拟**本机闸门**：素材字节数超上限就报 `cause=input_too_large`。

    与真实适配层逐字一致的口径（`integrations/seedance.py::_validate_submission`）：
    `category=DOCUMENT`、message「参考图片超过大小限制」、technical_detail
    `asset_id=…; cause=input_too_large` —— 执行层把它渲染成用户看到的
    `参考素材无法用于生成（generation_input_too_large）`。
    """

    provider = "seedance"
    supports_video_references = True
    reference_video_max_seconds = 30.0
    reference_video_accepted_codecs = frozenset({"h264"})

    def __init__(self, max_bytes: int) -> None:
        self.reference_video_max_bytes = max_bytes
        self.submissions: list[tuple[GenerationTask, list[MediaAsset]]] = []

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        self.submissions.append((task, list(assets)))
        for asset in assets:
            if asset.size > self.reference_video_max_bytes:
                raise AgentError(
                    ErrorDetail(
                        category=ErrorCategory.DOCUMENT,
                        message=(
                            f"参考图片超过大小限制（asset_id={asset.asset_id}）"
                        ),
                        technical_detail=(
                            f"asset_id={asset.asset_id}; cause=input_too_large"
                        ),
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


class _HevcRejectingGenerator(_LocalSizeGateGenerator):
    """模拟上游对 **HEVC** 参考视频的拒绝（`invalid_media`，发生在 poll 阶段）。

    真实口径（run `fa2fae44` / `d92a4c4b`）：
    `provider_terminal_error` / message
    「生成服务拒绝了请求：input media detect failed: invalid_media Request id: …」/
    `code=poll_invalidparameter`。注意上游是**先接受提交、再在 poll 阶段失败**。
    """

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        self.submissions.append((task, list(assets)))
        for asset in assets:
            if asset.size > self.reference_video_max_bytes:
                raise AgentError(
                    ErrorDetail(
                        category=ErrorCategory.DOCUMENT,
                        message=(
                            f"参考图片超过大小限制（asset_id={asset.asset_id}）"
                        ),
                        technical_detail=(
                            f"asset_id={asset.asset_id}; cause=input_too_large"
                        ),
                        retryable=False,
                    )
                )
        # 真实口径：素材是 HEVC（我们按文件名后缀模拟"压缩后就是 H.264"）。
        hevc_left = [
            asset
            for asset in assets
            if asset.mime_type.startswith("video/")
            and not asset.asset_id.endswith("-small")
        ]
        if hevc_left:
            # 与真实 `_terminal_status_error` 逐字同形（见 run fa2fae44/d92a4c4b 的
            # `execution_records`）：message 带上游原文，technical_detail 带
            # `provider_code` / `provider_message`，执行层据此还原 code 与原因。
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.PROVIDER_TERMINAL,
                    message=(
                        "生成服务拒绝了请求：input media detect failed: "
                        "invalid_media Request id: "
                        "02179159681120900000000000000000000ffffac1926adcef0c8"
                    ),
                    technical_detail=(
                        "operation=poll; status=failed; "
                        "provider_code=invalidparameter; "
                        "provider_message=input media detect failed: invalid_media "
                        "Request id: "
                        "02179159681120900000000000000000000ffffac1926adcef0c8"
                    ),
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


class _DurationRejectingGenerator:
    """体积过关、但**时长超限**的通道（用来验证"压缩在裁短之前"）。"""

    provider = "seedance"
    supports_video_references = True
    reference_video_max_seconds = 30.0
    reference_video_accepted_codecs = frozenset({"h264"})

    def __init__(self, max_bytes: int) -> None:
        self.reference_video_max_bytes = max_bytes
        self.submissions: list[tuple[GenerationTask, list[MediaAsset]]] = []

    async def submit(self, task, assets, *, submission_id=None):  # noqa: ARG002
        self.submissions.append((task, list(assets)))
        for asset in assets:
            if asset.size > self.reference_video_max_bytes:
                raise AgentError(
                    ErrorDetail(
                        category=ErrorCategory.DOCUMENT,
                        message=(
                            f"参考图片超过大小限制（asset_id={asset.asset_id}）"
                        ),
                        technical_detail=(
                            f"asset_id={asset.asset_id}; cause=input_too_large"
                        ),
                        retryable=False,
                    )
                )
        if any(
            reference.role == "reference_video"
            for reference in task.reference_images
        ) and not any(
            reference.asset_id.endswith("-trim")
            for reference in task.reference_images
        ):
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.PROVIDER_TERMINAL,
                    message=(
                        "生成服务拒绝了请求：the parameter video duration "
                        "(seconds) specified in the request must be less than "
                        "or equal to 30.2 for model doubao-seedance-2-5"
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


def _wire(
    fake_services: Any, tmp_path: Path, generator: Any
) -> Any:
    return replace(
        fake_services,
        video_generator=generator,
        seedance_video_generator=None,
        seedance_video_generators={},
        aiport_video_generator=None,
        file_store=_FileStore(tmp_path),
    )


async def _summaries(services: Any) -> str:
    events = await services.repository.list_events(_RUN_ID)
    return "\n".join(str(event.get("summary", "")) for event in events)


# ---------------------------------------------------------------------------
# ① 能力字段：唯一判据来源
# ---------------------------------------------------------------------------


def test_seedance_exposes_the_reference_video_byte_cap() -> None:
    """体积上限必须来自**适配层闸门本身**，不在节点层写死魔数。"""
    import httpx

    from feishu_generation_agent.domain.video_models import VIDEO_MODEL_BY_KEY
    from feishu_generation_agent.integrations.seedance import (
        SeedanceVideoGenerator,
    )

    capability = VIDEO_MODEL_BY_KEY["seedance2.5"]

    def build(**kwargs):
        return SeedanceVideoGenerator(
            httpx.AsyncClient(),
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key="ark-0000000000000000000000000000000000000000",
            model=capability.model,
            capability=capability,
            **kwargs,
        )

    default = build()
    # 与闸门是同一个数（不放松闸门，改成"超限自动压缩"）。
    assert default.reference_video_max_bytes == default._max_input_bytes
    assert default.reference_video_max_bytes == 32 * 1024 * 1024
    assert build(max_input_bytes=4096).reference_video_max_bytes == 4096
    # 与已有的时长上限并列（同一处能力判据）。
    assert default.reference_video_max_seconds == 30.0


def test_size_error_is_recognised_as_a_video_rejection(tmp_path: Path) -> None:
    """`cause=input_too_large` 以前**不认**，修复梯子因此根本没启动（直接判死）。"""
    video = _video_asset(tmp_path, 64)
    image = _image_asset(tmp_path)
    error = AgentError(
        ErrorDetail(
            category=ErrorCategory.DOCUMENT,
            message=f"参考图片超过大小限制（asset_id={_VIDEO_ID}）",
            technical_detail=f"asset_id={_VIDEO_ID}; cause=input_too_large",
            retryable=False,
        )
    )

    assert nodes_module._video_rejection_reason(
        error, _task([(_VIDEO_ID, "reference_video", 1)]), [video]
    )
    # 没有视频素材时不算「视频被拒」（体积超限的参考图不该走压缩梯子）。
    assert (
        nodes_module._video_rejection_reason(
            error, _task([("image-1", "reference_image", 1)]), [image]
        )
        is None
    )
    assert nodes_module._is_size_rejection("... cause=input_too_large")
    assert nodes_module._is_size_rejection("参考图片超过大小限制")
    assert not nodes_module._is_size_rejection("video duration must be <= 30.2")
    # **总量**超限不算：裁短某个视频确实能减小总量，所以不许在这挡死。
    assert not nodes_module._is_size_rejection(
        "参考图片总量超过大小限制; cause=total_input_too_large"
    )


@pytest.mark.asyncio
async def test_size_rejection_does_not_trim_a_different_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """体积超限时**不许**去裁另一条视频（2026-10-10 活体验证抓到的真 bug）。

    现场：素材 A 体积超限（压不动），素材 B 时长 65.5s —— 旧顺序下梯子会先把 B 裁到
    30s（白丢 35 秒运动参考），再提交一次注定被本机闸门拒掉的请求。体积/编码问题
    只能靠重编码解决，顺序必须是「压缩 → （体积问题就此为止）→ 裁短 → 抽帧」。
    """
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch, duration=43.7)
    _fake_compress(monkeypatch, raises=True)  # A 压不动
    trim_calls = _record_trim(monkeypatch)
    monkeypatch.setattr(nodes_module, "probe_video_duration", lambda _path: 43.7)

    big = _video_asset(tmp_path, size=4096)  # A：超闸门
    small_path = tmp_path / "upload-video-2.mp4"
    small_path.write_bytes(b"\x01" * 512)
    small = MediaAsset(
        asset_id="upload-video-2",
        source_block_id="local-upload-2",
        origin="local_upload",
        local_path=small_path,
        mime_type="video/mp4",
        size=512,
        sha256=sha256(small_path.read_bytes()).hexdigest(),
    )
    document = _document(big, small)
    task = _task(
        [
            (_VIDEO_ID, "reference_video", 1),
            ("upload-video-2", "reference_video", 2),
        ]
    )
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    result = await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    assert trim_calls == [], "体积问题不该去裁另一条视频"
    assert extracted == []
    assert result["status"] == "failed"
    assert result["artifacts"] == []
    summaries = await _summaries(services)
    assert "已裁剪到" not in summaries
    assert "生成失败" in summaries


# ---------------------------------------------------------------------------
# ② 未超限 → 一个字都不动
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_within_the_byte_cap_is_not_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch)
    compress_calls = _fake_compress(monkeypatch)
    trim_calls = _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=512)  # 低于 1024 的闸门
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    # 没超限 → 不重编码（无谓的画质损失与耗时），也不写任何修复事件。
    assert compress_calls == []
    assert trim_calls == []
    assert len(generator.submissions) == 1
    assert [asset.asset_id for asset in generator.submissions[0][1]] == [_VIDEO_ID]
    assert "压缩" not in await _summaries(services)
    assert "裁剪" not in await _summaries(services)


# ---------------------------------------------------------------------------
# ③ 超限 → 压缩后重传（保留时长与帧率，不抽帧）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_reference_video_is_compressed_not_sampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch)
    # 第一轮（只降码率，不动分辨率）就压到 512B ≤ 1024B 闸门。
    compress_calls = _fake_compress(monkeypatch, sizes=[512])
    trim_calls = _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=4096)
    image = _image_asset(tmp_path)
    document = _document(image, video)
    task = _task(
        [
            ("image-1", "reference_image", 1),
            (_VIDEO_ID, "reference_video", 2),
        ]
    )
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    # 第一轮只降码率：保分辨率（max_long_side=None）。
    assert len(compress_calls) == 1
    assert compress_calls[0]["max_long_side"] is None
    assert compress_calls[0]["max_bytes"] == _LIMIT
    # 压缩成功 → 不再裁短、更不抽帧（运动信息一点不丢）。
    assert trim_calls == []
    assert extracted == []
    # **提交前就修好了**：只提交一次（前端用户什么都不用做，也没有"注定失败的那一次"）。
    assert len(generator.submissions) == 1

    submitted_task, submitted_assets = generator.submissions[0]
    assert [
        (reference.asset_id, reference.role, reference.order)
        for reference in submitted_task.reference_images
    ] == [
        ("image-1", "reference_image", 1),
        (_COMPRESSED_ID, "reference_video", 2),
    ]
    # 关键不变量：reference_images 与 assets **集合一致**（2026-09-24 的
    # `generation_asset_mapping_mismatch` 零产物事故就是这里对不上）。
    assert [asset.asset_id for asset in submitted_assets] == [
        reference.asset_id
        for reference in sorted(
            submitted_task.reference_images, key=lambda item: item.order
        )
    ]
    # 视频引用的位置没变 → @视频1 不需要重映射。
    assert "@视频1" in submitted_task.prompt

    summaries = await _summaries(services)
    assert "参考视频体积过大" in summaries
    assert "已压缩后重传" in summaries
    assert "时长与运动保留" in summaries
    # 具体数字（体积 / 分辨率 / 码率）必须出现在事件里，用户才看得懂做了什么。
    assert _COMPRESSED_ID in summaries
    assert "4.0KB > 上限约 1.0KB" in summaries
    assert "640x480" in summaries and "320x240" in summaries
    assert "640kbps" in summaries
    assert "512B" in summaries


# ---------------------------------------------------------------------------
# ③b 编码不被上游接受（HEVC）→ 提交前转成 H.264，不再白撞一次 invalid_media
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hevc_reference_video_is_transcoded_before_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """`invalid_media` 的根因是 **HEVC**：体积没超也要转成 H.264（保分辨率/帧率）。"""
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    # 体积 512B 远低于 1KB 闸门，但编码是 hevc。
    _fake_probe(monkeypatch, codec="hevc", out_codec="h264")
    compress_calls = _fake_compress(monkeypatch, sizes=[512], out_codec="h264")
    trim_calls = _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=512)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _HevcRejectingGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    result = await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    # 先走**画质优先转码**（crf，不动分辨率），不是码率/分辨率档位。
    assert len(compress_calls) == 1
    assert compress_calls[0]["crf"] == 20
    assert compress_calls[0]["max_bytes"] is None
    assert compress_calls[0]["max_long_side"] is None
    assert trim_calls == []
    assert extracted == []
    # 只提交一次：带着转码后的 `-small` 素材，上游不再看到 HEVC。
    assert len(generator.submissions) == 1
    submitted_task, _assets = generator.submissions[0]
    assert [
        reference.asset_id for reference in submitted_task.reference_images
    ] == [_COMPRESSED_ID]
    summaries = await _summaries(services)
    assert "编码上游不认（hevc → h264）" in summaries
    assert "已转码后重传" in summaries
    assert "时长与运动保留" in summaries
    assert result["status"] == "failed"  # 假供应商最后用一个无标记错误收尾


@pytest.mark.asyncio
async def test_hevc_rejection_is_recorded_as_a_visible_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """万一还是被上游以 `invalid_media` 拒绝：**必须留一条带原文的事件**。

    2026-10-10 用户连撞两条 `invalid_media`，运行事件里一个字都没有（错误只写了
    `execution_records`/`last_error`），排查只能翻 checkpoint 二进制。
    """
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch, codec="hevc", out_codec="h264")
    # 转码压不动（模拟 ffmpeg 失败）→ 梯子升级到抽帧也失败 → 原始错误浮出来。
    _fake_compress(monkeypatch, raises=True)
    monkeypatch.setattr(
        nodes_module, "_video_references_to_frames", _no_frame_fallback
    )

    video = _video_asset(tmp_path, size=512)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _HevcRejectingGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    result = await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    summaries = await _summaries(services)
    assert "invalid_media" in summaries, "上游原文必须出现在可见事件里"
    assert "poll_invalidparameter" in summaries
    assert result["last_error"] is not None
    assert summaries.count("生成失败") >= 1


async def _no_frame_fallback(*args, **kwargs):
    raise AssertionError("这条用例不该走到抽帧")


# ---------------------------------------------------------------------------
# ④ 顺序：压缩 → 裁短 → 抽帧
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compression_runs_before_trimming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """体积与时长**都**超限时：先压体积（保时长），压完还被拒才裁短。"""
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch, duration=43.7)
    compress_calls = _fake_compress(monkeypatch, sizes=[512])
    trim_calls = _record_trim(monkeypatch)
    monkeypatch.setattr(
        nodes_module, "probe_video_duration", lambda _path: 43.7
    )

    video = _video_asset(tmp_path, size=4096)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _DurationRejectingGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    submitted = [
        [reference.asset_id for reference in item.reference_images]
        for item, _assets in generator.submissions
    ]
    # 裁短是在**压缩产物**上做的（`-small-trim`），不是回头拿原始大文件重来。
    # 而且原始的 4KiB 大文件**一次都没提交过**（预检在提交前就修好了）。
    assert submitted == [[_COMPRESSED_ID], [f"{_COMPRESSED_ID}-trim"]]
    assert len(compress_calls) == 1, "压缩是梯子第一级"
    assert len(trim_calls) == 1, "压缩后仍被拒才轮到裁短"
    assert extracted == [], "压缩+裁短都成功了，不该再抽帧"
    summaries = await _summaries(services)
    # 每一级都留了可见事件。
    assert "已压缩后重传" in summaries
    assert "已裁剪到 30s 后重传" in summaries


# ---------------------------------------------------------------------------
# ⑤ 迭代收敛与轮数上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compression_converges_within_two_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """第一轮压不动就再降一档，第二轮达标即收敛（不再多压）。"""
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch)
    compress_calls = _fake_compress(monkeypatch, sizes=[4096, 512])
    _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=4096)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    assert [call["max_long_side"] for call in compress_calls] == [None, 1280]
    # 预检阶段就收敛了 → 只提交一次（不是"先失败一次再修"）。
    assert len(generator.submissions) == 1
    summaries = await _summaries(services)
    assert "第 1 轮" in summaries and "第 2 轮" in summaries
    assert "逐轮" in summaries


@pytest.mark.asyncio
async def test_compression_stops_at_the_round_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """压不动时最多压 `_VIDEO_COMPRESS_MAX_ROUNDS` 轮，然后如实失败。"""
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch)
    compress_calls = _fake_compress(monkeypatch, sizes=[4096])  # 每轮都压不下来
    trim_calls = _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=4096)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    result = await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    assert len(compress_calls) == nodes_module._VIDEO_COMPRESS_MAX_ROUNDS
    assert [call["max_long_side"] for call in compress_calls] == [
        step.max_long_side for step in nodes_module._COMPRESS_SIZE_STEPS
    ]
    # 体积问题**不许**退化成裁短/抽帧（那既解决不了体积，又白丢信息）。
    assert trim_calls == []
    assert extracted == []
    # 且绝不静默：运行明确失败 + last_error 可读 + 事件里有压缩失败原因。
    assert result["status"] == "failed"
    assert result["artifacts"] == []
    assert result["last_error"] is not None
    summaries = await _summaries(services)
    assert "重编码失败" in summaries
    assert "仍未压到" in summaries
    assert str(nodes_module._VIDEO_COMPRESS_MAX_ROUNDS) in summaries


@pytest.mark.asyncio
async def test_compression_exception_is_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_services
) -> None:
    """ffmpeg 抛异常 → 捕获、写事件、如实失败（不许静默，也不许乱降级）。"""
    monkeypatch.setattr(nodes_module, "validate_plan", lambda *a, **k: [])
    extracted = _stub_extraction(monkeypatch)
    _fake_probe(monkeypatch)
    compress_calls = _fake_compress(monkeypatch, raises=True)
    trim_calls = _record_trim(monkeypatch)

    video = _video_asset(tmp_path, size=4096)
    document = _document(video)
    task = _task([(_VIDEO_ID, "reference_video", 1)])
    generator = _LocalSizeGateGenerator(max_bytes=_LIMIT)
    services = _wire(fake_services, tmp_path, generator)

    result = await nodes_module.execute_selected_tasks(
        _state(document, task),
        {"configurable": {"thread_id": _RUN_ID}},
        services=services,
    )

    assert len(compress_calls) == 1
    assert trim_calls == []
    assert extracted == []
    assert result["status"] == "failed"
    assert result["last_error"] is not None
    summaries = await _summaries(services)
    assert "重编码失败" in summaries
    assert "未改动该素材" in summaries


# ---------------------------------------------------------------------------
# ⑥ `compress_video` 本身：真 ffmpeg（保留时长/帧率，只降码率/必要时降分辨率）
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    try:
        video_reference._binary("ffmpeg")
        video_reference._binary("ffprobe")
        return True
    except Exception:  # noqa: BLE001
        return False


_FFMPEG = _ffmpeg_available()


def _make_test_video(path: Path, *, seconds: int = 3, size: str = "320x240") -> Path:
    """造一个高码率、30fps、带运动的测试视频（体积远超下面给的上限）。"""
    video_reference._binary("ffmpeg")
    import subprocess

    subprocess.run(
        [
            video_reference._binary("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size={size}:rate=30",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "8M",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg/ffprobe not available")
def test_probe_video_info_reads_real_parameters(tmp_path: Path) -> None:
    video = _make_test_video(tmp_path / "src.mp4")

    info = probe_video_info(video)

    assert info is not None
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(30.0, abs=0.5)
    assert info.duration == pytest.approx(3.0, abs=0.3)
    assert info.size_bytes == video.stat().st_size
    assert info.bitrate_bps and info.bitrate_bps > 0


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg/ffprobe not available")
def test_compression_shrinks_without_touching_duration_or_framerate(
    tmp_path: Path,
) -> None:
    """核心不变量：**体积变小、时长不变、帧率不变、分辨率不被放大也不被无谓降低**。"""
    source = _make_test_video(tmp_path / "src.mp4")
    before = probe_video_info(source)
    assert before is not None
    target = before.size_bytes // 4

    result = compress_video(source, target, tmp_path / "small.mp4")

    assert result is not None
    info = result.info
    assert info.size_bytes <= target
    # 时长保留（容器层面允许 ~0.1s 的重封装误差，绝不允许"越压越短"）。
    assert info.duration == pytest.approx(before.duration, abs=0.15)
    # 帧率保留（不抽帧：抽帧会破坏运动参考的连续性）。
    assert info.fps == pytest.approx(before.fps or 30.0, abs=0.5)
    # 第一轮不指定长边上限 → 分辨率保持原样。
    assert (info.width, info.height) == (before.width, before.height)
    assert result.video_kbps > 0


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg/ffprobe not available")
def test_compression_can_downsize_while_keeping_duration(tmp_path: Path) -> None:
    """第二轮档位（限长边）生效：等比缩小、时长/帧率仍然保留。"""
    source = _make_test_video(tmp_path / "src.mp4", size="640x480")
    before = probe_video_info(source)
    assert before is not None

    result = compress_video(
        source, 200_000, tmp_path / "small.mp4", max_long_side=160
    )

    assert result is not None
    info = result.info
    assert max(info.width, info.height) <= 160
    assert info.width % 2 == 0 and info.height % 2 == 0
    assert info.duration == pytest.approx(before.duration, abs=0.15)
    assert info.fps == pytest.approx(before.fps or 30.0, abs=0.5)
    assert info.size_bytes <= 200_000


def test_compression_rejects_a_non_positive_budget(tmp_path: Path) -> None:
    assert compress_video(tmp_path / "whatever.mp4", 0, tmp_path / "out.mp4") is None
    assert (
        compress_video(tmp_path / "whatever.mp4", -1, tmp_path / "out.mp4")
        is None
    )
    # 没有体积目标也没有 crf → 不知道该做什么，拒绝。
    assert (
        compress_video(tmp_path / "whatever.mp4", None, tmp_path / "out.mp4")
        is None
    )


def test_verify_reference_video_rejects_garbage(tmp_path: Path) -> None:
    """自检必须拦住脏产物（容器头不对 / 解不了）——「自检不过就不许交给上游」。"""
    from feishu_generation_agent.integrations.video_reference import (
        CompressedVideoCheck,
        verify_reference_video,
    )

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00" * 4096)
    assert not verify_reference_video(
        junk, CompressedVideoCheck(duration=1.0, codec="h264")
    )

    # 头对了但内容全是垃圾（解不了）。
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x11" * 4096)
    assert not verify_reference_video(
        fake, CompressedVideoCheck(duration=1.0, codec="h264")
    )

    missing = tmp_path / "missing.mp4"
    assert not verify_reference_video(
        missing, CompressedVideoCheck(duration=1.0, codec="h264")
    )


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg/ffprobe not available")
def test_compression_output_passes_self_check_and_is_h264(tmp_path: Path) -> None:
    """真 ffmpeg：产物编码是 H.264、时长/帧率保留、且能完整解码（自检通过）。"""
    from feishu_generation_agent.integrations.video_reference import (
        CompressedVideoCheck,
        verify_reference_video,
    )

    source = _make_test_video(tmp_path / "src.mp4", seconds=2, size="320x240")
    before = probe_video_info(source)
    assert before is not None

    result = compress_video(source, None, tmp_path / "crf.mp4", crf=23)

    assert result is not None
    info = result.info
    assert info.codec == "h264"
    assert (info.width, info.height) == (before.width, before.height)
    assert info.fps == pytest.approx(before.fps or 30.0, abs=0.5)
    assert info.duration == pytest.approx(before.duration, abs=0.15)
    assert verify_reference_video(
        tmp_path / "crf.mp4",
        CompressedVideoCheck(
            duration=before.duration,
            width=before.width,
            height=before.height,
            fps=before.fps,
            codec="h264",
        ),
    )
    # 自检也要能**否掉**一个期望值不对的产物（例如编码要求变了）。
    assert not verify_reference_video(
        tmp_path / "crf.mp4",
        CompressedVideoCheck(duration=before.duration, codec="hevc"),
    )
