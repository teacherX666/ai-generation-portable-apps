#!/usr/bin/env python3
"""可行性探针：飞书 Agent 的真实正文能不能从 checkpoints.sqlite3 里读出来。

要读的路径（CLAUDE.local 记录过）：LangGraph 的 SqliteSaver →
`get_tuple({"configurable": {"thread_id": ...}}).checkpoint["channel_values"]["approved_plan"]`
thread_id 在 agent.sqlite3 的 runs 表里。

只读访问，不写库。用飞书自己的 venv 跑（它装了 langgraph）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_DB = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
CKPT_DB = ROOT / "feishu-generation-agent" / "data" / "checkpoints.sqlite3"

con = sqlite3.connect(f"file:{AGENT_DB}?mode=ro", uri=True)
runs = con.execute(
    "select run_id, thread_id, status, created_at from runs order by created_at desc limit 5"
).fetchall()
print(f"取最近 {len(runs)} 个 run 做样本：")
for run_id, thread_id, status, created in runs:
    print(f"  run={run_id[:8]} thread={str(thread_id)[:8]} status={status} created={created[:19]}")

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError as exc:
    print(f"\n[!] 这个解释器没有 langgraph：{exc}")
    sys.exit(2)

ck = sqlite3.connect(f"file:{CKPT_DB}?mode=ro", uri=True, check_same_thread=False)
print("\ncheckpoints 表：", [r[0] for r in ck.execute("select name from sqlite_master where type='table'")])
total = ck.execute("select count(*) from checkpoints").fetchone()[0]
print("checkpoints 行数：", total)

saver = SqliteSaver(ck)
ok = 0
for run_id, thread_id, status, created in runs:
    if not thread_id:
        print(f"  run={run_id[:8]} 没有 thread_id，跳过")
        continue
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": str(thread_id)}})
    except Exception as exc:  # noqa: BLE001
        print(f"  run={run_id[:8]} 读取失败：{type(exc).__name__}: {exc}")
        continue
    if tup is None:
        print(f"  run={run_id[:8]} 该 thread 没有 checkpoint")
        continue
    cv = (tup.checkpoint or {}).get("channel_values", {})
    keys = sorted(cv.keys())
    plan = cv.get("approved_plan") or cv.get("draft_plan") or {}
    prompt = ""
    if isinstance(plan, dict):
        tasks = plan.get("tasks") or []
        if tasks and isinstance(tasks[0], dict):
            prompt = str(tasks[0].get("prompt") or "")
        prompt = prompt or str(plan.get("summary") or "") or str(plan.get("title") or "")
    print(f"\n  run={run_id[:8]} → channel_values 键 {len(keys)} 个")
    print(f"     含 approved_plan: {isinstance(cv.get('approved_plan'), dict)}"
          f" / draft_plan: {isinstance(cv.get('draft_plan'), dict)}")
    if prompt:
        ok += 1
        print(f"     正文样本（前 90 字）：{prompt[:90]!r}")
    else:
        print(f"     没取到正文；键示例：{keys[:12]}")
print(f"\n结论：{ok}/{len(runs)} 个样本能从 checkpoint 里取到正文")
