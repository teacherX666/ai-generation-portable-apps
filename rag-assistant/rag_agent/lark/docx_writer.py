"""飞书 docx 文档写入:追加块(text block only,极简)。

策略:markdown 按 \\n\\n 分段 → 每段一个 text block → 一次性追加到文档 root block。
副作用:飞书里显示为多个纯文本段(##、** 等 markdown 语法作为普通文本),
但足够 copy 用(选中段落 → 粘贴到 Text Editor 就是真 markdown)。
"""
from __future__ import annotations

import logging

import lark_oapi as lark
from lark_oapi.api.docx.v1 import (
    Block,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    Text,
    TextElement,
    TextRun,
)

logger = logging.getLogger(__name__)


def _make_text_block(content: str) -> Block:
    """构造一个 text block(block_type=2),单段纯文本。"""
    text_run = TextRun.builder().content(content).build()
    element = TextElement.builder().text_run(text_run).build()
    text = Text.builder().elements([element]).build()
    return Block.builder().block_type(2).text(text).build()


def append_markdown_section(
    client: lark.Client,
    doc_id: str,
    markdown_section: str,
) -> None:
    """把 markdown 段落追加到飞书文档末尾。

    注意:doc_id 必须是 docx document_id(不是 wiki node token)。若你手上是 wiki token,
    请先用 sync.lark_fetcher._resolve_wiki_token() 解析。
    """
    paragraphs = [p.strip() for p in markdown_section.split("\n\n") if p.strip()]
    if not paragraphs:
        logger.warning("empty markdown section, skip append")
        return

    children = [_make_text_block(p) for p in paragraphs]

    req = (
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(doc_id)
        .block_id(doc_id)  # root block id == document id
        .request_body(
            CreateDocumentBlockChildrenRequestBody.builder()
            .children(children)
            .build()
        )
        .build()
    )
    resp = client.docx.v1.document_block_children.create(req)
    if not resp.success():
        raise RuntimeError(
            f"append blocks failed: code={resp.code} msg={resp.msg} "
            f"log_id={resp.get_log_id()} doc_id={doc_id}"
        )
    logger.info("appended %d blocks to doc %s", len(children), doc_id)
