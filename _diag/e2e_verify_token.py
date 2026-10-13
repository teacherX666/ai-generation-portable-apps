#!/usr/bin/env python3
"""端到端验证：真实方舟视频任务的 token 是否一路落进 usage.json。

链路：seedance 子应用读 Ark usage → job["tokens"] → job JSON
      → Portal 轮询终态 → by_user[日期][用户][应用]["tokens"]

这是**唯一**能证明「上游解析真的接线了」的方式（单测只能证明契约与取值函数）。
只读 8787 与 usage.json，不做任何写操作。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOB_ID = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "http://127.0.0.1:8787"
TERMINAL = {"succeeded", "failed", "completed", "cancelled", "canceled"}


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:  # 本机回环，无需直连 opener
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not JOB_ID:
        print("usage: e2e_verify_token.py <job_id>", file=sys.stderr)
        return 2
    print(f"job_id = {JOB_ID}")
    last = ""
    job = {}
    deadline = time.time() + 420
    while time.time() < deadline:
        try:
            job = get_json(f"{BASE}/api/jobs/{JOB_ID}")
        except Exception as exc:  # noqa: BLE001
            print(f"  poll error: {exc}")
            time.sleep(10)
            continue
        status = str(job.get("status") or "")
        if status != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status} done={job.get('done')}/{job.get('total')}")
            last = status
        if status in TERMINAL:
            break
        time.sleep(10)

    status = str(job.get("status") or "")
    print("\n=== 子应用 job JSON（关键字段）===")
    print(json.dumps({k: job.get(k) for k in ("id", "status", "done", "total", "duration", "tokens", "provider", "model")},
                     ensure_ascii=False, indent=2))
    if status not in TERMINAL:
        print("!! 未在时限内到达终态，无法验证")
        return 1
    if status != "succeeded":
        print(f"!! 任务未成功（{status}），未产生 token 记账")
        return 1

    job_tokens = int(job.get("tokens") or 0)
    print(f"\n[1/2] 子应用已捕获 tokens = {job_tokens}")
    if job_tokens <= 0:
        print("!! 子应用没读到 usage —— 上游解析没接线")
        return 1

    print("等待 Portal 轮询（15 秒一轮）+ 落盘 debounce（2 秒）…")
    today = date.today().isoformat()
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(8)
        data = json.loads((ROOT / "portal" / "state" / "usage.json").read_text(encoding="utf-8"))
        entry = ((data.get("by_user", {}).get(today, {}) or {}).get("admin", {}) or {}).get("seedance", {})
        got = int(entry.get("tokens") or 0)
        print(f"  usage.json by_user[{today}][admin][seedance] = {json.dumps(entry, ensure_ascii=False)}")
        if got > 0:
            print(f"\n[2/2] OK Portal 已记账：tokens = {got}")
            return 0
    print("\n!! usage.json 里始终没有 tokens —— Portal 轮询或归属有问题")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
