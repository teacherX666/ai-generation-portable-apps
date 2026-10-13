"""只读核验：/api/portal/history 是否仍然**不写库**（updated_at 不能变）。

本仓今天刚修过「读接口会刷 updated_at 导致排序变化」，这次改动放开了
「每一行都读成片」，必须实测它没有引入写入。

用法： .venv\\Scripts\\python.exe _diag_readonly_check.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
HEADERS = {
    "X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee",
    "X-Username": "%E7%94%B2",
}
DB = Path(__file__).resolve().parent / "data" / "agent.sqlite3"


def snapshot() -> dict[str, str]:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT run_id, updated_at FROM runs").fetchall()
        return {run_id: updated for run_id, updated in rows}
    finally:
        conn.close()


def fetch_history() -> int:
    req = urllib.request.Request(BASE + "/api/portal/history?limit=200", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        items = json.loads(resp.read().decode("utf-8"))["items"]
    return len([i for i in items if i.get("results")])


def main() -> int:
    before = snapshot()
    counts = [fetch_history() for _ in range(3)]
    after = snapshot()

    print("history calls results>0:", counts)
    if set(before) != set(after):
        added = set(after) - set(before)
        removed = set(before) - set(after)
        print("RUN SET CHANGED", added, removed)
    changed = {
        run_id: (before[run_id], after[run_id])
        for run_id in before
        if before.get(run_id) != after.get(run_id)
    }
    print(f"runs total={len(before)} updated_at CHANGED after 3 read calls = {len(changed)}")
    for run_id, pair in list(changed.items())[:5]:
        print("  CHANGED", run_id, pair)
    print("READ-ONLY VERDICT:", "PASS (no write)" if not changed else "FAIL")
    return 0 if not changed else 1


if __name__ == "__main__":
    sys.exit(main())
