"""可见性收口（2026-10-28，用户要求接着做 B）。

守的是「参考图到底交没交给规划器」这条链路上原先把一切都吞掉的三处静默：
1. `build_planning_media_parts` —— 摸不到本地文件的素材**连日志都没有**，上传失败只
   写 `_LOGGER.warning`；
2. `_planner_supports_media_parts` 为假时 `_planning_media_argument` 静默返回 `{}`；
3. `_describe_images` —— 逐张异常塞回 outcomes 再筛掉，全失败返回 `[]`，规划照常跑。

排查需求1「铲蜡」时只能靠翻 `media-upload-cache.json` 里有没有那条 TOS 链接反推，
正是这些缺口的代价。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_generation_agent.domain.document import MediaAsset, VisionDescription
from feishu_generation_agent.graph.nodes import (
    _describe_images,
    _planner_supports_media_parts,
    _planning_media_argument,
    _record_visibility_event,
)
from feishu_generation_agent.integrations.planning_media import (
    MediaUploadCache,
    build_planning_media_parts,
)
from feishu_generation_agent.integrations.public_media import (
    PublicMediaUploadError,
)


def _asset(
    asset_id: str,
    path: Path,
    *,
    download_error: str | None = None,
    mime_type: str = "image/png",
) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="document",
        local_path=path,
        mime_type=mime_type,
        size=10,
        sha256=f"sha-{asset_id}",
        download_error=download_error,
    )


class _Host:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.uploads = 0

    async def upload(self, payload: bytes, name: str, mime_type: str) -> str:
        self.uploads += 1
        if self.fail:
            raise PublicMediaUploadError("图床 503")
        return f"https://tos/{name}"


# --- build_planning_media_parts：三处跳过都要留痕 ---


@pytest.mark.asyncio
async def test_missing_local_file_is_reported(tmp_path: Path) -> None:
    issues: list[str] = []
    parts = await build_planning_media_parts(
        [_asset("image-1", tmp_path / "不存在.png")],
        public_media_host=_Host(),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        issues=issues,
    )
    assert parts == []
    assert len(issues) == 1
    assert "image-1" in issues[0] and "本地文件不存在" in issues[0]


@pytest.mark.asyncio
async def test_upload_failure_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    issues: list[str] = []
    parts = await build_planning_media_parts(
        [_asset("image-1", f)],
        public_media_host=_Host(fail=True),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        issues=issues,
    )
    assert parts == []
    assert len(issues) == 1
    assert "image-1" in issues[0] and "上传公开图床失败" in issues[0]


@pytest.mark.asyncio
async def test_over_max_items_is_reported(tmp_path: Path) -> None:
    files = []
    for i in range(3):
        p = tmp_path / f"{i}.png"
        p.write_bytes(b"x")
        files.append(_asset(f"image-{i + 1}", p))
    issues: list[str] = []
    parts = await build_planning_media_parts(
        files,
        public_media_host=_Host(),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        max_items=2,
        issues=issues,
    )
    assert len(parts) == 2
    assert any("超过一次规划上限" in m for m in issues)


@pytest.mark.asyncio
async def test_download_error_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    issues: list[str] = []
    await build_planning_media_parts(
        [_asset("image-1", f, download_error="HTTP 404")],
        public_media_host=_Host(),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        issues=issues,
    )
    assert len(issues) == 1
    assert "下载失败" in issues[0]


@pytest.mark.asyncio
async def test_happy_path_stays_quiet(tmp_path: Path) -> None:
    """判别力自检的反面：一切正常时不许冒出噪音（否则告警会被无视）。"""
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    issues: list[str] = []
    parts = await build_planning_media_parts(
        [_asset("image-1", f)],
        public_media_host=_Host(),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        issues=issues,
    )
    assert len(parts) == 1
    assert issues == []


@pytest.mark.asyncio
async def test_audio_is_not_treated_as_an_anomaly(tmp_path: Path) -> None:
    """音频本来就不进多模态规划 —— 不是异常，不该制造噪音。"""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    issues: list[str] = []
    await build_planning_media_parts(
        [_asset("audio-1", f, mime_type="audio/mpeg")],
        public_media_host=_Host(),
        cache=MediaUploadCache(tmp_path / "cache.json"),
        issues=issues,
    )
    assert issues == []


# --- 规划器不支持 media_parts ---


def test_planner_support_probe_and_silent_drop() -> None:
    class _NoSupport:
        async def plan(self, document, visions, feedback=None):  # noqa: ANN001, ANN201
            return None

    class _Support:
        async def plan(  # noqa: ANN001, ANN201
            self, document, visions, feedback=None, media_parts=None
        ):
            return None

    parts = [{"type": "image_url", "image_url": {"url": "https://x"}}]
    assert _planner_supports_media_parts(_NoSupport()) is False
    assert _planner_supports_media_parts(_Support()) is True
    # 不支持时参数被丢掉（既有行为）—— 正因如此调用方必须自己留痕
    assert _planning_media_argument(_NoSupport(), parts) == {}
    assert _planning_media_argument(_Support(), parts)["media_parts"] == parts


# --- _describe_images 不再静默 ---


class _BoomAnalyzer:
    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        raise RuntimeError("vision 401")


class _OkAnalyzer:
    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        return VisionDescription(
            asset_id=asset.asset_id,
            subjects=["a"],
            scene="s",
            style="t",
            composition="c",
            characters=[],
            actions=[],
            visible_text=[],
            colors=[],
            probable_role="reference_image",
            uncertainties=[],
        )


class _Doc:
    def __init__(self, assets: list[MediaAsset]) -> None:
        self.media_assets = assets


class _Services:
    def __init__(self, analyzer: Any) -> None:
        self.vision_analyzer = analyzer


@pytest.mark.asyncio
async def test_describe_images_reports_each_failure() -> None:
    assets = [
        _asset("image-1", Path("/nowhere/1.png")),
        _asset("image-2", Path("/nowhere/2.png")),
    ]
    descriptions, issues = await _describe_images(
        _Doc(assets), _Services(_BoomAnalyzer())
    )
    assert descriptions == []
    assert len(issues) == 2
    assert "image-1" in issues[0] and "image-2" in issues[1]


@pytest.mark.asyncio
async def test_describe_images_missing_analyzer_is_reported() -> None:
    descriptions, issues = await _describe_images(_Doc([]), _Services(None))
    assert descriptions == []
    assert len(issues) == 1
    assert "未配置视觉分析器" in issues[0]


@pytest.mark.asyncio
async def test_describe_images_happy_path_stays_quiet() -> None:
    assets = [_asset("image-1", Path("/nowhere/1.png"))]
    descriptions, issues = await _describe_images(
        _Doc(assets), _Services(_OkAnalyzer())
    )
    assert len(descriptions) == 1
    assert issues == []


# --- 可见性事件本身绝不能变成新的失败点 ---


@pytest.mark.asyncio
async def test_visibility_event_never_raises() -> None:
    class _BrokenRepo:
        async def append_event(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    class _S:
        repository = _BrokenRepo()

    # 不该抛
    await _record_visibility_event(_S(), "run-1", "参考图未附上")
    # 没有 run_id 时直接跳过
    await _record_visibility_event(_S(), None, "参考图未附上")
