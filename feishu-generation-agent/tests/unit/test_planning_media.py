"""多模态规划的媒体准备：顺序、缓存、失败兜底。"""

from pathlib import Path

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.planning_media import (
    MediaUploadCache,
    build_planning_media_parts,
)


class _Host:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for or set()

    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        self.calls.append(filename)
        if filename.startswith(tuple(self._fail_for)):
            from feishu_generation_agent.integrations.public_media import (
                PublicMediaUploadError,
            )

            raise PublicMediaUploadError("boom")
        return f"https://example.invalid/{filename}"


def _asset(tmp_path: Path, asset_id: str, mime: str, sha: str) -> MediaAsset:
    suffix = ".mp4" if mime.startswith("video/") else ".png"
    path = tmp_path / f"{asset_id}{suffix}"
    path.write_bytes(b"bytes-" + asset_id.encode())
    return MediaAsset(
        asset_id=asset_id,
        source_block_id="block-1",
        origin="feishu",
        local_path=path,
        mime_type=mime,
        size=path.stat().st_size,
        sha256=sha,
    )


async def test_parts_follow_document_order_and_split_by_kind(tmp_path: Path) -> None:
    """按文档顺序产出：图片走 image_url、视频走 video_url。"""
    cache = MediaUploadCache(tmp_path / "cache.json")
    host = _Host()
    assets = [
        _asset(tmp_path, "video-1", "video/mp4", "sha-video"),
        _asset(tmp_path, "image-1", "image/png", "sha-1"),
        _asset(tmp_path, "image-2", "image/jpeg", "sha-2"),
    ]

    parts = await build_planning_media_parts(
        assets, public_media_host=host, cache=cache
    )

    assert [part["type"] for part in parts] == [
        "video_url",
        "image_url",
        "image_url",
    ]
    assert parts[0]["video_url"]["url"].endswith("video-1.mp4")
    assert parts[1]["image_url"]["url"].endswith("image-1.png")


async def test_second_call_reuses_cached_links(tmp_path: Path) -> None:
    """同一个素材第二次不再重传（实测 11 个媒体重传要 21.5 秒）。"""
    cache_path = tmp_path / "cache.json"
    assets = [_asset(tmp_path, "image-1", "image/png", "sha-1")]
    first_host = _Host()
    await build_planning_media_parts(
        assets, public_media_host=first_host, cache=MediaUploadCache(cache_path)
    )
    assert first_host.calls == ["image-1.png"]

    second_host = _Host()
    parts = await build_planning_media_parts(
        assets, public_media_host=second_host, cache=MediaUploadCache(cache_path)
    )

    assert second_host.calls == []  # 命中缓存
    assert parts[0]["image_url"]["url"].endswith("image-1.png")


async def test_failed_upload_is_skipped_not_fatal(tmp_path: Path) -> None:
    cache = MediaUploadCache(tmp_path / "cache.json")
    host = _Host(fail_for={"image-1"})
    assets = [
        _asset(tmp_path, "image-1", "image/png", "sha-1"),
        _asset(tmp_path, "image-2", "image/png", "sha-2"),
    ]

    parts = await build_planning_media_parts(
        assets, public_media_host=host, cache=cache
    )

    assert len(parts) == 1
    assert parts[0]["image_url"]["url"].endswith("image-2.png")


async def test_parts_skip_download_failures_and_cap_items(tmp_path: Path) -> None:
    cache = MediaUploadCache(tmp_path / "cache.json")
    assets = [
        _asset(tmp_path, "image-1", "image/png", "sha-1").model_copy(
            update={"download_error": "boom"}
        ),
        *[
            _asset(tmp_path, f"image-{index}", "image/png", f"sha-{index}")
            for index in range(2, 7)
        ],
    ]

    parts = await build_planning_media_parts(
        assets, public_media_host=_Host(), cache=cache, max_items=3
    )

    assert len(parts) == 3
    assert parts[0]["image_url"]["url"].endswith("image-2.png")
