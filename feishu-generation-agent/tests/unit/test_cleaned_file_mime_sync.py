"""换成「去水印清洗版」时必须同步 mime_type —— 2026-10-27 生产事故（generation_mime_mismatch）。

**事故**：两条生产 run（`13a795f4` / `fcc30516`）报
「任务 task-1 生成失败（generation_mime_mismatch）：参考素材无法用于生成」。
查下来：素材记录是 `image/jpeg`，而**清洗版的实际字节是 PNG** ——
提交时拼的 data URL 是 `data:{asset.mime_type};base64,...`，于是"声明 jpeg、字节 PNG"，
方舟在提交阶段校验真实格式，直接拒。

**为什么清洗版是 PNG**：去水印走本机图片应用（图生图），产出**恒定是 PNG**，
而文件名沿用原后缀（`xxx-nowm.jpg`），`_with_cleaned_file` 又只改了
`local_path/size/sha256`、**没改 `mime_type`**。

本测试钉住：换副本后 `mime_type` 必须与真实字节一致。
"""

from __future__ import annotations

from pathlib import Path

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.graph import nodes

#: 1×1 PNG / JPEG 的真实字节（只用 magic 判断，不需要是真图）
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
JPEG_BYTES = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x00" * 32


def _asset(path: Path, mime: str) -> MediaAsset:
    return MediaAsset(
        asset_id="image-1",
        source_block_id="blk",
        origin="feishu",
        local_path=path,
        mime_type=mime,
        size=len(path.read_bytes()),
        sha256="0" * 64,
    )


def test_jpeg_asset_gets_png_cleaned_file_switches_mime(tmp_path: Path) -> None:
    """jpeg 素材 + PNG 清洗版 → mime 必须变成 image/png（否则就是那两次生产失败）。"""
    original = tmp_path / "ref.jpg"
    original.write_bytes(JPEG_BYTES)
    cleaned = tmp_path / "ref-nowm.jpg"          # 名字还是 .jpg（图片模型产出是 PNG）
    cleaned.write_bytes(PNG_BYTES)

    out = nodes._with_cleaned_file(_asset(original, "image/jpeg"), cleaned)

    assert out.local_path == cleaned
    assert out.mime_type == "image/png", (
        "声明必须跟着真实字节走：否则 data URL 会写成 image/jpeg 而字节是 PNG，"
        "方舟回 generation_mime_mismatch"
    )
    assert out.size == len(PNG_BYTES)
    assert out.sha256 != "0" * 64, "sha256 要跟着换"


def test_same_format_keeps_declared_mime(tmp_path: Path) -> None:
    """清洗版与原图同格式时，mime 不变（不要无谓改写）。"""
    original = tmp_path / "ref.jpg"
    original.write_bytes(JPEG_BYTES)
    cleaned = tmp_path / "ref-nowm.jpg"
    cleaned.write_bytes(JPEG_BYTES)

    out = nodes._with_cleaned_file(_asset(original, "image/jpeg"), cleaned)
    assert out.mime_type == "image/jpeg"


def test_unknown_bytes_keep_declared_mime(tmp_path: Path) -> None:
    """认不出的字节不硬猜，沿用原声明（宁可维持旧行为，也不要写错类型）。"""
    original = tmp_path / "ref.jpg"
    original.write_bytes(JPEG_BYTES)
    cleaned = tmp_path / "ref-nowm.jpg"
    cleaned.write_bytes(b"NOT-AN-IMAGE-AT-ALL")

    out = nodes._with_cleaned_file(_asset(original, "image/jpeg"), cleaned)
    assert out.mime_type == "image/jpeg"


def test_sniff_recognises_the_common_formats() -> None:
    assert nodes._sniff_image_mime(PNG_BYTES) == "image/png"
    assert nodes._sniff_image_mime(JPEG_BYTES) == "image/jpeg"
    assert nodes._sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert nodes._sniff_image_mime(b"GIF89a....") == "image/gif"
    assert nodes._sniff_image_mime(b"BM\x00\x00") == "image/bmp"
    assert nodes._sniff_image_mime(b"") is None


def _real_png(path: Path, width: int, height: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 90)).save(buffer, format="PNG")
    data = buffer.getvalue()
    path.write_bytes(data)
    return data


def _asset_with_size(path: Path, mime: str, width: int, height: int) -> MediaAsset:
    return MediaAsset(
        asset_id="image-1",
        source_block_id="blk",
        origin="feishu",
        local_path=path,
        mime_type=mime,
        size=len(path.read_bytes()),
        sha256="0" * 64,
        width=width,
        height=height,
    )


def test_cleaned_file_dimensions_are_read_from_the_real_file(tmp_path: Path) -> None:
    """**尺寸也要跟着真实文件走**（2026-11-05 像素事故的根因之一）。

    清洗是**整张重绘**，尺寸可以比原图小得多。实测那次运行：素材记录 193x261 的
    去水印版被换回来提交，而下游尺寸守卫判的是**旧尺寸**、以为已经达标 →
    放大版被绕过 → 上游拒 `image pixel count must be greater than or equal to 90000`。

    判别力：修复前这个方法只改 `local_path/size/sha256/mime`，宽高沿用旧记录 →
    下面的 `(193, 261) == (120, 90)` 断言必红。
    """
    original = tmp_path / "ref.png"
    _real_png(original, 800, 1200)
    cleaned = tmp_path / "ref-nowm.png"
    # 去水印版**比原图小得多**（正是事故形状）
    _real_png(cleaned, 120, 90)

    out = nodes._with_cleaned_file(_asset_with_size(original, "image/png", 800, 1200), cleaned)

    assert (out.width, out.height) == (120, 90), (
        "尺寸必须从真实文件读：否则守卫会按旧尺寸以为达标，把没放大的小图提交上去"
    )


def test_unreadable_cleaned_file_keeps_recorded_dimensions(tmp_path: Path) -> None:
    """读不出尺寸时不硬猜、也不炸，沿用原记录（增量能力不许把任务弄失败）。"""
    original = tmp_path / "ref.png"
    _real_png(original, 800, 1200)
    cleaned = tmp_path / "ref-nowm.png"
    cleaned.write_bytes(b"NOT-AN-IMAGE-AT-ALL")

    out = nodes._with_cleaned_file(_asset_with_size(original, "image/png", 800, 1200), cleaned)

    assert (out.width, out.height) == (800, 1200)
