#!/usr/bin/env python3
"""等到「连续空闲」再报告 —— 只观察，绝不动手；确认后写一张**限时门票**。

为什么要这么谨慎：重启 Portal 会带走**所有**子应用与运行中的任务，而
`recover_backlog()` 是「重新入队重跑」而不是断点续传（会重复计费）。这仓库历史上
多次因为误判空闲打断同事任务，而且两次的根因都是**判据变得恒真**。

判据（全部成立才算空闲）：
  1. 各子应用 activity_log 里「非终态」任务数 == 0
  2. 有时间戳的访问日志（http.server 格式）：最近一次任务相关 POST 距今 >= QUIET
  3. activity_log 最近一次活动距今 >= QUIET（给第 2 条兜底）
  4. 没有时间戳的日志（drama-pipeline / feishu agent 是 uvicorn 风格、行内无时间）：
     自本次观察开始**新增的字节里没有任务相关 POST**

第 4 条用字节增量而不是时间戳：这类日志根本无法定位到时间，而固定读最后 N 行会
因为浏览器轮询把提交行刷出窗口，把「扫不到」误判成「很久没提交」（2026-10-12 真实
发生过：quiet 变成 1e9，判据瞬间恒真）。字节增量从根上绕开了这个问题。
代价是它需要**观察满 QUIET 秒**才算数（起点取观察开始时间），这正是我们要的。

判定口径偏保守：**任何**任务相关 POST（含刷新子镜等子操作）都算「有人在动」。
多算只会推迟重启，不会放行危险的窗口。

确认空闲后写 `_diag/idle_ticket.json`（默认 90 秒内有效），重启脚本必须拿它放行。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "portal" / "state" / "logs"
TICKET_PATH = Path(__file__).resolve().parent / "idle_ticket.json"
TERMINAL = {"succeeded", "failed", "completed", "cancelled", "canceled", "interrupted"}
APPS = ["seedance", "nano-banana", "volcengine-portrait", "dreamina", "director", "previz",
        "infinite-canvas", "drama-pipeline", "feishu-generation-agent", "rag-assistant"]

QUIET_SECONDS = 180
CHECK_INTERVAL = 20
CONFIRMATIONS = 2
TICKET_TTL_SECONDS = 90
_MAX_APPEND_SCAN = 64 * 1024 * 1024  # 单次增量扫描上限；超过就按「可能有提交」处理

_SCAN_WINDOWS = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 16 * 1024 * 1024, 64 * 1024 * 1024)

JOB_POST_RE = re.compile(r'"POST (?P<path>[^"]*?) HTTP/1\.\d"')
# 任务相关路径（含子操作 —— 保守起见，有人在动就算忙）
JOB_PATH_HINTS = ("/api/jobs", "/api/v1/jobs", "/api/text2image", "/api/image2image",
                  "/api/text2video", "/api/image2video", "/api/frames2video",
                  "/api/multimodal2video", "/api/virtual/jobs", "/api/real/jobs",
                  "/api/runs", "/api/video_local/jobs", "/api/image_local/jobs")
_TS_RE = re.compile(r"\[(\d{2}/\w{3}/\d{4} \d{2}:\d{2}:\d{2})\]")

_WATCH_STARTED_AT = time.time()
_TAIL_STATE: dict[str, int] = {}
_UNTIMED_SUBMIT_AT = 0.0
_LOG_KIND: dict[str, str] = {}  # "timed" | "untimed"


def _is_job_post(line: str) -> str | None:
    match = JOB_POST_RE.search(line)
    if not match:
        return None
    path = match.group("path")
    if "/cancel" in path:
        return None
    return path if any(hint in path for hint in JOB_PATH_HINTS) else None


def live_jobs() -> tuple[int, str]:
    """(非终态任务数, 最近一次活动时间字符串)。"""
    total = 0
    latest = ""
    for app in APPS:
        path = ROOT / app / "state" / "activity_log.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = data if isinstance(data, list) else (data.get("records") or [])
        for rec in records:
            status = str(rec.get("status") or "").lower()
            if status and status not in TERMINAL:
                total += 1
            stamp = str(rec.get("updated_at") or rec.get("created_at") or "")
            if stamp > latest:
                latest = stamp
    return total, latest


def _scan_last_job_post(path: Path) -> tuple[float, str, bool]:
    """在单个日志里找最后一条任务 POST。

    返回 (时间戳, 描述, 是否带时间戳)。`是否带时间戳=False` 表示这份日志是
    uvicorn/tag 风格，调用方应改用字节增量判断。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0.0, "", True
    for window in _SCAN_WINDOWS:
        start = max(0, size - window)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read()
        except OSError:
            return 0.0, "", True
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # 首行可能被截断
        newest = 0.0
        detail = ""
        for line in lines:
            job_path = _is_job_post(line)
            if not job_path:
                continue
            found = _TS_RE.search(line)
            if not found:
                return 0.0, f"{path.name}: {job_path}", False
            try:
                stamp = dt.datetime.strptime(found.group(1), "%d/%b/%Y %H:%M:%S").timestamp()
            except ValueError:
                # 有时间戳格式却解析不了：当「刚刚」（fail-closed）
                return time.time(), f"无法解析时间戳 ({path.name})", True
            if stamp > newest:
                newest = stamp
                detail = f"{path.name}: {job_path}"
        if newest:
            return newest, detail, True
        if start == 0:
            break
    return 0.0, "", True


def scan_untimed_growth() -> tuple[float, str]:
    """字节增量观察：只看自上次检查以来**新增**的字节里有没有任务 POST。

    返回 (最近一次「看到有人动任务」的时间, 描述)。起点用 _WATCH_STARTED_AT 兜底：
    观察不满 QUIET 秒时，这条判据不可能放行。
    """
    global _UNTIMED_SUBMIT_AT
    detail = ""
    if not LOG_DIR.exists():
        return _UNTIMED_SUBMIT_AT, detail
    for log in sorted(LOG_DIR.glob("*.log")):
        key = str(log)
        try:
            size = log.stat().st_size
        except OSError:
            continue
        prev = _TAIL_STATE.get(key)
        if prev is None:
            # 首次观察：只建立基线。不能假设「之前没有提交」，所以判据起点取观察开始时间。
            _TAIL_STATE[key] = size
            continue
        if size < prev:
            _TAIL_STATE[key] = size  # 日志被轮转/截断
            continue
        if size == prev:
            continue
        appended = size - prev
        _TAIL_STATE[key] = size
        if appended > _MAX_APPEND_SCAN:
            # 增量太大且来不及细看：按「刚有人提交」处理（保守）
            _UNTIMED_SUBMIT_AT = time.time()
            detail = f"{log.name}: 增量过大({appended}B)"
            continue
        try:
            with log.open("rb") as handle:
                handle.seek(prev)
                chunk = handle.read(appended)
        except OSError:
            continue
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            job_path = _is_job_post(line)
            if job_path:
                _UNTIMED_SUBMIT_AT = time.time()
                detail = f"{log.name}: {job_path}"
    return max(_UNTIMED_SUBMIT_AT, _WATCH_STARTED_AT), detail


def _parse_stamp(text: str) -> float:
    try:
        return dt.datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def idle_status() -> dict:
    """一次完整的空闲判定。quiet 取各条判据里**最小**的（最保守）。"""
    now = time.time()
    live, latest_activity = live_jobs()

    # 2) 有时间戳的日志
    submit_ts = 0.0
    submit_detail = ""
    untimed_seen = ""
    if LOG_DIR.exists():
        for log in sorted(LOG_DIR.glob("*.log")):
            stamp, info, timed = _scan_last_job_post(log)
            _LOG_KIND[str(log)] = "timed" if timed else "untimed"
            if timed and stamp > submit_ts:
                submit_ts, submit_detail = stamp, info
            elif not timed and not untimed_seen:
                untimed_seen = info

    # 3) activity_log 最近活动
    activity_ts = _parse_stamp(latest_activity)

    # 4) 无时间戳日志的字节增量
    untimed_ts, untimed_detail = scan_untimed_growth()

    quiet_submit = now - submit_ts if submit_ts else float("inf")
    quiet_activity = now - activity_ts if activity_ts else float("inf")
    quiet_untimed = now - untimed_ts if untimed_ts else float("inf")
    quiet = min(quiet_submit, quiet_activity, quiet_untimed)
    if quiet == float("inf"):
        quiet = 10 ** 9

    return {
        "live": live,
        "quiet": quiet,
        "quiet_submit": quiet_submit,
        "quiet_activity": quiet_activity,
        "quiet_untimed": quiet_untimed,
        "submit_detail": submit_detail,
        "untimed_detail": untimed_detail or untimed_seen,
        "latest_activity": latest_activity,
        "checked_at": now,
        "idle": live == 0 and quiet >= QUIET_SECONDS,
    }


def write_ticket(status: dict) -> Path:
    TICKET_PATH.write_text(json.dumps({
        "confirmed_at": status["checked_at"],
        "expires_at": status["checked_at"] + TICKET_TTL_SECONDS,
        "quiet": status["quiet"],
        "live": status["live"],
        "quiet_untimed": status["quiet_untimed"],
        "quiet_submit": status["quiet_submit"],
        "quiet_activity": status["quiet_activity"],
        "watch_started_at": _WATCH_STARTED_AT,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return TICKET_PATH


def main() -> int:
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    deadline = time.time() + minutes * 60
    confirms = 0
    if TICKET_PATH.exists():
        TICKET_PATH.unlink()  # 每次观察先作废旧门票，避免拿旧票放行
    while time.time() < deadline:
        status = idle_status()
        now = dt.datetime.now().strftime("%H:%M:%S")
        state = "IDLE" if status["idle"] else "BUSY"
        print(f"[{now}] {state} live={status['live']} quiet={status['quiet']:7.0f}s "
              f"(提交 {status['quiet_submit']:.0f} / 活动 {status['quiet_activity']:.0f} / "
              f"无时间戳日志 {status['quiet_untimed']:.0f}) "
              f"最近提交={status['submit_detail'] or '(窗口内没有)'} "
              f"最近活动={status['latest_activity'][:19]}", flush=True)
        if status["idle"]:
            confirms += 1
            if confirms >= CONFIRMATIONS:
                path = write_ticket(status)
                print(f"\n可以重启：连续 {confirms} 次确认（live=0，quiet={status['quiet']:.0f}s "
                      f">= {QUIET_SECONDS}s）。门票已写入 {path.name}（{TICKET_TTL_SECONDS}s 内有效）",
                      flush=True)
                return 0
        else:
            confirms = 0
        time.sleep(CHECK_INTERVAL)
    print(f"\n超时 {minutes} 分钟仍未确认空闲，未做任何动作", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
