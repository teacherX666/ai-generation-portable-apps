"""融合提示词：**保持原来的那版**（用户 2026-09-18：「你这改的什么玩意啊，我之前不是这个
效果的」）。

今天从 943bc88 起连改了 6 版融合提示词，效果反而变差。这版是从 95d9530 恢复的原文 ——
它本来就有「口语必须改写成具体、可执行、可判定的物理描述，不能照抄口语」，也就是用户想要
的"帮我设计优化"。

所以这个文件只做一件事：**守住原版的关键条款**，防止再被"多加几条规则"改跑。
"""

from feishu_generation_agent.integrations.planner import (
    _REWORK_FUSION_SYSTEM_PROMPT,
)


def test_fusion_prompt_keeps_the_original_clauses() -> None:
    """原版的 7 条硬性要求，一条都不能少。"""
    for clause in (
        "保留原始提示词里的全部画面信息",
        "@图片N / @视频N / @音频N 引用令牌都必须原样",
        "能写成正向画面描述的写进 prompt，只能写成禁止项的放进 must_avoid",
        "必须改写成具体、可执行、可判定的物理描述，不能照抄口语",
        "若新旧要求冲突，以更新的要求为准",
        "不要输出「【返工要求】」这类标记",
        "prompt 不超过",
    ):
        assert clause in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_does_not_grow_back() -> None:
    """防止再被"多加几条规则"撑长（撑长会撞 TPM 限流，2026-09-18 实测）。"""
    assert len(_REWORK_FUSION_SYSTEM_PROMPT) <= 700
