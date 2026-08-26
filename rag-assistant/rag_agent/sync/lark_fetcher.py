"""从飞书拉取 KB 文档,转 markdown。

支持:
- wiki node token(URL 里 /wiki/XXX)—— 先解析 obj_token
- docx document_id(URL 里 /docx/XXX)

飞书应用需要的权限:
- docx:document:readonly
- wiki:wiki:readonly(若 KB 是 wiki 文档)

实现细节:用 docx.v1.document_block.list 拉块结构,把 heading1-9 转成 #~#########,
其他 block 转段落 / bullet / ordered / code。docx.raw_content 已被抛弃(它剥掉
markdown 层级,标题都变成裸段落,MarkdownHeaderTextSplitter 无从下手)。
"""
import logging
from typing import Iterable

import lark_oapi as lark
from lark_oapi.api.docx.v1 import Block, ListDocumentBlockRequest, TextElement
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

logger = logging.getLogger(__name__)


def _resolve_wiki_token(client: lark.Client, token: str) -> str | None:
    """尝试把 token 当作 wiki node token 解析出真实 obj_token。

    - 成功且 obj_type == docx:返回 obj_token
    - 成功但 obj_type != docx(sheet/bitable/doc 等):抛 ValueError
    - 失败(不是 wiki token / 网络异常 / SDK 异常):返回 None,让调用方 fallback
    """
    try:
        req = GetNodeSpaceRequest.builder().token(token).build()
        resp = client.wiki.v2.space.get_node(req)
    except Exception as exc:  # noqa: BLE001 — SDK 调用出错也走 fallback
        logger.warning("wiki.get_node raised, fallback to docx: %s", exc)
        return None

    if not resp.success():
        logger.info(
            "wiki.get_node non-success (fallback to docx): code=%s msg=%s log_id=%s token=%s",
            resp.code,
            resp.msg,
            resp.get_log_id(),
            token,
        )
        return None

    node = resp.data.node if resp.data else None
    if node is None or not node.obj_token or not node.obj_type:
        logger.warning("wiki.get_node returned empty node payload for token=%s", token)
        return None

    if node.obj_type != "docx":
        raise ValueError(
            f"KB 节点 {token} 的 obj_type={node.obj_type!r},当前只支持 docx 类型;"
            f"请把知识库文档改为新版 docx(不是 sheet/bitable/老 doc)。"
        )

    return node.obj_token


def _join_elements(elements: Iterable[TextElement] | None) -> str:
    """把 Text.elements 拼成纯文本,只取 text_run.content,其他类型 fallback 空串。"""
    if not elements:
        return ""
    parts: list[str] = []
    for el in elements:
        tr = getattr(el, "text_run", None)
        if tr is not None and tr.content:
            parts.append(tr.content)
        # mention_user / mention_doc / equation / reminder 等其他类型暂时忽略
    return "".join(parts)


# heading1..heading9 一起遍历一次比 9 个 if 干净
_HEADING_FIELDS = tuple(f"heading{i}" for i in range(1, 10))


def _block_to_markdown(block: Block) -> str | None:
    """把单个 block 转成 markdown 片段;返回 None 表示这块不产出内容(如 page 根、未知类型)。"""
    # heading1..9 → #..#########
    for i, field in enumerate(_HEADING_FIELDS, start=1):
        h = getattr(block, field, None)
        if h is not None:
            text = _join_elements(getattr(h, "elements", None)).strip()
            if not text:
                return None
            return f"{'#' * i} {text}"

    # 普通段落
    if block.text is not None:
        text = _join_elements(block.text.elements).strip()
        return text or None

    # 无序列表
    if block.bullet is not None:
        text = _join_elements(block.bullet.elements).strip()
        return f"- {text}" if text else None

    # 有序列表(不追踪序号,统一 1.,GitHub 风 markdown 会自增)
    if block.ordered is not None:
        text = _join_elements(block.ordered.elements).strip()
        return f"1. {text}" if text else None

    # 代码块
    if block.code is not None:
        content = _join_elements(block.code.elements)
        lang = ""
        style = getattr(block.code, "style", None)
        if style is not None and getattr(style, "language", None):
            # SDK 里 language 是数字枚举;拿不到映射就先留空,输出裸 fence 也能被 markdown 识别
            lang_val = style.language
            if isinstance(lang_val, str):
                lang = lang_val
        fence = f"```{lang}\n{content.rstrip()}\n```"
        return fence

    # quote / callout 里的文本直接当段落抛出来(如果有 elements 字段)
    for field in ("quote", "callout"):
        v = getattr(block, field, None)
        if v is not None:
            text = _join_elements(getattr(v, "elements", None)).strip()
            if text:
                return f"> {text}" if field == "quote" else text

    # 其它类型(page 根 / image / bitable / divider / table 等)先忽略
    return None


def _fetch_docx_content(client: lark.Client, document_id: str) -> str:
    """分页拉 blocks,拼成 markdown。"""
    blocks: list[Block] = []
    page_token: str | None = None
    while True:
        builder = (
            ListDocumentBlockRequest.builder()
            .document_id(document_id)
            .page_size(500)
        )
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.docx.v1.document_block.list(builder.build())
        if not resp.success():
            raise RuntimeError(
                f"lark list_document_block failed: code={resp.code} msg={resp.msg} "
                f"log_id={resp.get_log_id()} document_id={document_id}"
            )
        data = resp.data
        if data and data.items:
            blocks.extend(data.items)
        if not data or not data.has_more:
            break
        page_token = data.page_token
        if not page_token:
            break

    if not blocks:
        raise ValueError(f"KB 文档看起来是空的,请检查 document_id={document_id}")

    lines: list[str] = []
    for b in blocks:
        md = _block_to_markdown(b)
        if md is not None:
            lines.append(md)

    content = "\n\n".join(lines).strip()
    if not content:
        raise ValueError(
            f"KB 文档解析出的 markdown 为空,可能全是不支持的 block 类型;document_id={document_id}"
        )
    return content


def fetch_kb_markdown(client: lark.Client, token: str) -> str:
    """拉取 KB 文档 markdown。

    token 可以是:
      - wiki node token(URL 里 /wiki/XXX):先解析出 obj_token 再拉
      - docx document_id(URL 里 /docx/XXX):直接拉

    自动判断:先当 wiki token 解析,失败则 fallback 到 docx。
    """
    resolved = _resolve_wiki_token(client, token)
    if resolved:
        logger.info("resolved wiki token %s -> docx %s", token, resolved)
        return _fetch_docx_content(client, resolved)
    logger.info("token treated as docx document_id: %s", token)
    return _fetch_docx_content(client, token)
