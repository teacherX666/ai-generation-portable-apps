#!/usr/bin/env python3
"""盘点「无单价」的都是什么：按 供应商 / 模型 分组，看有多少条、为什么没价。"""
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "portal" / "state" / "generations.jsonl"

rows = []
for line in GEN.read_text(encoding="utf-8").splitlines():
    if line.strip():
        rows.append(json.loads(line))

print(f"明细总条数: {len(rows)}")
print("按日期:", dict(collections.Counter(r.get("date") for r in rows)))
print()

priced = [r for r in rows if isinstance(r.get("amount"), (int, float))]
unpriced = [r for r in rows if not isinstance(r.get("amount"), (int, float))]
print(f"有单价 {len(priced)} 条 / 无单价 {len(unpriced)} 条")
print()
print("== 无单价的按 供应商 + 模型 分组（前 15） ==")
group = collections.Counter((r.get("provider_label") or "(空)", str(r.get("model") or "(空模型)")[:34])
                            for r in unpriced)
for (prov, model), n in group.most_common(15):
    print(f"  {n:4d}  {prov:<22} {model}")
print()
print("== 有单价的按 供应商 分组 ==")
for prov, n in collections.Counter(r.get("provider_label") or "(空)" for r in priced).most_common():
    print(f"  {n:4d}  {prov}")
