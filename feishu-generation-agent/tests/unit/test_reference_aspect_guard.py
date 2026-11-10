"""参考图**长宽比超范围**要自动补边，不能把任务判死（2026-11-02 生产事故）。

用户报：「生成失败：… Error while downloading image, error: expected the aspect ratio to be
between 0.39 and 2.50, but received image with aspect ratio: 2.76 （submit_invalidparameter）」。

实测那批参考图（文档里的分镜表切片）：
`411×1135`（w/h=0.362、长边/短边 **2.762** ← 报错里那个 2.76）、`280×766`（0.366 / 2.736）、
`640×2004`（0.319 / 3.131）—— 全是"细长条"，一提交就被上游拒。

判据（与代码同源）：`_REFERENCE_ASPECT_MIN/MAX`。**补边不裁剪** —— 这些图里是用户写的分镜内容，
裁掉两头等于丢内容；补边只加背景色（取四边中位色）。
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image

from feishu_generation_agent.integrations.seedance import (
    SeedanceVideoGenerator,
    _border_median_color,
)


def _png(width: int, height: int, color: tuple[int, int, int] = (240, 240, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _generator() -> SeedanceVideoGenerator:
    # 只测这一个纯方法：不建客户端、不连上游
    return SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)


def _fit(width: int, height: int) -> tuple[bytes, tuple[int, int]]:
    generator = _generator()
    raw = _png(width, height)
    out = generator._fit_reference_aspect_ratio(
        SimpleNamespace(asset_id="image-x"), raw
    )
    with Image.open(io.BytesIO(out)) as image:
        return out, image.size


def test_ultra_tall_reference_is_padded_into_range() -> None:
    """细长条（长边/短边 2.76 —— 报错那张的真实形状）必须被补到区间内。"""
    generator = _generator()
    out, (width, height) = _fit(411, 1135)
    ratio = width / height
    assert generator._REFERENCE_ASPECT_MIN <= ratio <= generator._REFERENCE_ASPECT_MAX
    assert ratio > 0.39 and ratio < 2.50          # 上游硬边界（留有余量）
    assert height == 1135                          # 内容两向都没被裁
    assert width > 411                             # 只加了侧边


def test_even_taller_reference_is_padded() -> None:
    """640×2004（长边/短边 3.13）同样要能过。"""
    generator = _generator()
    _, (width, height) = _fit(640, 2004)
    assert generator._REFERENCE_ASPECT_MIN <= width / height
    assert height == 2004


def test_ultra_wide_reference_is_padded() -> None:
    """反方向（超宽横幅）也要补 —— 同一约束的两侧。"""
    generator = _generator()
    _, (width, height) = _fit(2400, 600)
    assert width / height <= generator._REFERENCE_ASPECT_MAX
    assert width == 2400


def test_legal_reference_is_untouched() -> None:
    """本来就合法的图**一个字节都不动**（去水印/清洗链路已经定稿，别在这里又改一遍）。"""
    generator = _generator()
    raw = _png(1015, 2253)                        # 0.451，合法
    out = generator._fit_reference_aspect_ratio(
        SimpleNamespace(asset_id="image-ok"), raw
    )
    assert out is raw


def test_broken_bytes_fall_back_to_original() -> None:
    """坏图不能因为"补边"把任务弄失败：原样返回，交给后面的内容校验报错。"""
    generator = _generator()
    broken = b"not an image"
    out = generator._fit_reference_aspect_ratio(
        SimpleNamespace(asset_id="image-bad"), broken
    )
    assert out is broken


def test_border_median_color_ignores_a_stray_pixel() -> None:
    """补边底色取四边中位色：一个离群点不该让整条边变色。"""
    image = Image.new("RGB", (10, 10), (200, 200, 200))
    image.putpixel((0, 0), (0, 0, 0))
    assert _border_median_color(image) == (200, 200, 200)
