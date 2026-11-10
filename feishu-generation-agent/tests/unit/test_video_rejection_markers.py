"""「上游光秃秃的 400 也要触发修复梯子」的回归测试（2026-10-15）。

生产事故：用户报
`Seedance 2.5：生成服务拒绝了请求：Bad Request Request id: 021792028733769…（poll_invalidparameter）`

实测对照（同一文档《JewelLoop…齐成林-10.14》）：
* 12 秒、**只带图片** → 成功；
* 22 秒、**带参考视频** → 连续两次失败，一次 `invalid_media`、一次光秃秃的 `Bad Request`。

根因：`_video_rejection_reason` 靠判据词识别「上游拒绝了参考视频」，而那句
`Bad Request` 里**一个判据词都没有** → 返回 None → 修复梯子不启动、任务直接判死。
修法：把这类转述型 400 也认下来（只在**带参考视频**时才会走到这里，梯子有界、
第一级 `trim_video` 会用 `libx264 + aac` 重编码，正好修掉 HEVC / HE-AAC / 体积 / 时长）。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.domain.errors import ErrorCategory
from feishu_generation_agent.graph.nodes import _video_rejection_reason


def _task() -> GenerationTask:
    return GenerationTask(
        task_id="T1",
        title="测试任务",
        task_type="image_to_video",
        user_intent="测试用",
        source_block_ids=["blk"],
        prompt="测试",
        duration=22,
        aspect_ratio="9:16",
        resolution="720p",
    )


def _assets(kind: str):
    return [SimpleNamespace(mime_type=kind)]


def _exc(message: str, category=ErrorCategory.PROVIDER_TERMINAL):
    detail = SimpleNamespace(category=category, message=message, technical_detail="")
    return SimpleNamespace(detail=detail)


def test_terse_bad_request_with_reference_video_triggers_ladder() -> None:
    """光秃秃的 `Bad Request`（无参数细节）+ 参考视频 → 必须触发修复梯子。"""
    reason = _video_rejection_reason(
        _exc("生成服务拒绝了请求：Bad Request Request id: 0217920287337690ffffac18033c4f7050"),
        _task(),
        _assets("video/mp4"),
    )
    assert reason is not None, "转述型 400 没被认下来 —— 任务会原样判死"


def test_poll_invalidparameter_marker_is_covered() -> None:
    reason = _video_rejection_reason(
        _exc("生成服务拒绝了请求：input media detect failed: invalid_media (code=poll_invalidparameter)"),
        _task(),
        _assets("video/mp4"),
    )
    assert reason is not None


def test_bad_request_without_video_asset_is_not_a_video_rejection() -> None:
    """**边界**：没有参考视频时，这句话不能被当成"参考视频被拒"（否则会白跑梯子）。"""
    reason = _video_rejection_reason(
        _exc("生成服务拒绝了请求：Bad Request"),
        _task(),
        _assets("image/png"),
    )
    assert reason is None


def test_unrelated_error_with_video_is_not_a_video_rejection() -> None:
    """无关错误（例如额度不足）不该触发素材修复梯子。"""
    reason = _video_rejection_reason(
        _exc("账户余额不足，请充值"),
        _task(),
        _assets("video/mp4"),
    )
    assert reason is None
