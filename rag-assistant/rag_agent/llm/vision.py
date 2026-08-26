"""视觉摘要（读报错截图）：用 t8star 的 OpenAI 兼容视觉模型（gpt-4o）。"""
from __future__ import annotations

import httpx

from rag_agent.config import Settings

_VISION_PROMPT = (
    "以下图片是 AI 生成应用的浏览器错误截图。"
    "请仅用一段短句，复述图中可见的错误关键信息："
    "HTTP 状态码、英文错误短语(如 copyright violation / real person / rate limit)、"
    "中文提示等。只描述图中所见，不要发挥、不要提解决方案。"
)


def summarize_error_screenshots(settings: Settings, image_data_urls: list[str]) -> str:
    if not image_data_urls:
        return ""
    content: list[dict] = [{"type": "text", "text": _VISION_PROMPT}]
    for url in image_data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    resp = httpx.post(
        f"{settings.openai_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json={
            "model": settings.vision_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 256,
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
