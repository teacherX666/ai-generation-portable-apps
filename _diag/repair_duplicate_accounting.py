#!/usr/bin/env python3
"""修掉「同一任务被重复计账」造成的统计虚高（2026-10-12 事故）。

现象：Portal 重启后，同一 job 被轮询计账 5~6 次 → `by_user` 的 token/秒数被放大几倍，
按次明细也写多份。页面显示「30 次生成 / 16,363,394 token / ¥92.56」，实际约 7 个任务。

做法（按「多算了几份就减掉几份」，不做全量重建，避免毁掉重启前那些正确数字）：
  1. `generations.jsonl` 按 job_id 去重（同一 job 保留信息最全的一条：优先有金额的）；
  2. `usage.json` 的 `by_user[day][user][app]`：每个重复 job 减掉 (n-1) 份 token/秒/张数；
  3. `daily[day][app]["jobs"]`：重复注册同样让 jobs 多加了 (n-1) 次，一并减掉。

**必须在 Portal 停止时运行** —— Portal 把 usage.json 整个缓存在内存里，运行期间它会用
内存快照覆盖磁盘（backfill_usage.py 的注释里也写了同一条）。脚本自己会检测 Portal 是否
在跑并拒绝执行。

用法：
    python _diag/repair_duplicate_accounting.py            # 只报告，零写入
    python _diag/repair_duplicate_accounting.py --apply    # 真的修（先自动备份）
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
GENERATIONS = STATE / "generations.jsonl"
USAGE = STATE / "usage.json"
HISTORY = STATE / "history.json"


def portal_running() -> bool:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return True  # 查不出来就按「在跑」处理（fail-closed）
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":9090"):
                return True
    return False


def load_generation_rows() -> list[dict]:
    if not GENERATIONS.exists():
        return []
    rows = []
    for line in GENERATIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """每个 job_id 保留一条：优先「有金额」的，其次信息更全的。返回 (保留的行, 每个 job 的重复份数)。"""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        groups[str(row.get("job_id") or "")].append(row)
    kept: list[dict] = []

    def score(row: dict) -> tuple:
        return (
            1 if isinstance(row.get("amount"), (int, float)) else 0,
            1 if row.get("unit_price") is not None else 0,
            len(row.get("task_ids") or []),
            float(row.get("ts") or 0),
        )

    counts: dict[str, int] = {}
    for job_id, items in groups.items():
        counts[job_id] = len(items)
        kept.append(max(items, key=score))
    kept.sort(key=lambda r: (str(r.get("date") or ""), float(r.get("ts") or 0)))
    return kept, counts


def main() -> int:
    apply = "--apply" in sys.argv
    if portal_running():
        print("!! Portal 正在运行（9090 在监听）。它把 usage.json 缓存在内存里，会在下次 flush 时"
              "覆盖磁盘 —— 先停 Portal 再跑本脚本。什么都没做。")
        return 3

    rows = load_generation_rows()
    kept, counts = dedupe_rows(rows)
    dup_jobs = {j: n for j, n in counts.items() if n > 1}
    extra = sum(n - 1 for n in counts.values())

    print(f"明细行数 {len(rows)} → 去重后 {len(kept)}（重复的 job {len(dup_jobs)} 个，多出 {extra} 份）")
    for job_id, n in sorted(dup_jobs.items(), key=lambda kv: -kv[1]):
        sample = next(r for r in rows if str(r.get("job_id")) == job_id)
        print(f"  {job_id} x{n}  user={sample.get('username')} app={sample.get('app')} "
              f"tokens={sample.get('tokens')} seconds={sample.get('seconds')} images={sample.get('images')}")

    usage = json.loads(USAGE.read_text(encoding="utf-8")) if USAGE.exists() else {}
    adjustments: list[str] = []
    for job_id, n in dup_jobs.items():
        sample = next(r for r in rows if str(r.get("job_id")) == job_id)
        day = str(sample.get("date") or "")
        user = str(sample.get("username") or "")
        app = str(sample.get("app") or "")
        bucket = ((usage.get("by_user", {}).get(day, {}) or {}).get(user, {}) or {}).get(app)
        if not isinstance(bucket, dict):
            continue
        factor = n - 1
        delta = {
            "images": int(sample.get("images") or 0) * factor,
            "seconds": int(sample.get("seconds") or 0) * factor,
            "tokens": int(sample.get("tokens") or 0) * factor,
        }
        day_app = usage.get("daily", {}).get(day, {}).get(app)
        jobs_delta = factor if isinstance(day_app, dict) else 0
        adjustments.append(
            f"  {day} {user}/{app}: -{delta['tokens']} token, -{delta['seconds']}s, "
            f"-{delta['images']} 张，daily.jobs -{jobs_delta}（当前 {json.dumps(bucket, ensure_ascii=False)}）"
        )
        bucket["tokens"] = max(0, int(bucket.get("tokens") or 0) - delta["tokens"])
        bucket["seconds"] = max(0, int(bucket.get("seconds") or 0) - delta["seconds"])
        bucket["images"] = max(0, int(bucket.get("images") or 0) - delta["images"])
        if isinstance(day_app, dict):
            day_app["jobs"] = max(0, int(day_app.get("jobs") or 0) - jobs_delta)

    print("\n=== 需要下调的 by_user / daily ===")
    for line in adjustments:
        print(line)
    if not adjustments:
        print("  （没有需要下调的项）")

    if not apply:
        print("\nDRY-RUN：零写入。加 --apply 才真的修（会先备份 generations.jsonl 与 usage.json）。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if GENERATIONS.exists():
        shutil.copyfile(GENERATIONS, GENERATIONS.with_name(f"{GENERATIONS.name}.pre-dupfix.{stamp}.bak"))
    if USAGE.exists():
        shutil.copyfile(USAGE, USAGE.with_name(f"{USAGE.name}.pre-dupfix.{stamp}.bak"))
    if HISTORY.exists():
        shutil.copyfile(HISTORY, HISTORY.with_name(f"{HISTORY.name}.pre-dupfix.{stamp}.bak"))

    GENERATIONS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    tmp = USAGE.with_name(f"{USAGE.name}.{time.strftime('%H%M%S')}.tmp")
    tmp.write_text(json.dumps(usage, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USAGE)
    print(f"\n已修复。备份后缀 .pre-dupfix.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
