#!/usr/bin/env python3
"""两个决定性实验：

实验 1：**能不能单独查一个上个月的任务号**？
        列表只回溯 5~6 天；如果 `GET /tasks/{id}` 保留更久，飞书的历史 token 就能全补回来
        （飞书库里 810 个 run 的任务号都还在）。

实验 2：**子应用记的「一个作业的 token」= 它那几个任务的和吗**？
        如果不等，说明 by_user 少算了（那我们的历史统计本身也是偏低的）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.net import UPSTREAM  # noqa: E402

OUT = Path(__file__).resolve().parent / "decisive_experiments.txt"
buf: list[str] = []


def key() -> str:
    return json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
        "volcengine_api_key", "").strip()


def get_task(tid: str) -> tuple[int, dict]:
    url = f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{tid}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key()}"})
    try:
        with UPSTREAM.open(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"raw": exc.read(300).decode("utf-8", errors="replace")}
    except Exception as exc:  # noqa: BLE001
        return 0, {"raw": f"{type(exc).__name__}: {exc}"}


con = sqlite3.connect(f"file:{ROOT / 'feishu-generation-agent' / 'data' / 'agent.sqlite3'}?mode=ro",
                      uri=True)
old_ids = [str(r[0]) for r in con.execute(
    "select official_id from operations where official_id like 'cgt-%' "
    "order by updated_at asc limit 6")]
new_ids = [str(r[0]) for r in con.execute(
    "select official_id from operations where official_id like 'cgt-%' "
    "order by updated_at desc limit 3")]
con.close()

buf.append("== 实验 1：单独查历史任务号 ==")
for label, ids in (("最老 6 个", old_ids), ("最新 3 个", new_ids)):
    for tid in ids:
        status, data = get_task(tid)
        usage = data.get("usage") or {}
        buf.append(f"  [{label}] {tid} -> HTTP {status}  status={data.get('status')}  "
                   f"tokens={usage.get('total_tokens') or usage.get('completion_tokens') or 0}"
                   + (f"  {str(data.get('raw'))[:80]}" if status != 200 else ""))

buf.append("")
buf.append("== 实验 2：一个作业的子应用 token 合计 vs 它各任务之和 ==")
log = json.loads((ROOT / "seedance" / "state" / "activity_log.json").read_text("utf-8"))
recs = log if isinstance(log, list) else log.get("records", [])
checked = 0
for rec in recs:
    res = rec.get("result") or {}
    runs = [r for r in (res.get("results") or []) if isinstance(r, dict)]
    tids = [str(r.get("task_id") or "") for r in runs if str(r.get("task_id") or "").startswith("cgt-")]
    if len(tids) < 2:
        continue
    app_sum = sum(int(r.get("tokens") or 0) for r in runs)
    ark_sum = 0
    detail = []
    for tid in tids:
        status, data = get_task(tid)
        usage = data.get("usage") or {}
        t = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
        ark_sum += t
        detail.append(f"{tid[-6:]}={t:,}")
    verdict = "一致" if app_sum == ark_sum else f"**不等（差 {app_sum - ark_sum:+,}）**"
    buf.append(f"  {str(rec.get('created_at'))[:19]} {rec.get('username')} "
               f"{len(tids)} 个任务：子应用合计={app_sum:,} 方舟合计={ark_sum:,} {verdict}")
    if checked >= 5:
        break
    checked += 1
if checked == 0:
    buf.append("  （活动日志里没有多 run 的作业可核对）")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
