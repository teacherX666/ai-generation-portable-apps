"""返工提示词净化：要求只累积不覆盖、AI 融合、永不因超长失败。"""

from feishu_generation_agent.domain.plan import (
    SEEDANCE_PROMPT_MAX_CHARS,
    GenerationTask,
)
from feishu_generation_agent.integrations.rework_prompt import (
    REWORK_MARKER,
    build_fallback_prompt,
    build_rework_prompt,
    fusion_rejection_reason,
    is_acceptable_fusion,
    merge_negative_constraints,
    merge_requirements,
    parse_fusion_payload,
    rework_inputs,
    split_legacy_requirements,
)

#: 墨滴任务第 7 轮真实攒出来的「必须避免」（2026-09-17 生产快照，21 条）。
INK_DROP_NEGATIVES = [
    "不要出现任何 logo 或品牌特征",
    "不要出现水印、贴纸、乱码文字或字幕",
    "不要出现人物分身、凤凰分身或形象突变",
    "不要出现穿模、悬空漂浮、重心失衡的动作",
    "不要出现镜头剧烈抖动、跳帧或画面变形",
    "不要出现与水墨写实风格不符的低质模糊与过度锐化",
    "墨滴不得凭空出现在半空中",
    "墨滴不得脱离笔尖正下方的下落轨迹",
    "不得出现 logo 或品牌特征",
    "不得出现水印或乱码文字",
    "人物与凤凰不得分身、换形或装扮突变",
    "人物与物体不得悬浮或穿模",
    "画面不得变形",
    "墨滴不得凭空出现在半空",
    "墨滴不得脱离笔尖后横向或斜向偏移",
    "墨滴不得从笔尖上方或侧面生成",
    "墨滴不得凭空出现或从笔尖以外位置掉落",
    "墨滴不得从笔尖上方或侧面滴落",
    "墨滴不得凭空出现在空中",
    "墨滴不得脱离笔尖后横向偏移或斜向飘落",
    "墨滴下落轨迹不得偏离",
]


async def test_build_rework_prompt_passes_visual_context_to_fuser() -> None:
    """「上一版成片的实际画面」要传给融合模型 —— 它才能看到实际画成了什么。

    用户要求（2026-09-17）：返工时把上一版成片直接上传视频让模型看。
    """
    seen: dict[str, str] = {}

    async def fuse(prompt, requirements, *, visual_context=""):
        seen["visual_context"] = visual_context
        return {"prompt": prompt + "（已按画面修正）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始提示词",
        ["不要参考人物形象"],
        fuse=fuse,
        visual_context="画面里出现了一个人物形象，占画面中心。",
    )

    assert seen["visual_context"] == "画面里出现了一个人物形象，占画面中心。"
    assert "已按画面修正" in prompt


async def test_build_rework_prompt_tolerates_legacy_fuser_without_context() -> None:
    """老融合器只收两个参数 —— 不能因此报错（签名探测兜底）。"""

    async def legacy_fuse(prompt, requirements):
        return {"prompt": prompt + "（旧融合器）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始提示词",
        ["要求"],
        fuse=legacy_fuse,
        visual_context="画面描述",
    )

    assert "旧融合器" in prompt


def test_merge_negative_constraints_collapses_rewording() -> None:
    """同一句约束的换皮写法只留一条 —— 规则一条不少，只去掉重复措辞。

    2026-09-17 实测（墨滴任务）：每轮返工都把「墨滴不得凭空出现…」再写一遍，
    7 轮后攒到 21 条 / 297 字，占提交文本 26%，其中 10 条是同一件事。否定句重复
    既稀释正向描述，又容易把「凭空出现的墨滴」反复喂给模型。
    """
    merged = merge_negative_constraints([], INK_DROP_NEGATIVES)

    # 21 条 → 10 条：按「主语 + 概念集合互为子集」合并后仍有 13 条，再被限量到 10 条。
    # 墨滴那一族收敛成 4 条（来源/轨迹/侧面各一条），其余是通用风格与主体一致性。
    assert len(merged) <= 10, merged
    ink_drop = [item for item in merged if item.startswith("墨滴")]
    assert len(ink_drop) <= 4, ink_drop
    # 一条规则都不能丢：并掉的只是措辞。
    joined = "".join(ink_drop)
    for keyword in ("凭空", "轨迹", "侧面"):
        assert keyword in joined
    # 限量只丢**最旧**的（通用风格约束提示词正文里本来就写着，丢了不会漏）。
    assert INK_DROP_NEGATIVES[-1] in merged
    assert INK_DROP_NEGATIVES[0] not in merged


def test_merge_negative_constraints_keeps_the_longest_wording() -> None:
    """组内保留最完整的那条写法（别把细节并没了）。"""
    merged = merge_negative_constraints(
        ["不得出现 logo 或品牌特征"],
        ["不要出现任何 logo 或品牌特征"],
    )

    assert merged == ["不要出现任何 logo 或品牌特征"]


def test_merge_negative_constraints_keeps_distinct_rules_apart() -> None:
    """不同规则不许并 —— 相似度只是启发式，不能吃掉真规则。"""
    merged = merge_negative_constraints(
        ["人物与凤凰不得分身、换形或装扮突变"],
        ["人物与物体不得悬浮或穿模"],
    )

    assert len(merged) == 2


def test_merge_negative_constraints_keeps_numbered_rules_apart() -> None:
    """只差数字的两条是不同规则（镜头1 vs 镜头2），不许并。"""
    merged = merge_negative_constraints(
        ["镜头 1 不得抖动", "约束-001 必须保持"],
        ["镜头 2 不得抖动", "约束-002 必须保持"],
    )

    assert merged == [
        "镜头 1 不得抖动",
        "约束-001 必须保持",
        "镜头 2 不得抖动",
        "约束-002 必须保持",
    ]


def test_merge_negative_constraints_keeps_the_superset_wording() -> None:
    """一条被另一条完全覆盖时，留信息量更大的那条。"""
    merged = merge_negative_constraints(
        ["不得出现水印或乱码文字"],
        ["不要出现水印、贴纸、乱码文字或字幕"],
    )

    assert merged == ["不要出现水印、贴纸、乱码文字或字幕"]


def test_merge_negative_constraints_ignores_blank_and_non_string() -> None:
    merged = merge_negative_constraints(
        ["", "   ", None, 42],
        ["画面不得变形", " 画面不得变形 "],
    )

    assert merged == ["画面不得变形"]


def _task(prompt: str, **updates) -> GenerationTask:
    task = GenerationTask(
        task_id="t1",
        task_type="image_to_video",
        title="任务",
        source_block_ids=[],
        user_intent="意图",
        prompt=prompt,
        aspect_ratio="16:9",
        duration=5,
        resolution="720p",
    )
    return task.model_copy(update=updates) if updates else task


# --- 累积：这是用户最核心的诉求「说过的绝不再犯」 ---


def test_merge_requirements_accumulates_without_dropping() -> None:
    assert merge_requirements(["手不要僵"], ["背景太暗"]) == [
        "手不要僵",
        "背景太暗",
    ]


def test_merge_requirements_dedupes_keeping_first_position() -> None:
    assert merge_requirements(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_merge_requirements_ignores_blank_entries() -> None:
    assert merge_requirements(["  ", "a", ""]) == ["a"]


# --- 兼容历史数据：老提示词里的【返工要求】段要能被接住，不能丢 ---


def test_split_legacy_requirements_extracts_marker_block() -> None:
    base, requirements = split_legacy_requirements(
        "画面描述\n【返工要求】手不要僵\n背景太暗"
    )
    assert base == "画面描述"
    assert requirements == ["手不要僵", "背景太暗"]


def test_split_legacy_requirements_without_marker_is_identity() -> None:
    base, requirements = split_legacy_requirements("画面描述")
    assert base == "画面描述"
    assert requirements == []


# --- 兜底：融合不可用时**保持正文原样**（不再拼接要求，永不超长、永不抛错） ---


def test_fallback_prompt_keeps_base_unchanged() -> None:
    """用户 2026-09-18：「不要越叠越多，就地在正文里改」。

    以前兜底会把要求拼到末尾（带【返工要求】标记），每返工一次就多一坨。现在
    宁可不改 —— 要求仍在 requirements 里累积，下一轮融合照样带上。
    """
    prompt, truncated = build_fallback_prompt("原始画面", ["手不要僵", "背景太暗"])

    assert prompt == "原始画面"
    assert truncated is False
    assert "【返工要求】" not in prompt


def test_fallback_prompt_clips_overlong_base_instead_of_raising() -> None:
    base = "画" * (SEEDANCE_PROMPT_MAX_CHARS + 50)
    prompt, truncated = build_fallback_prompt(base, ["背景太暗"])

    assert len(prompt) <= SEEDANCE_PROMPT_MAX_CHARS
    assert truncated is True


def test_fallback_prompt_without_requirements_keeps_short_base() -> None:
    prompt, truncated = build_fallback_prompt("原始画面", [])

    assert prompt == "原始画面"
    assert truncated is False


# --- 融合结果验收：不合契约就回退，绝不让坏结果进生产 ---


def test_fusion_rejected_when_reference_token_lost() -> None:
    assert not is_acceptable_fusion("一只猫在跑", base_prompt="@图片1 中的猫在跑")


def test_fusion_rejected_when_reference_token_added() -> None:
    assert not is_acceptable_fusion(
        "@图片1 与 @图片2 在跑", base_prompt="@图片1 在跑"
    )


def test_fusion_rejected_when_overlong() -> None:
    assert not is_acceptable_fusion(
        "画" * (SEEDANCE_PROMPT_MAX_CHARS + 1), base_prompt="x"
    )


def test_fusion_rejected_when_marker_would_leak_into_prompt() -> None:
    assert not is_acceptable_fusion(f"x\n{REWORK_MARKER}y", base_prompt="x")


def test_fusion_rejected_when_missing_or_blank() -> None:
    assert not is_acceptable_fusion(None, base_prompt="x")
    assert not is_acceptable_fusion("   ", base_prompt="x")


# 用户 2026-09-30（杂技小象）：「现在怎么重做又变了」。真实数据：那一轮只有一条要求
# 「场景不要发生突变，动作流畅，不要人机感」，融合却在**结尾整段新增**了一个
# `【场景与动作连续性】` 小节（+212 字）。两道旧拦截都没挡住：
#   - `_SUMMARY_BLOCK` 只认固定抬头（约束/汇总/补充要求/衔接…），新抬头不认识；
#   - `trailing_growth_reason` 只是"结尾段增长 ≤1.5 倍"的比例阈值，基准结尾段本来就长。
_ELEPHANT_BASE = (
    "【一句话概述】小象在马戏团舞台上表演。\n"
    "【全局视觉设定】全局风格：3D卡通渲染，圆润黏土质感。\n"
    "【详细情节】\n镜头 1（0-9秒）：小象走上圆台抛接彩球，重心转移自然。\n"
    "【全局连续性与结尾约束】全片为同一处马戏团场地，红幕布与彩灯位置不变，18秒内恒定。"
)
_ELEPHANT_APPENDED = (
    "\n\n【场景与动作连续性】\n全片舞台空间自始至终为同一处马戏团场地：圆形木质舞台地面、"
    "红幕布、星空彩灯与虚化观众席的位置、比例与光照方向在18秒内保持恒定，镜头切换时背景"
    "陈设不跳变、不替换、不新增或消失道具。小象的行走、抛接、下压、腾空、回旋与落台各动作"
    "之间以连续的重心转移和肢体惯性衔接，前一个动作的收势直接成为后一个动作的起势，关节与"
    "躯干运动轨迹平滑连贯，无卡顿、无跳帧、无机械式匀速位移，整体呈现自然流畅的卡通表演节奏。"
)


def test_fusion_rejected_when_a_new_section_is_appended() -> None:
    reason = fusion_rejection_reason(
        _ELEPHANT_BASE + _ELEPHANT_APPENDED, base_prompt=_ELEPHANT_BASE
    )

    assert reason is not None, "结尾新增【场景与动作连续性】小节必须被拒"
    assert "场景与动作连续性" in reason


def test_fusion_rejected_when_a_new_section_is_inserted_in_the_middle() -> None:
    """插在中间同样是结构破坏 —— 不只拦结尾。"""
    fused = _ELEPHANT_BASE.replace(
        "【详细情节】", "【场景与动作连续性】同一处场地。\n【详细情节】"
    )

    assert not is_acceptable_fusion(fused, base_prompt=_ELEPHANT_BASE)


def test_fusion_still_accepted_when_existing_sections_are_only_rewritten() -> None:
    """只重写原有小节里的句子照旧放行 —— 别把正常返工也拦死。"""
    fused = _ELEPHANT_BASE.replace(
        "小象走上圆台抛接彩球，重心转移自然。",
        "小象以连续的重心转移走上圆台，抛接彩球时肩背惯性顺势带动，动作不停顿。",
    )

    assert is_acceptable_fusion(fused, base_prompt=_ELEPHANT_BASE)


def test_fusion_accepted_when_contract_holds() -> None:
    assert is_acceptable_fusion(
        "@图片1 中的猫缓慢行走", base_prompt="@图片1 中的猫在跑"
    )


#: 用户 2026-09-22 的真实提示词（去掉结尾叠加块后的基准）。
_MULTISHOT_BASE = (
    "吉卜力动画风格，9:16 竖屏。"
    "镜头 1（0-5s）：卡车行驶，南瓜滚落，后方车辆紧急刹车。"
    "镜头 2（5-10s）：司机惊叫打方向，撞上路边护栏。"
    "镜头 3（10-15s）：双向车流堵成长龙，车辆杂乱错落停放。"
    "风格与约束：全片风格统一，画质清晰稳定、无锐化过度，人物与车辆不悬浮、"
    "不穿模、重心稳定，动作符合身体结构与因果顺序；不要出现任何 logo 或品牌"
    "特征，无水印、不变形。"
)


def test_fusion_rejected_when_requirements_are_piled_up_at_the_end() -> None:
    """「怎么重做又变成叠加在结尾了」—— 结尾多出一大段罗列必须拦住。

    真实产出用「全片动作连续流畅：…」起头，**绕过了按小标题匹配的
    `_SUMMARY_BLOCK`**，所以这里改成量结尾段的长度。
    """
    fused = _MULTISHOT_BASE + (
        "全片动作连续流畅：卡车行驶、绑带崩断、南瓜滚落、后车急刹追尾、司机惊叫"
        "打方向撞护栏、南瓜从车顶飞过、最终堵成长龙，各动作首尾相接、速度均匀无"
        "跳帧；镜头切换点保持主体位置与运动方向连续，前一镜头结尾的南瓜滚动方向"
        "与下一镜头开头一致，硬切处不出现画面停顿或重复帧。"
    )

    assert not is_acceptable_fusion(fused, base_prompt=_MULTISHOT_BASE)


def test_fusion_accepted_when_requirements_are_woven_into_the_sentences() -> None:
    """就地并进原句（哪怕风格段多写了几十字）要放行。"""
    fused = _MULTISHOT_BASE.replace(
        "风格与约束：全片风格统一，",
        "风格与约束：全片风格统一，全片动作首尾相接、速度均匀无跳帧，"
        "镜头切换点保持主体位置与运动方向连续，",
    )

    assert is_acceptable_fusion(fused, base_prompt=_MULTISHOT_BASE)


def test_trailing_growth_check_is_skipped_without_a_style_block() -> None:
    """单镜头 / 文生视频没有段落结构，不做结尾长度判断。"""
    from feishu_generation_agent.integrations.rework_prompt import (
        trailing_growth_reason,
    )

    assert trailing_growth_reason("一只猫在跑，跑得更慢了", "一只猫在跑") is None


# --- 共享入口：两条重跑路径（图节点 / 多维表格 clone）必须走同一套语义 ---


def test_rework_inputs_accumulates_requirements_from_task() -> None:
    task = _task(
        "原始画面",
        rework_base_prompt="原始画面",
        rework_requirements=["手不要僵"],
    )
    base, requirements = rework_inputs(task, "背景太暗")
    assert base == "原始画面"
    assert requirements == ["手不要僵", "背景太暗"]


def test_rework_inputs_adopts_legacy_marker_segment() -> None:
    task = _task(f"原始画面\n{REWORK_MARKER}手不要僵")
    base, requirements = rework_inputs(task, "背景太暗")
    assert base == "原始画面"
    assert requirements == ["手不要僵", "背景太暗"]


def test_rework_inputs_freezes_base_on_first_rework() -> None:
    task = _task("原始画面")
    base, _ = rework_inputs(task, "手不要僵")
    assert base == "原始画面"
    assert task.rework_base_prompt is None


def test_rework_inputs_bases_on_previous_version_not_the_frozen_first() -> None:
    """融合基准是**上一版**，不是冻结的第一版。

    用第一版当基准意味着每一轮都要从最初那份重写一遍：融合只要有一点遗漏，
    前几轮已经修好的东西就真的回退了（用户 2026-09-17 报的
    「甚至会把我之前的重做优化给回退」）。以「上一版」为基准，之前几轮的
    优化天然被保留下来，本轮只在其上叠加这一次的要求。
    """
    task = _task(
        "第一版。手不要僵。",
        rework_base_prompt="第一版。",
        rework_requirements=["手不要僵"],
    )
    base, requirements = rework_inputs(task, "背景明亮")

    assert base == "第一版。手不要僵。"
    assert requirements == ["手不要僵", "背景明亮"]


def test_rework_inputs_strips_the_legacy_tail_from_the_base() -> None:
    """上一轮是兜底拼接时，正文尾巴里的【返工要求】要从基准里剥掉。

    剥掉的同时那些要求必须并回清单再喂给融合器 —— 否则一剥就丢，
    这正是 2026-09-16 修过的老 bug。
    """
    task = _task(f"第一版。\n{REWORK_MARKER}手不要僵")
    base, requirements = rework_inputs(task, "背景明亮")

    assert base == "第一版。"
    assert REWORK_MARKER not in base
    assert requirements == ["手不要僵", "背景明亮"]


async def test_build_rework_prompt_uses_accepted_fusion() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> str:
        assert requirements == ["手不要僵"]
        return f"{original_prompt}（已融合）"

    prompt, truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert prompt == "原始画面（已融合）"
    assert truncated is False
    assert must_avoid == []


async def test_build_rework_prompt_falls_back_when_fuser_raises() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> str:
        raise RuntimeError("上游 500")

    prompt, _truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    # 融合不可用时**保持正文原样**：不再把要求拼到末尾（用户 2026-09-18：
    # 「不要越叠越多，就地在正文里改」）。要求仍在 requirements 里累积。
    assert prompt == "原始画面"
    assert must_avoid == []


async def test_build_rework_prompt_falls_back_without_fuser() -> None:
    prompt, _truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"]
    )
    # 融合不可用时**保持正文原样**：不再把要求拼到末尾（用户 2026-09-18：
    # 「不要越叠越多，就地在正文里改」）。要求仍在 requirements 里累积。
    assert prompt == "原始画面"
    assert must_avoid == []


async def test_build_rework_prompt_rejects_fusion_that_drops_tokens() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> str:
        return "丢了 @图片N 的结果"

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "@图片1 中的猫在跑", ["手不要僵"], fuse=fuse
    )

    # 同上：兜底不再拼接，正文原样。
    assert prompt == "@图片1 中的猫在跑"


# --- 双通道：融合时顺带产出「必须避免」清单，写进 negative_constraints ---


async def test_build_rework_prompt_does_not_append_must_avoid_to_body() -> None:
    """must_avoid **不**再补进正文。

    用户 2026-09-18 明确要求「不要越叠越多，就地在正文里改」：补写只能追加到末尾，
    正是他要避免的形态。要求本身仍累积在 rework_requirements 里，下一轮融合还会带上。
    """
    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {
            "prompt": f"{original_prompt}（已融合）",
            "must_avoid": ["红衣服老头不得跑出起跑线", "眼睛不得发光"],
        }

    prompt, truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["不要让红衣服老头跑出去"], fuse=fuse
    )

    assert prompt == "原始画面（已融合）"  # 原样采用融合结果，不追加
    assert truncated is False
    assert must_avoid == ["红衣服老头不得跑出起跑线", "眼睛不得发光"]


async def test_build_rework_prompt_does_not_duplicate_must_avoid_in_body() -> None:
    """正文里已经写了的要求，不要再追加一遍。"""
    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {
            "prompt": f"{original_prompt}（已融合，眼睛不得发光）",
            "must_avoid": ["眼睛不得发光"],
        }

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["眼睛别发光"], fuse=fuse
    )

    assert prompt.count("眼睛不得发光") == 1


async def test_build_rework_prompt_keeps_prompt_when_must_avoid_is_malformed() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {"prompt": f"{original_prompt}（已融合）", "must_avoid": "不是数组"}

    prompt, _truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert prompt == "原始画面（已融合）"
    assert must_avoid == []


async def test_build_rework_prompt_ignores_blank_must_avoid_entries() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {
            "prompt": f"{original_prompt}（已融合）",
            "must_avoid": ["  ", "眼睛不得发光", ""],
        }

    _prompt, _truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert must_avoid == ["眼睛不得发光"]


def test_parse_fusion_payload_accepts_plain_text() -> None:
    assert parse_fusion_payload("一段提示词") == ("一段提示词", [])


def test_parse_fusion_payload_accepts_mapping() -> None:
    assert parse_fusion_payload(
        {"prompt": "一段提示词", "must_avoid": ["不要发光"]}
    ) == ("一段提示词", ["不要发光"])


def test_parse_fusion_payload_rejects_missing_prompt() -> None:
    assert parse_fusion_payload({"must_avoid": ["不要发光"]}) is None
    assert parse_fusion_payload(None) is None
    assert parse_fusion_payload("   ") is None


# --- 2026-09-16 生产事故：融合偶发不合契约 → 静默掉进兜底拼接 ---
#
# 现象：用户在成片审核页填的返工要求，没有融进提示词，而是以
# `原始提示词 + 【返工要求】+ 原话` 的形式被整段贴在末尾。
# 实测（真实模型、同一输入、temperature=0）3 次里 2 次成功、1 次失败——
# 失败是「偶发」而不是「必然」，但旧代码一次失败就回退，于是用户看到的就是
# 「又变成直接加返工要求」。而且这条回退路径一行日志都没有，生产上无从查起。


def test_fusion_accepted_when_token_mention_count_drops() -> None:
    """契约是「素材不能丢/不能多」，不是「提及次数必须一模一样」。

    实测被误拒的一例：base 里 @图片1 出现 5 次，融合后 4 次（同一张图，
    只是少提了一次），旧的多重集比较直接判拒 → 掉进兜底拼接。
    """
    base = "@图片1 中的猫在跑，@图片1 的毛色是白色"
    assert is_acceptable_fusion("@图片1 中的猫缓慢行走", base_prompt=base)


def test_fusion_rejection_reason_names_each_violation() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    assert fusion_rejection_reason(
        "@图片1 中的猫缓慢行走", base_prompt="@图片1 中的猫在跑"
    ) is None
    assert "不是文本" in fusion_rejection_reason(None, base_prompt="x")
    assert "为空" in fusion_rejection_reason("   ", base_prompt="x")
    assert "超长" in fusion_rejection_reason(
        "画" * (SEEDANCE_PROMPT_MAX_CHARS + 1), base_prompt="x"
    )
    assert "标记" in fusion_rejection_reason(
        f"x\n{REWORK_MARKER}y", base_prompt="x"
    )
    assert "丢了素材引用" in fusion_rejection_reason(
        "一只猫在跑", base_prompt="@图片1 中的猫在跑"
    )
    assert "多出素材引用" in fusion_rejection_reason(
        "@图片1 与 @图片2 在跑", base_prompt="@图片1 在跑"
    )


async def test_build_rework_prompt_retries_after_rate_limit(monkeypatch) -> None:
    """融合被限流（429 TPM）时要退避重试，不能连发重试然后直接贴末尾。

    2026-09-18 线上实测：deepseek-v4-1-flash 报
    `429 ModelAccountTpmRateLimitExceeded`，三次连发全撞上限 → 用户看到的是
    「正文完全不改、结尾多一坨」。
    """
    from feishu_generation_agent.integrations import rework_prompt as module

    monkeypatch.setattr(module, "_RATE_LIMIT_BACKOFF_SECONDS", 0.01)
    calls: list[int] = []

    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Error code: 429 - ModelAccountTpmRateLimitExceeded")
        return {"prompt": f"{original_prompt}（已融合）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert len(calls) == 2  # 限流后重试成功
    assert prompt.startswith("原始画面（已融合）")


async def test_build_rework_prompt_falls_back_on_non_rate_limit_error() -> None:
    """非限流的错误照旧立刻回退（不浪费时间等）。"""
    calls: list[int] = []

    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        calls.append(1)
        raise RuntimeError("model does not exist")

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert len(calls) == 1
    # 兜底不再拼接：正文原样（要求仍在 requirements 里，下一轮还会带上）
    assert prompt == "原始画面"


async def test_build_rework_prompt_retries_rejected_fusion_until_accepted() -> None:
    """偶发不合契约时应当重试，而不是一次失败就整段贴。

    第 1 次故意丢令牌（旧代码在这里就回退了），第 2 次合规。
    """
    calls: list[int] = []

    async def fuse(original_prompt: str, requirements: list[str]):
        calls.append(1)
        if len(calls) == 1:
            return {"prompt": "丢了 @图片N 的结果", "must_avoid": []}
        return {
            "prompt": f"{original_prompt}（已融合）",
            "must_avoid": ["手不得僵"],
        }

    prompt, truncated, must_avoid = await build_rework_prompt(
        "@图片1 中的猫在跑", ["手不要僵"], fuse=fuse
    )

    assert len(calls) == 2
    assert prompt == "@图片1 中的猫在跑（已融合）"  # 原样采用，不追加 must_avoid
    assert REWORK_MARKER not in prompt
    assert truncated is False
    assert must_avoid == ["手不得僵"]


async def test_build_rework_prompt_gives_up_after_attempt_limit() -> None:
    """重试用尽仍不合契约，才回退安全拼接（兜底不能让返工整体失败）。"""
    calls: list[int] = []

    async def fuse(original_prompt: str, requirements: list[str]):
        calls.append(1)
        return "丢了 @图片N 的结果"

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "@图片1 中的猫在跑", ["手不要僵"], fuse=fuse
    )

    assert len(calls) == 4  # _FUSION_ATTEMPTS（2026-09-18 从 3 提到 4，配合限流退避）
    # 同上：兜底不再拼接，正文原样。
    assert prompt == "@图片1 中的猫在跑"


async def test_build_rework_prompt_does_not_retry_upstream_exception(
    caplog,
) -> None:
    """上游抛错不重试：httpx 客户端已经带了 max_retries=2，再叠重试只会拖慢返工。"""
    calls: list[int] = []

    async def fuse(original_prompt: str, requirements: list[str]):
        calls.append(1)
        raise RuntimeError("上游 500")

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert len(calls) == 1
    # 融合不可用时**保持正文原样**：不再把要求拼到末尾（用户 2026-09-18：
    # 「不要越叠越多，就地在正文里改」）。要求仍在 requirements 里累积。
    assert prompt == "原始画面"


async def test_build_rework_prompt_logs_rejection_reason(caplog) -> None:
    """回退必须留下原因，否则生产上只能看到「又变成直接加返工要求」。"""
    import logging

    async def fuse(original_prompt: str, requirements: list[str]):
        return {"prompt": "丢了 @图片N 的结果", "must_avoid": []}

    with caplog.at_level(logging.WARNING):
        await build_rework_prompt(
            "@图片1 中的猫在跑", ["手不要僵"], fuse=fuse
        )

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "丢了素材引用" in joined
    assert "@图片1" in joined


# --- 2026-09-28 生产实测：返工的两个反向故障 ---
#
# 同一周内用户先报「两次修改都没有改动我的提示词，都提示不用改」，后报
# 「它会改一些我没有说的问题，即使本来是对的，它也要加一些词」。查下来是
# 同一个洞的两面：看片没带提示词 → 融合拿不到差距 → 要么原样退回、要么自己
# 发明问题。契约当时两条都拦不住。


def test_fusion_rejected_when_result_is_identical_to_base() -> None:
    """逐字（或只差空白）交回原文 = 没落实任何要求，必须判不合格。

    2026-09-28 实测：14:05 那轮只删了 17 个换行符、14:17 那轮逐字节相同，
    两次都通过契约进了生产，用户看到的是「本次返工没有改变提示词正文」。
    """
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    assert "一致" in fusion_rejection_reason("一只猫在跑", base_prompt="一只猫在跑")
    assert "一致" in fusion_rejection_reason(
        "一只猫\n在跑  ", base_prompt="一只猫在跑"
    )


def test_fusion_accepted_when_only_wording_changed() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    assert fusion_rejection_reason("一只猫缓缓走开", base_prompt="一只猫在跑") is None


_BUS_TAKE = "镜头 1：红色玩具巴士停在石板台上，木质网格切板自上而下切割车身。"

#: 2026-09-28 真实看片描述里的物件（生产记录原文摘录）。
_BUS_VISUAL = (
    "约4.7秒起镜头突然横移，出现黑色多孔电木块（被银色金属细杆与透光底平台"
    "隔开），6.5秒后画面转为中远景，可见水泥砖墙、带脚轮工具柜与黄色电缆。"
)


def test_fusion_rejected_when_it_turns_the_picture_into_prohibitions() -> None:
    """把看片描述里的杂物抄成「不许出现××」= 用户没提过的新约束，必须拦住。

    2026-09-28 14:57 那轮的真实产出（净 +85 字）：「…不出现黑色多孔电木/胶木块、
    银色金属细杆、透光底平台、水泥砖墙、带脚轮工具柜、黄色电缆等任何其他物件」。
    这些物件名全部来自上一版成片的看片描述 —— 用户从没说过一个。
    """
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    fused = _BUS_TAKE + (
        "画面中不出现黑色多孔电木块、银色金属细杆、透光底平台、水泥砖墙、"
        "带脚轮工具柜与黄色电缆。"
    )

    reason = fusion_rejection_reason(
        fused, base_prompt=_BUS_TAKE, visual_context=_BUS_VISUAL
    )

    assert reason is not None
    assert "画面描述" in reason


def test_picture_copy_guard_needs_the_visual_context() -> None:
    """没有画面上下文时不能凭这段文字判死（否则正常提示词会被误伤）。"""
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    fused = _BUS_TAKE + "画面中不出现黑色多孔电木块、水泥砖墙与黄色电缆。"

    assert (
        fusion_rejection_reason(fused, base_prompt=_BUS_TAKE, visual_context="")
        is None
    )


def test_fusion_accepted_when_it_only_rewrites_the_sentence_that_was_wrong() -> None:
    """正常返工（就地改那句错的）不许被新判据误伤。"""
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_rejection_reason,
    )

    fused = _BUS_TAKE.replace(
        "木质网格切板自上而下切割车身",
        "木质网格切板自上而下切割车身、一路贯穿到底，车身作为刚体不变形",
    )

    assert (
        fusion_rejection_reason(
            fused, base_prompt=_BUS_TAKE, visual_context=_BUS_VISUAL
        )
        is None
    )


async def test_build_rework_prompt_tells_the_model_why_retrying() -> None:
    """重试必须带上「上一次为什么不合格」—— 同一输入原样重发 4 次毫无意义。

    2026-09-28 实测：融合器原样返回时，旧代码只是把同样的请求再发 3 遍，
    第 4 次失败后回退成"正文原样"，用户什么也得不到。
    """
    seen: list[str] = []

    async def fuse(prompt, requirements, *, visual_context="", retry_feedback=""):
        seen.append(retry_feedback)
        if len(seen) == 1:
            return {"prompt": prompt, "must_avoid": []}  # 原样返回 → 判不合格
        return {"prompt": f"{prompt}（已融合）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse
    )

    assert prompt == "原始画面（已融合）"
    assert seen[0] == ""
    assert "一致" in seen[1]


# --- 2026-09-28 用户要求：一次多模态调用（成片视频进同一次融合请求） ---

_TAKE_PART = {
    "type": "video_url",
    "video_url": {"url": "https://example.invalid/take.mp4"},
}


async def test_build_rework_prompt_passes_the_take_video_to_the_fuser() -> None:
    """成片作为 video_url part 传进融合器 —— 不再先转成一段文字描述。"""
    seen: list[object] = []

    async def fuse(
        prompt, requirements, *, visual_context="", media_parts=None
    ):
        seen.append(media_parts)
        return {"prompt": f"{prompt}（已融合）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse, media_parts=[_TAKE_PART]
    )

    assert prompt == "原始画面（已融合）"
    assert seen == [[_TAKE_PART]]


async def test_build_rework_prompt_retry_keeps_the_take_video() -> None:
    """重试时视频要一起重发，否则第 2 次起模型又看不到成片了。"""
    seen: list[object] = []

    async def fuse(
        prompt, requirements, *, media_parts=None, retry_feedback=""
    ):
        seen.append(media_parts)
        if len(seen) == 1:
            return {"prompt": prompt, "must_avoid": []}  # 原样 → 判不合格
        return {"prompt": f"{prompt}（已融合）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=fuse, media_parts=[_TAKE_PART]
    )

    assert prompt == "原始画面（已融合）"
    assert seen == [[_TAKE_PART], [_TAKE_PART]]


async def test_build_rework_prompt_tolerates_fuser_without_media_parts() -> None:
    """老融合器不收 media_parts —— 不能因此报错（签名探测兜底）。"""

    async def legacy_fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {"prompt": f"{original_prompt}（旧融合器）", "must_avoid": []}

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"], fuse=legacy_fuse, media_parts=[_TAKE_PART]
    )

    assert "旧融合器" in prompt


# --- 2026-09-29 用户要求：「把累积的规则删了」 ---


def test_fusion_requirements_takes_only_this_round() -> None:
    """喂给融合的只有本轮要求 —— 历史不再重喂（见 test_optimize_prompts 的实测说明）。"""
    from feishu_generation_agent.integrations.rework_prompt import (
        fusion_requirements,
    )

    assert fusion_requirements("手不要僵") == ["手不要僵"]
    assert fusion_requirements("  背景太暗  ") == ["背景太暗"]
    assert fusion_requirements("") == []
    assert fusion_requirements(None) == []


def test_rework_inputs_still_accumulates_for_the_record() -> None:
    """`rework_inputs` 的累积语义**不变**（记录 + 重新规划的历史都靠它）。"""
    from types import SimpleNamespace

    from feishu_generation_agent.integrations.rework_prompt import rework_inputs

    task = SimpleNamespace(
        prompt="上一版正文",
        rework_requirements=["历史要求"],
        rework_base_prompt="冻结的第一版",
    )

    base_prompt, requirements = rework_inputs(task, "本轮要求")

    assert base_prompt == "上一版正文"
    assert requirements == ["历史要求", "本轮要求"]


# --- 2026-10-10「铲」任务：视觉要求被融合器落进了【声音】段 ---
#
# 真实数据（run cf40359d，需求「铲」）：用户本轮要求是
#   「上一版质感对了，但是这一版质感又变回去了，不是硬化后的，然后铲子铲的是
#     明确的红色区域，不要铲橙色」
# 融合结果的**唯一改动**是在【全局声音与字幕约束】里把「蜡片卷曲」改成
# 「蜡片脆性断裂卷曲」（+4 字），**视觉段一个字没动** —— 而这两段在上一版
# 已经逐字相同，所以用户看到的现象是「改了提示词，画面却没有任何变化」。
#
# 旧契约拦不住：`_same_text` 只拦「逐字返回」，新小节/结尾段增长两道判据都不适用
# （改动发生在原有小节内部）。所以补一条**落点**判据：本轮要求讲的是画面/材质时，
# 改动必须发生在**非声音段**的正文里。

_SPADE_BASE = (
    "【全局声音与字幕约束】不要BGM、不要字幕，只保留音效：铲子刮擦蜡层、蜡片卷曲、"
    "蜡豆碰撞堆叠与铲出蜡面的实拍声。\n"
    "【素材说明】@图片1 中的彩虹色蜡层与蜡豆布局。\n"
    "【一句话概述】真人手握铲子铲开蜡层。\n"
    "【全局视觉设定】全局风格：写实实拍产品特写，蜡体为明显硬化的实心蜡质。\n"
    "【详细情节】0-3秒：铲尖对准画面最右侧的红色条纹，沿条纹切下一条红色条状蜡。\n"
    "【全局连续性与结尾约束】桌面上不要出现任何 logo、品牌特征、水印与文字。"
)

_SPADE_VISUAL_REQUIREMENT = (
    "上一版质感对了，但是这一版质感又变回去了，不是硬化后的，"
    "然后铲子铲的是明确的红色区域，不要铲橙色"
)


def test_fusion_rejected_when_a_visual_requirement_lands_only_in_the_audio_section() -> None:
    fused = _SPADE_BASE.replace("蜡片卷曲", "蜡片脆性断裂卷曲")

    reason = fusion_rejection_reason(
        fused,
        base_prompt=_SPADE_BASE,
        requirements=[_SPADE_VISUAL_REQUIREMENT],
    )

    assert reason is not None, "只在声音段改字、视觉段一字未动，必须判不合格"
    assert "声音" in reason or "音效" in reason


def test_fusion_accepted_when_the_visual_requirement_is_woven_into_the_visual_section() -> None:
    """就地改进视觉句（哪怕声音段同时也有改动）照旧放行 —— 别把正常返工拦死。"""
    fused = _SPADE_BASE.replace("蜡片卷曲", "蜡片脆性断裂卷曲").replace(
        "铲尖对准画面最右侧的红色条纹",
        "铲尖只对准画面最右侧的红色条纹、铲面始终不接触相邻的橙色条纹",
    )

    assert (
        fusion_rejection_reason(
            fused,
            base_prompt=_SPADE_BASE,
            requirements=[_SPADE_VISUAL_REQUIREMENT],
        )
        is None
    )


def test_fusion_accepted_when_an_audio_requirement_changes_the_audio_section() -> None:
    """要求本身就在讲音效时，落在声音段正是**正确**落点。"""
    fused = _SPADE_BASE.replace("蜡片卷曲", "蜡片脆性断裂卷曲")

    assert (
        fusion_rejection_reason(
            fused,
            base_prompt=_SPADE_BASE,
            requirements=["只要铲、蜡和蜡豆的动作音效，不要任何BGM"],
        )
        is None
    )


def test_audio_landing_check_is_skipped_without_sections() -> None:
    """单镜头 / 文生视频没有分段结构，不做落点判断。"""
    assert (
        fusion_rejection_reason(
            "一只猫在缓慢行走",
            base_prompt="一只猫在跑",
            requirements=[_SPADE_VISUAL_REQUIREMENT],
        )
        is None
    )


# --- 2026-10-10：整篇「重新生成计划」要继承累积的返工要求（而不是清零） ---
#
# 真实数据（run e3ac684b）：用户点了「重新生成计划」，新 run 从
# `ingest_source → plan_requirements` 重跑，新任务的 `rework_requirements` 变成
# **空列表** —— 上一轮攒下的「蜡是比较硬化状态的」直接消失，用户不得不在
# 两小时后重新说一遍「蜡的质感是明显硬化的」。这就是「上一版质感对了，
# 这一版质感又变回去了」的来源。
#
# 这两条是纯函数：跨 run 收集 + 并回计划，接线在 runtime / nodes 里。


def test_collect_rework_requirements_unions_across_runs_in_order() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        collect_rework_requirements,
    )

    older = {"approved_plan": {"tasks": [{"rework_requirements": ["蜡要硬化"]}]}}
    newer = {
        "draft_plan": {
            "tasks": [
                {"rework_requirements": ["蜡要硬化", "只铲最右侧的红色"]},
                {"rework_requirements": ["不要铲橙色"]},
            ]
        }
    }

    collected = collect_rework_requirements([older, newer])

    assert collected == ["蜡要硬化", "只铲最右侧的红色", "不要铲橙色"]


def test_collect_rework_requirements_caps_the_history() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        collect_rework_requirements,
    )

    state = {
        "draft_plan": {
            "tasks": [{"rework_requirements": [f"要求{i}" for i in range(30)]}]
        }
    }

    collected = collect_rework_requirements([state], limit=5)

    assert len(collected) == 5
    # 留最近的 5 条
    assert collected == [f"要求{i}" for i in range(25, 30)]


def test_collect_rework_requirements_ignores_blank_and_broken_states() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        collect_rework_requirements,
    )

    assert collect_rework_requirements([]) == []
    assert collect_rework_requirements([{}, {"draft_plan": None}]) == []
    assert collect_rework_requirements(
        [{"draft_plan": {"tasks": [{"rework_requirements": ["", "  ", "真要求"]}]}}]
    ) == ["真要求"]


def test_inherit_rework_requirements_stamps_every_task() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        inherit_rework_requirements,
    )

    plan = {
        "tasks": [
            {"task_id": "task-1"},
            {"task_id": "task-2", "rework_requirements": ["本轮新要求"]},
        ]
    }

    stamped = inherit_rework_requirements(plan, ["蜡要硬化", "只铲最右侧的红色"])

    assert stamped == 2
    assert plan["tasks"][0]["rework_requirements"] == ["蜡要硬化", "只铲最右侧的红色"]
    # 累积语义：继承的在前，任务自己的在后
    assert plan["tasks"][1]["rework_requirements"] == [
        "蜡要硬化",
        "只铲最右侧的红色",
        "本轮新要求",
    ]


def test_inherit_rework_requirements_without_history_is_a_noop() -> None:
    from feishu_generation_agent.integrations.rework_prompt import (
        inherit_rework_requirements,
    )

    plan = {"tasks": [{"task_id": "task-1"}]}

    assert inherit_rework_requirements(plan, []) == 0
    assert "rework_requirements" not in plan["tasks"][0]
