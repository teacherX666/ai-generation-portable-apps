#!/usr/bin/env python3
"""只读：飞书 Agent 当前有没有在跑的运行（决定能否安全重启 8765）。"""
import collections
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
print("runs 状态分布:", dict(collections.Counter(r[0] for r in con.execute("select status from runs"))))
rows = list(con.execute(
    "select run_id, status, created_at, updated_at from runs where status in ('running','resuming')"
))
print(f"在跑（running/resuming）: {len(rows)}")
for run_id, status, created, updated in rows[:8]:
    print(f"  {run_id[:8]} {status} created={created[:19]} updated={updated[:19]}")
