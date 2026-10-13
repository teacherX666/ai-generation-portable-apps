#!/usr/bin/env python3
"""只读：飞书 Agent 的 operations 表里有没有存方舟任务号（official_id / provider_id）。

有的话，就能把「方舟任务列表」里的 usage **精确**对到飞书的某次生成上，
不用靠时间窗口猜。
"""
import collections
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
db = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("operations 按 operation/provider 统计，以及 official_id 是否非空：")
rows = list(con.execute(
    "select operation, provider, count(*) as n,"
    " sum(case when official_id is not null and official_id != '' then 1 else 0 end) as with_official,"
    " sum(case when provider_id is not null and provider_id != '' then 1 else 0 end) as with_provider"
    " from operations group by operation, provider order by n desc"))
for r in rows:
    print(f"  {r['operation']:<12} {str(r['provider']):<22} n={r['n']:<5} official_id非空={r['with_official']:<5} provider_id非空={r['with_provider']}")

print("\n抽样：official_id 长什么样（前 5 条非空）")
for r in con.execute(
        "select run_id, task_id, provider, official_id, provider_id, status, updated_at"
        " from operations where official_id is not null and official_id != '' limit 5"):
    print("  ", dict(r))

print("\nartifacts.artifact_json 里是否带方舟任务号：")
for (aj,) in con.execute("select artifact_json from artifacts limit 3"):
    text = str(aj)
    print("  ", ("含 cgt-" if "cgt-" in text else "不含 cgt-"), text[:130])
