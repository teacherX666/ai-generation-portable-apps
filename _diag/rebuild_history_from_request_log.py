#!/usr/bin/env python3
"""从请求日志补回「创作记录」里缺的任务条目（针对活动日志覆盖不到的老记录）。

为什么需要它：`history.json` 被毁后我用子应用活动日志恢复了 257 条，但活动日志**每个应用只
保留最近 100 条** —— 忙的时候 100 条只覆盖几小时。像「吴雨蔓」这种 9/21–9/24 活跃、之后没再
用的用户，活动日志里一条都没有，恢复集里也就没有她。

可用的源：`portal/state/logs/usage-YYYY-MM-DD.jsonl`（保留 30 天）记录了每一次代理请求
（time / app / username / method / path）。其中 **POST 建任务** 的那些就是「一次生成」，
足以还原：时间 + 用户 + 应用。**正文（提示词/模型）与产物链接不可考**，所以这类记录会带
`note` 标注来源，绝不假装是完整记录。

去重：若 history.json 里已有同一 (app, 用户, ±120 秒内) 的记录，则跳过 —— 避免与活动日志
恢复的 257 条重复。

用法：
    python _diag/rebuild_history_from_request_log.py            # 只报告
    python _diag/rebuild_history_from_request_log.py --apply    # 合并写回（先备份）
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
HISTORY = STATE / "history.json"
LOGS = STATE / "logs"

JOB_PATH_HINTS = ("/api/jobs", "/api/v1/jobs", "/api/text2image", "/api/image2image",
                  "/api/text2video", "/api/image2video", "/api/frames2video",
                  "/api/multimodal2video", "/api/virtual/jobs", "/api/real/jobs")
KIND_BY_APP = {"seedance": "video", "volcengine-portrait": "video", "director": "video",
               "nano-banana": "image", "dreamina": "image", "previz": "image"}
MATCH_WINDOW = 120.0


def epoch(text: str) -> float:
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def collect_requests() -> list[dict]:
    out: list[dict] = []
    for path in sorted(LOGS.glob("usage-*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"POST"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("method") != "POST":
                        continue
                    p = str(entry.get("path") or "")
                    if "/cancel" in p or not any(h in p for h in JOB_PATH_HINTS):
                        continue
                    app = str(entry.get("app") or "")
                    user = str(entry.get("username") or "")
                    when = str(entry.get("time") or "")
                    if not app or not user or not when:
                        continue
                    out.append({"app": app, "username": user, "time": when, "ts": epoch(when)})
        except OSError:
            continue
    out.sort(key=lambda e: e["ts"])
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    existing = {}
    if HISTORY.exists():
        try:
            existing = json.loads(HISTORY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    # 已有记录按 (app, user) 建时间索引，用于去重
    index: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for rec in existing.values():
        if isinstance(rec, dict):
            index[(str(rec.get("app") or ""), str(rec.get("username") or ""))].append(
                float(rec.get("submitted_at") or 0))

    requests = collect_requests()
    per_user = collections.Counter()
    per_user_app = collections.Counter()
    added: dict[str, dict] = {}
    skipped = 0
    for entry in requests:
        key = (entry["app"], entry["username"])
        near = index.get(key) or []
        if any(abs(t - entry["ts"]) <= MATCH_WINDOW for t in near):
            skipped += 1
            continue
        stamp = dt.datetime.fromtimestamp(entry["ts"]).strftime("%Y%m%d-%H%M%S")
        job_id = f"recovered-{stamp}-{entry['app']}"
        hkey = f"{entry['app']}:{job_id}"
        n = 1
        while hkey in existing or hkey in added:      # 同一秒多次提交
            n += 1
            job_id = f"recovered-{stamp}-{entry['app']}-{n}"
            hkey = f"{entry['app']}:{job_id}"
        added[hkey] = {
            "app": entry["app"], "job_id": job_id, "username": entry["username"],
            "kind": KIND_BY_APP.get(entry["app"], "image"),
            "activity_id": "", "title": "(由请求日志恢复)", "prompt": "", "model": "",
            "params": {}, "status": "done",
            "submitted_at": entry["ts"], "completed_at": None, "duration": 0,
            "thumb_url": "", "results": [], "error": "",
            "note": "history.json 损坏后按请求日志恢复：时间/用户/应用可靠，正文与产物不可考",
            "_recovered_from": "request_log",
        }
        per_user[entry["username"]] += 1
        per_user_app[(entry["username"], entry["app"])] += 1

    print(f"请求日志里的建任务请求: {len(requests)} 条；已在 history 中（±{int(MATCH_WINDOW)}s）: {skipped} 条")
    print(f"可补回: {len(added)} 条，涉及 {len(per_user)} 个用户")
    print("\n按用户（前 15）：")
    for user, n in per_user.most_common(15):
        apps = ", ".join(f"{a}x{c}" for (u, a), c in per_user_app.items() if u == user)
        print(f"  {user:<14}{n:>5} 条   {apps}")

    if not apply:
        print("\nDRY-RUN：零写入。加 --apply 才写回。")
        return 0

    merged = dict(existing)
    merged.update(added)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if HISTORY.exists():
        shutil.copyfile(HISTORY, HISTORY.with_name(f"{HISTORY.name}.pre-reqlog.{stamp}.bak"))
    tmp = HISTORY.with_name(f"{HISTORY.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, HISTORY)
            break
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
    print(f"\n已写回：{len(existing)} → {len(merged)} 条。备份 history.json.pre-reqlog.{stamp}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
