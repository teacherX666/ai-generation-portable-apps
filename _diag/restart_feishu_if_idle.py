#!/usr/bin/env python3
"""重启飞书 Agent（8765）—— 硬性空闲门禁，检查与杀进程在**同一个进程**里完成。

为什么必须这样（2026-10-12 我的失误）：
我分两条命令做「检查空闲」和「杀进程」。第一条显示 `running: 0`，两分钟后我照旧执行杀进程，
而这条命令自己的输出里已经写着 `running: 1` —— **我照着过期的检查结果动手，打断了一条在跑的
飞书任务**（进程重启后它自动续跑了，但被打断的那一步可能重复执行）。

所以：检查与动手必须原子化 —— 先查，忙就立刻退出，**绝不把检查结果带到下一次命令里复用**。

用法：
    python _diag/restart_feishu_if_idle.py            # 门禁不过就拒绝
    python _diag/restart_feishu_if_idle.py --dry-run  # 只看门禁
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_DB = ROOT / "feishu-generation-agent" / "data" / "agent.sqlite3"
PORT = 8765
LIVE = ("running", "resuming")
WAIT_SECONDS = 120


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def listener() -> int:
    for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{PORT}"):
                try:
                    return int(parts[4])
                except ValueError:
                    return 0
    return 0


def live_runs() -> list[tuple[str, str, str]]:
    con = sqlite3.connect(f"file:{AGENT_DB}?mode=ro", uri=True)
    rows = list(con.execute(
        "select run_id, status, updated_at from runs where status in (?, ?)",
        LIVE,
    ))
    con.close()
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def main() -> int:
    dry = "--dry-run" in sys.argv
    live = live_runs()
    print(f"门禁检查：在跑 {len(live)} 条（{'、'.join(s for _, s, _ in live) or '无'}）")
    for run_id, status, updated in live:
        print(f"   {run_id[:8]} {status} updated={updated[:19]}")
    if live:
        print("拒绝重启：还有运行在跑。什么都没做。")
        return 3
    pid = listener()
    if not pid:
        print(f"拒绝重启：{PORT} 上没有监听进程（可能已经不在？）")
        return 4
    print(f"确认空闲。重启 {PORT}（pid={pid}）—— Portal 的健康循环会在 ~15 秒内把它拉起来。")
    if dry:
        print("--dry-run：不做任何动作。")
        return 0
    run(["taskkill", "/F", "/PID", str(pid)])
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(5)
        new_pid = listener()
        if new_pid and new_pid != pid:
            print(f"已拉起：新 pid={new_pid}")
            code = run(["curl.exe", "-s", "-o", "NUL", "-w", "%{http_code}", "-m", "8",
                        f"http://127.0.0.1:{PORT}/api/health"]).strip()
            print(f"health -> HTTP {code or '?'}")
            return 0
    print("!! 端口没有回来，请人工介入")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
