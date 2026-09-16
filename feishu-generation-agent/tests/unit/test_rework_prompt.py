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
    merge_requirements,
    parse_fusion_payload,
    rework_inputs,
    split_legacy_requirements,
)


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