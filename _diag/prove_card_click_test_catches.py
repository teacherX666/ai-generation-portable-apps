"""判别力验证：把卡片点击改回「直接判 isDraftJob(卡片快照)」（旧写法），确认新测试会红。

只做一次「改文件 → 跑测试 → 还原」。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "seedance" / "static" / "app.js"
TEST = ROOT / "tests" / "test_seedance_draft_mode_ui.mjs"

# 当前（正确）写法
CURRENT = """      tile.addEventListener('click', () => {
        const current = tile._jobSnapshot || j;
        // 样片卡片进二级窗口（预览 + 出成片），其余进通用详情。
        // 注意走 _openCardJob：卡片快照里**没有** draft 字段（列表接口为瘦身不下发
        // params），直接判会把样片当普通任务 → 再也回不到样片窗口。
        this._openCardJob(current);
      });"""

# 旧写法（就是造成「窗口无法返回」的那一版）
LEGACY = """      tile.addEventListener('click', () => {
        const current = tile._jobSnapshot || j;
        if (isDraftJob(current)) { this.openDraftPreview(current); return; }
        this.openJobDetail(current.job_id || current.id, jobActivityId(current), current);
      });"""


def run_tests() -> tuple[int, int]:
    out = subprocess.run(
        ["node", str(TEST)],
        capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    ).stdout or ""
    passed = failed = 0
    for line in out.splitlines():
        if line.startswith("ok   "):
            passed += 1
        if line.startswith("FAIL "):
            failed += 1
    return passed, failed


def main() -> int:
    original = APP.read_text(encoding="utf-8")
    if CURRENT not in original:
        print("找不到当前写法，脚本需更新")
        return 2
    try:
        APP.write_text(original.replace(CURRENT, LEGACY, 1), encoding="utf-8")
        passed, failed = run_tests()
        print(f"旧写法（直接判卡片快照）: pass={passed} fail={failed}  ← 期望 fail > 0")
    finally:
        APP.write_text(original, encoding="utf-8")
    passed, failed = run_tests()
    print(f"当前写法（补取详情再判）: pass={passed} fail={failed}  ← 期望 fail == 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
