"""SyncService:编排 fetch → split → embed → write → 蓝绿切换。"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from langchain_core.embeddings import Embeddings

from rag_agent.sync.indexer import embed_and_write, split_markdown

logger = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    last_sync: str
    chunk_count: int
    active_collection: str
    source_doc_id: str
    snapshot_path: str

    def to_dict(self) -> dict:
        return {
            "last_sync": self.last_sync,
            "chunk_count": self.chunk_count,
            "active_collection": self.active_collection,
            "source_doc_id": self.source_doc_id,
            "snapshot_path": self.snapshot_path,
        }


@dataclass
class SyncResult:
    dry_run: bool
    chunk_count: int
    chunk_titles: list[str]
    active_collection: str | None
    duration_seconds: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _next_collection_name(current: str | None) -> str:
    """从 kb_vN 生成 kb_v(N+1);无当前则从 kb_v1 开始。"""
    if not current or not current.startswith("kb_v"):
        return "kb_v1"
    try:
        n = int(current[len("kb_v"):])
        return f"kb_v{n + 1}"
    except ValueError:
        return "kb_v1"


class SyncService:
    def __init__(
        self,
        fetcher: Callable[[], str],  # 无参数,返回 markdown
        embeddings: Embeddings,
        chroma_dir: Path,
        snapshots_dir: Path,
        status_path: Path,
        doc_id: str,
    ) -> None:
        self._fetcher = fetcher
        self._embeddings = embeddings
        self._chroma_dir = chroma_dir
        self._snapshots_dir = snapshots_dir
        self._status_path = status_path
        self._doc_id = doc_id

    def _load_status(self) -> SyncStatus | None:
        if not self._status_path.exists():
            return None
        data = json.loads(self._status_path.read_text(encoding="utf-8"))
        return SyncStatus(**data)

    def _write_status_atomic(self, status: SyncStatus) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._status_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(status.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._status_path)

    def _delete_collection(self, name: str) -> None:
        """删除指定 collection,失败仅记 warning。"""
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(self._chroma_dir))
            client.delete_collection(name)
            logger.info("deleted old collection: %s", name)
        except Exception as e:
            logger.warning("delete old collection %s failed: %s (残留不影响)", name, e)

    def run(self, dry_run: bool = False) -> SyncResult:
        t0 = time.time()
        logger.info("sync start (dry_run=%s)", dry_run)

        markdown = self._fetcher()
        docs = split_markdown(markdown)
        titles = [d.metadata["error_title"] for d in docs]

        if dry_run:
            logger.info("dry-run: %d chunks", len(docs))
            for t in titles:
                logger.info("  - %s", t)
            return SyncResult(
                dry_run=True,
                chunk_count=len(docs),
                chunk_titles=titles,
                active_collection=None,
                duration_seconds=time.time() - t0,
            )

        # 备份快照
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot_name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".md"
        snapshot_path = self._snapshots_dir / snapshot_name
        snapshot_path.write_text(markdown, encoding="utf-8")

        # 蓝绿切换
        old = self._load_status()
        old_collection = old.active_collection if old else None
        new_collection = _next_collection_name(old_collection)

        embed_and_write(
            docs=docs,
            embeddings=self._embeddings,
            persist_dir=self._chroma_dir,
            collection_name=new_collection,
        )

        new_status = SyncStatus(
            last_sync=_now_iso(),
            chunk_count=len(docs),
            active_collection=new_collection,
            source_doc_id=self._doc_id,
            snapshot_path=str(snapshot_path),
        )
        self._write_status_atomic(new_status)
        logger.info("switched to %s", new_collection)

        if old_collection:
            self._delete_collection(old_collection)

        duration = time.time() - t0
        logger.info("sync done: %d chunks in %.1fs", len(docs), duration)
        return SyncResult(
            dry_run=False,
            chunk_count=len(docs),
            chunk_titles=titles,
            active_collection=new_collection,
            duration_seconds=duration,
        )
