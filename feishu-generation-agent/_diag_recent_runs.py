"""只读：列出今天（2026-10-10）的 run，并抠出每个 run 的原始错误关键词。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = (
    "invalid_media",
    "input media detect failed",
    "input_too_large",
    "video duration",
    "provider_message",
)
_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")

con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
con.row_factory = sqlite3.Row
ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
ccon.row_factory = sqlite3.Row

runs = con.execute(
    "select run_id, thread_id, status, source_url, created_at, updated_at, "
    "owner_user_id from runs where created_at >= '2026-10-10T00:00' "
    "order by created_at"
).fetchall()
print(f"今天的 run：{len(runs)} 条")
for run in runs:
    print("-" * 78)
    print(dict(run))
    events = con.execute(
        "select node, status, summary, created_at from events where run_id=? "
        "order by id desc limit 6",
        (run["run_id"],),
    ).fetchall()
    for event in events:
        print("   ev:", dict(event))
    hits: set[str] = set()
    for row in ccon.execute(
        "select channel, value from writes where thread_id=? "
        "order by checkpoint_id, idx",
        (run["thread_id"],),
    ):
        blob = row["value"]
        if not isinstance(blob, bytes):
            continue
        text = _PRINTABLE.sub(" ▸ ", blob.decode("utf-8", "replace"))
        if not any(key in text for key in KEYS):
            continue
        for key in KEYS:
            index = text.find(key)
            if index < 0:
                continue
            snippet = text[max(0, index - 200) : index + 260]
            if snippet in hits:
                continue
            hits.add(snippet)
            print(f"   err[{row['channel']}]: … {snippet}")
sys.stdout.flush()
