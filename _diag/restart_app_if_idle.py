"""空闲才重启子应用（检查与杀进程在**同一个进程**里完成，判据绝不跨命令复用）。

为什么必须单进程：2026-10-12 两次事故都是「上一条命令查到空闲、下一条命令直接杀」，
中间几百秒里同事提交了新任务。这里忙就退出（exit 3），不碰任何进程。

忙判据（两条独立来源，任一命中即忙）：
  1. `state/activity_log.json` 里有非终态记录（running/pending/queued/submitted/processing）
     —— 跨工作区、含所有用户（`GET /api/jobs` 不带 ws 只看默认作用域，不能当判据）；
  2. `state/jobs_backlog.json` 非空（有任务在队列/在跑）。

用法（在 ai-generation-portable-apps 目录下）：
    python _diag/restart_app_if_idle.py                      # 只看，不动
    python _diag/restart_app_if_idle.py --apply              # 空闲才重启
    python _diag/restart_app_if_idle.py --apply seedance     # 只重启一个
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = {
    "seedance": {"dir": ROOT / "seedance", "port": 8787},
    "nano-banana": {"dir": ROOT / "nano-banana", "port": 8797},
}
ACTIVE = {"running", "pending", "queued", "submitted", "processing", "starting"}
TERMINAL = {"succeeded", "success", "completed", "failed", "failure", "cancelled", "canceled"}


def busy_reasons(app: str, cfg: dict) -> list[str]:
    reasons: list[str] = []
    activity_path = cfg["dir"] / "state" / "activity_log.json"
    try:
        items = json.loads(activity_path.read_text(encoding="utf-8"))
    except Exception as exc:
        reasons.append(f"activity_log 读不了（保守当忙）：{exc}")
        items = []
    for item in items if isinstance(items, list) else []:
        status = str(item.get("status") or "").lower()
        if status in ACTIVE:
            reasons.append(
                f"activity {item.get('id')} job={item.get('job_id')} "
                f"status={status} user={item.get('username')}"
            )
    backlog_path = cfg["dir"] / "state" / "jobs_backlog.json"
    try:
        backlog = json.loads(backlog_path.read_text(encoding="utf-8")) if backlog_path.exists() else {}
    except Exception as exc:
        reasons.append(f"jobs_backlog 读不了（保守当忙）：{exc}")
        backlog = {}
    for job_id, meta in (backlog or {}).items():
        stage = str((meta or {}).get("stage") or "?")
        runs = (meta or {}).get("runs") or {}
        phases = ",".join(sorted({str((r or {}).get("phase") or "?") for r in runs.values()})) or "-"
        reasons.append(f"backlog {job_id} stage={stage} runs={len(runs)} phases={phases}")
    return reasons


def listening_pid(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen | Select-Object -First 1).OwningProcess"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def http_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(2)
            if sock.connect_ex(("127.0.0.1", port)) == 0 and http_ok(port):
                return True
        time.sleep(2)
    return False


def main(argv: list[str]) -> int:
    apply_ = "--apply" in argv
    names = [a for a in argv[1:] if not a.startswith("--")] or list(APPS)
    exit_code = 0
    busy_count = 0
    idle_count = 0
    restarted = 0
    for name in names:
        cfg = APPS.get(name)
        if cfg is None:
            print(f"[{name}] 未知应用")
            print(f"STATUS {name} UNKNOWN")
            exit_code = 2
            continue
        reasons = busy_reasons(name, cfg)
        pid = listening_pid(cfg["port"])
        print(f"\n=== {name} (port {cfg['port']}, pid={pid}) ===")
        if reasons:
            print(f"  [忙] 命中 {len(reasons)} 条：")
            for reason in reasons[:10]:
                print(f"      - {reason}")
            print(f"  -> 拒绝重启（exit 3）")
            print(f"STATUS {name} BUSY reasons={len(reasons)}")
            busy_count += 1
            exit_code = 3
            continue
        idle_count += 1
        print("  [空闲] activity_log 无非终态记录、jobs_backlog 为空")
        print(f"STATUS {name} IDLE")
        if not apply_:
            print("  （未加 --apply，不做任何动作）")
            continue
        if pid is None:
            print("  端口没人听：等 Portal 自己拉起")
        else:
            before = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CreationDate"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            print(f"  杀 pid={pid}（启动于 {before}）—— 由 Portal _health_loop 在 15s 内拉起")
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
        if wait_port(cfg["port"], 120):
            new_pid = listening_pid(cfg["port"])
            print(f"  [OK] 已恢复：新 pid={new_pid}，/api/config 200")
            print(f"STATUS {name} RESTARTED new_pid={new_pid}")
            restarted += 1
        else:
            # 注意：这里刻意不用 emoji —— GBK 控制台会 UnicodeEncodeError 把脚本整死
            print("  [FAIL] 120s 内没恢复，需要人工介入")
            print(f"STATUS {name} RESTART_FAILED")
            exit_code = 4
    print(f"\nSUMMARY busy={busy_count} idle={idle_count} restarted={restarted} exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
