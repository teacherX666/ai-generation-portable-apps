#!/usr/bin/env python3
"""把被我自己改错的 by_user 数字改回来。

背景（2026-10-12，我的失误）：明细文件里同一 job 出现了 5~7 行，我据此**假定** by_user 也被
同样放大，于是写了「扣掉多算份数」的修复 —— 但用子应用活动日志独立核对后发现：**by_user
本来是逐人正确的**（MM 13,212,900 / cfy 4,658,400 / 左籽悦 2,984,400 / 高大王 4,786,727 /
admin 40,594，与活动日志按 distinct task 去重后完全一致），重复只发生在明细文件里。
那一步扣掉了真实用量（MM 少 8,053,200 token，左籽悦被扣成 0）。

本脚本的重建方式（为什么不是简单加回）：
  基准 = **修复前的备份**（已知正确）；再按活动日志**回放备份之后发生的事**：
    - `finished_at` > 备份时间 且终态的作业 → 补它的 token/秒数/张数（按 distinct task 去重）
    - `created_at`  > 备份时间 的作业 → daily.jobs +1；其中失败/取消的再 -1（对齐 finalize_job 的回滚）
  其余字段（records / job_owners / daily.requests 等）保留**当前文件**的值，不退回旧快照。

**必须在 Portal 停止时运行**：usage.json 常驻 Portal 内存，运行期间会被内存快照覆盖。

用法：
    python _diag/restore_by_user_from_backup.py            # 只报告
    python _diag/restore_by_user_from_backup.py --apply    # 写回（先备份当前文件）
"""
from __future__ import annotations

import collections
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
USAGE = STATE / "usage.json"
APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina"]
TERMINAL_FAILED = {"failed", "cancelled", "canceled", "error"}


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


def epoch(text: str) -> float:
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def replay(app: str, since: float) -> tuple[dict, dict]:
    """返回 (每人增量, {'jobs_created': n, 'jobs_failed': n})。"""
    path = ROOT / app / "state" / "activity_log.json"
    if not path.exists():
        return {}, {"jobs_created": 0, "jobs_failed": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else (data.get("records") or [])
    per_user: dict[str, dict] = collections.defaultdict(lambda: {"images": 0, "seconds": 0, "tokens": 0})
    counters = {"jobs_created": 0, "jobs_failed": 0}
    for rec in records:
        created = epoch(str(rec.get("created_at") or ""))
        finished = float(rec.get("finished_at") or 0) or 0.0
        status = str(rec.get("status") or "")
        if created > since:
            counters["jobs_created"] += 1
            if status in TERMINAL_FAILED:
                counters["jobs_failed"] += 1
        if finished <= since:
            continue
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        runs = result.get("results") or []
        if not runs:
            continue
        user = str(rec.get("username") or "")
        done = int(result.get("done") or 0) or len(runs)
        duration = int(result.get("duration") or 0)
        per_task: dict[str, int] = {}
        untagged = 0
        for run in runs:
            tokens = int(run.get("tokens") or 0)
            tid = str(run.get("task_id") or "")
            if tid:
                per_task[tid] = max(per_task.get(tid, 0), tokens)
            else:
                untagged += tokens
        per_user[user]["tokens"] += sum(per_task.values()) + untagged
        if duration > 0:      # 视频：done × 时长
            per_user[user]["seconds"] += done * duration
        else:                 # 图片：done 张
            per_user[user]["images"] += done
    return dict(per_user), counters


def main() -> int:
    apply = "--apply" in sys.argv
    backups = sorted(glob.glob(str(STATE / "usage.json.pre-dupfix.*.bak")))
    if not backups:
        print("找不到 usage.json.pre-dupfix.*.bak（修复前备份），无法以此为准。什么都没做。")
        return 4
    base_path = Path(backups[-1])
    since = base_path.stat().st_mtime
    print(f"基准备份: {base_path.name}（{time.strftime('%H:%M:%S', time.localtime(since))}）")

    base = json.loads(base_path.read_text(encoding="utf-8"))
    current = json.loads(USAGE.read_text(encoding="utf-8")) if USAGE.exists() else {}
    today = time.strftime("%Y-%m-%d")

    patched = json.loads(json.dumps(current))  # 以当前文件为底，只改受影响的桶
    print(f"\n{'应用':<12}{'用户':<12}{'备份基线':>14}{'备份后新增':>12}{'应为':>14}{'当前':>14}")
    for app in APPS:
        increments, counters = replay(app, since)
        base_day = (base.get("by_user", {}).get(today, {}) or {})
        users = sorted(set(increments) | {u for u, a in base_day.items() if app in (a or {})})
        if not users:
            continue
        for user in users:
            want_tokens = int((base_day.get(user, {}).get(app) or {}).get("tokens") or 0) + increments.get(user, {}).get("tokens", 0)
            want_seconds = int((base_day.get(user, {}).get(app) or {}).get("seconds") or 0) + increments.get(user, {}).get("seconds", 0)
            want_images = int((base_day.get(user, {}).get(app) or {}).get("images") or 0) + increments.get(user, {}).get("images", 0)
            cur = ((current.get("by_user", {}).get(today, {}) or {}).get(user, {}) or {}).get(app, {}) or {}
            print(f"{app:<12}{user:<12}{int((base_day.get(user, {}).get(app) or {}).get('tokens') or 0):>14}"
                  f"{increments.get(user, {}).get('tokens', 0):>12}{want_tokens:>14}{int(cur.get('tokens') or 0):>14}")
            if apply:
                bucket = patched.setdefault("by_user", {}).setdefault(today, {}).setdefault(user, {})
                bucket[app] = {"images": want_images, "seconds": want_seconds, "tokens": want_tokens}
        if apply and counters["jobs_created"]:
            day_app = patched.setdefault("daily", {}).setdefault(today, {}).setdefault(app, {"requests": 0, "jobs": 0})
            base_jobs = int(((base.get("daily", {}).get(today, {}) or {}).get(app, {}) or {}).get("jobs") or 0)
            new_jobs = max(0, counters["jobs_created"] - counters["jobs_failed"])
            day_app["jobs"] = base_jobs + new_jobs
            print(f"{app:<12}{'daily.jobs':<12}{base_jobs:>14}{new_jobs:>12}{base_jobs + new_jobs:>14}"
                  f"{int((current.get('daily', {}).get(today, {}) or {}).get(app, {}).get('jobs') or 0):>14}")

    if not apply:
        print("\nDRY-RUN：零写入。")
        return 0
    if portal_running():
        print("!! Portal 在跑，会覆盖 usage.json。先停 Portal。什么都没做。")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(USAGE, USAGE.with_name(f"{USAGE.name}.pre-restore.{stamp}.bak"))
    tmp = USAGE.with_name(f"{USAGE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(patched, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USAGE)
    print(f"\n已写回。当前文件的备份: usage.json.pre-restore.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
