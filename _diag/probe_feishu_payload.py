#!/usr/bin/env python3
"""只读：飞书 Agent 的 operations.payload_json 里是否留着提示词（若有，就能便宜地补正文）。"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)

print("== operations 里 payload_json 非空的按操作类型统计 ==")
for op, n in con.execute("select operation, count(*) from operations where payload_json is not null group by operation"):
    print(f"   {op}: {n}")

print("\n== 抽 3 条 submit 的 payload 看键 ==")
q = "select run_id, provider, payload_json from operations where operation='submit' and payload_json is not null limit 3"
for run_id, provider, pj in con.execute(q):
    try:
        d = json.loads(pj)
    except (TypeError, ValueError):
        print("   (payload 不是 JSON)")
        continue
    keys = list(d.keys())[:14] if isinstance(d, dict) else type(d).__name__
    print(f"   provider={provider} keys={keys}")
    if isinstance(d, dict):
        for k in ("prompt", "text", "content", "request", "params", "values"):
            if k in d:
                print(f"      {k} = {str(d[k])[:120]!r}")

print("\n== artifacts 的 artifact_json 样本 ==")
for (aj,) in con.execute("select artifact_json from artifacts limit 2"):
    print("  ", str(aj)[:170])
