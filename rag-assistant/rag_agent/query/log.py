"""问答日志 JSONL 追加写。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_query_log(
    path: Path,
    user_id: str,
    query: str,
    image_count: int,
    retrieved_titles: list[str],
    answer: str,
    latency_ms: int,
    metadata: dict | None = None,
) -> None:
    """追加一条问答日志到 JSONL 文件。

    metadata: 可选,附加结构化元数据(如 coverage / confidence / candidate_written 等
    self-learn 相关字段),若提供则作为 record 的 "metadata" 键写入。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _now_iso(),
        "user": user_id,
        "query": query,
        "image_count": image_count,
        "retrieved": retrieved_titles,
        "answer": answer,
        "latency_ms": latency_ms,
    }
    if metadata is not None:
        record["metadata"] = metadata
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
