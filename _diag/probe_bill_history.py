#!/usr/bin/env python3
"""只读：你给的那张方舟账单表覆盖哪些月份、总量与金额（用来补「账号级」历史）。"""
import collections
import csv
from pathlib import Path

CSV = Path(r"C:\Users\123\.dsh\attachments\v1\files\53"
           r"\53b9a777ce09deaed88daa42cd76d47be59fed446a6d0d7deb20b8c900de08c0"
           r"\Seedance2.0_消费明细_3个Key_202604-202609.csv")
OUT = Path(__file__).resolve().parent / "bill_history_summary.txt"

with CSV.open("r", encoding="utf-8-sig", newline="") as fh:
    reader = csv.DictReader(fh)
    cols = reader.fieldnames or []
    rows = list(reader)

buf = [f"列名：{cols}", f"总行数：{len(rows)}", ""]
# 找列（中英文都可能）
def pick(*names):
    for n in names:
        if n in cols:
            return n
    return ""

c_period, c_day = pick("账期"), pick("消费日期")
c_model, c_item = pick("模型"), pick("计费项")
c_usage, c_unit = pick("用量"), pick("单位")
c_pay, c_price = pick("应付金额", "折后价", "原价"), pick("原价")
c_key = pick("API Key ID")

per_month = collections.Counter()
usage_month = collections.Counter()
pay_month = collections.Counter()
per_item = collections.Counter()
keys = collections.Counter()
for r in rows:
    month = (r.get(c_period) or "").strip() or (r.get(c_day) or "")[:7]
    per_month[month] += 1
    try:
        usage_month[month] += float((r.get(c_usage) or "0").replace(",", "") or 0)
    except ValueError:
        pass
    try:
        pay_month[month] += float((r.get(c_pay) or "0").replace(",", "") or 0)
    except ValueError:
        pass
    per_item[(r.get(c_item) or "").strip()] += 1
    keys[(r.get(c_key) or "").strip()[:12]] += 1

buf.append("按月：行数 / 用量合计 / 金额合计")
for m in sorted(per_month):
    buf.append(f"  {m}  {per_month[m]:6d} 行  用量={usage_month[m]:>16,.0f}  金额=¥{pay_month[m]:>12,.2f}")
buf.append("")
buf.append(f"币种列：{pick('币种')}；单位列样本：" +
           str(sorted({(r.get(c_unit) or '').strip() for r in rows})[:5]))
buf.append("")
buf.append("计费项（前 12）：")
for item, n in per_item.most_common(12):
    buf.append(f"  {n:6d}  {item}")
buf.append("")
buf.append("涉及 API Key ID（前 6，只显示前 12 位）：")
for k, n in keys.most_common(6):
    buf.append(f"  {n:6d}  {k}…")

OUT.write_text("\n".join(buf) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
