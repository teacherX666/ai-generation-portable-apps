"""规划时把「这条需求历次返工被要求改的地方」一起喂进去（用户 2026-09-18 选的第 2 条）。

目的：一次规划就把信息喂够，让重新规划直接避开上次被挑的问题，少一轮"生成完才发现不对"。
这些要求本来就在我们自己的数据里，**不增加任何模型调用**。
"""

from feishu_generation_agent.graph.nodes import _history_context_for_plan


def _state(tasks: list[dict]) -> dict:
    return {"draft_plan": {"tasks": tasks}}


async def test_collects_previous_rework_requirements() -> None:
    state = _state([
        {"rework_requirements": ["手机不能露屏幕", "白鸟说话时其他鸟不能说话"]},
        {"rework_requirements": ["手机不能露屏幕", "镜头要横移"]},
    ])

    context = await _history_context_for_plan(state)

    assert context is not None
    # 去重（「手机不能露屏幕」只出现一次）
    assert context.count("手机不能露屏幕") == 1
    assert "白鸟说话时其他鸟不能说话" in context
    assert "镜头要横移" in context
    assert context.startswith("- ")


async def test_returns_none_for_a_brand_new_requirement() -> None:
    """全新需求没有历史 —— 不能凭空塞一段空的进去。"""
    assert await _history_context_for_plan({}) is None
    assert await _history_context_for_plan(_state([{"prompt": "x"}])) is None


async def test_reads_approved_plan_too() -> None:
    state = {"approved_plan": {"tasks": [{"rework_requirements": ["别分身"]}]}}

    assert "别分身" in (await _history_context_for_plan(state) or "")


async def test_caps_the_history_length() -> None:
    """历次要求可能很多，只带最近 limit 条，别把规划提示词撑爆。"""
    state = _state([
        {"rework_requirements": [f"要求{i}" for i in range(30)]},
    ])

    context = await _history_context_for_plan(state, limit=5)

    assert context is not None
    assert len(context.splitlines()) == 5
    assert "要求29" in context
