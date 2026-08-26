"""候选池写入:fingerprint 去重 + section 构造 + 飞书 docx 追加。"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import lark_oapi as lark

from rag_agent.lark.docx_writer import append_markdown_section
from rag_agent.self_learn.analyzer import AnalysisResult
from rag_agent.sync.lark_fetcher import _resolve_wiki_token, fetch_kb_markdown

logger = logging.getLogger(__name__)


def _fingerprint(query_text: str) -> str:
    """query_text 的 sha256 前 8 位十六进制,用于去重。"""
    return hashlib.sha256(query_text.encode("utf-8")).hexdigest()[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _build_candidate_section(fp: str, analysis: AnalysisResult, query_text: str) -> str:
    """构造候选池要追加的 markdown 段。

    组成:HTML 注释元信息 4 行(fp / ts / query / sources)+ kb_candidate_section 原文 + 分隔线。
    """
    ts = _now_iso()
    sources_line = ", ".join(analysis.cited_sources) if analysis.cited_sources else "(无)"
    return (
        f"<!-- fp:{fp} -->\n"
        f"<!-- ts:{ts} -->\n"
        f'<!-- query:"{query_text}" -->\n'
        f"<!-- sources:{sources_line} -->\n"
        f"\n"
        f"{analysis.kb_candidate_section}\n"
        f"\n---\n"
    )


def write_candidate_if_new(
    api_client: lark.Client,
    pending_doc_token: str,
    analysis: AnalysisResult,
    query_text: str,
) -> bool:
    """写候选到候选池;fp 已存在则跳过。返回 True=已写,False=重复跳过。

    - pending_doc_token 可以是 wiki node token 或 docx doc_id
    - 先拉候选池 markdown 检查 fp;若已存在 skip
    - fetch 用 fetch_kb_markdown(会自动 wiki→docx 解析)
    - append 用 docx_writer.append_markdown_section,doc_id 必须是 docx(需先解析)
    """
    fp = _fingerprint(query_text)

    # 候选池可能是空的(初始状态或用户清理后),fetch_kb_markdown 会抛 ValueError;
    # 这种情况下视为空池,直接进入写入
    try:
        existing_md = fetch_kb_markdown(api_client, pending_doc_token)
    except ValueError as e:
        logger.info("pending pool appears empty (%s), treating as no dedupe check", e)
        existing_md = ""

    if f"<!-- fp:{fp} -->" in existing_md:
        logger.info("candidate with fp=%s already exists, skip", fp)
        return False

    section = _build_candidate_section(fp, analysis, query_text)

    resolved = _resolve_wiki_token(api_client, pending_doc_token) or pending_doc_token
    append_markdown_section(api_client, resolved, section)

    logger.info("wrote candidate fp=%s to pending pool", fp)
    return True
