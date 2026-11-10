"""「去水印」不许变成「涂黑」（2026-10-28 用户报：前两张有水印的图右下角被替换成纯黑）。

查下来的链路：那三张（10-28 13:23）是**图片模型整张重绘**的产物 —— 它被要求去掉右下角水印，
结果把那一块**涂成了纯黑**；而台账里 `status=kept`（被护栏拒）却 `decision=cleaned`
（用户早点过"用清洗版"）→ 按"用户选择压倒护栏"的规则，黑角版本照样生效。

实测校准（240 宽采样，"原图>70 且 清洗后<26"的占比）：
* 三张被涂黑的真实成品 → 0.135% / 0.171% / 0.187%（必须拒）
* 两版正常清洗 → 0.000%（必须放行）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from feishu_generation_agent.integrations import reference_watermark_clean as module


def _original(path: Path) -> Path:
    """中间一块明亮、结构丰富；角落也有一小块偏亮区域（水面/UI 那种）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (400, 700), (40, 40, 44))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 120, 360, 420), fill=(210, 200, 190))
    for i in range(40):  # 细节
        y = 130 + i * 7
        draw.line((60, y, 340, y + 3), fill=(120, 110, 100))
    draw.rectangle((300, 620, 380, 680), fill=(180, 175, 170))  # 角落那块"亮"
    image.save(path)
    return path


def test_black_patch_guard_rejects_painted_black(tmp_path: Path) -> None:
    original = _original(tmp_path / "ref.png")
    painted = tmp_path / "ref-nowm.png"
    image = Image.open(original).copy()
    ImageDraw.Draw(image).rectangle((300, 620, 380, 680), fill=(0, 0, 0))  # 涂黑
    image.save(painted)

    ratio = module._black_patch_ratio(original, painted)
    assert ratio is not None and ratio > module._BLACK_PATCH_MAX_RATIO, (
        f"把亮处涂黑必须被抓到（实测 {ratio}）"
    )


def test_black_patch_guard_passes_a_normal_cleaning(tmp_path: Path) -> None:
    original = _original(tmp_path / "ref.png")
    cleaned = tmp_path / "ref-nowm.png"
    Image.open(original).save(cleaned)  # 没动过任何像素
    ratio = module._black_patch_ratio(original, cleaned)
    assert ratio == 0.0


def test_black_patch_guard_is_skipped_for_different_sizes(tmp_path: Path) -> None:
    original = _original(tmp_path / "ref.png")
    other = tmp_path / "other.png"
    Image.new("RGB", (200, 350), (0, 0, 0)).save(other)
    assert module._black_patch_ratio(original, other) is None


def test_black_patch_guard_is_wired_before_adoption(tmp_path: Path) -> None:
    """接线守门：这道判据要在"采用"之前跑到，且判拒时标记 `content_lost`（不可被选定）。"""
    import inspect

    source = inspect.getsource(module._model_clean_reference)
    assert "_black_patch_ratio" in source
    assert source.index("_black_patch_ratio") < source.index("已去水印并采用")
