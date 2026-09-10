"""Best-effort RAG prompt optimization for generated task prompts.

The Feishu module generates prompts directly (no separate optimize button), so
it should fold the rag-assistant generation knowledge base into the prompt at
generation time. This module calls the rag-assistant ``/api/rag/preflight``
endpoint with ``optimize=true`` and only accepts a rewritten prompt when the
rag service reports a detected rule and the rewrite preserves every reference
token (``@图片N`` / ``@视频N`` / ``@音频N``) from the original prompt.

Failures are intentionally silent: prompt optimization must never downgrade a
successful planning step into a failed run.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from feishu_generation_agent.domain.plan import TaskPlan

_LOGGER = logging.getLogger(__name__)
_REFERENCE_TOKEN = re.compile(r"@(?:图片|视频|音频)\d+")
_TIMEOUT_SECONDS = 20.0


def _reference_tokens(prompt: str) -> set[str]:
    return set(_REFERENCE_TOKEN.findall(prompt or ""))


async def optimize_plan_prompts(
    plan: TaskPlan,
    base_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> TaskPlan:
    """Optimize every task prompt through the RAG preflight endpoint.

    Returns a new :class:`TaskPlan` with the updated prompts (or the original
    plan unchanged when the RAG service is unavailable or has no matching rule).
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return plan
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, trust_env=False)
    try:
        tasks = []
        changed = False
        for task in plan.tasks:
            prompt = task.prompt
            updated = await _optimize_one(client, url, prompt)
            if updated and updated != prompt:
                prompt = updated
                changed = True
            tasks.append(task.model_copy(update={"prompt": prompt}))
        if not changed:
            return plan
        return plan.model_copy(update={"tasks": tasks})
    finally:
        if owns_client:
            await client.aclose()


async def _optimize_one(
    client: httpx.AsyncClient,
    base_url: str,
    prompt: str,
) -> str | None:
    if not (prompt or "").strip():
        return None
    try:
        response = await client.post(
            f"{base_url}/api/rag/preflight",
            json={"prompt": prompt, "optimize": True},
        )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _LOGGER.debug("rag prompt optimization request failed: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("ok") or not payload.get("detected"):
        return None
    updated = payload.get("updated_prompt")
    if not isinstance(updated, str) or not updated.strip():
        return None
    if _reference_tokens(updated) != _reference_tokens(prompt):
        # The rewrite dropped or added reference tokens; keep the original to
        # avoid breaking the reference contract validation downstream.
        return None
    return updated.strip()
