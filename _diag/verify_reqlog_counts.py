#!/usr/bin/env python3
"""核对：从请求日志恢复出来的记录条数，与子应用访问日志里的「成功建任务」次数是否吻合。

为什么要核：`usage-*.jsonl`（请求日志）**不记响应状态码**，`record()` 是在代理之前调用的，
所以**创建失败（400/500）的请求也会被记下来**。如果直接把每条 POST 当成一次生成，
就会把失败的尝试也算成记录（多算）。

子应用访问日志（`portal/state/logs/<app>.log`，http.server 格式）**带状态码**，是独立的对照源。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
LOGS = STATE / "logs"
OUT = Path(__file__).resolve().parent / "verify_reqlog_counts.txt"

APPS = ["seedance", "nano-banana"]
# http.server 访问日志：127.0.0.1 - - [12/Oct/2026 15:32:25] "POST /api/jobs?ws=... HTTP/1.1" 200 -
LINE_RE = re.compile(r'\[(?P<ts>\d{2}/\w{3}/\d{4} \d{2}:\d{2}:\d{2})\] "POST (?P<path>[^"]*?) HTTP/1\.\d" (?P<code>\d{3})')
JOB_HINT = ("/api/jobs", "/api/text2image", "/api/image2image", "/api/text2video",
            "/api/virtual/jobs", "/api/real/jobs")

buf: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


# 1) 我恢复出来的记录，按 (日期, 应用) 计数
hist = json.loads((STATE / "history.json").read_text(encoding="utf-8"))
recovered = collections.Counter()
for rec in hist.values():
    if str(rec.get("_recovered_from")) != "request_log":
        continue
    day = dt.datetime.fromtimestamp(float(rec.get("submitted_at") or 0)).strftime("%Y-%m-%d")
    recovered[(day, str(rec.get("app")))] += 1

# 2) 子应用访问日志：按 (日期, 应用) 统计成功/全部建任务请求
success = collections.Counter()
attempt = collections.Counter()
for app in APPS:
    path = LOGS / f"{app}.log"
    if not path.exists():
        continue
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = LINE_RE.search(line)
            if not m:
                continue
            p = m.group("path")
            if "/cancel" in p or not any(h in p for h in JOB_HINT):
                continue
            try:
                day = dt.datetime.strptime(m.group("ts"), "%d/%b/%Y %H:%M:%S").strftime("%Y-%m-%d")
            except ValueError:
                continue
            attempt[(day, app)] += 1
            if m.group("code").startswith("2"):
                success[(day, app)] += 1

days = sorted({d for d, _ in recovered} | {d for d, _ in success} | {d for d, _ in attempt})
w("逐日核对：请求日志恢复的条数  vs  子应用日志里成功建任务次数")
w("=" * 96)
w(f"{'日期':<12}{'应用':<14}{'恢复条数':>10}{'成功(2xx)':>12}{'全部尝试':>10}{'差值(恢复-成功)':>16}")
w("-" * 96)
tot_rec = tot_ok = tot_all = 0
for day in days:
    for app in APPS:
        r = recovered.get((day, app), 0)
        s = success.get((day, app), 0)
        a = attempt.get((day, app), 0)
        if not (r or s or a):
            continue
        tot_rec += r
        tot_ok += s
        tot_all += a
        flag = "" if abs(r - s) <= max(2, s * 0.05) else "   <<< 需解释"
        w(f"{day:<12}{app:<14}{r:>10}{s:>12}{a:>10}{r - s:>16}{flag}")
w("-" * 96)
w(f"{'合计':<26}{tot_rec:>10}{tot_ok:>12}{tot_all:>10}{tot_rec - tot_ok:>16}")
w("")
w(f"请求日志恢复总条数 = {sum(recovered.values())}（含所有应用）")
w(f"其中 seedance+nano-banana = {tot_rec}；这两个应用的访问日志成功建任务 = {tot_ok}")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
