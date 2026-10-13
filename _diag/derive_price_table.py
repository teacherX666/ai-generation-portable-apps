#!/usr/bin/env python3
"""从方舟控制台导出的「消费明细」反推单价表。

价格来源：仓库外的控制台导出（3 个 Key，2026-04 ~ 2026-09）。
关键列：模型 / 计费项（含分辨率与是否带输入视频）/ 用量 / 单位 / 原价 / 账期。
单价 = 原价 / 用量（再按单位换算），按 账期 分开看，因为价格会调。

输出写成 UTF-8 文件（PowerShell 控制台会把中文显示成乱码）。
"""
from __future__ import annotations

import collections
import csv
import io
from pathlib import Path

SRC = Path(r"C:\Users\123\Documents\LTX\Seedance2.0_消费明细_3个Key_202604-202609.csv")
OUT = Path(__file__).resolve().parent / "price_table_derived.txt"

buf = io.StringIO()


def w(line: str = "") -> None:
    buf.write(line + "\n")


rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
w(f"source rows = {len(rows)}")
w(f"columns = {list(rows[0].keys())}")
w()

# (模型, 计费项, 单位, 账期) -> [用量, 原价, 行数]
g: dict[tuple, list] = collections.defaultdict(lambda: [0.0, 0.0, 0])
for r in rows:
    try:
        u = float(r["用量"] or 0)
        p = float(r["原价"] or 0)
    except ValueError:
        continue
    g[(r["模型"], r["计费项"], r["单位"], r["账期"])][0] += u
    g[(r["模型"], r["计费项"], r["单位"], r["账期"])][1] += p
    g[(r["模型"], r["计费项"], r["单位"], r["账期"])][2] += 1

w("=" * 130)
w("按 模型 / 计费项 / 账期 的单价（元每单位；单位=千tokens 时另给 元/百万token）")
w("=" * 130)
for key in sorted(g, key=lambda k: (k[0], k[1], k[3])):
    model, item, unit, period = key
    u, p, n = g[key]
    if not u:
        continue
    per_unit = p / u
    if unit == "千tokens":
        detail = f"{per_unit * 1000:7.2f} 元/百万token"
    else:
        detail = f"{per_unit:8.4f} 元/{unit}"
    w(f"{model:22s} | {period} | {item:52s} | 用量={u:12.1f} {unit:8s} 原价={p:10.2f} | {detail} | 行={n}")

w()
w("=" * 130)
w("只看视频类：模型 × 计费项（跨账期汇总，单价取加权平均，并列出各账期单价以便发现调价）")
w("=" * 130)
by_model_item: dict[tuple, dict[str, list]] = collections.defaultdict(dict)
for (model, item, unit, period), (u, p, n) in g.items():
    if unit != "千tokens" or not u:
        continue
    by_model_item[(model, item)][period] = [u, p]

for (model, item) in sorted(by_model_item):
    periods = by_model_item[(model, item)]
    tu = sum(v[0] for v in periods.values())
    tp = sum(v[1] for v in periods.values())
    w(f"\n{model} — {item}")
    w(f"    加权平均单价 = {tp / tu * 1000:.2f} 元/百万token（总用量 {tu:.0f} 千tokens，总原价 {tp:.2f} 元）")
    for period in sorted(periods):
        u, p = periods[period]
        w(f"      {period}: {p / u * 1000:7.2f} 元/百万token   (用量 {u:10.1f}, 原价 {p:9.2f})")

OUT.write_text(buf.getvalue(), encoding="utf-8")
print(f"wrote {OUT}")
