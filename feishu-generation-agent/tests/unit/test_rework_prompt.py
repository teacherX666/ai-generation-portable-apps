"""返工提示词净化：要求只累积不覆盖、AI 融合、永不因超长失败。"""

from feishu_generation_agent.domain.plan import SEEDANCE_PROMPT_MAX_CHARS
from feishu_generation_agent.integrations.rework_prompt import (
    REWORK_MARKER,
    build_fallback_prompt,
    is_acceptable_fusion,
    merge_requirements,
    split_legacy_requirements,
)


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