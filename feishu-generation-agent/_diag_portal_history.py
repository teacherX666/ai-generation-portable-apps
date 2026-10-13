"""只读诊断：/api/portal/history 成片预览 + 库内真实分布。

用法： .venv\\Scripts\\python.exe _diag_portal_history.py [--label before]
绝不写库（sqlite 以 mode=ro 打开）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path

BASE = "http://127.0.0.1:8765"
PORTAL_USER = "6b34d9e8-3058-41b6-89b2-4cedac2362ee"
HEADERS = {"X-Portal-User-Id": PORTAL_USER, "X-Username": "%E7%94%B2"}
DB = Path(__file__).resolve().parent / "data" / "agent.sqlite3"

READY_KINDS = {"image", "video"}


def http_json(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    label = args.label or "run"
    print(f"===== {label} =====")

    try:
        health = http_json("/api/health")
        print("health:", json.dumps(health, ensure_ascii=False)[:300])
    except Exception as exc:  # noqa: BLE001
        print("health FAILED:", exc)

    payload = http_json(f"/api/portal/history?limit={args.limit}")
    items = payload.get("items") or []
    with_results = [item for item in items if item.get("results")]
    print(f"HTTP /api/portal/history?limit={args.limit}")
    print(f"  items={len(items)}  results>0={len(with_results)}")
    print("  status distribution:", dict(Counter(i.get("status") for i in items)))
    print(
        "  results>0 by status:",
        dict(Counter(i.get("status") for i in with_results)),
    )

    for item in with_results[:5]:
        print(
            "  SAMPLE",
            item.get("job_id"),
            "status=", item.get("status"),
            "kind=", item.get("kind"),
            "thumb=", item.get("thumb_url"),
            "results=", json.dumps(item.get("results"), ensure_ascii=False),
        )

    if not DB.exists():
        print("db missing:", DB)
        return 0

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='artifacts'"
        )
        row = cur.fetchone()
        print("artifacts schema:", (row[0] if row else None))

        cur.execute("SELECT run_id, artifact_json FROM artifacts")
        preview_by_run: dict[str, int] = {}
        for run_id, blob in cur.fetchall():
            try:
                data = json.loads(blob)
            except Exception:  # noqa: BLE001
                continue
            if data.get("status") == "ready" and data.get("kind") in READY_KINDS:
                preview_by_run[run_id] = preview_by_run.get(run_id, 0) + 1

        cur.execute("SELECT run_id, status, created_at FROM runs")
        rows = [dict(r) for r in cur.fetchall()]
        with_art = [r for r in rows if preview_by_run.get(r["run_id"])]
        print(f"DB total runs={len(rows)}  runs with ready image/video artifact={len(with_art)}")
        print("  statuses of runs WITH artifacts:", dict(Counter(r["status"] for r in with_art)))

        # 复刻接口的口径：同 owner、按 created_at desc 排序取前 N 条
        owner = PORTAL_USER
        cur.execute(
            """
            SELECT run_id, status, owner_user_id, created_at
            FROM runs WHERE owner_user_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (owner, args.limit),
        )
        window = [dict(r) for r in cur.fetchall()]
        have = {r["run_id"] for r in window if preview_by_run.get(r["run_id"])}
        print(
            f"DB window(owner,limit={args.limit}): runs={len(window)} "
            f"with preview = {len(have)}  <- 接口本该返回的 results>0 条数"
        )
        print("  window status distribution:", dict(Counter(r["status"] for r in window)))
        print(
            "  window statuses of runs WITH preview:",
            dict(Counter(r["status"] for r in window if r["run_id"] in have)),
        )

        active_rows = [
            r for r in rows if r["status"] in {"running", "resuming"}
        ]
        print(f"DB 非终态 runs (running/resuming) = {len(active_rows)}")
        for r in sorted(active_rows, key=lambda x: x["created_at"]):
            print(
                "   ACTIVE",
                r["run_id"],
                "status=", r["status"],
                "created_at=", r["created_at"],
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
