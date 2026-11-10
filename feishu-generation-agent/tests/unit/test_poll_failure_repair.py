"""「轮询阶段的失败也要进修复梯子」的回归测试（2026-10-15 第二次收口）。

生产事故（同一用户同一天第二次报）：
`Seedance 2.5：生成服务拒绝了请求：input media detect failed: invalid_media（poll_invalidparameter）`
—— 那条 run **只提交了一次，修复梯子一步没跑**。

根因是结构性的：方舟的失败几乎都是**异步**的（`submit` 成功 → `poll` 返回 failed），
而梯子只挂在 `submit` 的 `except` 上。于是体积/编码/时长这类**只在轮询阶段暴露**的问题
永远修不了。修法：把同一批判据词也用在轮询返回的**失败文本**上。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.graph.nodes import _upstream_text_rejection_reason


def _assets(*mimes: str):
    return [SimpleNamespace(mime_type=m) for m in mimes]


def test_poll_failure_invalid_media_triggers_repair_when_video_present() -> None:
    """这正是用户报的那句：带参考视频时必须被认成「可按素材修复」。"""
    reason = _upstream_text_rejection_reason(
        "input media detect failed: invalid_media Request id: 0217920303096930ffffac19",
        _assets("video/mp4", "image/png"),
    )
    assert reason is not None
    assert "invalid_media" in reason


def test_poll_failure_size_and_duration_markers_are_covered() -> None:
    for message in (
        "generation_input_too_large: input too large",
        "参考视频超过大小限制",
        "the parameter video duration (seconds) specified in the request must be less than or equal to 30.2",
        "unsupported video codec",
    ):
        assert _upstream_text_rejection_reason(message, _assets("video/mp4")) is not None, message


def test_images_only_poll_failure_is_not_a_video_rejection() -> None:
    """边界：只有图片时不能把普通失败当成"参考视频被拒"（否则白跑一遍转码）。"""
    assert (
        _upstream_text_rejection_reason(
            "input media detect failed: invalid_media", _assets("image/png")
        )
        is None
    )


def test_unrelated_poll_failure_is_ignored() -> None:
    assert (
        _upstream_text_rejection_reason("账户余额不足", _assets("video/mp4")) is None
    )
    assert _upstream_text_rejection_reason(None, _assets("video/mp4")) is None
    assert _upstream_text_rejection_reason("", _assets("video/mp4")) is None
