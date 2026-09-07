"""DeepSeek 生成封装（替换原版 Claude）。"""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from shared import model_gateway  # noqa: E402


from rag_agent.config import Settings


def chat(
    settings: Settings,
    messages: list[dict],
    max_tokens: int = 2048,
) -> str:
    """强制 DeepSeek：提示词优化和 RAG 问答统一走 DeepSeek，避免本地 Qwen 慢或卡住。"""
    result = model_gateway.call_llm(
        messages,
        api_key=settings.deepseek_api_key,
        provider="deepseek",
        local_first=False,
        enable_thinking=False,
        temperature=0,
        max_tokens=max_tokens,
        timeout=180,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "LLM 调用失败"))
    return str(result.get("content", ""))
