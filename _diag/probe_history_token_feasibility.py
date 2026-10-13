#!/usr/bin/env python3
"""盘点：历史 token 到底能补回多少（哪些能精确、哪些只能汇总）。

要看三件事：
  1. 方舟任务列表能回溯多久（账号级，只读）—— 这是历史 token 的唯一原始来源；
  2. 飞书库自己的 operations 存着方舟任务号，能覆盖到 9/1（精确可补）；
  3. `usage.json.by_user` 里**本来就有历史每人每日的 token 聚合**（之前核对过是准的）
     —— 用它当校验基准：如果「方舟任务按天汇总」≈「by_user 按天汇总」，
     说明可以按时间把任务归属到人。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent / "history_token_feasibility.txt"

from shared.net import UPSTREAM  # noqa: E402
import urllib.request  # noqa: E402

buf: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def ark_tasks() -> list[dict]:
    key = json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
        "volcengine_api_key", "").strip()
    out: list[dict] = []
    for page in range(1, 30):
        url = ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
               f"?page_size=100&page_num={page}")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with UPSTREAM.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            w(f"  (第 {page} 页失败: {type(exc).__name__})")
            break
        items = payload.get("items") or []
        if not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
    return out


tasks = ark_tasks()
w(f"方舟任务列表：共取到 {len(tasks)} 条")
if tasks:
    times = sorted(int(t.get("created_at") or 0) for t in tasks)
    fmt = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    w(f"  时间范围：{fmt(times[0])} .. {fmt(times[-1])}")
    per_day = collections.Counter()
    tok_day = collections.Counter()
    tok_user_model = collections.Counter()
    for t in tasks:
        day = fmt(int(t.get("created_at") or 0))[:10]
        per_day[day] += 1
        usage = t.get("usage") or {}
        tok_day[day] += int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
    w("  按天任务数 / token 合计（最近 12 天）：")
    for day in sorted(per_day)[-12:]:
        w(f"    {day}  {per_day[day]:4d} 个任务  {tok_day[day]:>12,} token")

# by_user 里的历史 token
usage = json.loads((ROOT / "portal" / "state" / "usage.json").read_text("utf-8"))
by_user = usage.get("by_user") or {}
w("")
w("usage.json.by_user 里有 token 的（日期 / 用户 / 应用 / token）：")
rows = []
for day, users in sorted(by_user.items()):
    for uname, apps in (users or {}).items():
        for app, stat in (apps or {}).items():
            tok = int((stat or {}).get("tokens") or 0)
            if tok:
                rows.append((day, uname, app, tok))
for day, uname, app, tok in rows[-14:]:
    w(f"    {day}  {uname:<8} {app:<22} {tok:>12,}")
w(f"  合计 {len(rows)} 条 (日期,用户,应用) 有 token")

# 与方舟按天对比
w("")
w("校验：方舟任务按天 token 合计  vs  by_user 按天 token 合计")
by_day = collections.Counter()
for day, _u, _a, tok in rows:
    by_day[day] += tok
for day in sorted(set(list(tok_day)[-10:] + list(by_day)[-10:])):
    w(f"    {day}  方舟={tok_day.get(day, 0):>12,}   by_user={by_day.get(day, 0):>12,}   "
      f"差={tok_day.get(day, 0) - by_day.get(day, 0):>+12,}")

# 能按时间归属的材料有多少
hist_path = ROOT / "portal" / "state" / "history.json"
hist = json.loads(hist_path.read_text("utf-8"))
per_day_app = collections.Counter()
for rec in hist.values():
    day = dt.datetime.fromtimestamp(float(rec.get("submitted_at") or 0)).strftime("%Y-%m-%d")
    per_day_app[(day, str(rec.get("app")))] += 1
w("")
w("可用于归属的历史记录（history.json，按天×应用，最近 12 天）：")
for (day, app), n in sorted(per_day_app.items())[-12:]:
    w(f"    {day}  {app:<24} {n:4d} 条")

# 飞书库覆盖
con = sqlite3.connect(f"file:{ROOT / 'feishu-generation-agent' / 'data' / 'agent.sqlite3'}?mode=ro",
                      uri=True)
w("")
w("飞书库：runs 按天 + 有方舟任务号的 submit 操作数（这些能精确补 token）")
n_runs = con.execute("select count(*) from runs").fetchone()[0]
w(f"  runs 总数 {n_runs}")
q = ("select substr(created_at,1,10) d, count(*) from runs group by d order by d desc limit 8")
for day, n in con.execute(q):
    w(f"    {day}  {n} 个 run")
q2 = ("select substr(updated_at,1,10) d, count(*) from operations "
      "where official_id like 'cgt-%' group by d order by d desc limit 8")
w("  有 cgt- 任务号的操作（按 updated_at 日）：")
for day, n in con.execute(q2):
    w(f"    {day}  {n}")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
