"""「分辨率/时长脏值直达上游」的根治回归测试（2026-10-15）。

用户问「能根治吗」。结构性根因（不是单点 bug）：

* 计划期的能力归一 `normalize_video_task_payload`（`domain/video_models.py`）**只在
  「生成新计划 → 校验」这条路上跑**；
* 走「**复用上一版已批准计划**」（生产 run `05244f6c` 的事件原文：「已复用上一版已批准
  计划，未填返工要求，提示词原样保留」）或**审批页改过参数**时，**根本不经过它**；
* 于是脏值（生产真值 `resolution = "720*1280"`，星号）直达方舟 →
  `generation_invalid_resolution / Input validation failed`；
* 而 `_supported_resolution` 当时只把 `×` 换成 `x`，认不出星号 → 档位选择权也丢了。

根治 = **最后一道关卡**：Seedance 客户端 `submit()` 在构造 payload 之前，
按本通道能力表夹一次 `resolution` / `duration`。这样无论值来自计划、复用、还是人工编辑，
都不可能带病下发。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.domain.video_models import (
    VIDEO_MODEL_BY_KEY,
    _supported_resolution,
)
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _task(**kwargs) -> GenerationTask:
    base = dict(
        task_id="SEG-01-sandfall-perfume-truck",
        title="拯救香水",
        task_type="image_to_video",
        user_intent="拯救香水",
        source_block_ids=["blk"],
        prompt="镜头一",
        duration=16,
        aspect_ratio="9:16",
        resolution="720p",
    )
    base.update(kwargs)
    return GenerationTask(**base)


def _generator(model_key: str) -> SeedanceVideoGenerator:
    capability = VIDEO_MODEL_BY_KEY[model_key]
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    generator._capability = capability
    return generator


def test_pixel_star_resolution_is_clamped_before_submit() -> None:
    """生产真值：`720*1280`（星号）必须在提交前被夹成 `720p`。"""
    capability = VIDEO_MODEL_BY_KEY["seedance2.5"]
    assert _supported_resolution("720*1280", capability) == "720p"
    # 域模型本身也已归一（双保险）
    assert _task(resolution="720*1280").resolution == "720p"


def test_clamp_runs_even_when_task_was_hand_edited() -> None:
    """模拟「复用已批准计划 / 审批页改参」：绕过域校验直接塞脏值，提交前仍被夹住。"""
    generator = _generator("seedance2.5")
    dirty = _task()
    object.__setattr__(dirty, "resolution", "720*1280")  # 绕过校验器，模拟存量脏数据
    clamped = generator._clamp_task_to_capability(dirty)
    assert clamped.resolution == "720p"


def test_clamp_keeps_valid_values_untouched() -> None:
    generator = _generator("seedance2.5")
    task = _task(resolution="1080p")
    assert generator._clamp_task_to_capability(task) is task


def test_clamp_duration_to_capability_range() -> None:
    """时长同样按能力表夹（上游对时长也有硬上限）。"""
    generator = _generator("seedance2.5")
    capability = VIDEO_MODEL_BY_KEY["seedance2.5"]
    task = _task(duration=capability.duration_max + 30)
    assert generator._clamp_task_to_capability(task).duration == capability.duration_max


def test_clamp_is_noop_without_capability() -> None:
    """拿不到能力信息时不猜、不动（保持既有行为）。"""
    generator = SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)
    generator._capability = None
    task = _task(resolution="720*1280")
    assert generator._clamp_task_to_capability(task) is task


def test_submit_calls_the_clamp() -> None:
    """接线守门：`submit` 必须在构造 payload 前调用夹取（否则守门是死代码）。"""
    import inspect

    from feishu_generation_agent.integrations import seedance

    source = inspect.getsource(seedance.SeedanceVideoGenerator.submit)
    assert "_clamp_task_to_capability" in source
    assert source.index("_clamp_task_to_capability") < source.index('"model": self._model')
