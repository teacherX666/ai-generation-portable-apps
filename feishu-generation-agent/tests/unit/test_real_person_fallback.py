"""Seedance 拒绝真人素材时自动改走真人类通道（用户 2026-09-18）。"""

from dataclasses import replace

from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.graph.nodes import _should_switch_to_portrait


class _Portrait:
    """只要不是 None 就算配了真人通道。"""


def _rejection(message: str) -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message=message,
            technical_detail="operation=submit; status=400",
            retryable=False,
        )
    )


class _Services:
    def __init__(self, portrait: object | None) -> None:
        self.portrait_video_generator = portrait


ARK_REAL_PERSON = (
    "Seedance 拒绝了请求：The request failed because the input image "
    "'content[1]' may contain real person. Request id: 0217897008087743"
)


def test_switches_when_ark_says_input_may_contain_real_person() -> None:
    assert _should_switch_to_portrait(
        _rejection(ARK_REAL_PERSON), "seedance2.5", _Services(_Portrait())
    )


def test_switches_for_chinese_wording_too() -> None:
    assert _should_switch_to_portrait(
        _rejection("请求被拒绝：输入图疑似真人"),
        "seedance2.5",
        _Services(_Portrait()),
    )


def test_does_not_switch_when_already_on_portrait_channel() -> None:
    assert not _should_switch_to_portrait(
        _rejection(ARK_REAL_PERSON), "volcengine_portrait", _Services(_Portrait())
    )


def test_does_not_switch_without_a_configured_portrait_channel() -> None:
    assert not _should_switch_to_portrait(
        _rejection(ARK_REAL_PERSON), "seedance2.5", _Services(None)
    )


def test_does_not_switch_for_unrelated_rejections() -> None:
    """别的拒绝（参数、时长…）不该乱切通道。"""
    assert not _should_switch_to_portrait(
        _rejection("Seedance 拒绝了请求：duration is not valid"),
        "seedance2.5",
        _Services(_Portrait()),
    )
