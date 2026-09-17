"""返工提示词净化：要求只累积不覆盖、AI 融合、永不因超长失败。"""

from feishu_generation_agent.domain.plan import (
    SEEDANCE_PROMPT_MAX_CHARS,
    GenerationTask,
)
from feishu_generation_agent.integrations.rework_prompt import (
    REWORK_MARKER,
    build_fallback_prompt,
    build_rework_prompt,
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

    # 21 条 → 15 条（实测）。墨滴那一族 10 条 → 7 条：凭空出现/半空中、横向斜向偏移、
    # 上方或侧面生成/滴落这些换皮写法都并掉了。剩下 3 对是**结构完全改写**的同义句
    # （「脱离笔尖正下方的下落轨迹」vs「下落轨迹不得偏离」，集合相似度仅 0.47），
    # 确定性合并做不到，硬并就得靠语义推断 —— 那会吃掉真规则，所以不做。
    assert len(merged) <= 15, merged
    ink_drop = [item for item in merged if item.startswith("墨滴")]
    assert len(ink_drop) <= 7, ink_drop
    # 一条规则都不能丢：并掉的只是措辞。
    joined = "".join(ink_drop)
    for keyword in ("凭空", "轨迹", "侧面"):
        assert keyword in joined
    # 明显不同类的规则原样保留。
    assert "不要出现水印、贴纸、乱码文字或字幕" in merged
    assert "人物与物体不得悬浮或穿模" in merged


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


# --- 兜底拼接：必须包含全部要求，且永不超长、永不抛错 ---


def test_fallback_prompt_keeps_every_requirement() -> None:
    prompt, truncated = build_fallback_prompt("原始画面", ["手不要僵", "背景太暗"])
    assert "手不要僵" in prompt
    assert "背景太暗" in prompt
    assert truncated is False


def test_fallback_prompt_single_requirement_matches_legacy_format() -> None:
    prompt, _ = build_fallback_prompt("画面描述", ["动作再慢一点"])
    assert prompt == "画面描述\n【返工要求】动作再慢一点"


def test_fallback_prompt_truncates_base_instead_of_raising() -> None:
    base = "画" * SEEDANCE_PROMPT_MAX_CHARS
    prompt, truncated = build_fallback_prompt(base, ["背景太暗"])
    assert len(prompt) <= SEEDANCE_PROMPT_MAX_CHARS
    assert "背景太暗" in prompt
    assert truncated is True


def test_fallback_prompt_keeps_newest_when_requirements_alone_overflow() -> None:
    prompt, truncated = build_fallback_prompt(
        "原始画面",
        ["旧" * 1200, "中" * 1200, "新" * 1200],
    )
    assert len(prompt) <= SEEDANCE_PROMPT_MAX_CHARS
    assert truncated is True
    assert "新" in prompt
    assert "旧" not in prompt


def test_fallback_prompt_never_exceeds_limit_for_single_huge_requirement() -> None:
    prompt, truncated = build_fallback_prompt("原始画面", ["要" * 5000])
    assert len(prompt) <= SEEDANCE_PROMPT_MAX_CHARS
    assert truncated is True


def test_fallback_prompt_without_requirements_clips_base() -> None:
    base = "画" * (SEEDANCE_PROMPT_MAX_CHARS + 50)
    prompt, truncated = build_fallback_prompt(base, [])
    assert len(prompt) <= SEEDANCE_PROMPT_MAX_CHARS
    assert truncated is True


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


def test_fusion_accepted_when_contract_holds() -> None:
    assert is_acceptable_fusion(
        "@图片1 中的猫缓慢行走", base_prompt="@图片1 中的猫在跑"
    )


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

    assert prompt == f"原始画面\n{REWORK_MARKER}手不要僵"
    assert must_avoid == []


async def test_build_rework_prompt_falls_back_without_fuser() -> None:
    prompt, _truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["手不要僵"]
    )
    assert prompt == f"原始画面\n{REWORK_MARKER}手不要僵"
    assert must_avoid == []


async def test_build_rework_prompt_rejects_fusion_that_drops_tokens() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> str:
        return "丢了 @图片N 的结果"

    prompt, _truncated, _must_avoid = await build_rework_prompt(
        "@图片1 中的猫在跑", ["手不要僵"], fuse=fuse
    )

    assert prompt == f"@图片1 中的猫在跑\n{REWORK_MARKER}手不要僵"


# --- 双通道：融合时顺带产出「必须避免」清单，写进 negative_constraints ---


async def test_build_rework_prompt_returns_must_avoid_from_fusion() -> None:
    async def fuse(original_prompt: str, requirements: list[str]) -> dict:
        return {
            "prompt": f"{original_prompt}（已融合）",
            "must_avoid": ["红衣服老头不得跑出起跑线", "眼睛不得发光"],
        }

    prompt, truncated, must_avoid = await build_rework_prompt(
        "原始画面", ["不要让红衣服老头跑出去"], fuse=fuse
    )

    assert prompt == "原始画面（已融合）"
    assert truncated is False
    assert must_avoid == ["红衣服老头不得跑出起跑线", "眼睛不得发光"]


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
    assert prompt == "@图片1 中的猫在跑（已融合）"
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

    assert len(calls) == 3
    assert prompt == f"@图片1 中的猫在跑\n{REWORK_MARKER}手不要僵"


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
    assert prompt == f"原始画面\n{REWORK_MARKER}手不要僵"


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