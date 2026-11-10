"""「带参考视频的任务比例被参考视频带偏」的回归测试（2026-10-16）。

用户报：「为什么西兰花那个视频不是 9.16」。

实测（生产两两对照）：
* run `83ec21ff`：任务 `aspect_ratio=9:16`，带**横屏参考视频 958×702** →
  成片 **1112×834**（横屏）✗（1112/834 = 1.333 ≈ 参考视频比例 ✗）；
* run `f82c8813` / `732b2bd5`：只有参考图 → 成片 **720×1280** ✓。

根因：方舟硬约束「**带参考视频的任务 `ratio` 只能 `adaptive`，输出比例跟随输入的
参考视频**」（不这样做直接 400，见 `_apply_frame_ratio_constraint`）。
修法：提交前把参考视频**裁成任务画幅**（cover 居中），`adaptive` 才会输出正确比例。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.integrations.video_reference import _parse_aspect_ratio
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9:16", 9 / 16),
        ("16:9", 16 / 9),
        ("1:1", 1.0),
        ("9：16", 9 / 16),          # 全角冒号
        ("adaptive", None),          # 自适应 → 不做画幅归一
        ("auto", None),
        ("", None),
        (None, None),
        ("abc", None),
        ("0:16", None),
    ],
)
def test_parse_aspect_ratio(raw, expected) -> None:
    assert _parse_aspect_ratio(raw) == expected


def test_reframe_is_wired_into_submit_with_task_ratio() -> None:
    """接线守门：提交前必须带着**任务比例**调用参考视频画幅归一。"""
    import inspect

    from feishu_generation_agent.integrations import seedance

    submit_source = inspect.getsource(seedance.SeedanceVideoGenerator.submit)
    assert "aspect_ratio=task.aspect_ratio" in submit_source, (
        "参考视频画幅归一必须拿到任务比例，否则等于没接"
    )
    normalize_source = inspect.getsource(
        seedance.SeedanceVideoGenerator._normalize_video_references
    )
    assert "reframe_reference_video" in normalize_source


def test_real_landscape_reference_is_cropped_to_portrait(tmp_path: Path) -> None:
    """用生产那条真实横屏参考视频验证（存在才跑，不依赖测试环境）。"""
    base = Path(__file__).resolve().parents[2]
    source = base / (
        "data/runs/KbPGdvVmjop58yxW77HcP0AZnXf/inputs/"
        "a53c83079bb294f562c9d6255efad140ac6f4a6c5104bb6b49fd2ac2a16eacbd.mp4"
    )
    if not source.is_file():
        pytest.skip("生产参考视频不在本机（换机器跑测试时正常）")
    from feishu_generation_agent.integrations.video_reference import (
        probe_video_info,
        reframe_reference_video,
    )

    before = probe_video_info(source)
    assert before is not None and before.width > before.height, "这条参考视频本来是横屏"
    out = reframe_reference_video(source, "9:16")
    assert out is not None, "横屏参考视频必须被裁成 9:16"
    after = probe_video_info(out)
    assert after is not None
    assert abs(after.width / after.height - 9 / 16) < 0.01
    # 幂等：已经 9:16 的不再重编码
    assert reframe_reference_video(out, "9:16") is None


def test_media_asset_update_keeps_hash_consistent() -> None:
    """裁切后必须同步 local_path/size/sha256（产物校验依赖 sha256）。"""
    import inspect

    from feishu_generation_agent.integrations import seedance

    block = inspect.getsource(seedance.SeedanceVideoGenerator._normalize_video_references)
    assert '"sha256": sha256(data).hexdigest()' in block
