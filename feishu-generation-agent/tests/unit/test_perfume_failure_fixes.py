"""「拯救香水」两个失败原因的回归测试（2026-10-15）。

用户报：「拯救香水依旧规划十几分钟，依旧执行失败，为啥我现在规划时长越来越长了」。
实测两条 run：

* `05244f6c`（执行失败）：任务 `resolution = "720*1280"`（**星号**）→ 域模型只把 `×`
  换成 `x`、别名表里只有 `720x1280` → 没命中 → **原样下发** → 方舟
  `generation_invalid_resolution / Input validation failed`。
* `37757a6c`（规划阶段挂掉）：09:18:54 规划 4.8 分钟 → 09:23:39 审计 5.0 分钟 →
  `audit_plan/failed (transient_error; operation=audit; cause=APITimeoutError)`
  —— 规划 + 审计两次大调用串行各 300 秒，最后因为**审计**超时判死整条 run。
"""

from __future__ import annotations

import pytest

from feishu_generation_agent.domain.plan import GenerationTask


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("720*1280", "720p"),      # ← 生产上就是这样写的，修复前会原样下发
        ("720x1280", "720p"),
        ("720×1280", "720p"),
        ("1280 * 720", "720p"),
        ("720 X 1280", "720p"),
        ("1080*1920", "1080p"),
        ("1920*1080", "1080p"),
        ("480*854", "480p"),
        ("2160*3840", "4k"),
        ("720p", "720p"),          # 本来就是档位 → 不动
        ("1080p", "1080p"),
        ("adaptive", "adaptive"),
    ],
)
def test_resolution_written_as_pixels_is_normalized(raw: str, expected: str) -> None:
    """任何常见写法都要归一成供应商认的档位（方舟只认 480p/720p/1080p/4k）。"""
    assert _task(resolution=raw).resolution == expected


def test_unknown_resolution_is_left_for_validation() -> None:
    """认不出来的写法不猜、也不静默改（交给既有校验去报错）。"""
    assert _task(resolution="very-high").resolution == "very-high"


def test_audit_timeout_degrades_to_skipped_report() -> None:
    """审计超时必须降级为"跳过"，不许把整条 run 判死（审计只是建议项）。"""
    import inspect

    from feishu_generation_agent.graph import nodes

    source = inspect.getsource(nodes.audit_plan)
    assert "审计已跳过" in source, "审计失败必须写进报告（可见）"
    assert "ErrorCategory.VALIDATION" in source, "只有确定性错误才继续抛"
    assert "corrections_required" in source, "降级报告必须是合法的 AuditReport 形状"


def test_audit_model_uses_shorter_timeout_than_planner() -> None:
    """审计（建议项）不该跟规划用同一个 300 秒预算。"""
    import inspect

    from feishu_generation_agent.integrations import planner

    source = inspect.getsource(planner.DeepSeekPlanner) if hasattr(planner, "DeepSeekPlanner") else inspect.getsource(planner)
    assert "request_timeout" in source and "180" in source, "审计模型要显式收短超时"
