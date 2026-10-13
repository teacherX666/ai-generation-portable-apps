#!/usr/bin/env python3
"""重启 Portal（连带所有子应用）以让新代码生效 —— 带硬性空闲门禁。

**这个脚本会打断运行中的任务**，而恢复是「重新入队重跑」（recover_backlog，会重复
计费）。所以它要求两样东西，缺一不可：

  1. **限时门票** `_diag/idle_ticket.json`（由 `watch_until_idle.py` 在连续两次确认
     空闲后写入，90 秒内有效）。门票承载的是「无时间戳日志」那条判据 —— 它必须靠
     一段观察时间才成立，单独起一个进程查不出来。
  2. **现场复核**（无状态、可当场查）：非终态任务 == 0，且距上次提交/活动 >= QUIET。

用法：
    python _diag/restart_portal_if_idle.py            # 门禁不过就拒绝
    python _diag/restart_portal_if_idle.py --dry-run  # 只看门禁与计划，不杀进程
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_until_idle as idle  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PORTS = [9090, 9089]
SUBPORT_WAIT_SECONDS = 180
PORTAL_WAIT_SECONDS = 120


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def portal_pid() -> int:
    """从 netstat 找 9090 的监听进程。"""
    for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":9090"):
                try:
                    return int(parts[4])
                except ValueError:
                    return 0
    return 0


def children_of(pid: int) -> list[int]:
    out = run(["powershell", "-NoProfile", "-Command",
               f"Get-CimInstance Win32_Process -Filter \"ParentProcessId = {pid}\" | "
               f"Select-Object -ExpandProperty ProcessId"])
    pids = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def kill(pid: int) -> None:
    run(["taskkill", "/F", "/PID", str(pid)])


def listening_ports() -> set[int]:
    ports: set[int] = set()
    for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            try:
                ports.add(int(parts[1].rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                continue
    return ports


def _ticket_status() -> tuple[bool, str]:
    """门票是「无时间戳日志」那条判据的唯一凭据 —— 它必须靠**一段观察时间**才成立，
    单独起一个进程是查不出来的（新进程的观察起点就是现在）。"""
    if not idle.TICKET_PATH.exists():
        return False, (f"没有门票（{idle.TICKET_PATH.name}）—— 先跑 "
                       f"_diag/watch_until_idle.py 并等它确认空闲")
    try:
        ticket = json.loads(idle.TICKET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"门票无法解析：{exc}"
    now = time.time()
    expires = float(ticket.get("expires_at") or 0)
    if now > expires:
        return False, f"门票已过期（{now - expires:.0f}s 前）"
    return True, (f"门票有效（{expires - now:.0f}s 后过期；确认时 quiet={ticket.get('quiet')}s，"
                  f"其中无时间戳日志 {ticket.get('quiet_untimed')}s）")


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    reason = sys.argv[sys.argv.index("--reason") + 1] if "--reason" in sys.argv else ""

    # 无论走哪条路，都先把当前状态打出来 —— 强制重启时这就是「打断了什么」的凭据
    status = idle.idle_status()
    print(f"当前状态：live={status['live']} quiet={status['quiet']:.0f}s "
          f"(提交 {status['quiet_submit']:.0f} / 活动 {status['quiet_activity']:.0f}) "
          f"最近活动={status['latest_activity'][:19]}")

    if force:
        if not reason.strip():
            print('--force 必须带 --reason "..."：把谁批准、为什么非要打断写清楚，否则不留痕。')
            return 2
        print(f"!!! 已按明确授权**跳过空闲门禁**。授权理由：{reason}")
        print(f"!!! 当前 live={status['live']}：这些任务会被 kill 并由 recover_backlog 重新入队重跑"
              f"（上游可能已计费，等于重复付费）。")
    else:
        ok, ticket_msg = _ticket_status()
        print(f"门票检查：{ticket_msg}")
        if not ok:
            print("拒绝重启。先让看门人确认空闲（或明确授权后加 --force --reason）。什么都没做。")
            return 3
        if status["live"] != 0:
            print(f"拒绝重启：现在有 {status['live']} 个非终态任务。什么都没做。")
            return 3
        if min(status["quiet_submit"], status["quiet_activity"]) < idle.QUIET_SECONDS:
            print(f"拒绝重启：距上次提交/活动不足 {idle.QUIET_SECONDS}s。什么都没做。")
            return 3

    pid = portal_pid()
    if not pid:
        print("拒绝重启：没找到 9090 的监听进程（Portal 可能已经不在？）")
        return 4
    kids = children_of(pid)
    print(f"Portal pid={pid}，其下子进程 {len(kids)} 个：{kids}")
    if dry:
        print("--dry-run：不做任何动作。")
        return 0

    print("先杀 Portal（避免它的健康循环在半路把子应用拉起来），再杀子进程…")
    kill(pid)
    time.sleep(2)
    for child in kids:
        kill(child)
    time.sleep(3)

    survivors = [p for p in [pid, *kids] if p in listening_ports()]
    print(f"残留监听进程：{survivors or '无'}")

    print(f"等看门狗拉起 Portal（最多 {PORTAL_WAIT_SECONDS}s）…")
    deadline = time.time() + PORTAL_WAIT_SECONDS
    new_pid = 0
    while time.time() < deadline:
        time.sleep(5)
        new_pid = portal_pid()
        if new_pid and new_pid != pid:
            break
    if not new_pid or new_pid == pid:
        print("!! Portal 没有被看门狗拉起，请人工介入")
        return 5
    print(f"Portal 已由看门狗拉起：新 pid={new_pid}")

    print(f"等子应用端口回来（最多 {SUBPORT_WAIT_SECONDS}s）…")
    want = {8787, 8797, 8888, 8891, 8893, 8895, 8896, 8900, 8903}
    deadline = time.time() + SUBPORT_WAIT_SECONDS
    while time.time() < deadline:
        have = listening_ports()
        missing = want - have
        if not missing:
            print("全部子应用端口就绪")
            break
        time.sleep(5)
    else:
        print(f"!! 仍有端口未起来：{sorted(want - listening_ports())}")
        return 6

    print("完成。请再用 healthz + 子应用 /api/config 做一次健康核验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
