"""Prompt 组装:System + User(含检索结果和可选图片)。"""
from __future__ import annotations

import re
from typing import Literal, TypedDict

from langchain_core.documents import Document


SYSTEM_PROMPT = """你是 AI Generation Portable Apps 的错误诊断助手。
职责:根据【知识库】片段,回答用户遇到的报错问题。
规则:
- 只使用【知识库】给出的信息回答;若 KB 里没有相关内容,明确告知"KB 里没有找到匹配的条目,建议联系开发者"
- 每条建议后标注参考的 KB 章节标题(格式:[参考:章节标题])
- 不要编造 API、命令、文件路径
- 回答尽量简短分步,面向非技术用户,使用中文

回答完成后,末尾**必须**追加一行(独占一行,不含其他文字):
【KB 覆盖度】完全命中 | 部分命中 | 未命中

判定标准:
- 完全命中:检索到的 KB 片段直接命中了用户报错,答案完全基于 KB
- 部分命中:KB 片段沾边但不完全对上;答案主要基于 KB 但可能不够精确
- 未命中:KB 片段和用户报错无关或明显缺失;若强答会误导

严禁:
- 用你的通用编程知识猜测 —— KB 未命中时,直接标 "未命中" 并告知用户 KB 无匹配"""


class Message(TypedDict):
    role: str
    content: list | str


def _format_kb_context(chunks: list[Document]) -> str:
    if not chunks:
        return "(检索到 0 条相关内容)"
    parts = []
    for c in chunks:
        title = c.metadata.get("error_title", "(无标题)")
        parts.append(f"### {title}\n{c.page_content}")
    return "\n\n".join(parts)


def build_messages(
    query_text: str,
    chunks: list[Document],
    image_data_urls: list[str] | None = None,
) -> list[Message]:
    """构造 OpenAI 格式 messages（DeepSeek 文本版）。

    image_data_urls 保留兼容旧机器人调用；视觉摘要应由 query_text
    在预处理阶段注入，DeepSeek 本身不直接接收原图。
    """
    kb_ctx = _format_kb_context(chunks)
    user_text = (
        f"== 知识库片段 ==\n{kb_ctx}\n\n"
        f"== 用户报错 ==\n"
        f"文本:{query_text or '(无文本)'}\n\n"
        f"请基于以上知识库给出诊断和解决方案。"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


CoverageValue = Literal["完全命中", "部分命中", "未命中", "未知"]


def parse_coverage_tag(text: str) -> CoverageValue:
    """从答复末尾提取 【KB 覆盖度】。缺 tag 视为 '未知'(保守走未命中分支)。"""
    m = re.search(r"【KB 覆盖度】\s*(完全命中|部分命中|未命中)", text)
    return m.group(1) if m else "未知"


def strip_coverage_tag(text: str) -> str:
    """去掉答复末尾的 【KB 覆盖度】xxx 行,面向用户展示时用。"""
    return re.sub(
        r"\n?【KB 覆盖度】\s*(完全命中|部分命中|未命中)\s*$",
        "",
        text.rstrip(),
    ).strip()
