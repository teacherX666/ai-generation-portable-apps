"""供应商拒绝请求时，要把它的错误码与消息记下来（用户 2026-09-18 要求）。"""

import json

import httpx
import pytest

from feishu_generation_agent.domain.errors import ErrorCategory
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _response(status_code: int, payload: object) -> httpx.Response:
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(
        status_code,
        content=content,
        request=httpx.Request("POST", "https://ark.example/api/v3/tasks"),
    )


async def test_provider_error_reason_reads_code_and_message() -> None:
    """火山的 `{"error": {"code", "message"}}` 要两个都取到。"""
    response = _response(400, {
        "error": {
            "code": "InvalidParameter",
            "message": "The parameter duration is not valid for this model.",
        }
    })

    code, message = await SeedanceVideoGenerator._safe_provider_error_reason(response)

    assert code == "InvalidParameter"
    assert message == "The parameter duration is not valid for this model."


async def test_provider_error_reason_redacts_secret_like_text() -> None:
    """错误文本会进日志和界面 —— 里面像密钥的片段要抹掉。"""
    response = _response(403, {
        "error": {
            "code": "AuthenticationError",
            "message": "bad key ark-REDACTED",
        }
    })

    _code, message = await SeedanceVideoGenerator._safe_provider_error_reason(response)

    assert "ark-REDACTED" not in (message or "")
    assert "<已隐藏>" in (message or "")


async def test_provider_error_reason_tolerates_non_json_body() -> None:
    response = _response(502, b"<html>bad gateway</html>")

    code, message = await SeedanceVideoGenerator._safe_provider_error_reason(response)

    assert (code, message) == (None, None)


def test_http_error_puts_provider_message_into_detail_and_message() -> None:
    """400 的失败原因要带上供应商原话，否则排查只能猜。"""
    error = SeedanceVideoGenerator._http_error(
        "submit",
        400,
        provider_code="InvalidParameter",
        provider_message="duration is not valid",
    )

    assert error.detail.category is ErrorCategory.PROVIDER_TERMINAL
    assert "duration is not valid" in error.detail.message
    assert "provider_code=InvalidParameter" in error.detail.technical_detail
    assert "provider_message=duration is not valid" in error.detail.technical_detail


def test_http_error_without_provider_message_keeps_generic_text() -> None:
    error = SeedanceVideoGenerator._http_error("submit", 400)

    assert error.detail.message.startswith("Seedance 拒绝了请求")
    assert "provider_message" not in error.detail.technical_detail


def test_execution_error_appends_provider_reason() -> None:
    """失败记录里要把供应商原话带上（中文泛化文案 + 「：<原话>」）。"""
    from feishu_generation_agent.domain.errors import AgentError, ErrorDetail
    from feishu_generation_agent.graph.nodes import _execution_error

    error = AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message="Seedance 拒绝了请求",
            technical_detail=(
                "operation=submit; status=400; provider_code=InvalidParameter; "
                "provider_message=duration is not valid for this model"
            ),
            retryable=False,
        )
    )

    payload = _execution_error(error)

    assert payload["message"] == (
        "生成服务拒绝了请求：duration is not valid for this model"
    )
    # 有 provider_code 时记录的是它（`operation_providercode`），否则退回 `submit_400`。
    assert payload["code"] == "submit_invalidparameter"
    assert "technical_detail" not in payload


def test_execution_error_without_provider_reason_stays_generic() -> None:
    from feishu_generation_agent.domain.errors import AgentError, ErrorDetail
    from feishu_generation_agent.graph.nodes import _execution_error

    error = AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message="The workflow node could not be completed",
            technical_detail="operation=submit; status=400",
            retryable=False,
        )
    )

    payload = _execution_error(error)

    # 不能把英文内部话术露给用户。
    assert payload["message"] == "生成服务拒绝了请求"
