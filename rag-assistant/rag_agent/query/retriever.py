"""KbRetriever:每次查询读 sync_status.json 决定 active collection。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from chromadb.errors import NotFoundError
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)


class KbRetriever:
    def __init__(
        self,
        chroma_dir: Path,
        status_path: Path,
        embeddings: Embeddings,
        top_k: int = 5,
    ) -> None:
        self._chroma_dir = chroma_dir
        self._status_path = status_path
        self._embeddings = embeddings
        self._top_k = top_k

    def _active_collection(self) -> str:
        if not self._status_path.exists():
            raise RuntimeError(
                f"sync_status.json 不存在:{self._status_path};"
                "请先运行 `uv run python -m rag_agent.sync` 建库"
            )
        data = json.loads(self._status_path.read_text(encoding="utf-8"))
        active = data.get("active_collection")
        if not active:
            raise RuntimeError("sync_status.json 中缺 active_collection 字段")
        return active

    def _do_retrieve(self, query: str) -> list[Document]:
        collection = self._active_collection()
        vs = Chroma(
            collection_name=collection,
            persist_directory=str(self._chroma_dir),
            embedding_function=self._embeddings,
        )
        return vs.similarity_search(query, k=self._top_k)

    def retrieve(self, query: str) -> list[Document]:
        """检索 top-K,每次调用重新读 status 以感知蓝绿切换。

        若 chromadb 抛 NotFoundError(蓝绿切换极短窗口内 collection 被删),
        重读 status 后重试一次;第二次再失败就抛出去。
        """
        try:
            return self._do_retrieve(query)
        except NotFoundError:
            logger.warning(
                "collection not found during retrieval, retrying with fresh status"
            )
            return self._do_retrieve(query)
