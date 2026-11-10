"""成品「一键去水印」的单元测试（2026-10-14）。

只测**不依赖 ffmpeg** 的部分：相对框换算、产物命名（绝不覆盖原产物）、
复用逻辑、以及源文件缺失时的明确报错。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from feishu_generation_agent.integrations.watermark_removal import (
    STANDARD_BOX_REL,
    output_path_for,
    remove_video_watermark,
    watermark_box,
)


def test_box_scales_with_resolution_and_stays_inside_frame() -> None:
    """720x1280 下应落在实测位置（x584..718 / y1230..1274 附近），且严格在画面内。

    实测教训：第一版框取窄了 20px（x614 起），用户当场发现「没去干净」——
    水印最左边的字压在会动的画面上，时间维方差抓不到它。**框必须吃余量**。
    """
    x, y, w, h = watermark_box(720, 1280)
    assert 570 <= x <= 590, f"左边界要盖住最左边的字，实际 x={x}"
    assert x + w >= 712, f"右边界要盖住整条水印，实际 x+w={x + w}"
    assert 1220 <= y <= 1240 and y + h >= 1270
    # delogo 要求框严格在画面内，否则会报 "Logo area is outside of the frame"
    assert x + w <= 720 - 1 and y + h <= 1280 - 1


def test_box_is_relative_so_larger_frames_scale() -> None:
    small = watermark_box(720, 1280)
    large = watermark_box(1440, 2560)
    assert large[0] == pytest.approx(small[0] * 2, abs=2)
    assert large[2] == pytest.approx(small[2] * 2, abs=2)
    assert large[0] + large[2] <= 1440 - 1


def test_output_never_overwrites_artifact(tmp_path: Path) -> None:
    """产物必须**另存** —— 原产物文件名是它的 sha256，改了会破坏产物校验。"""
    source = tmp_path / "e1b3013fd1508b731d42476c035f52095f8be8a59b3824072d06398e73f32c4e.mp4"
    source.write_bytes(b"x")
    target = output_path_for(source)
    assert target != source
    assert target.name.endswith("-nowatermark.mp4")
    assert target.parent == source.parent


def test_missing_source_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="源文件不存在"):
        remove_video_watermark(tmp_path / "nope.mp4")


def test_reuses_existing_output_without_reencoding(tmp_path: Path, monkeypatch) -> None:
    """已有更新的去水印版本时直接复用（不重复转码）。"""
    source = tmp_path / "orig.mp4"
    source.write_bytes(b"x")
    target = output_path_for(source)
    target.write_bytes(b"y")
    later = time.time() + 10
    import os

    os.utime(target, (later, later))
    monkeypatch.setattr(
        "feishu_generation_agent.integrations.watermark_removal._probe_size",
        lambda _path: (720, 1280),
    )
    result = remove_video_watermark(source)
    assert result.reused is True
    assert result.output == target
    assert result.box == watermark_box(720, 1280)


def test_standard_box_matches_measured_position() -> None:
    """把实测值钉住：720x1280 → x584 / y1230 / 134x44（去掉后字符图上无残留文字结构）。"""
    assert STANDARD_BOX_REL[0] == pytest.approx(584 / 720)
    assert STANDARD_BOX_REL[1] == pytest.approx(1230 / 1280)
