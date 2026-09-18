"""返工「有没有看上一版成片」必须可见 —— 记进运行事件。"""

from feishu_generation_agent.graph.nodes import _record_visual_context


class _Repository:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def append_event(self, run_id, node, status, summary) -> None:
        self.events.append((node, status, summary))


class _Services:
    def __init__(self) -> None:
        self.repository = _Repository()


class _BrokenRepository:
    async def append_event(self, *args, **kwargs) -> None:
        raise RuntimeError("写事件失败")


class _BrokenServices:
    def __init__(self) -> None:
        self.repository = _BrokenRepository()


async def test_records_what_the_model_saw() -> None:
    """用户 2026-09-18：「融合返工要求没有上传前一次的生成视频吗，这样看不出问题啊」。"""
    services = _Services()

    await _record_visual_context(
        services, "run-1", "【artifact-1】一名男子在厨房拆纸箱\n疑似穿帮：0:03 手部穿模"
    )

    node, status, summary = services.repository.events[0]
    assert (node, status) == ("review_artifacts", "running")
    assert "已让模型看过上一版成片" in summary
    assert "手部穿模" in summary


async def test_records_when_there_was_no_take() -> None:
    services = _Services()

    await _record_visual_context(services, "run-1", "")

    summary = services.repository.events[0][2]
    assert "没带上一版画面" in summary


async def test_event_failure_does_not_break_the_rework() -> None:
    """写事件失败绝不能影响返工本身。"""
    await _record_visual_context(_BrokenServices(), "run-1", "画面描述")
