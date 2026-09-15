"""知识库上下文注入 planner 的单测。

背景：知识库原先是在 planner 写完提示词**之后**才介入、命中就改写提示词，
那是两次提示词加工（planner 也在写提示词），且逐任务调导演台，既打架又费 N 次调用。
现在改成规划**之前**取规则、当上下文喂给 planner，一次写成。

这里守的就是这两条：参数正确传递，以及事后改写不再存在。
"""

from __future__ import annotations

import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODES = ROOT / "src" / "feishu_generation_agent" / "graph" / "nodes.py"


class _Planner:
    async def plan(self, document, visions, feedback=None, knowledge_context=None):
        raise NotImplementedError


class _LegacyPlanner:
    async def plan(self, document, visions, feedback=None):
        raise NotImplementedError


def _services(rag_url):
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(rag_preflight_url=rag_url)
    )


def _document(text="文档正文"):
    return types.SimpleNamespace(text_view=text)


def test_argument_is_omitted_when_context_is_empty():
    from feishu_generation_agent.graph.nodes import _knowledge_context_argument

    assert _knowledge_context_argument(_Planner(), "") == {}


def test_argument_carries_the_knowledge_context():
    from feishu_generation_agent.graph.nodes import _knowledge_context_argument

    argument = _knowledge_context_argument(_Planner(), "【参数错误】:时长超限\n时长必须 <=15 秒")

    assert argument["knowledge_context"] == "【参数错误】:时长超限\n时长必须 <=15 秒"


def test_argument_is_omitted_for_legacy_planner():
    """老 planner 没有该参数，必须省略而不是 TypeError。"""
    from feishu_generation_agent.graph.nodes import _knowledge_context_argument

    assert _knowledge_context_argument(_LegacyPlanner(), "【规则】内容") == {}


async def test_no_rag_url_returns_empty_without_calling(monkeypatch):
    from feishu_generation_agent.graph.nodes import _knowledge_context_for_plan

    called: list[str] = []

    async def fake_fetch(text, url, **kwargs):
        called.append(url)
        return []

    monkeypatch.setattr(
        "feishu_generation_agent.graph.nodes.fetch_knowledge_rules", fake_fetch
    )

    assert await _knowledge_context_for_plan(_document(), _services("")) == ""
    assert await _knowledge_context_for_plan(_document(), _services("   ")) == ""
    assert called == []


async def test_blank_document_text_returns_empty_without_calling(monkeypatch):
    from feishu_generation_agent.graph.nodes import _knowledge_context_for_plan

    called: list[str] = []

    async def fake_fetch(text, url, **kwargs):
        called.append(url)
        return []

    monkeypatch.setattr(
        "feishu_generation_agent.graph.nodes.fetch_knowledge_rules", fake_fetch
    )

    assert await _knowledge_context_for_plan(_document("   "), _services("http://x")) == ""
    assert called == []


async def test_document_full_text_is_sent_and_rules_are_formatted(monkeypatch):
    """实测真实文档 1.7k~7.6k 字符，planner 本来就吃全文，所以不截断。"""
    from feishu_generation_agent.graph.nodes import _knowledge_context_for_plan

    long_text = "需求描述" * 500
    seen: dict[str, str] = {}

    async def fake_fetch(text, url, **kwargs):
        seen["text"] = text
        seen["url"] = url
        return [{"title": "【参数错误】:视频时长超限", "content": "时长必须 <=15 秒"}]

    monkeypatch.setattr(
        "feishu_generation_agent.graph.nodes.fetch_knowledge_rules", fake_fetch
    )

    context = await _knowledge_context_for_plan(
        _document(long_text), _services("http://127.0.0.1:8900")
    )

    assert seen["text"] == long_text, "必须是文档全文，不截断"
    assert seen["url"] == "http://127.0.0.1:8900"
    assert "视频时长超限" in context
    assert "<=15 秒" in context


def test_planning_no_longer_rewrites_prompts_afterwards():
    """守设计：planner 之后不再有第二次提示词加工。"""
    source = NODES.read_text(encoding="utf-8")

    assert "optimize_plan_prompts" not in source
    assert "fetch_knowledge_rules" in source
    assert "_knowledge_context_argument(services.planner, knowledge_context)" in source
