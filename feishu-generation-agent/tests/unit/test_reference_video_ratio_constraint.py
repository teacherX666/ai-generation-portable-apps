"""「带参考视频的任务必须 ratio=adaptive」的回归测试（2026-10-15）。

生产事故：用户报 `Seedance 2.5：生成服务拒绝了请求：Bad Request …（poll_invalidparameter）`。
同一文档实测对照：12 秒**纯图片**成功；22 秒 + **参考视频**连败三次。

根因：方舟**按提示词**把带参考视频的任务判成「视频延长/编辑」，而这类任务 `ratio` 只支持
`adaptive`；客户端原来只对「首帧/首尾帧」角色做归一，`reference_video` 漏了 → 始终下发 `9:16` → 400。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _task(aspect: str = "9:16") -> GenerationTask:
    return GenerationTask(
        task_id="T1",
        title="测试",
        task_type="image_to_video",
        user_intent="测试用",
        source_block_ids=["blk"],
        prompt="镜头缓慢推进",
        duration=22,
        aspect_ratio=aspect,
        resolution="720p",
    )


def _generator() -> SeedanceVideoGenerator:
    # 只需要调用这个纯函数式的归一方法，不必真连上游
    return SeedanceVideoGenerator.__new__(SeedanceVideoGenerator)


def test_reference_video_forces_adaptive_ratio() -> None:
    """带参考视频 → 必须改写成 adaptive（否则方舟按 video extension 校验直接 400）。"""
    task = _task()
    references = [SimpleNamespace(role="reference_video"), SimpleNamespace(role="reference_image")]
    updated = _generator()._apply_frame_ratio_constraint(task, references)
    assert updated.aspect_ratio == "adaptive"


def test_frame_roles_still_force_adaptive() -> None:
    """原有行为不许回退：首帧/首尾帧仍然强制 adaptive。"""
    for role in ("first_frame", "last_frame"):
        updated = _generator()._apply_frame_ratio_constraint(_task(), [SimpleNamespace(role=role)])
        assert updated.aspect_ratio == "adaptive"


def test_images_only_keeps_user_ratio() -> None:
    """**边界**：只有图片时不该动用户确认过的比例。"""
    task = _task()
    updated = _generator()._apply_frame_ratio_constraint(
        task, [SimpleNamespace(role="reference_image")]
    )
    assert updated.aspect_ratio == "9:16"
    assert updated is task


def test_adaptive_stays_adaptive() -> None:
    task = _task(aspect="adaptive")
    updated = _generator()._apply_frame_ratio_constraint(
        task, [SimpleNamespace(role="reference_video")]
    )
    assert updated.aspect_ratio == "adaptive"
    assert updated is task
