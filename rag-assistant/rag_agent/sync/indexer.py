"""KB 索引流程：按 KB 条目切分 → embedding → 写 ChromaDB。

这里故意不使用固定字数/固定 token 的切分器。KB 本身每个 ``##`` 标题就是
一个完整的错误条目，下面通常包含症状、触发条件、原因、解决方案和关键词。
把一个条目拆开会让检索拿到“半截答案”，也更容易把相邻错误混在一起。
"""
from __future__ import annotations

import re
from pathlib import Path

from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings


_SECTION_RE = re.compile(r"^##[ \t]+(.+?)\s*$", re.MULTILINE)
_KEYWORDS_RE = re.compile(r"^关键词[：:][ \t]*(.+?)\s*$", re.MULTILINE)


def _section_keywords(content: str) -> str:
    """提取 KB 明确写出的关键词，供混合检索使用。"""
    match = _KEYWORDS_RE.search(content)
    return match.group(1).strip() if match else ""


def split_markdown(markdown: str) -> list[Document]:
    """按 ``##`` KB 条目切分，每个条目保持完整，不按固定长度硬切。

    返回的每个 Document 都包含一个完整的 ``##`` 章节，并保留标题、关键词
    和章节序号元数据。这样重新同步 KB 后，关键词检索可以优先找到明确的
    错误码/错误短语，而语义检索仍然可以处理用户的自然语言描述。
    """
    if not markdown or not markdown.strip():
        raise ValueError("KB markdown 为空,拒绝切分")

    matches = list(_SECTION_RE.finditer(markdown))
    if not matches:
        raise ValueError("KB 切分后 chunk 数为 0,请检查文档是否含 ## 章节")

    docs: list[Document] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        title = match.group(1).strip()
        if not content or not title:
            continue
        docs.append(
            Document(
                page_content=content,
                metadata={
                    "error_title": title,
                    "section_index": index,
                    "kb_keywords": _section_keywords(content),
                    "chunk_strategy": "markdown_section",
                },
            )
        )

    if not docs:
        raise ValueError("KB 切分后 chunk 数为 0,请检查文档是否含 ## 章节")
    return docs


def embed_and_write(
    docs: list[Document],
    embeddings: Embeddings,
    persist_dir: Path,
    collection_name: str,
) -> Chroma:
    """对 docs 做 embedding 并写入指定 collection。

    返回构造好的 Chroma vector store(供调用方可选地立即验证)。
    """
    persist_dir.mkdir(parents=True, exist_ok=True)
    vs = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        collection_name=collection_name,
        persist_directory=str(persist_dir),
    )
    return vs
