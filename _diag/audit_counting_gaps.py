#!/usr/bin/env python3
"""审计：今天各子应用**真正发生的**建任务次数 vs Portal 统计到的次数。

Portal 只在三个条件同时成立时才登记一个任务：
  ① POST 路径命中 `_is_job_request` 白名单；② 响应 200/201；③ 响应头里有 `X-Job-Id`。
任何一个不满足 → **功能全正常、统计静默为 0**（本仓库记录过多次）。

对照组：子应用自己的访问日志（带状态码）—— 它记的是**所有**建任务请求，包括
不经 Portal 代理的直连提交（那些永远不会被 Portal 统计）。
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "portal" / "state"
LOGS = STATE / "logs"
OUT = Path(__file__).resolve().parent / "audit_counting_gaps.txt"

JOB_PATHS = ("/api/jobs", "/api/v1/jobs", "/api/text2image", "/api/image2image",
             "/api/text2video", "/api/image2video", "/api/frames2video",
             "/api/multimodal2video", "/api/virtual/jobs", "/api/real/jobs", "/api/runs")
LINE_RE = re.compile(r'\[(?P<ts>\d{2}/\w{3}/\d{4} \d{2}:\d{2}:\d{2})\] "POST (?P<path>[^"]*?) HTTP/1\.\d" (?P<code>\d{3})')

buf: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


today_tag = None  # 由第一份日志推断「今天」的英文月份写法
import datetime as _dt
today_tag = _dt.date.today().strftime("%d/%b/%Y")

usage = json.loads((STATE / "usage.json").read_text(encoding="utf-8"))
daily = (usage.get("daily") or {})
today = _dt.date.today().isoformat()
counted = {app: int((stats or {}).get("jobs") or 0) for app, stats in (daily.get(today) or {}).items()}

w(f"日期 {today}（访问日志日期标记 {today_tag}）")
w("=" * 92)
w(f"{'应用':<24}{'日志里成功建任务':>16}{'Portal 统计到':>14}{'差额（漏记）':>14}")
w("-" * 92)
rows = []
for log in sorted(LOGS.glob("*.log")):
    app = log.stem
    ok = 0
    bad = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if today_tag not in line:
                    continue
                m = LINE_RE.search(line)
                if not m:
                    continue
                p = m.group("path")
                if "/cancel" in p or not any(h in p for h in JOB_PATHS):
                    continue
                if m.group("code").startswith("2"):
                    ok += 1
                else:
                    bad += 1
    except OSError:
        continue
    c = counted.get(app, 0)
    if ok or c:
        rows.append((app, ok, c, ok - c))
for app, ok, c, gap in sorted(rows, key=lambda r: -r[3]):
    flag = "   <<< 漏记" if gap > 0 else ""
    w(f"{app:<24}{ok:>16}{c:>14}{gap:>14}{flag}")
w("-" * 92)
w(f"{'合计':<24}{sum(r[1] for r in rows):>16}{sum(r[2] for r in rows):>14}{sum(r[3] for r in rows):>14}")
w("")
w("说明：差额 > 0 的部分是「真的发生了但没被统计」的任务；")
w("      其中包含①该应用的建任务路径不在白名单里 ②响应没有 X-Job-Id ③直连子应用端口提交（不经 Portal）。")
w("")
w("=== Portal 的白名单（_is_job_request）===")
src = (ROOT / "portal" / "app.py").read_text(encoding="utf-8")
start = src.find("job_patterns = [")
w(src[start:start + 620].split("return any")[0].strip())

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
