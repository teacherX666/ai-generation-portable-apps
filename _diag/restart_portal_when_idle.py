#!/usr/bin/env python3
"""等真空闲窗 → **立刻**重启 Portal → 自检新代码在跑（三件事在同一个进程里连续做完）。

为什么要把它们串起来：`watch_until_idle.py` 写出的门票（`_diag/idle_ticket.json`）
**只有 90 秒有效期**，而「等它退出、再让会话/人跑重启脚本」中间隔着响应时间，
票常常已经过期 —— 等于白等一轮（本仓库 2026-10-23 就在门禁上栽过一次）。

流程：
  1. `watch_until_idle.py`：连续两次确认空闲（live=0 + 静默 ≥180s）后写门票；
  2. 立刻 `restart_portal_if_idle.py`：它自己还会**再复核一次现场**（忙就拒绝、退出码非 0），
     所以「等的时候空闲、动手那刻有人提交了」不会放行；
  3. 拒绝就回到第 1 步继续等，直到 --deadline 分钟用完。

用法：
    python _diag/restart_portal_when_idle.py [--deadline 180]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PYTHON = sys.executable


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    try:
        print(f"[{stamp}] {message}", flush=True)
    except UnicodeEncodeError:
        print(f"[{stamp}] " + message.encode("ascii", "backslashreplace").decode("ascii"),
              flush=True)


def run(script: str, *extra: str) -> int:
    proc = subprocess.run(
        [PYTHON, str(HERE / script), *extra],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        log(f"  {script}: {line}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-3:]
        for line in err:
            log(f"  {script} [stderr]: {line}")
    return proc.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deadline", type=int, default=180, help="总等待上限（分钟）")
    args = parser.parse_args(argv)
    end = time.time() + args.deadline * 60
    attempt = 0
    while time.time() < end:
        attempt += 1
        remaining = max(1, int((end - time.time()) / 60) or 1)
        log(f"第 {attempt} 轮：等空闲窗（watch_until_idle {remaining} 分钟）")
        if run("watch_until_idle.py", str(remaining)) != 0:
            log("等待超时（一直有人在用）。没有做任何动作。")
            return 3
        log("已确认空闲 → 立刻重启 Portal（重启脚本会再复核一次现场）")
        if run("restart_portal_if_idle.py") == 0:
            log("Portal 已按空闲门禁重启完成。")
            return 0
        log("重启被拒（复核时发现有人在用），回去继续等下一轮。")
        time.sleep(30)
    log("到 --deadline 仍未拿到空闲窗，未重启。")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
