import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
    VideoReferenceAnalysis,
    VideoReferenceKind,
)
from feishu_generation_agent.graph.nodes import (
    _materialize_video_references,
)
from feishu_generation_agent.integrations.video_reference import (
    extract_video_frames,
)
from feishu_generation_agent.storage.files import FileStore


def _make_video(path: Path) -> bytes:
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
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_extract_video_frames_evenly_distributes(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    _make_video(video)

    frames = extract_video_frames(video, 3, tmp_path / "frames")

    assert [frame.name for frame in frames] == [
        "frame-01.jpg",
        "frame-02.jpg",
        "frame-03.jpg",
    ]
    assert all(frame.is_file() and frame.stat().st_size > 0 for frame in frames)


class _FakeVideoVisionAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze_video(self, asset, frames):
        self.calls += 1
        return VideoReferenceAnalysis(
            asset_id=asset.asset_id,
            kind=VideoReferenceKind.CHARACTER,
            summary="人物形象参考：长发、红色上衣",
            representative_frame_index=2,
            uncertainties=[],
        )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
async def test_materialize_video_references_replaces_with_frame(
    tmp_path: Path,
):
    video = tmp_path / "sample.mp4"
    _make_video(video)
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
    file_store = FileStore(
        tmp_path / "data",
        tmp_path / "outputs",
        max_bytes=10 * 1024 * 1024,
    )
    analyzer = _FakeVideoVisionAnalyzer()
    services = SimpleNamespace(
        file_store=file_store,
        vision_analyzer=analyzer,
    )

    updated = await _materialize_video_references(document, services)

    assert analyzer.calls == 1
    assert len(updated.media_assets) == 1
    assert updated.media_assets[0].asset_id == "video-1-frame"
    assert updated.media_assets[0].mime_type.startswith("image/")
    assert len(updated.video_semantics) == 1
    assert updated.video_semantics[0].kind == VideoReferenceKind.CHARACTER
    assert updated.video_semantics[0].asset_id == "video-1-frame"
    assert "[image:video-1-frame]" in updated.text_view
    assert "[video:video-1]" not in updated.text_view

    assert updated.media_assets[0].local_path.is_file()
