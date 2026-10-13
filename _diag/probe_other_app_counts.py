#!/usr/bin/env python3
"""只读：飞书/短剧/画布今天真实发生的生成任务数（它们的日志格式不是 http.server，审计脚本覆盖不到）。"""
import collections
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
today = dt.date.today().isoformat()
print(f"今天 = {today}")

p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
n_today = con.execute("select count(*) from runs where created_at like ?", (today + "%",)).fetchone()[0]
n_total = con.execute("select count(*) from runs").fetchone()[0]
print(f"飞书 Agent：今天新建 run {n_today} 条（库内共 {n_total} 条）")

runs = ROOT / "drama-pipeline" / "state" / "runs"
if runs.exists():
    n = 0
    for d in runs.iterdir():
        f = d / "run.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if str(data.get("created_at") or "").startswith(today):
                n += 1
    print(f"AI 短剧：今天新建 run {n} 条（目录共 {sum(1 for d in runs.iterdir() if d.is_dir())} 个）")

usage = json.loads((ROOT / "portal" / "state" / "usage.json").read_text(encoding="utf-8"))
counted = {a: int((s or {}).get("jobs") or 0) for a, s in ((usage.get("daily") or {}).get(today) or {}).items()}
print("Portal 今天统计到的 jobs：", {k: v for k, v in counted.items() if v})
print(f"  其中 feishu-generation-agent = {counted.get('feishu-generation-agent', 0)}、"
      f"drama-pipeline = {counted.get('drama-pipeline', 0)}")
