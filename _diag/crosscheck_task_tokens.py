#!/usr/bin/env python3
"""决定性核对：**同一个方舟任务号**，在子应用记录里的 token 与方舟任务列表里的 token 是否一致。

不一致的话，谁对谁错决定了整套统计的口径 —— 不能靠猜。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import urllib.request  # noqa: E402
from shared.net import UPSTREAM  # noqa: E402

OUT = Path(__file__).resolve().parent / "task_token_crosscheck.txt"
buf: list[str] = []


def ark_index() -> dict[str, dict]:
    key = json.loads((ROOT / "seedance" / "state" / "secrets.json").read_text("utf-8")).get(
        "volcengine_api_key", "").strip()
    out: dict[str, dict] = {}
    for page in range(1, 30):
        url = ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
               f"?page_size=100&page_num={page}")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with UPSTREAM.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            buf.append(f"第 {page} 页失败 {type(exc).__name__}")
            break
        items = payload.get("items") or []
        if not items:
            break
        for it in items:
            out[str(it.get("id"))] = it
        if len(items) < 100:
            break
    return out


ark = ark_index()
buf.append(f"方舟任务 {len(ark)} 条")

# 子应用活动日志里每个任务的 token（那是 by_user 的来源）
log = json.loads((ROOT / "seedance" / "state" / "activity_log.json").read_text("utf-8"))
recs = log if isinstance(log, list) else log.get("records", [])
pairs: list[tuple[str, int, str]] = []
for rec in recs:
    res = rec.get("result") or {}
    for run in (res.get("results") or []):
        tid = str(run.get("task_id") or "")
        if tid and tid.startswith("cgt-"):
            pairs.append((tid, int(run.get("tokens") or 0), str(rec.get("created_at"))[:19]))
buf.append(f"活动日志里带方舟任务号的 run：{len(pairs)} 条")

same = diff = missing = 0
buf.append("")
buf.append("任务号 | 子应用记录的 token | 方舟列表的 token | 备注")
for tid, app_tokens, when in pairs[:25]:
    item = ark.get(tid)
    if item is None:
        missing += 1
        buf.append(f"  {tid} | {app_tokens:>10,} | —（列表里没有） | {when}")
        continue
    usage = item.get("usage") or {}
    ark_tokens = int(usage.get("total_tokens") or usage.get("completion_tokens") or 0)
    if ark_tokens == app_tokens:
        same += 1
        flag = "一致"
    else:
        diff += 1
        flag = f"**不一致** (方舟/子应用 = {ark_tokens / app_tokens if app_tokens else float('inf'):.2f}x)"
    buf.append(f"  {tid} | {app_tokens:>10,} | {ark_tokens:>10,} | {flag}")

buf.append("")
buf.append(f"一致 {same} / 不一致 {diff} / 列表里查不到 {missing}（共比对 {len(pairs)}）")
buf.append("")
buf.append("方舟列表里一条成功任务的 usage 原文（看有哪些字段）：")
for tid, item in list(ark.items()):
    if item.get("usage"):
        buf.append(f"  {tid}: {json.dumps({k: item.get(k) for k in ('model','resolution','duration','usage','status')}, ensure_ascii=False)}")
        break

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
