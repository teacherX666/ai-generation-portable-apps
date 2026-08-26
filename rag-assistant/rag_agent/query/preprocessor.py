"""多模态查询预处理:检索走文本(摘要),生成走原图。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PreparedQuery:
    query_for_retrieval: str
    context_text_for_generation: str
    image_data_urls: list[str] = field(default_factory=list)
    image_summary: str = ""


# summarizer 类型:list[data_url] -> str(用于隔离网络调用便于测试)
Summarizer = Callable[[list[str]], str]


def prepare_query(
    text: str,
    image_data_urls: list[str],
    summarizer: Summarizer,
) -> PreparedQuery:
    """三分支预处理。

    - 纯文本:query = 原文;生成也用原文
    - 纯图:先摘图 → query = 摘要;生成用 (摘要作为 context 文本描述) + 原图
    - 文本 + 图:query = 文本 + 摘要;生成用原文本 + 原图
    """
    if not image_data_urls:
        return PreparedQuery(
            query_for_retrieval=text,
            context_text_for_generation=text,
            image_data_urls=[],
            image_summary="",
        )

    summary = summarizer(image_data_urls)

    if not text:
        return PreparedQuery(
            query_for_retrieval=summary,
            context_text_for_generation=f"用户仅附了图,自动摘要为:{summary}",
            image_data_urls=image_data_urls,
            image_summary=summary,
        )

    return PreparedQuery(
        query_for_retrieval=f"{text} {summary}",
        context_text_for_generation=f"{text}\n截图自动摘要:{summary}",
        image_data_urls=image_data_urls,
        image_summary=summary,
    )
