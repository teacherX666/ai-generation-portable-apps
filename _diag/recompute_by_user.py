#!/usr/bin/env python3
"""从子应用活动日志重算某天某应用的 by_user（权威口径：按 distinct 方舟任务去重）。

为什么要这个脚本（2026-10-12 教训）：
明细文件里同一 job 出现过 5~7 行，我据此**假定** by_user 也被同样放大，写了个「扣掉
多算份数」的修复 —— 结果**把本来正确的数字改错了**（by_user 当时与活动日志逐人完全一致，
重复只发生在明细文件里）。教训：改生产数据前，必须先用独立数据源验证「假定」本身。

正确做法：直接按活动日志重算 —— 每个作业的 `result.results[]` 里带 `task_id` 与 `tokens`，
按 **distinct task_id** 累加（重启后 recover_backlog 真的重跑会产生新的 task_id，那是真花的钱，
要计；同一 task_id 出现多次只是重复记录，只计一次）。

用法：
    python _diag/recompute_by_user.py 2026-10-12 seedance          # 只对照
    python _diag/recompute_by_user.py 2026-10-12 seedance --apply  # Portal 停机时写回
"""
from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
USAGE = STATE / "usage.json"
TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "completed"}


def portal_running() -> bool:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return True
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":9090"):
                return True
    return False


def recompute(app: str, day: str) -> dict[str, dict[str, int]]:
    path = ROOT / app / "state" / "activity_log.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else (data.get("records") or [])
    out: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"images": 0, "seconds": 0, "tokens": 0})
    jobs = 0
    for rec in records:
        if not str(rec.get("created_at") or "").startswith(day):
            continue
        if str(rec.get("status") or "") not in TERMINAL:
            continue
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        runs = result.get("results") or []
        if not runs:
            continue
        jobs += 1
        user = str(rec.get("username") or "")
        done = int(result.get("done") or 0) or len(runs)
        duration = int(result.get("duration") or 0)
        is_video = str(rec.get("provider") or "") != "" and duration > 0
        # task 去重：同一 task_id 的多条记录只算一次（取该 task 的最大 token 数）
        per_task: dict[str, int] = {}
        untagged = 0
        for run in runs:
            tokens = int(run.get("tokens") or 0)
            tid = str(run.get("task_id") or "")
            if tid:
                per_task[tid] = max(per_task.get(tid, 0), tokens)
            else:
                untagged += tokens
        out[user]["tokens"] += sum(per_task.values()) + untagged
        if is_video:
            out[user]["seconds"] += done * duration
        else:
            out[user]["images"] += done
    out["__jobs__"] = {"images": jobs, "seconds": 0, "tokens": 0}
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    day, app = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv

    computed = recompute(app, day)
    jobs = computed.pop("__jobs__")["images"]

    usage = json.loads(USAGE.read_text(encoding="utf-8"))
    current = (usage.get("by_user", {}).get(day, {}) or {})

    print(f"=== {day} / {app}：活动日志重算 vs usage.json 现值 ===")
    print(f"{'用户':<12}{'重算 tokens':>14}{'现值 tokens':>14}{'重算秒':>9}{'现值秒':>9}{'重算张':>8}{'现值张':>8}")
    users = sorted(set(computed) | {u for u, a in current.items() if app in a})
    for u in users:
        want = computed.get(u, {"images": 0, "seconds": 0, "tokens": 0})
        got = (current.get(u, {}) or {}).get(app, {}) or {}
        flag = "" if (want["tokens"] == int(got.get("tokens") or 0)
                      and want["seconds"] == int(got.get("seconds") or 0)
                      and want["images"] == int(got.get("images") or 0)) else "   <<< 不一致"
        print(f"{u:<12}{want['tokens']:>14}{int(got.get('tokens') or 0):>14}"
              f"{want['seconds']:>9}{int(got.get('seconds') or 0):>9}"
              f"{want['images']:>8}{int(got.get('images') or 0):>8}{flag}")
    print(f"\n活动日志里当天有产出的作业数 = {jobs}"
          f"；usage.json daily.{app}.jobs = {(usage.get('daily', {}).get(day, {}) or {}).get(app, {}).get('jobs')}")

    if not apply:
        print("\nDRY-RUN：零写入。")
        return 0
    if portal_running():
        print("!! Portal 在跑，会覆盖 usage.json。先停 Portal。什么都没做。")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(USAGE, USAGE.with_name(f"{USAGE.name}.pre-recompute.{stamp}.bak"))
    for user, want in computed.items():
        bucket = usage.setdefault("by_user", {}).setdefault(day, {}).setdefault(user, {})
        bucket[app] = {"images": want["images"], "seconds": want["seconds"], "tokens": want["tokens"]}
    tmp = USAGE.with_name(f"{USAGE.name}.{stamp}.tmp")
    tmp.write_text(json.dumps(usage, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USAGE)
    print(f"\n已按活动日志写回。备份 usage.json.pre-recompute.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
