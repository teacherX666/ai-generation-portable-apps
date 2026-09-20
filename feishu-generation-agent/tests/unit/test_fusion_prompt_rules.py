"""融合提示词：按提示词工程指南重写后的契约。

用户 2026-09-18：「现在的融合提示词感觉还是得改好几版都搞不到正确的答案，能不能去网上
找那些收藏高的 skill 融合提示词」，并要求「不要越叠越多，就地在正文里改」。

这里同时锁住三件事：① 规则（就地改、不许新增段落、禁汇总段）② few-shot 例子
③ 提示词不能太长（曾因撑到 1417 字撞上 TPM 限流，导致融合失败、退回"原样贴末尾"）。
"""

from feishu_generation_agent.integrations.planner import (
    _REWORK_FUSION_SYSTEM_PROMPT,
)

#: 上限：既保证例子放得下，又不至于把每次调用的 token 撑到撞 TPM 限流（2026-09-18）。
_MAX_FUSION_PROMPT_CHARS = 1000


def test_fusion_prompt_stays_small_enough_for_rate_limits() -> None:
    assert len(_REWORK_FUSION_SYSTEM_PROMPT) <= _MAX_FUSION_PROMPT_CHARS


def test_fusion_prompt_asks_for_in_place_edits() -> None:
    assert "就地修订器" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "不许新增段落" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "镜头数量和镜头编号完全一致" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "绝对禁止在结尾追加" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_forbids_negative_lists() -> None:
    assert "否定式要求改写成画面描述" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "手机不得瞬间出现在地面" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "沿重力弧线" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_applies_official_seedance_guidance() -> None:
    """Seedance 官方指南：一个镜头一个主要动作 + 标准运镜 + 别越写越长 + 安全词。"""
    assert "一个镜头只保留" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "固定机位/推近/拉远" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "总长度与原文接近" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "真实人名、品牌名、暴力或政治敏感词" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_has_a_worked_example() -> None:
    """格式敏感的任务必须给例子（提示词工程指南：few-shot 是关键）。"""
    assert "输入原文：「镜头1" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "正确输出" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "要求并进了对应镜头，镜头数不变" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "错误输出（禁止）" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_still_keeps_the_hard_rules() -> None:
    """原有硬性要求不能被重写挤掉。"""
    for rule in (
        "@图片N / @视频N / @音频N 令牌原样保留",
        "保留原文全部信息",
        "新旧要求冲突时以新的为准",
        "只输出这个 JSON",
    ):
        assert rule in _REWORK_FUSION_SYSTEM_PROMPT
