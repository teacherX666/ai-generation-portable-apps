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


def test_fusion_prompt_still_keeps_the_hard_rules() -> None:
    """原有硬性要求不能被这次改动挤掉。"""
    for rule in (
        "@图片N / @视频N / @音频N 引用令牌都必须原样",
        "不得删减",
        "不要输出「【返工要求】」这类标记",
        "以更新的要求为准",
    ):
        assert rule in _REWORK_FUSION_SYSTEM_PROMPT
