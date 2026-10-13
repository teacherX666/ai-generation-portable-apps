#!/usr/bin/env python3
"""补一步：把「汇总」区块挪到「使用记录」正下方（上一步因标题更长没匹配上）。"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "portal" / "static" / "index.html"


def main() -> int:
    shutil.copyfile(TARGET, TARGET.with_name(f"{TARGET.name}.pre-summary-move.{time.strftime('%H%M%S')}.bak"))
    lines = TARGET.read_text(encoding="utf-8").split("\n")
    hi = next(i for i, l in enumerate(lines) if "<h2>汇总（按天" in l)
    si = hi
    while si >= 0 and not lines[si].lstrip().startswith("<section"):
        si -= 1
    ei = hi
    while ei < len(lines) and "</section>" not in lines[ei]:
        ei += 1
    block = lines[si:ei + 1]
    del lines[si:ei + 1]
    # 插到「按用户趋势」折叠块之前
    anchor = next(i for i, l in enumerate(lines) if 'class="stats-fold"' in l)
    lines[anchor:anchor] = block + [""]
    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print(f"汇总区块从第 {si + 1} 行移到第 {anchor + 1} 行前（使用记录下方、折叠区上方）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
