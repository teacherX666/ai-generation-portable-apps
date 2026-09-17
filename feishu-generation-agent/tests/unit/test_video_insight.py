"""参考视频直接送 ds4.1 分析（不抽帧）—— 请求形状与解析。"""

import json
from pathlib import Path

import pytest

from feishu_generation_agent.domain.document import MediaAsset, VideoReferenceKind
from feishu_generation_agent.integrations.video_insight import (
    DeepSeekVideoInsight,
    describe_output_videos,
)


class _Insight:
    def __init__(self, summary: str, uncertainties: list[str] | None = None) -> None:
        self.summary = summary
        self.uncertainties = uncertainties or []


class _Analyzer:
    def __init__(self, summary: str = "画面里出现了一个人物形象。") -> None:
        self.summary = summary
        self.calls: list[str] = []

    async def analyze_video(self, asset, frames=None):
        self.calls.append(asset.asset_id)
        if self.summary == "boom":
            raise RuntimeError("分析失败")
        return _Insight(self.summary, ["光照不确定"])


def _artifact(tmp_path: Path, artifact_id: str = "art-1", kind: str = "video") -> dict:
    path = tmp_path / f"{artifact_id}.mp4"
    path.write_bytes(b"video")
    return {
        "artifact_id": artifact_id,
        "task_id": "task-1",
        "kind": kind,
        "local_path": str(path),
        "mime_type": "video/mp4",
        "size": 5,
        "sha256": "sha",
        "status": "ready",
    }


async def test_describe_output_videos_feeds_previous_take_to_the_model(
    tmp_path: Path,
) -> None:
    """返工时把上一版成片直接送模型（不抽帧），产出「实际画面」上下文。"""
    analyzer = _Analyzer()

    context = await describe_output_videos(
        analyzer,
        [_artifact(tmp_path), _artifact(tmp_path, "art-2", kind="image")],
    )

    assert analyzer.calls == ["art-1"]  # 只送视频，图片产物不是成片
    assert "画面里出现了一个人物形象" in context
    assert "不确定" in context


async def test_describe_output_videos_is_empty_without_analyzer_or_video(
    tmp_path: Path,
) -> None:
    assert await describe_output_videos(None, [_artifact(tmp_path)]) == ""
    assert await describe_output_videos(_Analyzer(), []) == ""
    assert (
        await describe_output_videos(
            _Analyzer(), [_artifact(tmp_path, kind="image")]
        )
        == ""
    )


async def test_describe_output_videos_swallows_analysis_failure(
    tmp_path: Path,
) -> None:
    """分析失败不能让返工本身失败 —— 返回空上下文，返工照常。"""
    assert await describe_output_videos(_Analyzer("boom"), [_artifact(tmp_path)]) == ""


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Http:
    def __init__(self, payload: dict) -> None:
        self.calls: list[dict] = []
        self._payload = payload

    async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _Response(self._payload)


class _Host:
    def __init__(self, url: str = "https://example.invalid/ref.mp4") -> None:
        self.url = url
        self.uploads: list[tuple[int, str, str]] = []

    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        self.uploads.append((len(content), filename, mime_type))
        return self.url


def _asset(tmp_path: Path, *, mime_type: str = "video/mp4") -> MediaAsset:
    path = tmp_path / "ref.mp4"
    path.write_bytes(b"fictional-video-bytes")
    return MediaAsset(
        asset_id="video-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=path,
        mime_type=mime_type,
        size=path.stat().st_size,
        sha256="sha-video-1",
    )


def _body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


async def test_analyze_video_sends_video_url_not_frames(tmp_path: Path) -> None:
    """整段视频走 video_url（实测 ds4.1 认这个字段），不是抽帧塞 image。"""
    host = _Host()
    http = _Http(_body(json.dumps({
        "kind": "camera_movement",
        "summary": "镜头从近景推近，人物抬头。",
        "uncertainties": ["光照变化"],
    })))
    insight = DeepSeekVideoInsight(
        http,
        base_url="https://ark.example/api/v3",
        api_key="ark-test",
        model="deepseek-v4-1-flash-260910",
        public_media_host=host,
    )

    result = await insight.analyze_video(_asset(tmp_path))

    # 视频先传到公开图床（不外传就送不了），拿到 https 链接。
    assert host.uploads == [(21, "video-1.mp4", "video/mp4")]
    request = http.calls[0]
    assert request["url"] == "https://ark.example/api/v3/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer ark-test"
    content = request["json"]["messages"][1]["content"]
    assert {"type": "video_url", "video_url": {"url": host.url}} in content
    assert all(part["type"] != "image_url" for part in content)
    assert request["json"]["model"] == "deepseek-v4-1-flash-260910"

    assert result.asset_id == "video-1"
    assert result.kind is VideoReferenceKind.CAMERA_MOVEMENT
    assert "推近" in result.summary
    assert result.uncertainties == ["光照变化"]


async def test_analyze_video_raises_on_bad_payload(tmp_path: Path) -> None:
    insight = DeepSeekVideoInsight(
        _Http(_body("不是 JSON")),
        base_url="https://ark.example/api/v3",
        api_key="ark-test",
        model="m",
        public_media_host=_Host(),
    )

    with pytest.raises(RuntimeError):
        await insight.analyze_video(_asset(tmp_path))


async def test_analyze_video_falls_back_to_other_kind(tmp_path: Path) -> None:
    insight = DeepSeekVideoInsight(
        _Http(_body(json.dumps({"kind": "没这个类型", "summary": "  描述  "}))),
        base_url="https://ark.example/api/v3",
        api_key="ark-test",
        model="m",
        public_media_host=_Host(),
    )

    result = await insight.analyze_video(_asset(tmp_path))

    assert result.kind is VideoReferenceKind.OTHER
    assert result.summary == "描述"


async def test_analyze_video_rejects_failed_download(tmp_path: Path) -> None:
    asset = _asset(tmp_path).model_copy(update={"download_error": "boom"})
    insight = DeepSeekVideoInsight(
        _Http(_body("{}")),
        base_url="https://ark.example/api/v3",
        api_key="ark-test",
        model="m",
        public_media_host=_Host(),
    )

    with pytest.raises(RuntimeError):
        await insight.analyze_video(asset)
