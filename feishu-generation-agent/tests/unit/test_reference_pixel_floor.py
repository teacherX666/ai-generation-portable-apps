"""提交前**最后一道**尺寸保障：参考图总像素必须 ≥90,000（2026-11-05 生产事故）。

用户报：「还是失败啊，我第一版视频都能正常生成，现在为什么不行了」，报错原文：

    The parameter `content[6]` specified in the request is not valid: the parameter
    image pixel count specified in the request must be greater than or equal to
    90000 for model doubao-seedance-2-5 in r2v（submit_invalidparameter）

**为什么修了一次还在犯**：第一版只在 `graph/nodes.py` 的「执行前整理素材」
（`_upscale_reference`）里加了像素判据，但**提交路径上还有环节会替换素材** ——
`nodes._clean_media_assets` 在提交前把素材换成台账里的**去水印版**，而清洗是
**整张重绘**、尺寸可以比原图小得多。实测那次运行的真实文件：

| 文件 | 尺寸 | 像素 | 结论 |
|---|---|---|---|
| `4fbab…-nowm.png` | 190x147 | **27,930** | 去水印版，被换回来提交 |
| `4fbab…-upscaled.png` | 388x300 | 116,400 | 守卫生成过，但被上面那张绕过 |
| `fd046…-nowm.png` | 193x261 | **50,373** | 同上 |
| `fd046…-upscaled.png` | 300x406 | 121,800 | 同上 |

于是判据必须落在**真正要发出去的那份字节**上（`_ensure_reference_pixel_floor`），
而不是某个中间环节的素材记录上。
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image

from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _png(width: int, height: int, color: tuple[int, int, int] = (210, 140, 110)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _size(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        return image.size


def _guard(width: int, height: int) -> bytes:
    # 只测这一个纯方法：不建客户端、不连上游
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    return generator._ensure_reference_pixel_floor(
        SimpleNamespace(asset_id="image-x"), _png(width, height)
    )


def test_cleaned_version_below_floor_is_upscaled() -> None:
    """事故的真实形状：去水印版 193x261 = 50,373 像素，必须被放大到过线。

    判别力：在加 `_ensure_reference_pixel_floor` 之前，这个方法返回**原字节** →
    `_size(out) == (193, 261)` → 下面的像素断言必红。
    """
    out = _guard(193, 261)
    width, height = _size(out)
    assert width * height >= 90_000
    assert width >= 300 and height >= 300
    # 比例保持（193:261 ≈ 0.739）
    assert abs(width / height - 193 / 261) < 0.02


def test_tiny_cleaned_version_is_upscaled() -> None:
    """最小的一张：190x147 = 27,930 像素（差 3 倍多）。"""
    out = _guard(190, 147)
    width, height = _size(out)
    assert width * height >= 90_000
    assert width >= 300 and height >= 300


def test_image_already_at_the_floor_is_untouched() -> None:
    """300x300 正好 90,000 —— 过线就不动，不白转一次 PNG。"""
    raw = _png(300, 300)
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    out = generator._ensure_reference_pixel_floor(
        SimpleNamespace(asset_id="image-x"), raw
    )
    assert out is raw


def test_image_above_the_floor_is_untouched() -> None:
    raw = _png(1440, 2560)
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    out = generator._ensure_reference_pixel_floor(
        SimpleNamespace(asset_id="image-x"), raw
    )
    assert out is raw


def test_resized_image_still_passes_the_aspect_window() -> None:
    """放大后比例也不能越界 —— 这是紧挨着的另一条上游硬约束（0.39~2.50）。

    取一个"放大后取整会让长边/短边贴边"的极端比例来压这条路径。
    """
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    for width, height in ((193, 261), (190, 147), (400, 60), (60, 400)):
        out = generator._ensure_reference_pixel_floor(
            SimpleNamespace(asset_id="image-x"), _png(width, height)
        )
        out_width, out_height = _size(out)
        assert out_width * out_height >= 90_000, (width, height, out_width, out_height)
        ratio = out_width / out_height
        assert (
            generator._REFERENCE_ASPECT_MIN
            <= ratio
            <= generator._REFERENCE_ASPECT_MAX
        ), (width, height, ratio)


def test_broken_bytes_fall_back_to_the_original() -> None:
    """读不出图片就返回原字节 —— 绝不因为这道增量能力把任务弄失败。"""
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    raw = b"not-an-image"
    out = generator._ensure_reference_pixel_floor(
        SimpleNamespace(asset_id="image-x"), raw
    )
    assert out == raw
