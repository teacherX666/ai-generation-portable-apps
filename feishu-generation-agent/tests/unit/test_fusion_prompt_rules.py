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
    """原版的硬性要求，一条都不能少。"""
    for clause in (
        "保留原始提示词里的全部画面信息",
        "@图片N / @视频N / @音频N 引用令牌都必须原样",
        "能写成正向画面描述的写进 prompt，只能写成禁止项的放进 must_avoid",
        "反过来"
        "改写成要达成的具体画面效果",
        "若新旧要求冲突，以更新的要求为准",
        "不要输出「【返工要求】」这类标记",
        "prompt 不超过",
        "严禁在结尾另起一段罗列要求",
    ):
        assert clause in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_does_not_grow_back() -> None:
    """防止再被"多加几条规则"撑长（撑长会撞 TPM 限流，2026-09-18 实测）。

    2026-09-22 把上限从 700 提到 850：第 4 条改成「必须把用户的抱怨**反过来**改写成
    要达成的效果」—— 这条是承重的，少了它融合会把「镜头之间不连续」原样写进提示词
    （实测产出了「各镜头之间画面不连续、不衔接，每段为独立画面」，等于要求模型继续
    做错）。850 仍是硬上限，别再往上加。
    """
    assert len(_REWORK_FUSION_SYSTEM_PROMPT) <= 850


def test_fusion_prompt_requires_replacing_the_wrong_sentence() -> None:
    """修错句必须**整句替换**，不能只插入。

    2026-09-28 生产实测（爱心气球 `1daa69ae`）：要求是「气球/绳子从女孩的手上方滑走，
    而不是女孩突然张开手」，融合却把新表述**插在旧句旁边**，产出：

    > 气球被风力吹的有向上的运动趋势，绳子被带动着从女孩的手部慢慢向上滑，滑到镜头
    > 气球绳子从女孩手中脱落，向天空飞去，**脱手瞬间她的手指仍保持握持细杆的姿势、
    > 指节被风带得微微张开**…

    —— 气球已经「脱落、向天空飞去」，紧接着又说「脱手瞬间手指仍保持握持」：自相矛盾，
    而且「吹的有」「滑到镜头气球绳子」是病句。

    根因是规则冲突：第 1 条说「不得删减」，第 3 条说「每条要求都落实」—— 于是模型
    只敢加、不敢删。必须明确授权（并要求）删掉与要求冲突的旧表述。
    """
    assert "整句替换" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "并存" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_requires_a_self_consistent_rewrite() -> None:
    """改完那一句必须自洽（这条直接对应上面那段「已飞走 + 仍握持」）。"""
    assert "自洽" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_merges_duplicate_requirements() -> None:
    """多条要求说的是同一件事时要合并成一句。

    2026-09-28 爱心气球那轮带了三条几乎同义的要求（「气球脱手自然点」「改成气球从女孩
    的手上方滑走」「改成气球的绳子从女孩的手上方滑走」），而第 3 条要求「一条都不能
    遗漏」—— 模型就把同一件事写了三遍。
    """
    assert "同一件事" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_ignores_findings_unrelated_to_this_round() -> None:
    """核对结论里与本次要求无关的问题不要顺手改（用户 2026-09-28：「改我没说的问题」）。"""
    assert "无关的问题别动" in _REWORK_FUSION_SYSTEM_PROMPT


def test_fusion_prompt_rewrites_whole_sequence_when_the_order_is_wrong() -> None:
    """动作顺序/时序错了必须重写整段序列，不能只补修饰。

    2026-09-29 生产实测（刀切水果碗）：正文早就写明「先沿长轴竖直切两刀分成三块并排
    厚片 → 把三块厚片整体水平旋转 90 度 → 逐片横切成方块阵列」，成片却**漏了旋转
    这一步**、就地在原位置乱切。返工（已多模态看片）只改了 13 个字
    （加「每块厚片均带绿色尖端」+「芒果」→「三块厚片」），没有重写动作序列 ——
    因为旧规则只说「只改相关句子」，它不敢增删整句。
    """
    assert "动作顺序" in _REWORK_FUSION_SYSTEM_PROMPT
    assert "重写整段" in _REWORK_FUSION_SYSTEM_PROMPT

