"""只读诊断：从 checkpoint 库里把最近失败 run 的**原始错误文本**抠出来。

不需要 msgpack：langgraph 的 writes 值里字符串是 UTF-8 明文，直接按可打印字符扫描
就能拿到 `invalid_media` / `input_too_large` 这类上游原文。
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYWORDS = (
    "invalid_media",
    "input media detect failed",
    "input_too_large",
    "duration",
    "temperature",
    "provider_message",
    "provider_code",
    "last_error",
)

_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")


def _readable(blob: bytes) -> str:
    text = blob.decode("utf-8", "replace")
    text = _PRINTABLE.sub(" ▸ ", text)
    return re.sub(r"( ▸ )+", " ▸ ", text)


def dump(run_id: str) -> None:
    con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select run_id, thread_id, status, source_url, created_at, updated_at "
        "from runs where run_id=?",
        (run_id,),
    ).fetchall()
    print("=" * 78)
    if not rows:
        print(f"run {run_id}: 不在 runs 表里（已删除）")
        thread = None
        ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
        got = ccon.execute(
            "select distinct thread_id from writes where thread_id=?",
            (run_id,),
        ).fetchall()
        if got:
            thread = run_id
            print(f"  但 checkpoints 里还有 thread {run_id}")
    else:
        print("run:", dict(rows[0]))
        thread = rows[0]["thread_id"]
    if not thread:
        return

    con2 = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
    con2.row_factory = sqlite3.Row
    print("--- events (最新 12 条) ---")
    for row in con2.execute(
        "select node, status, summary, created_at from events where run_id=? "
        "order by id desc limit 12",
        (run_id,),
    ):
        print("  ", dict(row))

    ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
    ccon.row_factory = sqlite3.Row
    print("--- writes 里命中关键词的原文 ---")
    seen: set[str] = set()
    for row in ccon.execute(
        "select channel, value from writes where thread_id=? order by checkpoint_id, idx",
        (thread,),
    ):
        blob = row["value"]
        if not isinstance(blob, bytes):
            continue
        text = _readable(blob)
        if not any(key in text for key in KEYWORDS):
            continue
        for key in KEYWORDS:
            index = text.find(key)
            if index < 0:
                continue
            snippet = text[max(0, index - 260) : index + 320]
            if snippet in seen:
                continue
            seen.add(snippet)
            print(f"  [{row['channel']}] … {snippet}")


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        "d92a4c4b-965d-4f32-ab15-51d28fede590",
        "fa2fae44-0000-0000-0000-000000000000",
        "13268f86-1522-43f4-ada4-35cdfc72b9e6",
    ]
    for target in targets:
        dump(target)
    sys.stdout.flush()
