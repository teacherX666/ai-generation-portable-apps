"""融合提示词：既要就地改，又不能把 token 撑爆（撑爆会撞 TPM 限流）。

用户 2026-09-18：「怎么他又是在结尾叠加一堆，不能直接在正文中间进行微调吗」，
随后实测发现真正的根因是**融合调用被限流**（429 TPM）→ 走了"原样贴末尾"的兜底。
所以这里同时锁住两件事：规则还在 + 提示词够短。
"""

from feishu_generation_agent.integrations.planner import (
    _REWORK_FUSION_SYSTEM_PROMPT,
)

#: 2026-09-18 的教训：提示词一度被撑到 1417 字，融合调用 token 翻倍撞上 TPM 限流。
#: 这里给一个上限，防止以后再被"多加几条规则"撑回去。
_MAX_FUSION_PROMPT_CHARS = 900


def test_fusion_prompt_stays_small_enough_for_rate_limits() -> None:
    assert len(_REWORK_FUSION_SYSTEM_PROMPT) <= _MAX_FUSION_PROMPT_CHARS


def test_fusion_prompt_asks_for_in_place_edits() -> None:
    assert "就地在正文里改" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "禁止" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "在结尾另起一段堆要求" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_forbids_negative_lists() -> None:
    assert "禁止" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "「…不得…」清单" in _REWORK_FUSION_SYSTEM_PROMPT
    # 要给出可执行的改写示例，而不是只说"别写否定句"
    assert "手机不得瞬间出现在地面" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "沿重力弧线" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_applies_official_seedance_guidance() -> None:
    """Seedance 官方指南：一个镜头一个主要动作 + 标准运镜 + 别越写越长 + 安全词。"""
    assert "一个镜头只保留一个主要动作" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "固定机位/推近/拉远/横向平移" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "长度与原文接近" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "真实人名、品牌名、暴力或政治敏感词" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_has_a_worked_example() -> None:
    """格式敏感的任务必须给例子（提示词工程指南：few-shot 是关键）。"""
    assert "原文：「镜头3" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "改成：「镜头3" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "并进原句、长度基本不变" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_still_keeps_the_hard_rules() -> None:
    """原有硬性要求不能被裁剪挤掉。"""
    for rule in (
        "@图片N / @视频N / @音频N 令牌原样",
        "不得删减",
        "新旧要求冲突以新的为准",
        "只输出 JSON",
    ):
        assert rule in _REWORK_FUSION_SYSTEM_PROMPT
