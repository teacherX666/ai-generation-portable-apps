#!/usr/bin/env python3
"""统计各子应用 activity_log 里的 provider 分布与时间范围（只读）。"""
from __future__ import annotations

import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina"]

for app in APPS:
    f = ROOT / app / "state" / "activity_log.json"
    if not f.exists():
        print(f"{app}: (no activity_log)")
        continue
    d = json.loads(f.read_text(encoding="utf-8"))
    recs = d if isinstance(d, list) else (d.get("records") or [])
    if not recs:
        print(f"{app}: 0 records")
        continue
    prov = collections.Counter((r.get("provider") or "?") for r in recs)
    dates = sorted((r.get("created_at") or "") for r in recs)
    print(f"{app}: n={len(recs)}")
    print(f"   providers: {dict(prov)}")
    print(f"   date range: {dates[0][:19]} .. {dates[-1][:19]}")
    print(f"   keys: {sorted(recs[-1].keys())}")
