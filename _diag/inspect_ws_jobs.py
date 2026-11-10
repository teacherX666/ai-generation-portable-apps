"""看某工作区的任务在接口里长什么样（卡片就是从这里画的）。

用法：python _diag/inspect_ws_jobs.py <ws>
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8787"


def get(path: str) -> object:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    ws = sys.argv[1]
    listing = get(f"/api/jobs?ws={ws}")
    jobs = listing.get("jobs") or []
    print(f"/api/jobs?ws={ws} -> {len(jobs)} 条")
    for job in jobs:
        params = job.get("params") or {}
        results = job.get("results") or []
        print(
            f"  job={job.get('job_id')} status={job.get('status')} "
            f"model={job.get('model')} draft={params.get('draft')!r} "
            f"repeat={params.get('repeat_count')} conc={params.get('concurrency')} "
            f"results={len(results)}"
        )
        for item in results:
            print(
                f"      -> task_id={item.get('task_id')!r} "
                f"download_url={item.get('download_url')!r} file={item.get('filename')!r}"
            )
        detail = get(f"/api/jobs/{job.get('job_id')}?ws={ws}")
        dparams = detail.get("params") or {}
        dresults = detail.get("results") or []
        print(
            f"      详情接口：draft={dparams.get('draft')!r} results={len(dresults)} "
            f"首个 task_id={(dresults[0] or {}).get('task_id') if dresults else None!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
