#!/usr/bin/env python3
"""补修 generations.jsonl 里「有 token 但算不出金额」的历史行。

背景（2026-11-10 用户报「seedance 2.5 的 1080p 飞书计费算不出费用」）：
`pricing.json` 的 seedance-2.5 族只列了 480p/720p 档、`prices` 里又没有 `default`，
于是 1080p 整档静默返回 amount=None（页面只剩「—」）。而 2.5 的样片模式成片**只能出 1080p**。

单价表已补齐（portal/app.py 的 `_DEFAULT_PRICING` + portal/state/pricing.json），
但**统计页是直接读明细里存的金额、不重算的**，所以历史行不会自动变好 —— 这个脚本负责补上。

安全约定（与 `repair_seedance_input_video_tier.py` 一致）：
  · 默认 dry-run，只打印不写盘；确认无误再加 --apply；
  · --apply 先按时间戳备份整个文件，再原子替换；
  · **只填空值**：已经有金额的行一律不碰（避免用今天的单价改写历史）；
  · 单价表也算不出来的行（例如 model 为空的记录）原样保留。

用法：
    python _diag/repair_seedance_25_1080p_pricing.py            # dry-run
    python _diag/repair_seedance_25_1080p_pricing.py --apply    # 写回（先备份）
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GENERATIONS = ROOT / "portal" / "state" / "generations.jsonl"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_portal_module():
    """复用 portal/app.py 里真实的计费实现，不在这里复制一份口径。"""
    spec = importlib.util.spec_from_file_location("portal_app_for_repair", ROOT / "portal" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    if not GENERATIONS.exists():
        log(f"找不到 {GENERATIONS}")
        return 2

    mod = load_portal_module()

    lines = GENERATIONS.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[str] = []
    changed: list[dict] = []
    skipped_unpriceable: list[dict] = []
    recovered = 0.0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            out.append(raw)
            continue

        amount = item.get("amount")
        tokens = int(item.get("tokens") or 0)
        # 只动「明确没有金额」且「有 token」的行
        if amount is not None or tokens <= 0:
            out.append(json.dumps(item, ensure_ascii=False))
            continue

        quote = mod.quote_generation(
            model=str(item.get("model") or ""),
            resolution=str(item.get("resolution") or ""),
            image_size=str(item.get("image_size") or ""),
            has_input_video=bool(item.get("has_input_video")),
            tokens=tokens,
            images=int(item.get("images") or 0),
            app=str(item.get("app") or ""),
        )
        new_amount = quote.get("amount")
        if not isinstance(new_amount, (int, float)) or new_amount <= 0:
            skipped_unpriceable.append(item)
            out.append(json.dumps(item, ensure_ascii=False))
            continue

        before = dict(item)
        item["unit_price"] = quote.get("unit_price")
        item["unit"] = quote.get("unit")
        item["amount"] = new_amount
        item["price_source"] = quote.get("source") or "pricing_table"
        item["price_family"] = quote.get("label") or item.get("price_family") or ""
        note = str(item.get("note") or "")
        extra = "金额由 2026-11-10 的单价表补算（原记录当时无该分辨率档位）"
        item["note"] = (note + "；" + extra) if note else extra
        changed.append({"before": before, "after": dict(item)})
        recovered += float(new_amount)
        out.append(json.dumps(item, ensure_ascii=False))

    log(f"明细行数：{len(lines)}")
    log(f"可补算的行：{len(changed)}    仍算不出的行：{len(skipped_unpriceable)}")
    log(f"补回金额合计：¥{recovered:,.2f}")
    log()
    log("补算明细（最多 20 条）：")
    for entry in changed[:20]:
        b, a = entry["before"], entry["after"]
        log(f"  {b.get('date')}  {b.get('model')}  {b.get('resolution')}  "
            f"tokens={int(b.get('tokens') or 0):,}  "
            f"¥{b.get('amount')} → ¥{a.get('amount')}  (单价 {a.get('unit_price')})")

    if skipped_unpriceable:
        log()
        log(f"仍算不出的 {len(skipped_unpriceable)} 条（原样保留，通常是 model 为空的记录）：")
        seen = set()
        for item in skipped_unpriceable:
            key = (str(item.get("model") or ""), str(item.get("resolution") or ""),
                   str(item.get("app") or ""))
            if key in seen:
                continue
            seen.add(key)
            log(f"  model={key[0]!r} resolution={key[1]!r} app={key[2]!r}")

    if not changed:
        log("\n没有需要补修的行。")
        return 0

    if not apply:
        log("\n--dry-run：没有写任何文件；确认无误后加 --apply")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = GENERATIONS.with_name(f"generations.jsonl.bak-before-1080p-fix-{stamp}")
    shutil.copy2(GENERATIONS, backup)
    tmp = GENERATIONS.with_name(GENERATIONS.name + f".tmp{os.getpid()}")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, GENERATIONS)
    log(f"\n已写回 {GENERATIONS}（修正 {len(changed)} 行），备份：{backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
