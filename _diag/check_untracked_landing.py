#!/usr/bin/env python3
"""核对：重启（17:09:29）之后新建的飞书 run，有没有被 Portal 的「兜底登记」记上。

Portal 重启后才有兜底登记代码，所以只看 17:09 之后创建的 run。
"""
import collections
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
SINCE = dt.datetime(2026, 10, 12, 17, 9, 29)  # Portal 本次重启时刻（本机时间）

con = sqlite3.connect(f"file:{ROOT / 'feishu-generation-agent' / 'data' / 'agent.sqlite3'}?mode=ro", uri=True)
rows = list(con.execute("select run_id, status, created_at from runs order by created_at desc limit 40"))
new_runs = []
for run_id, status, created in rows:
    try:
        when = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        continue
    if when >= SINCE:
        new_runs.append((run_id, status, when.strftime("%H:%M:%S")))
print(f"Portal 重启（17:09:29）之后新建的飞书 run：{len(new_runs)} 个")
for run_id, status, when in new_runs[:10]:
    print(f"   {run_id[:8]} {status:<16} {when}")

usage = json.loads((STATE / "usage.json").read_text(encoding="utf-8"))
today = dt.date.today().isoformat()
print("\ndaily.jobs（今天）:", {a: s.get("jobs") for a, s in ((usage.get('daily') or {}).get(today) or {}).items() if s.get("jobs")})

gen = STATE / "generations.jsonl"
untracked = []
if gen.exists():
    for line in gen.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("price_source") == "untracked" or rec.get("tracked") is False:
            untracked.append(rec)
print(f"\n兜底登记（price_source=untracked）的明细条数：{len(untracked)}")
for rec in untracked[-6:]:
    print(f"   {rec.get('time')} {rec.get('username')} {rec.get('app')} status={rec.get('status')} tokens={rec.get('tokens')} amount={rec.get('amount')} title={str(rec.get('title'))[:30]!r}")

print(f"\n今天全部明细按应用：{dict(collections.Counter(json.loads(l).get('app') for l in gen.read_text(encoding='utf-8').splitlines() if l.strip())) if gen.exists() else {}}")
