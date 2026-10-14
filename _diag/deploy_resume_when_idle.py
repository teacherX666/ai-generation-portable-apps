"""部署「重启续跑」：逐个等空闲窗口重启子应用，**同一个应用只重启一次**。

为什么要这个脚本：本机上同事随时会提交付费任务，重启会打断在途任务。判据必须
在**同一进程内**「查完立刻杀」（2026-10-12 两次事故的教训），而且不能反复重启
已经部署好的应用。

用法（在 ai-generation-portable-apps 目录下）：
    python _diag/deploy_resume_when_idle.py --timeout-min 30
"""
import json
import subprocess
import sys
import time
from pathlib import Path

DIAG = Path(__file__).resolve().parent
sys.path.insert(0, str(DIAG))
from restart_app_if_idle import APPS, busy_reasons, listening_pid, wait_port  # noqa: E402


def code_newer_than_process(app: str, pid: int | None) -> bool:
    """新进程必须启动于代码 mtime 之后 —— 否则还在跑旧代码（仓库通用判据）。"""
    if pid is None:
        return False
    mtime = (APPS[app]["dir"] / "app.py").stat().st_mtime
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"[int][double]::Parse((Get-Date (Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CreationDate "
         f"-UFormat %s))"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    try:
        return int(out) >= int(mtime)
    except ValueError:
        return False


def main(argv: list[str]) -> int:
    timeout_min = 30
    if "--timeout-min" in argv:
        timeout_min = int(argv[argv.index("--timeout-min") + 1])
    targets = [a for a in argv[1:] if not a.startswith("--") and a in APPS] or list(APPS)
    deadline = time.time() + timeout_min * 60
    pending = list(targets)
    deployed: dict[str, str] = {}

    while pending and time.time() < deadline:
        for name in list(pending):
            cfg = APPS[name]
            reasons = busy_reasons(name, cfg)
            stamp = time.strftime("%H:%M:%S")
            if reasons:
                print(f"[{stamp}] {name}: 忙（{len(reasons)} 条，例：{reasons[0][:90]}）—— 等下一个窗口")
                continue
            pid = listening_pid(cfg["port"])
            print(f"[{stamp}] {name}: 空闲，重启 pid={pid}")
            if pid is not None:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=60)
            if wait_port(cfg["port"], 120):
                new_pid = listening_pid(cfg["port"])
                fresh = code_newer_than_process(name, new_pid)
                deployed[name] = f"pid={new_pid} fresh_code={fresh}"
                print(f"[{stamp}] {name}: 已重启 new_pid={new_pid} 跑新代码={fresh}")
                pending.remove(name)
            else:
                print(f"[{stamp}] {name}: 120s 内没恢复 —— 需要人工介入")
                return 4
        if pending:
            time.sleep(30)

    print("\nRESULT " + json.dumps({
        "deployed": deployed,
        "pending": pending,
        "timed_out": bool(pending),
    }, ensure_ascii=False))
    return 0 if not pending else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
