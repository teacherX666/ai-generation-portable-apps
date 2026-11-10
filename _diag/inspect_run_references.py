"""查 run 31770465 的归属、任务与参考素材 order/role（只读）。"""
import sqlite3
import json
import sys
from pathlib import Path

DATA = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent\data")
RUN = "31770465-b994-4e7e-98f5-187481a00d7e"

for db in ["agent.sqlite3", "production-bitable.sqlite3"]:
    p = DATA / db
    if not p.exists():
        continue
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    print(f"=== {db} ===")
    for (name,) in c.execute("select name from sqlite_master where type='table'"):
        print("  table:", name)
    c.close()

# 找出 run 的归属
c = sqlite3.connect(f"file:{DATA / 'agent.sqlite3'}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for tbl in ("runs", "run", "sessions"):
    try:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})")]
    except sqlite3.Error:
        continue
    if not cols:
        continue
    print(f"--- {tbl} 列: {cols}")
    try:
        rows = list(c.execute(f"select * from {tbl} limit 3"))
        for r in rows:
            d = {k: (str(r[k])[:120] if r[k] is not None else None) for k in r.keys()}
            print("   ", json.dumps(d, ensure_ascii=False))
    except sqlite3.Error as exc:
        print("   query err", exc)
c.close()
