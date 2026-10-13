"""只读：看某条 run 最近的事件与产物（判断它是不是真的在跑）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
run_id = sys.argv[1]
con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
con.row_factory = sqlite3.Row
print("--- run ---")
for row in con.execute("select * from runs where run_id=?", (run_id,)):
    print(dict(row))
print("--- last 25 events ---")
for row in con.execute(
    "select node, status, summary, created_at from events where run_id=? "
    "order by id desc limit 25",
    (run_id,),
):
    print(dict(row))
print("--- artifacts ---")
for row in con.execute(
    "select artifact_id, task_id, kind, size, status from artifacts where run_id=?",
    (run_id,),
):
    print(dict(row))
print("--- operations ---")
for row in con.execute(
    "select task_id, phase, provider, official_id, updated_at from operations "
    "where run_id=?",
    (run_id,),
):
    print(dict(row))
sys.stdout.flush()
