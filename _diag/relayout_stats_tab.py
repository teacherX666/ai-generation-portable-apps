#!/usr/bin/env python3
"""使用统计前端重排（用户 2026-10-13 批准）：

1. 导航：把「使用统计」提到最前面（新开一个「总览」分组，第 1 位）；
2. 面板重排：总览 / 今日用量 / 使用记录 / 汇总 置顶，不用滑过一批无关面板；
3. 折叠：按用户趋势 + 系统与权限（本地模型服务 / 用户管理 / 共用 Key / 飞书日报 / 最近活动）收进
   `<details>`（默认收起）；
4. 使用记录表加 `gen-table-wrap` 类 → 固定高度、内部滚动、表头吸顶。

按 `<h2>标题</h2>` 定位区块（向上找 `<section`，向下找 `</section>`），不手抄大段 HTML。
写前先备份。可重复执行（找得到就搬，找不到就跳过并报告）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "portal" / "static" / "index.html"

MAIN = ["总览", "今日用量", "使用记录", "汇总（按天 × 计费项"]
TREND = ["按用户趋势"]
SYS = ["本地模型服务", "用户管理", "人像生成共用 Key", "飞书日报", "最近活动"]
ALL = MAIN + TREND + SYS


def section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    try:
        hi = next(i for i, l in enumerate(lines) if f"<h2>{heading}</h2>" in l)
    except StopIteration:
        return None
    si = hi
    while si >= 0 and not lines[si].lstrip().startswith("<section"):
        si -= 1
    ei = hi
    while ei < len(lines) and "</section>" not in lines[ei]:
        ei += 1
    if si < 0 or ei >= len(lines):
        return None
    return si, ei


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    shutil.copyfile(TARGET, TARGET.with_name(f"{TARGET.name}.pre-layout.{time.strftime('%Y%m%d-%H%M%S')}.bak"))
    lines = text.split("\n")

    # ── 1. 导航：使用统计提到第 1 位 ──────────────────────────────────────────
    bi = next((i for i, l in enumerate(lines) if 'id="tab-btn-stats"' in l), -1)
    if bi >= 0:
        be = bi
        while be < len(lines) and "</button>" not in lines[be]:
            be += 1
        block = lines[bi:be + 1]
        del lines[bi:be + 1]
        block = ["  " + l if l.strip() else l for l in block]     # 缩进对齐新分组
        anchor = next(i for i, l in enumerate(lines) if 'class="portal-nav__group"' in l)
        new_group = [
            '    <div class="portal-nav__group">',
            '      <div class="portal-nav__title">总览</div>',
            '      <div class="portal-nav__items">',
            *block,
            '      </div>',
            '    </div>',
        ]
        lines[anchor:anchor] = new_group
        print(f"导航：使用统计已移到第 1 位（新分组「总览」），插入于第 {anchor + 1} 行前")
    else:
        print("导航：找不到 tab-btn-stats，跳过")

    # ── 2/3. 面板重排 + 折叠 ─────────────────────────────────────────────────
    spans = {}
    for heading in ALL:
        span = section_span(lines, heading)
        if span is None:
            print(f"面板：找不到区块「{heading}」，跳过")
            continue
        spans[heading] = span
    if not spans:
        TARGET.write_text("\n".join(lines), encoding="utf-8")
        return 0
    blocks = {h: lines[s:e + 1] for h, (s, e) in spans.items()}
    first = min(s for s, _ in spans.values())
    for s, e in sorted(spans.values(), reverse=True):
        del lines[s:e + 1]

    def unfold(names: list[str]) -> list[str]:
        out: list[str] = []
        for n in names:
            if n in blocks:
                out.extend(blocks[n])
                out.append("")
        return out

    assembled: list[str] = []
    assembled += unfold(MAIN)
    if TREND:
        assembled.append('      <details class="stats-fold">')
        assembled.append('        <summary>按用户趋势（每人每天的用量曲线）</summary>')
        assembled.append('        <div class="stats-fold__body">')
        assembled += ["  " + l for l in unfold(TREND)]
        assembled.append('        </div>')
        assembled.append('      </details>')
        assembled.append('')
    if SYS:
        assembled.append('      <details class="stats-fold">')
        assembled.append('        <summary>系统与权限：本地模型服务 / 用户管理 / 共用 Key / 飞书日报 / 最近活动</summary>')
        assembled.append('        <div class="stats-fold__body">')
        assembled += ["  " + l for l in unfold(SYS)]
        assembled.append('        </div>')
        assembled.append('      </details>')
        assembled.append('')
    lines[first:first] = assembled
    print(f"面板：置顶 {len(MAIN)} 块 + 折叠 {len(TREND)} 块 + 折叠 {len(SYS)} 块，插入于第 {first + 1} 行")

    # ── 4. 使用记录表加滚动容器类 ────────────────────────────────────────────
    hit = 0
    for i, l in enumerate(lines):
        if 'class="ui-table-wrap" v-if="genData.rows.length"' in l:
            lines[i] = l.replace('class="ui-table-wrap"', 'class="ui-table-wrap gen-table-wrap"')
            hit += 1
    print(f"使用记录表：加了 {hit} 个 gen-table-wrap（固定高度 + 内部滚动）")

    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print("已写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
