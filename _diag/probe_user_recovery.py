#!/usr/bin/env python3
"""诊断：某个用户在各个数据源里还剩多少痕迹（用于历史记录恢复）。

背景：history.json 被毁后，我用「子应用活动日志」恢复了 257 条 —— 但每个应用只保留最近
100 条，忙的时候 100 条只覆盖几小时，早期活跃、近期不活跃的用户（如吴雨蔓）就一条都没有。
本脚本盘点还有哪些源可以补：请求日志 usage-*.jsonl（30 天，含 POST /api/jobs + 用户名）、
输出目录 outputs/<用户>/<日期>/、download_files.json、by_user 按天聚合。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = sys.argv[1] if len(sys.argv) > 1 else "吴雨蔓"
OUT = Path(__file__).resolve().parent / "recover_probe.txt"
APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina"]

buf: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


w(f"目标用户: {TARGET}")
w("=" * 70)

# 1) 当前 history.json
hist = {}
hp = ROOT / "portal" / "state" / "history.json"
if hp.exists():
    hist = json.loads(hp.read_text(encoding="utf-8"))
mine = [v for v in hist.values() if str(v.get("username")) == TARGET]
w(f"[1] history.json 总 {len(hist)} 条；属于该用户 {len(mine)} 条")
for v in mine[:5]:
    w(f"      {v.get('app')} {str(v.get('submitted_at'))[:12]} {v.get('title','')[:30]}")

# 2) 子应用活动日志
w("")
for app in APPS:
    p = ROOT / app / "state" / "activity_log.json"
    if not p.exists():
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    recs = d if isinstance(d, list) else d.get("records", [])
    hit = [r for r in recs if str(r.get("username")) == TARGET]
    span = ""
    if recs:
        span = f"{str(recs[0].get('created_at'))[:16]} .. {str(recs[-1].get('created_at'))[:16]}"
    w(f"[2] {app:<20} 日志 {len(recs)} 条（{span}）；属于该用户 {len(hit)} 条")
    for r in hit[:5]:
        w(f"      {str(r.get('created_at'))[:16]} {r.get('status')} {str(r.get('title'))[:30]}")

# 3) 请求日志 usage-*.jsonl：POST /api/jobs 的日期分布
w("")
per_day = collections.Counter()
per_day_app = collections.Counter()
for f in sorted((ROOT / "portal" / "state" / "logs").glob("usage-*.jsonl")):
    try:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if TARGET not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(e.get("username")) != TARGET:
                    continue
                if e.get("method") != "POST":
                    continue
                path = str(e.get("path") or "")
                if not any(h in path for h in ("/api/jobs", "/api/text2image", "/api/image2image",
                                               "/api/text2video", "/api/virtual/jobs", "/api/real/jobs")):
                    continue
                day = str(e.get("time"))[:10]
                per_day[day] += 1
                per_day_app[(day, str(e.get("app")))] += 1
    except OSError:
        continue
w(f"[3] 请求日志里该用户的「创建任务」请求：{sum(per_day.values())} 次，覆盖 {len(per_day)} 天")
for day in sorted(per_day):
    apps = ", ".join(f"{a}x{n}" for (d, a), n in sorted(per_day_app.items()) if d == day)
    w(f"      {day}  {per_day[day]:3d} 次   {apps}")

# 4) 输出目录
w("")
for app in APPS:
    base = ROOT / app / "outputs"
    if not base.exists():
        continue
    udir = base / TARGET
    if not udir.exists():
        w(f"[4] {app:<20} 无该用户输出目录")
        continue
    days = collections.Counter()
    files = 0
    for f in udir.rglob("*"):
        if f.is_file():
            files += 1
            days[f.parent.name] += 1
    w(f"[4] {app:<20} 输出 {files} 个文件，覆盖 {len(days)} 天")
    for day in sorted(days):
        ext = collections.Counter(x.suffix.lower() for x in (udir / day).glob("*") if x.is_file())
        w(f"      {day}  {days[day]:3d} 个  {dict(ext)}")

# 5) by_user 按天
w("")
usage = json.loads((ROOT / "portal" / "state" / "usage.json").read_text(encoding="utf-8"))
days = []
for day, users in (usage.get("by_user") or {}).items():
    if TARGET in users:
        days.append((day, users[TARGET]))
w(f"[5] by_user 里该用户出现在 {len(days)} 天：")
for day, apps in sorted(days):
    w(f"      {day}  {json.dumps(apps, ensure_ascii=False)}")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
