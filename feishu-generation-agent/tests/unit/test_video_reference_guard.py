"""视频不能当参考图：执行前拦掉，并把火山失败原因带出来。"""

import json
from pathlib import Path

import httpx

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import ErrorCategory
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


class _Repository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def append_event(self, run_id, node, status, summary) -> None:
        self.events.append((node, status, summary))


class _Services:
    def __init__(self) -> None:
        self.repository = _Repository()


def _asset(
    tmp_path: Path,
    asset_id: str,
    mime: str,
    *,
    width: int = 0,
    height: int = 0,
) -> MediaAsset:
    suffix = ".mp4" if mime.startswith("video/") else ".png"
    path = tmp_path / f"{asset_id}{suffix}"
    path.write_bytes(b"x")
    return MediaAsset(
        asset_id=asset_id,
        source_block_id="b1",
        origin="upload",
        local_path=path,
        mime_type=mime,
        size=1,
        sha256=f"sha-{asset_id}",
        width=width,
        height=height,
    )


def _png(tmp_path: Path, asset_id: str, width: int, height: int) -> MediaAsset:
    """写一张真实的小 PNG（放大逻辑要走 Pillow，假的字节流会失败）。"""
    from PIL import Image as _Image

    path = tmp_path / f"{asset_id}.png"
    _Image.new("RGB", (width, height), (200, 120, 90)).save(path, format="PNG")
    return MediaAsset(
        asset_id=asset_id,
        source_block_id="b1",
        origin="feishu",
        local_path=path,
        mime_type="image/png",
        size=path.stat().st_size,
        sha256=f"sha-{asset_id}",
        width=width,
        height=height,
    )


async def test_too_small_image_is_upscaled_not_dropped(tmp_path: Path) -> None:
    """火山要求参考图每边 ≥300px；用户口径（2026-09-18）：「不要拦截，自动等比例放大」。

    实测报错：「expected the height to be at least 300px, but received a 384x254px
    image」—— 文档里的横幅被当参考图提交，整单被拒。
    """
    from feishu_generation_agent.graph.nodes import _without_unusable_references

    services = _Services()
    assets = [
        _png(tmp_path, "image-ok", 1440, 2560),
        _png(tmp_path, "banner-small", 384, 254),
    ]

    kept = await _without_unusable_references(assets, "run-1", services)

    # 一张都不丢
    assert [asset.asset_id for asset in kept] == ["image-ok", "banner-small"]
    fixed = kept[1]
    assert fixed.local_path != assets[1].local_path  # 用了放大后的副本
    assert fixed.height >= 300 and fixed.width >= 300
    # 比例保持（384:254 ≈ 1.512）
    assert abs(fixed.width / fixed.height - 384 / 254) < 0.02
    summary = services.repository.events[0][2]
    assert "banner-small" in summary and "等比放大" in summary


async def test_extreme_aspect_ratio_is_not_blocked(tmp_path: Path) -> None:
    """比例问题放大也改不了，照旧提交，交给供应商判断（用户要求不要拦截）。"""
    from feishu_generation_agent.graph.nodes import _without_unusable_references

    services = _Services()
    assets = [_png(tmp_path, "wide", 1900, 228)]

    kept = await _without_unusable_references(assets, "run-1", services)

    assert [asset.asset_id for asset in kept] == ["wide"]
    assert kept[0].height >= 300  # 高度被抬到下限


async def test_unknown_size_is_not_blocked(tmp_path: Path) -> None:
    """拿不到尺寸就不要乱拦，交给供应商判断。"""
    from feishu_generation_agent.graph.nodes import _without_unusable_references

    services = _Services()
    assets = [_asset(tmp_path, "image-1", "image/png")]

    kept = await _without_unusable_references(assets, "run-1", services)

    assert [asset.asset_id for asset in kept] == ["image-1"]
    assert services.repository.events == []


async def test_video_reference_is_dropped_with_an_event(tmp_path: Path) -> None:
    from feishu_generation_agent.graph.nodes import _without_video_references

    services = _Services()
    assets = [
        _asset(tmp_path, "image-1", "image/png"),
        _asset(tmp_path, "upload-abc", "video/mp4"),
        _asset(tmp_path, "image-2", "image/jpeg"),
    ]

    kept = await _without_video_references(assets, "run-1", services)

    assert [asset.asset_id for asset in kept] == ["image-1", "image-2"]
    assert services.repository.events
    node, status, summary = services.repository.events[0]
    assert (node, status) == ("execute_selected_tasks", "running")
    assert "视频" in summary


async def test_no_event_when_there_is_no_video(tmp_path: Path) -> None:
    from feishu_generation_agent.graph.nodes import _without_video_references

    services = _Services()
    assets = [_asset(tmp_path, "image-1", "image/png")]

    kept = await _without_video_references(assets, "run-1", services)

    assert [asset.asset_id for asset in kept] == ["image-1"]
    assert services.repository.events == []


def test_terminal_status_error_carries_provider_reason() -> None:
    """任务失败时要把火山的 code/message 带出来，而不是只报 poll_http_failed。"""
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)

    error = generator._terminal_status_error(
        "poll",
        "failed",
        provider_error={
            "code": "InvalidParameter",
            "message": "input media detect failed: invalid_media",
        },
    )

    assert error.detail.category is ErrorCategory.PROVIDER_TERMINAL
    assert "invalid_media" in error.detail.message
    assert "provider_code=InvalidParameter" in error.detail.technical_detail
    assert "provider_message=input media detect failed" in error.detail.technical_detail


def test_terminal_status_error_without_provider_error_stays_generic() -> None:
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)

    error = generator._terminal_status_error("poll", "failed")

    assert error.detail.message == "Seedance 视频任务未成功完成"
    assert "provider_message" not in error.detail.technical_detail


def test_terminal_status_error_redacts_secret_like_text() -> None:
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)

    error = generator._terminal_status_error(
        "poll",
        "failed",
        provider_error={
            "code": "AuthenticationError",
            "message": "bad key ark-REDACTED",
        },
    )

    assert "ark-REDACTED" not in json.dumps(error.detail.message)
