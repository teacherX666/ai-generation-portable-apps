"""导演技能（Deepwhite v3.7）接入：加载、注入、开关。

用户 2026-09-22：「这个 skill 能不能直接放到这个模块里…直接把这个 skill 接上」。
"""

from __future__ import annotations

import asyncio

from feishu_generation_agent.domain.document import NormalizedDocument
from feishu_generation_agent.integrations.director_skill import (
    DIRECTOR_SKILL_ID,
    director_skill_prompt_section,
    director_skill_rules,
    skill_is_available,
    skill_root,
)
from feishu_generation_agent.integrations.planner import DeepSeekPlanner


def test_skill_package_is_bundled_with_the_app() -> None:
    """技能包随代码发布，缺了就等于没接上。"""
    assert skill_is_available()
    assert (skill_root() / "SKILL.md").is_file()
    assert (skill_root() / "reference" / "PROMPT_PATTERNS.md").is_file()
    assert DIRECTOR_SKILL_ID in str(skill_root())


def test_core_scope_carries_the_prompt_format_rules() -> None:
    """省 token 模式也必须带上「提示词长什么样」的四篇。"""
    rules = director_skill_rules(scope="core")

    assert "Seedance 2.5 提示词规则基线" in rules
    assert "一镜到底" in rules  # 单镜头模板
    assert "剧本" in rules  # 剧本忠实


def test_all_scope_is_larger_than_core_and_respects_the_cap() -> None:
    core = director_skill_rules(scope="core")
    everything = director_skill_rules(scope="all")

    assert len(everything) > len(core)
    capped = director_skill_rules(scope="all", max_chars=3000)
    assert len(capped) <= 3600  # 允许截断提示语的那点余量


def test_prompt_section_states_the_priority_split() -> None:
    """技能管「怎么写」，契约管「机械要求」—— 这条分工必须写清楚。"""
    section = director_skill_prompt_section(scope="core")

    assert "导演技能规则" in section
    assert "TaskPlan" in section
    assert "@图片N" in section


class _FakeModel:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, messages, config=None):  # noqa: ANN001
        self.messages.append(messages)

        class _Response:
            content = '{"document_summary":"摘要","tasks":[]}'

        return _Response()


def _document() -> NormalizedDocument:
    return NormalizedDocument.model_validate(
        {
            "document_id": "doc-1",
            "title": "测试",
            "revision": 1,
            "blocks": [],
            "text_view": "剧本：一镜到底。",
            "media_assets": [],
            "source_type": "docx",
            "source_token": "tok",
        }
    )


def _system_prompt(skill_section: str) -> str:
    model = _FakeModel()
    planner = DeepSeekPlanner(model, max_output_count=4, skill_section=skill_section)
    try:
        asyncio.run(planner.plan(_document(), []))
    except Exception:  # noqa: BLE001 —— 计划为空会报错，这里只要 system prompt
        pass
    assert model.messages, "planner 没有调用模型"
    return model.messages[0][0]["content"]


def test_skill_section_is_injected_into_the_planner_system_prompt() -> None:
    with_skill = _system_prompt(director_skill_prompt_section(scope="core"))
    without = _system_prompt("")

    assert "导演技能规则" in with_skill
    assert "Seedance 2.5 提示词规则基线" in with_skill
    assert len(with_skill) > len(without)
    # 机械契约在两种情况下都必须保留
    assert "TaskPlan" in with_skill and "TaskPlan" in without
    assert "导演技能规则" not in without
