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
    choose_reference_frame,
    extract_video_frames,
)
from feishu_generation_agent.storage.files import FileStore


def _plain_frame(path: Path, color: str = "gray") -> Path:
    from PIL import Image

    Image.new("RGB", (240, 180), color).save(path, format="JPEG")
    return path


def _watermarked_frame(path: Path) -> Path:
    """在下角画一个高对比「水印块」——模拟小红书 / 即梦这类角标。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 180), "gray")
    draw = ImageDraw.Draw(image)
    for offset in range(0, 40, 4):
        draw.rectangle(
            [8 + offset, 150, 12 + offset, 174], fill="white" if offset % 8 else "black"
        )
        draw.rectangle(
            [200, 150 + offset // 4, 236, 152 + offset // 4], fill="white"
        )
    image.save(path, format="JPEG")
    return path


def test_choose_reference_frame_avoids_the_edges(tmp_path: Path) -> None:
    """兜底抽帧时**不要首尾帧**（账号卡 / 黑场 / 平台结尾页都在两头）。"""
    paths = [
        _plain_frame(tmp_path / f"frame-{index:02d}.jpg")
        for index in range(1, 6)
    ]

    choice = choose_reference_frame(paths)

    assert choice.index == 3  # 5 帧 → 首尾各排 1 帧，取最靠近中间的那一帧
    assert "避开首尾" in choice.reason


def test_choose_reference_frame_skips_the_watermarked_frame(tmp_path: Path) -> None:
    """候选里既有干净帧也有带角标水印的帧时，必须选干净的那一帧。

    生产实测（刮太空沙 run 4154013f）：兜底抽的第 1 帧带小红书水印，成片结尾因此
    复现了水印来源的主体。这里保证「有干净帧时不会选脏帧」。
    """
    paths = [
        _watermarked_frame(tmp_path / "frame-01.jpg"),
        _watermarked_frame(tmp_path / "frame-02.jpg"),
        _plain_frame(tmp_path / "frame-03.jpg"),
        _watermarked_frame(tmp_path / "frame-04.jpg"),
        _watermarked_frame(tmp_path / "frame-05.jpg"),
    ]

    choice = choose_reference_frame(paths)

    assert choice.index == 3
    assert "没有平台水印" in choice.reason
    assert "避开首尾" in choice.reason


def test_choose_reference_frame_with_a_single_frame(tmp_path: Path) -> None:
    paths = [_plain_frame(tmp_path / "frame-01.jpg")]

    assert choose_reference_frame(paths).index == 1


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
async def test_materialize_video_references_only_adds_semantics(
    tmp_path: Path,
):
    """`analyze_images` 自 2026-09-17 起**不再抽帧**：视频只补分镜参考描述，素材本体不动。

    抽帧改到了**执行前**（`graph/nodes.py` 的 `_replace_video_references_with_frames`，
    2026-09-24 修复「参考视频被摘掉 → 零产物静默完成」时补回）。本用例原先断言
    `media_assets[0].asset_id == "video-1-frame"`，那是已删除的旧行为，本机因为
    ffmpeg 不在 PATH 上一直 skip，所以没暴露。
    """
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
        video_analyzer=analyzer,
    )

    updated = await _materialize_video_references(document, services)

    assert analyzer.calls == 1
    assert [asset.asset_id for asset in updated.media_assets] == ["video-1"]
    assert updated.media_assets[0].mime_type == "video/mp4"
    assert len(updated.video_semantics) == 1
    assert updated.video_semantics[0].kind == VideoReferenceKind.CHARACTER
    assert updated.video_semantics[0].asset_id == "video-1"
    assert "[video:video-1]" in updated.text_view
