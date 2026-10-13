"""重启 8765（飞书创作助手）：先查非终态 run → 杀进程 → 等 Portal 拉起 → 核验。

判据（CLAUDE.local.md + 本次任务口径）：
- `data/agent.sqlite3` 的 `runs` 表里 status in ('running','resuming') 必须为 0；
- 新进程启动时间必须**晚于**所有源码 mtime（否则还在跑旧代码）；
- `/api/health` 必须 ready=true；
- 如实列出被中断的 run_id（这次应为空）。
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

REPO = Path(r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\feishu-generation-agent")
BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}


def non_terminal_runs() -> list[tuple]:
    con = sqlite3.connect(f"file:{REPO / 'data' / 'agent.sqlite3'}?mode=ro", uri=True)
    try:
        return con.execute(
            "select run_id, status, created_at from runs "
            "where status in ('running','resuming') order by created_at"
        ).fetchall()
    finally:
        con.close()


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def feishu_pids() -> list[int]:
    out = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
            "Where-Object { $_.CommandLine -like '*feishu_generation_agent.main*' } | "
            "Select-Object -ExpandProperty ProcessId",
        ]
    )
    return [int(line) for line in out.split() if line.strip().isdigit()]


def newest_source_mtime() -> float:
    newest = 0.0
    for path in (REPO / "src").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".js", ".html", ".css"}:
            newest = max(newest, path.stat().st_mtime)
    return newest


def process_start(pid: int) -> str:
    return _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CreationDate",
        ]
    ).strip()


def main() -> None:
    print("== 改动前检查 ==")
    pending = non_terminal_runs()
    print(f"非终态 run（running/resuming）：{len(pending)}")
    for row in pending:
        print("  ", row)

    old_pids = feishu_pids()
    print(f"旧进程：{old_pids}")
    for pid in old_pids:
        print(f"  启动时间 {process_start(pid)}")
    print(f"最新源码 mtime（本地时间）："
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest_source_mtime()))}")

    if pending:
        raise SystemExit("有正在跑的 run，按口径应当先等 —— 本次不重启。")

    print("\n== 重启 ==")
    for pid in old_pids:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(f"  killed {pid}")
    # Portal 的 _health_loop 每 15 秒探一次，最多 3 次失败才拉起 → 给 120 秒
    deadline = time.time() + 180
    new_pids: list[int] = []
    while time.time() < deadline:
        time.sleep(5)
        new_pids = feishu_pids()
        if new_pids:
            try:
                health = httpx.get(
                    f"{BASE}/api/health", headers=HEADERS, timeout=10.0
                ).json()
            except Exception:  # noqa: BLE001
                continue
            if health.get("ready"):
                break
    else:
        raise SystemExit("重启超时：/api/health 未就绪")

    print(f"新进程：{new_pids}")
    for pid in new_pids:
        print(f"  启动时间 {process_start(pid)}")
    newest = newest_source_mtime()
    print(f"最新源码 mtime：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest))}")
    for pid in new_pids:
        # CreationDate 形如 20261010120105.123456+480
        text = process_start(pid)
        stamp = text.split(".")[0]
        if len(stamp) >= 14:
            started = time.mktime(time.strptime(stamp[:14], "%Y%m%d%H%M%S"))
            print(
                f"  pid {pid} 启动 {time.strftime('%H:%M:%S', time.localtime(started))} "
                f"> 源码 mtime？{started > newest}"
            )
    health = httpx.get(f"{BASE}/api/health", headers=HEADERS, timeout=10.0).json()
    print("health ready =", health.get("ready"))
    print("被中断的 run_id：", pending or "无")


if __name__ == "__main__":
    main()
