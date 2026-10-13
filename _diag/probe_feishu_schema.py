#!/usr/bin/env python3
"""只读：飞书 Agent 库里 runs 表的结构与样本，判断「正文能不能直接取」。"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for table in ("runs", "artifacts", "operations"):
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error as exc:
        print(f"{table}: {exc}")
        continue
    n = con.execute(f"select count(*) from {table}").fetchone()[0]
    print(f"== {table}（{n} 行）==")
    print("   列:", cols)
    row = con.execute(f"select * from {table} limit 1").fetchone()
    if row:
        for k in row.keys():
            v = row[k]
            s = str(v)
            print(f"     {k} = {s[:110]!r}")
    print()
