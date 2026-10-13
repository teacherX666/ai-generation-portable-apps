"""清理 + 核验：删掉两个测试 run，并逐项核对没有污染真实数据。

核验项：
1. 两个 run 的 `DELETE /api/runs/{id}` 生效（GET 404）；
2. `production-bitable.sqlite3` 的 `production_tasks` / `production_task_history`
   里**没有**这两个 run_id（直连入口的 run 没有多维表格绑定 → 不该被 claim）；
3. `storyboard_candidates` = 0；
4. `runs` 表里这两个 run 已消失、没有残留的非终态运行。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import httpx

APP_DIR = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
TARGETS = [
    ("size", "d448511a-9334-4771-8940-425d124c6af9"),
    ("codec", "f3c3efee-8a9e-49c0-a9bf-5077394cf68b"),
]


def main() -> None:
    for case, run_id in TARGETS:
        with httpx.Client(
            base_url=BASE, headers=HEADERS, timeout=120, trust_env=False
        ) as client:
            response = client.delete(f"/api/runs/{run_id}")
            print(f"[{case}] DELETE ->", response.status_code, response.text[:200])
            probe = client.get(f"/api/runs/{run_id}")
            print(f"[{case}] DELETE 后 GET ->", probe.status_code, probe.text[:120])

    con = sqlite3.connect(APP_DIR / "data" / "agent.sqlite3")
    con.row_factory = sqlite3.Row
    print("--- runs 表里是否还有这两个 run ---")
    for case, run_id in TARGETS:
        rows = con.execute(
            "select run_id, status from runs where run_id=?", (run_id,)
        ).fetchall()
        print(f"  [{case}] rows={[dict(r) for r in rows]}")
    print("--- 非终态运行 ---")
    rows = con.execute(
        "select run_id, status from runs where status in ('running','resuming')"
    ).fetchall()
    print("  ", [dict(r) for r in rows])

    prod = APP_DIR / "data" / "production-bitable.sqlite3"
    con2 = sqlite3.connect(prod)
    con2.row_factory = sqlite3.Row
    tables = [
        r[0]
        for r in con2.execute("select name from sqlite_master where type='table'")
    ]
    print("--- production-bitable 表 ---", tables)
    for table in tables:
        columns = [d[1] for d in con2.execute(f"pragma table_info({table})")]
        key_column = next(
            (
                name
                for name in ("run_id", "claimed_run_id", "source_url")
                if name in columns
            ),
            None,
        )
        count = con2.execute(f"select count(*) from {table}").fetchone()[0]
        hit = None
        if key_column is not None:
            hit = [
                dict(row)
                for row in con2.execute(
                    f"select * from {table} where {key_column} in (?, ?)",
                    tuple(run_id for _, run_id in TARGETS),
                )
            ]
        print(f"  {table}: rows={count} key={key_column} 命中测试 run = {hit}")
    if "storyboard_candidates" in tables:
        total = con2.execute(
            "select count(*) from storyboard_candidates"
        ).fetchone()[0]
        print("  storyboard_candidates（K）= ", total)
    else:
        print("  storyboard_candidates 不在这张库里")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
