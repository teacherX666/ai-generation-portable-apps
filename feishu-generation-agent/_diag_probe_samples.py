"""只读核验：抽样成片 URL 是否真能取到字节 + 重启窗口内有没有被打断的 run。

用法： .venv\\Scripts\\python.exe _diag_probe_samples.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "http://127.0.0.1:8765"
HEADERS = {
    "X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee",
    "X-Username": "%E7%94%B2",
    "Range": "bytes=0-2047",
}
DB = Path(__file__).resolve().parent / "data" / "agent.sqlite3"


def main() -> int:
    req = urllib.request.Request(
        BASE + "/api/portal/history?limit=200", headers=HEADERS
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        items = json.loads(resp.read().decode("utf-8"))["items"]

    with_results = [i for i in items if i.get("results")]
    print(f"results>0 items = {len(with_results)}")
    print("keys of a result item:", sorted({k for i in with_results for k in i}))
    print(
        "result-entry keys:",
        sorted({k for i in with_results for r in i["results"] for k in r}),
    )

    for item in with_results[:3]:
        entry = item["results"][0]
        url = BASE + entry["url"]
        try:
            probe = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(probe, timeout=60) as resp:
                blob = resp.read(2048)
            print(
                "PROBE OK",
                item["job_id"],
                "status=", item["status"],
                "kind=", item["kind"],
                "http=", resp.status,
                "ctype=", resp.headers.get("Content-Type"),
                "range-total=", resp.headers.get("Content-Range"),
                "bytes-read=", len(blob),
                "magic=", blob[:12].hex(),
            )
        except urllib.error.HTTPError as exc:
            print("PROBE FAIL", item["job_id"], exc.code, exc.reason)
        except Exception as exc:  # noqa: BLE001
            print("PROBE FAIL", item["job_id"], exc)

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        cur.execute(
            """
            SELECT run_id, status, created_at, updated_at FROM runs
            WHERE substr(created_at, 1, 16) >= ? OR substr(updated_at, 1, 16) >= ?
            ORDER BY updated_at
            """,
            (cutoff, cutoff),
        )
        rows = [dict(r) for r in cur.fetchall()]
        print(f"\nruns touched since {cutoff} (restart window): {len(rows)}")
        for r in rows:
            print("  ", r["run_id"], r["status"], "created=", r["created_at"], "updated=", r["updated_at"])

        cur.execute("SELECT max(created_at) AS c, max(updated_at) AS u FROM runs")
        edge = dict(cur.fetchone())
        print(
            "newest run rows: max(created_at)=", edge["c"],
            " max(updated_at)=", edge["u"],
        )

        cur.execute(
            "SELECT run_id, status FROM runs WHERE status IN ('running','resuming')"
        )
        active = [dict(r) for r in cur.fetchall()]
        print(f"non-terminal runs now: {len(active)}")
        for r in active:
            print("  ACTIVE", r["run_id"], r["status"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
