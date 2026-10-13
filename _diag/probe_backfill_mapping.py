#!/usr/bin/env python3
"""只读：确认 (1) 输出目录能否对齐 user+date+task_id，(2) activity_log.response 里有什么。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

print("=== seedance outputs 目录结构（前 3 层）===")
base = ROOT / "seedance" / "outputs"
if base.exists():
    for user_dir in sorted(base.iterdir())[:5]:
        if not user_dir.is_dir():
            continue
        day_dirs = sorted([d for d in user_dir.iterdir() if d.is_dir()])[:2]
        print(f"  {user_dir.name}/  -> days: {[d.name for d in day_dirs]}")
        for dd in day_dirs[:1]:
            files = sorted(dd.glob("*.mp4"))[:3]
            for f in files:
                has_cgt = bool(re.search(r"cgt-\d{14}-[a-z0-9]+", f.name))
                m = re.search(r"cgt-\d{14}-[a-z0-9]+", f.name)
                print(f"      {f.name}  cgt={m.group(0) if m else None}")
else:
    print("  (no outputs dir)")

print("\n=== activity_log.response 字段样本 ===")
for app in ["seedance", "nano-banana", "volcengine-portrait"]:
    f = ROOT / app / "state" / "activity_log.json"
    if not f.exists():
        continue
    recs = json.loads(f.read_text(encoding="utf-8"))
    recs = recs if isinstance(recs, list) else (recs.get("records") or [])
    done = [r for r in recs if r.get("status") in ("succeeded", "failed", "completed")]
    sample = (done or recs)[-1]
    print(f"\n--- {app} ---")
    print("  username:", sample.get("username"), "| provider:", sample.get("provider"), "| model:", sample.get("model"))
    resp = sample.get("response")
    txt = json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp
    print("  response type:", type(resp).__name__, "len:", len(txt or ""))
    print("  response head:", (txt or "")[:500])
    print("  result:", json.dumps(sample.get("result"), ensure_ascii=False)[:300] if sample.get("result") else None)
