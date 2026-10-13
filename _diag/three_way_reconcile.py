#!/usr/bin/env python3
"""按天算三方账，判断「按时间归属」是否可用：

    方舟账号合计  =  飞书（按任务号精确）  +  自有（by_user，Portal 当时只在 10-12 起记录）  +  未知

未知部分如果很大，说明账号下还有别的来源（别的工具/别的 key/飞书里没留任务号的运行），
**按时间硬归属就会把这部分算到同事头上**（dry-run 实测放大 2.5 倍）。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "portal"))
from shared.net import UPSTREAM  # noqa: E402
import app as portal_app  # noqa: E402

OUT = Path(__file__).resolve().parent / "three_way_reconcile.txt"


def ark_days() -> tuple[dict[str, int], dict[str, int]]:
    key = json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
        "volcengine_api_key", "").strip()
    tok = collections.Counter()
    cnt = collections.Counter()
    for page in range(1, 30):
        url = ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
               f"?page_size=100&page_num={page}")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with UPSTREAM.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            break
        items = payload.get("items") or []
        if not items:
            break
        for it in items:
            usage = it.get("usage") or {}
            t = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
            day = dt.datetime.fromtimestamp(int(it.get("created_at") or 0)).strftime("%Y-%m-%d")
            tok[day] += t
            cnt[day] += 1
        if len(items) < 100:
            break
    return tok, cnt


ark_tok, ark_cnt = ark_days()

# 飞书：按任务号精确归属（tokens 从方舟取）
con = sqlite3.connect(f"file:{ROOT / 'feishu-generation-agent' / 'data' / 'agent.sqlite3'}?mode=ro",
                      uri=True)
feishu_ids = {str(r[0]) for r in con.execute(
    "select official_id from operations where official_id like 'cgt-%'")}
con.close()
fs_tok = collections.Counter()
fs_cnt = collections.Counter()
# 需要任务号→token/天，重新取一遍（从 ark_days 拿不到明细，这里再拉一次）
key = json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
    "volcengine_api_key", "").strip()
for page in range(1, 30):
    url = ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
           f"?page_size=100&page_num={page}")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with UPSTREAM.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        break
    items = payload.get("items") or []
    if not items:
        break
    for it in items:
        tid = str(it.get("id") or "")
        if tid not in feishu_ids:
            continue
        usage = it.get("usage") or {}
        t = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
        day = dt.datetime.fromtimestamp(int(it.get("created_at") or 0)).strftime("%Y-%m-%d")
        fs_tok[day] += t
        fs_cnt[day] += 1
    if len(items) < 100:
        break

usage = json.loads(portal_app.USAGE_PATH.read_text("utf-8"))
by_day = collections.Counter()
for day, users in (usage.get("by_user") or {}).items():
    for uname, apps in (users or {}).items():
        for app, stat in (apps or {}).items():
            by_day[day] += int((stat or {}).get("tokens") or 0)

lines = ["按天三方账（token）：",
         f"{'日期':<12}{'方舟账号':>14}{'飞书(精确)':>14}{'by_user自有':>14}{'未知差额':>14}{'任务数':>8}"]
days = sorted(set(list(ark_tok) + list(by_day)))
for day in days:
    a, f, u = ark_tok.get(day, 0), fs_tok.get(day, 0), by_day.get(day, 0)
    lines.append(f"{day:<12}{a:>14,}{f:>14,}{u:>14,}{a - f - u:>+14,}{ark_cnt.get(day, 0):>8}")
lines.append("")
lines.append("飞书任务号能对上的任务数（按天）：")
for day in sorted(fs_cnt):
    lines.append(f"  {day}  {fs_cnt[day]:4d} 个任务  {fs_tok[day]:>12,} token")
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
