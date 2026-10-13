#!/usr/bin/env python3
"""只读：看各子应用当前有没有「非终态」任务，以及最近提交时间。

重启前必须看这个，不要看「最近有没有人提交」（本仓库多次因此打断同事任务）。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"succeeded", "failed", "completed", "cancelled", "canceled", "interrupted"}
APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina", "director", "previz"]

now = dt.datetime.now()
for app in APPS:
    f = ROOT / app / "state" / "activity_log.json"
    if not f.exists():
        print(f"{app:22s} (no activity_log)")
        continue
    d = json.loads(f.read_text(encoding="utf-8"))
    recs = d if isinstance(d, list) else (d.get("records") or [])
    live = [r for r in recs if str(r.get("status") or "").lower() not in TERMINAL]
    statuses = collections.Counter(str(r.get("status") or "?") for r in recs)
    latest = ""
    for r in recs:
        ts = str(r.get("updated_at") or r.get("created_at") or "")
        latest = max(latest, ts)
    age = ""
    try:
        t = dt.datetime.strptime(latest[:19], "%Y-%m-%d %H:%M:%S")
        age = f"{(now - t).total_seconds() / 60:.0f} 分钟前"
    except Exception:
        pass
    flag = "  <<< LIVE" if live else ""
    print(f"{app:22s} n={len(recs):3d} live={len(live)} latest={latest[:19]} ({age}) 状态分布={dict(statuses)}{flag}")
    for r in live[:5]:
        print(f"      live: {r.get('job_id')} user={r.get('username')} status={r.get('status')} title={str(r.get('title'))[:40]}")
