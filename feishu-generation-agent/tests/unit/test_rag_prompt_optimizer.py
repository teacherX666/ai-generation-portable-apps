"""知识库规则查询的单测。

这里**不再**测「改写提示词」——那条路已经删掉了。现在的契约是：
拿文本去取命中的规则，任何失败都返回空列表，让规划照常继续。
"""

from __future__ import annotations

import httpx

from feishu_generation_agent.integrations.rag_prompt_optimizer import (
    format_knowledge_context,
    fetch_knowledge_rules,
)

BASE_URL = "http://127.0.0.1:8900"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, json_exc=None):
        self.status_code = status_code
        self._payload = payload
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


def _detected(matches):
    return _FakeResponse(200, {"ok": True, "detected": True, "matches": matches})


async def test_returns_matched_rules() -> None:
    client = _FakeClient(
        _detected([{"title": "【参数错误】:视频时长超限", "content": "时长必须 <=15 秒"}])
    )

    rules = await fetch_knowledge_rules("文档正文", BASE_URL, client=client)

    assert rules == [
        {"title": "【参数错误】:视频时长超限", "content": "时长必须 <=15 秒"}
    ]


async def test_request_asks_only_for_rules_not_a_rewrite() -> None:
    """关键约定：optimize 必须是 False。

    服务端只在 optimize=True 时才会去调导演台改写提示词
    （rag-assistant/app_fastapi.py:615）——那正是我们要避免的二次加工。
    """
    client = _FakeClient(_detected([]))

    await fetch_knowledge_rules("文档正文", BASE_URL, client=client)

    url, kwargs = client.calls[0]
    assert url == f"{BASE_URL}/api/rag/preflight"
    assert kwargs["json"] == {"prompt": "文档正文", "optimize": False}


async def test_returns_empty_when_not_detected() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": False, "matches": []})
    )

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_returns_empty_when_service_reports_not_ok() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": False, "detected": True, "matches": [{"title": "x"}]})
    )

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_returns_empty_on_non_200() -> None:
    client = _FakeClient(_FakeResponse(500, {"ok": True, "detected": True}))

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_returns_empty_when_request_raises() -> None:
    client = _FakeClient(exc=httpx.ConnectError("boom"))

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_returns_empty_on_bad_json() -> None:
    client = _FakeClient(_FakeResponse(200, None, json_exc=ValueError("bad")))

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_returns_empty_when_matches_is_not_a_list() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": True, "matches": "nope"})
    )

    assert await fetch_knowledge_rules("文档正文", BASE_URL, client=client) == []


async def test_skips_malformed_entries() -> None:
    client = _FakeClient(
        _detected(
            [
                "not-a-dict",
                {"title": "", "content": "   "},
                {"title": "有效规则", "content": "内容"},
            ]
        )
    )

    rules = await fetch_knowledge_rules("文档正文", BASE_URL, client=client)

    assert rules == [{"title": "有效规则", "content": "内容"}]


async def test_caps_number_of_rules() -> None:
    many = [{"title": f"规则{i}", "content": "内容"} for i in range(20)]
    client = _FakeClient(_detected(many))

    rules = await fetch_knowledge_rules("文档正文", BASE_URL, client=client)

    assert len(rules) == 5


async def test_blank_url_or_text_short_circuits_without_calling() -> None:
    client = _FakeClient(_detected([{"title": "x", "content": "y"}]))

    assert await fetch_knowledge_rules("文档正文", "   ", client=client) == []
    assert await fetch_knowledge_rules("   ", BASE_URL, client=client) == []
    assert client.calls == []


def test_format_knowledge_context_renders_rules() -> None:
    context = format_knowledge_context(
        [{"title": "【参数错误】:时长超限", "content": "时长必须 <=15 秒"}]
    )

    assert "【参数错误】:时长超限" in context
    assert "时长必须 <=15 秒" in context


def test_format_knowledge_context_is_empty_without_rules() -> None:
    assert format_knowledge_context([]) == ""
