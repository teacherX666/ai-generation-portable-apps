"""融合提示词必须要求「就地在正文里改」，不许在结尾堆一坨。

用户 2026-09-18：「怎么他又是在结尾叠加一堆，这样会越叠越多，不能直接在正文中间
进行微调吗」。
"""

from feishu_generation_agent.integrations.planner import (
    _REWORK_FUSION_SYSTEM_PROMPT,
)


def test_fusion_prompt_asks_for_in_place_edits() -> None:
    assert "就地在正文里改" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "不要**在正文结尾另起" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "越叠越多" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_forbids_negative_lists() -> None:
    assert "不要输出「…不得…」清单" in _REWORK_FUSION_SYSTEM_PROMPT
    # 给出可执行的改写示例，而不是只说"别写否定句"
    assert "手机不得瞬间出现在地面" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "沿重力弧线" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_applies_official_seedance_guidance() -> None:
    """按 Seedance 官方提示词指南补的几条：单动作/标准运镜/别越写越长/安全词。"""
    assert "一个镜头只保留一个主要动作" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "固定机位 / 推近 / 拉远 / 横向平移" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "正文长度不要增长" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "真实人名、品牌名、暴力或政治敏感词" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_has_a_worked_example() -> None:
    """格式敏感的任务必须给例子（提示词工程指南：few-shot 对格式类任务关键）。"""
    assert "反面例子（禁止）" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "正面例子（要求）" in _REWORK_FUSION_SYSTEM_PROMPT
    # 例子要演示"并进原句"而不是另起一段
    assert "并进了原来那一句里" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_still_keeps_the_hard_rules() -> None:
    """原有硬性要求不能被这次改动挤掉。"""
    for rule in (
        "@图片N / @视频N / @音频N 引用令牌都必须原样",
        "不得删减",
        "不要输出「【返工要求】」这类标记",
        "以更新的要求为准",
    ):
        assert rule in _REWORK_FUSION_SYSTEM_PROMPT
