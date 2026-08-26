"""KB 索引流程:切分 → embedding → 写 ChromaDB。"""
from __future__ import annotations

from pathlib import Path

from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter


def split_markdown(markdown: str) -> list[Document]:
    """按 ## 章节切分 KB markdown。

    每个 ## 章节 → 一个 Document,metadata["error_title"] 为该 ## 标题。
    """
    if not markdown or not markdown.strip():
        raise ValueError("KB markdown 为空,拒绝切分")

    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("##", "error_title")],
        strip_headers=False,  # 保留 ## 标题在 page_content 中,让 embedding 能吃到标题
    )
    docs = splitter.split_text(markdown)

    # 过滤掉没有 error_title 的段(H1 前言那部分)
    docs = [d for d in docs if d.metadata.get("error_title")]

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
