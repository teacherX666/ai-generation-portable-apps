#!/usr/bin/env python3
"""只读：被重启打断的那条飞书运行现在什么状态，以及有没有恢复痕迹。"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("== 6b2b801a 现状 ==")
for row in con.execute("select * from runs where run_id like '6b2b801a%'"):
    for k in row.keys():
        print(f"   {k} = {row[k]}")
    rid = row["run_id"]

print("\n== 它的最近事件（看有没有恢复/失败记录）==")
for row in con.execute(
    "select * from events where run_id = ? order by rowid desc limit 8", (rid,)
):
    d = dict(row)
    print("  ", {k: str(v)[:80] for k, v in d.items() if k in ("kind", "status", "message", "created_at", "event_type")})

print("\n== 当前所有运行状态分布 ==")
import collections
for status, n in con.execute("select status, count(*) from runs group by status"):
    print(f"   {status}: {n}")
