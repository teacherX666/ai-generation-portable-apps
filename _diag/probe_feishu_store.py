#!/usr/bin/env python3
"""只读：飞书创作助手自己的库里还有多少 run（正文是否还在）。"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
print("表:", tables[:15])
for t in tables:
    if "run" in t.lower() or "task" in t.lower():
        try:
            n = con.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"  {t}: {n} 行")
        except sqlite3.Error as exc:
            print(f"  {t}: {exc}")
