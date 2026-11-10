"""验证「卡片能不能认出样片」的两个数据来源（重启后）。

用法：python _diag/verify_draft_flag_live.py <本页ws> [其它ws...]
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8787"


def get(path: str) -> object:
    with urllib.request.urlopen(BASE + path, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    page_ws = sys.argv[1]
    jobs = get(f"/api/jobs?ws={page_ws}").get("jobs") or []
    print(f"任务列表 /api/jobs：{len(jobs)} 条")
    if jobs:
        print(f"  字段：{sorted(jobs[0].keys())}")
        print(f"  每条都有 draft 字段：{all('draft' in job for job in jobs)}")
        print(f"  其中判为样片：{sum(1 for job in jobs if job.get('draft'))} 条")

    for ws in sys.argv[2:]:
        payload = get(f"/api/activity?ws={ws}")
        records = payload.get("records") or []
        ok = all("draft" in rec for rec in records) if records else None
        drafts = sum(1 for rec in records if rec.get("draft"))
        print(f"\n活动列表 /api/activity ws={ws}：{len(records)} 条；每条都有 draft 字段={ok}；其中样片 {drafts} 条")
        for rec in records[:3]:
            print(f"   {rec.get('created_at')} draft={rec.get('draft')} title={(rec.get('title') or '')[:18]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
