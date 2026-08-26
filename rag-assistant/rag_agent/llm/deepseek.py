"""DeepSeek 生成封装（替换原版 Claude）。"""
from __future__ import annotations

import httpx

from rag_agent.config import Settings


def chat(
    settings: Settings,
    messages: list[dict],
    max_tokens: int = 2048,
) -> str:
    """非流式生成。messages 为 OpenAI 格式：
    [{"role": "system"|"user"|"assistant", "content": "..."}]
    返回纯文本。
    """
    resp = httpx.post(
        f"{settings.deepseek_base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        json={
            "model": settings.deepseek_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
