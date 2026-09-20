"""Best-effort RAG knowledge lookup for generation planning.

原来的做法是「planner 先写完提示词 → 再拿提示词去匹配知识库 → 命中就改写提示词」。
那是**两次提示词加工**，而 planner 本来就在写提示词，所以两者会互相打架；
而且改写是**逐任务**调导演台（一个 10 任务的计划就是 10 次调用）。

现在把知识库**提到规划之前**：拿文档文本取命中的规则，当上下文喂给 planner，
让它**一次**就把这些经验写进提示词。所以这里只做「取规则」一件事，
不做任何提示词改写 —— 请求里 `optimize=False`，服务端那条调导演台的代码
（`rag-assistant/app_fastapi.py:615`）根本不会执行。

失败一律静默：知识库不可用绝不能把一次成功的规划变成失败。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 20.0
# 命中规则太多会把 planner 的注意力稀释掉，只取分数最高的前几条。
_MAX_RULES = 5


async def fetch_knowledge_rules(
    text: str,
    base_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """取命中的知识库规则（每条含 title / content）。

    任何失败（服务不可达、非 200、返回体不合法、超时）都返回空列表，
    调用方拿到空列表时应当照常继续规划。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url or not (text or "").strip():
        return []
    owns_client = client is None
    if client is None:
        # trust_env=False：本机系统代理是 socks4://，httpx 不认这个 scheme，
        # 构造客户端时就会抛 "Unknown scheme for proxy URL"。
        client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, trust_env=False)
    try:
        response = await client.post(
            f"{url}/api/rag/preflight",
            json={"prompt": text, "optimize": False},
        )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _LOGGER.debug("rag knowledge lookup failed: %s", exc)
        return []
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        return []
    try:
        payload: Any = response.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    if not payload.get("ok") or not payload.get("detected"):
        return []
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return []
    rules: list[dict[str, str]] = []
    for item in matches[:_MAX_RULES]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title and not content:
            continue
        rules.append({"title": title, "content": content})
    return rules


def format_knowledge_context(rules: list[dict[str, str]]) -> str:
    """把规则拼成给 planner 读的上下文；没有规则时返回空串。"""
    if not rules:
        return ""
    blocks: list[str] = []
    for rule in rules:
        title = (rule.get("title") or "").strip() or "未命名规则"
        content = (rule.get("content") or "").strip()
        blocks.append(f"【{title}】\n{content}".strip())
    return "\n\n".join(blocks)
