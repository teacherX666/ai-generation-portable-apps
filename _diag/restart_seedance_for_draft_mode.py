"""为「Seedance 2.5 样片模式」上线重启 seedance（8787）。

为什么不能直接用 `_diag/restart_app_if_idle.py`：
它把**任何非空 backlog** 都当「忙」，而本机当前有 4 条**死条目**（ws-a / tester）：

  * `GET /api/jobs/<id>` 返回 404 —— JOBS 里根本没有 worker；
  * `state/activity_log.json` 里查不到对应 activity —— 没人看得到它，也不会有人被它打断；
  * 没有 restore 数据 → `recover_backlog()` 走的是 app.py:1795-1825 的
    「参数已无法找回 → 标失败」分支，**不会重投付费请求**；
  * 没有任何 run 级记录（runs=0）。

也就是说它们是垃圾数据，不是「有活在跑」。本次判定按「活」的定义来，而不是「非空」。

忙判据（三条独立来源，任一命中即拒绝；**检查与杀进程在同一个进程内完成**，
判据绝不跨命令复用 —— 2026-10-12 两次事故都是这么来的）：

  1. `activity_log.json` 有非终态记录（跨工作区、含所有用户）；
  2. backlog 里有**活**条目（HTTP 查得到该 job / 有 activity 记录 / 有 run 级记录）；
  3. 当日 usage 日志里最近 10 分钟有 seedance 的 POST（有人正在提交生成）。

用法（在 ai-generation-portable-apps 目录下）：
    python _diag/restart_seedance_for_draft_mode.py           # 只判定，不动
    python _diag/restart_seedance_for_draft_mode.py --apply   # 判定通过才杀 + 等 Portal 拉起
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "seedance"
PORT = 8787
ACTIVE = {"running", "pending", "queued", "submitted", "processing", "starting"}
TERMINAL = {"succeeded", "success", "completed", "failed", "failure", "cancelled", "canceled"}
POST_WINDOW_SECONDS = 600


def log(message: str) -> None:
    """GBK 控制台打 emoji 会 UnicodeEncodeError 把脚本整死（2026-10-12 踩过），这里降级打印。"""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def ps(command: str) -> str:
    """跑一段 PowerShell 并取 stdout。

    **必须指定 utf-8 + errors=replace**：默认 text=True 用 GBK 解码，PowerShell 输出里
    只要有一个非 GBK 字节，`subprocess.run` 就会在内部 reader 线程抛 UnicodeDecodeError、
    stdout 变成 None —— 本次第一次 --apply 就是这么在「打印旧 pid 启动时间」这一步崩掉、
    白跑一趟（进程还没杀，所以没造成后果，但脚本自己死了）。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(查询失败: {exc})"


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"  [warn] {path.name} 读不了：{exc}")
        return None


def job_in_memory(job_id: str) -> bool | None:
    """True=JOBS 里有（活）；False=404（不在内存里）；None=查不出来（保守当活）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/jobs/{job_id}", timeout=5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return None if exc.code != 404 else False
    except Exception:  # noqa: BLE001
        return None


def listening_pid(port: int) -> int | None:
    out = ps(f"(Get-NetTCPConnection -LocalPort {port} -State Listen | Select-Object -First 1).OwningProcess")
    return int(out) if out.isdigit() else None


def http_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
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


def busy_reasons() -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    dead: list[str] = []

    activities = load_json(APP_DIR / "state" / "activity_log.json") or []
    if not isinstance(activities, list):
        reasons.append("activity_log 结构异常（保守当忙）")
        activities = []
    by_activity = {str(a.get("id") or ""): a for a in activities if isinstance(a, dict)}
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        status = str(activity.get("status") or "").lower()
        if status in ACTIVE:
            reasons.append(
                f"activity {activity.get('id')} job={activity.get('job_id')} "
                f"status={status} user={activity.get('username')}"
            )

    backlog = load_json(APP_DIR / "state" / "jobs_backlog.json")
    if backlog is None:
        if (APP_DIR / "state" / "jobs_backlog.json").exists():
            reasons.append("jobs_backlog 读不了（保守当忙）")
        backlog = {}
    if not isinstance(backlog, dict):
        reasons.append("jobs_backlog 结构异常（保守当忙）")
        backlog = {}
    for job_id, meta in backlog.items():
        meta = meta if isinstance(meta, dict) else {}
        runs = meta.get("runs") or {}
        activity_id = str(meta.get("activity_id") or "")
        activity = by_activity.get(activity_id) or {}
        in_memory = job_in_memory(job_id)
        if in_memory or runs or (activity and str(activity.get("status") or "").lower() not in TERMINAL):
            reasons.append(
                f"backlog {job_id} stage={meta.get('stage')} runs={len(runs)} "
                f"in_memory={in_memory} activity={str(activity.get('status') or '(无)')}"
            )
            continue
        dead.append(
            f"backlog {job_id} stage={meta.get('stage')} runs=0 in_memory=False "
            f"activity={'有(终态)' if activity else '(无记录)'} -> recover 走「参数找不回标记失败」分支，不重投"
        )

    today = time.strftime("%Y-%m-%d")
    usage = ROOT / "portal" / "state" / "logs" / f"usage-{today}.jsonl"
    if usage.exists():
        now = time.time()
        recent_posts = 0
        for line in usage.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[-4000:]:
            line = line.strip()
            if not line or '"seedance"' not in line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if row.get("app") != "seedance" or str(row.get("method") or "").upper() != "POST":
                continue
            try:
                stamp = time.mktime(time.strptime(str(row.get("time")), "%Y-%m-%d %H:%M:%S"))
            except Exception:  # noqa: BLE001
                continue
            if now - stamp <= POST_WINDOW_SECONDS:
                recent_posts += 1
        if recent_posts:
            reasons.append(f"最近 {POST_WINDOW_SECONDS // 60} 分钟内有 {recent_posts} 次 seedance POST（有人正在提交生成）")
    return reasons, dead


def main(argv: list[str]) -> int:
    apply_ = "--apply" in argv
    log(f"=== seedance (port {PORT}) 上线前判定 ===")
    reasons, dead = busy_reasons()
    pid = listening_pid(PORT)
    log(f"  当前监听 pid={pid}")
    for item in dead:
        log(f"  [死条目] {item}")
    if reasons:
        log(f"  [忙] 命中 {len(reasons)} 条：")
        for reason in reasons[:10]:
            log(f"      - {reason}")
        log("  -> 拒绝重启（exit 3），不做任何动作")
        return 3
    log("  [空闲] 无非终态活动、无活 backlog 条目、近 10 分钟无 POST")
    if not apply_:
        log("  （未加 --apply，不做任何动作）")
        return 0
    if pid is None:
        log("  端口没人听：等 Portal 自己拉起")
    else:
        before = ps(f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CreationDate")
        log(f"  杀 pid={pid}（启动于 {before}）—— 由 Portal _health_loop 在 15s 内拉起")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
    if wait_port(PORT, 120):
        new_pid = listening_pid(PORT)
        started = ps(f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {new_pid}\").CreationDate")
        app_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((APP_DIR / "app.py").stat().st_mtime))
        log(f"  [OK] 已恢复：新 pid={new_pid}，启动于 {started}；app.py 修改于 {app_mtime}")
        log("  -> 新的启动时间必须晚于 app.py 的修改时间，否则跑的还是旧代码")
        return 0
    log("  [FAIL] 120s 内没恢复，需要人工介入")
    return 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
