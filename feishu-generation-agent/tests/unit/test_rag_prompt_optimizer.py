"""Unit tests for best-effort RAG prompt optimization."""

from __future__ import annotations

import httpx

from feishu_generation_agent.domain.plan import GenerationTask, TaskPlan
from feishu_generation_agent.integrations.rag_prompt_optimizer import (
    optimize_plan_prompts,
)

# Reference tokens used by the planner contract.
IMAGE = "@图片1"  # @图片N
VIDEO = "@视频1"  # @视频N
AUDIO = "@音频1"  # @音频N


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


def _plan(prompt: str) -> TaskPlan:
    return TaskPlan(
        document_summary="test",
        tasks=[
            GenerationTask(
                task_id="task-1",
                task_type="image_to_image",
                title="t",
                source_block_ids=["b1"],
                user_intent="u",
                prompt=prompt,
                aspect_ratio="1:1",
                image_size="1024x1024",
            )
        ],
        excluded_assets=[],
    )


async def test_ignores_when_rag_reports_not_detected() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": False})
    )
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://127.0.0.1:8900", client=client)
    assert result is plan
    assert result.tasks[0].prompt == plan.tasks[0].prompt


async def test_ignores_when_rag_reports_ok_false() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": False, "detected": True, "updated_prompt": "x"})
    )
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result is plan


async def test_replaces_prompt_when_rewrite_preserves_reference_tokens() -> None:
    original = f"{IMAGE} 生成一张图"
    updated = f"{IMAGE} 高质量版本"
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": True, "updated_prompt": updated})
    )
    plan = _plan(original)
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == updated
    assert result.tasks[0].prompt != original
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == "http://x/api/rag/preflight"
    assert kwargs["json"] == {"prompt": original, "optimize": True}


async def test_keeps_original_when_reference_tokens_change() -> None:
    original = f"{IMAGE} 生成一张图"
    updated = "无引用token的prompt"
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": True, "updated_prompt": updated})
    )
    plan = _plan(original)
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == original


async def test_keeps_original_when_request_raises() -> None:
    client = _FakeClient(exc=httpx.ConnectError("boom"))
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == plan.tasks[0].prompt


async def test_keeps_original_on_non_200() -> None:
    client = _FakeClient(_FakeResponse(500, {}))
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == plan.tasks[0].prompt


async def test_keeps_original_on_bad_json() -> None:
    client = _FakeClient(_FakeResponse(200, None, json_exc=ValueError("bad")))
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == plan.tasks[0].prompt


async def test_keeps_original_on_non_string_updated_prompt() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"ok": True, "detected": True, "updated_prompt": 42})
    )
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "http://x", client=client)
    assert result.tasks[0].prompt == plan.tasks[0].prompt


async def test_empty_base_url_returns_plan_unchanged() -> None:
    plan = _plan(f"{IMAGE} 生成一张图")
    result = await optimize_plan_prompts(plan, "   ")
    assert result is plan
