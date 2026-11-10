#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等飞书 Agent（8765）空闲后自动重启 —— 供"改完代码但要等长任务跑完"的场景用。

**为什么不直接杀**：2026-10-12 的教训 —— 拿过期判据重启，打断了一条在跑的飞书任务。
重启的判据（`runs.status in {running, resuming}`）**每一次尝试都必须当场重新读**，
绝不能把上一次的结果带过来复用。

所以这里只是一个**循环外壳**：每一轮都调用
`_diag/restart_feishu_if_idle.py`（检查与杀进程在同一个进程里原子完成），
它拒绝（exit 3 = 忙 / 4 = 没监听）就睡一会儿再来，直到成功或超时。

日志写到 `_diag/restart_feishu_when_idle_loop.log`（UTF-8，控制台是 GBK 看不了）。
用法：
    python _diag/restart_feishu_when_idle_loop.py [--minutes 45]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = Path(__file__).resolve().parent / "restart_feishu_if_idle.py"
LOG = Path(__file__).resolve().parent / "restart_feishu_when_idle_loop.log"


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    try:
        print(line)
    except UnicodeEncodeError:  # 控制台 GBK
        pass
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=45.0)
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()

    log("启动：等空闲后重启 8765（最长 %.0f 分钟，每 %.0f 秒试一次）" % (args.minutes, args.interval))
    deadline = time.time() + args.minutes * 60
    round_no = 0
    while time.time() < deadline:
        round_no += 1
        done = subprocess.run(
            [sys.executable, str(GUARD)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if done.returncode == 0:
            log("重启成功（guard exit 0，第 %d 轮）" % round_no)
            log("guard 输出：" + (done.stdout or "").strip().replace("\n", " | "))
            return 0
        detail = (done.stdout or done.stderr or "").strip().splitlines()
        log("第 %d 轮：guard exit %d —— %s；%.0f 秒后再试"
            % (round_no, done.returncode, (detail[0] if detail else ""), args.interval))
        time.sleep(args.interval)
    log("超时放弃（%.0f 分钟）—— 请人工确认后再重启" % args.minutes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
