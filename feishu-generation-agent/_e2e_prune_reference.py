"""把某条 run 的任务引用裁成「只留我上传的那条参考视频」，保证实验变量干净。

用法：python _e2e_prune_reference.py <run_id> <task_id> <keep_asset_id>
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}

run_id, task_id, keep = sys.argv[1], sys.argv[2], sys.argv[3]

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=120, trust_env=False) as c:
    view = c.get(f"/api/runs/{run_id}").json()
    task = next(
        item for item in view["approval"]["tasks"] if item["task_id"] == task_id
    )
    for reference in list(task.get("reference_images") or []):
        if reference["asset_id"] == keep:
            continue
        response = c.delete(
            f"/api/runs/{run_id}/tasks/{task_id}/references/"
            f"{reference['asset_id']}"
        )
        print(
            "DELETE", reference["asset_id"], "->",
            response.status_code, response.text[:200],
        )
    view = c.get(f"/api/runs/{run_id}").json()
    task = next(
        item for item in view["approval"]["tasks"] if item["task_id"] == task_id
    )
    print(
        "剩余引用：",
        json.dumps(task.get("reference_images"), ensure_ascii=False),
    )
    print("status:", view.get("status"))
