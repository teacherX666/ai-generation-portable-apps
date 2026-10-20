# -*- coding: utf-8 -*-
"""修复「绑定在自己的运行还在创建时就被判死」留下的生产数据（2026-10-20）。

**背景**：`claim()` / `rerun()` 先写绑定、再建运行；列表每 5 秒轮询一次就会走
`_reconcile_bindings`，运行还没落库时 `get_run_view` 必然 `RunNotFound`，于是
绑定被 `release` 成「失败：本地运行记录不存在」并把 `active` 置 0 —— 记录被解锁，
用户能立刻再领一次，同一记录几分钟内堆出多个 run（**重复计费的生成**）。

代码侧的修复见 `bitable/production_service.py`（内存创建标记 + 宽限期 +
`BindingNotFound`）。这个脚本负责把**修复前已经造成的损坏**分类、报告，并在
`--apply` 时修好：

1. **被误杀的绑定**（`last_error` 是那句「本地运行记录不存在」，但 run 其实存在）
   → 说明判死是错的。把状态按 run 的真实状态写回去、清掉 last_error；
   若该记录当前没有别的活跃绑定，再把它恢复成 `active = 1`（这样停在待审批的
   那次运行又能被审批/继续，而不是挂在记录历史里够不着）。
2. **判死判对了的绑定**（run 确实不存在）→ 只把文案升级成「…已释放，可重新领取」，
   让用户知道自己可以重试，而不是以为运行丢了。
3. **僵尸活跃绑定**（`active = 1` 且超过 `--stale-days` 天没动、run 已经不是
   非终态）→ 默认只报告；加 `--release-stale` 才释放，避免静默改变用户正在
   等人的任务。

默认 **dry-run**（只读、只打印）。要写入必须显式 `--apply`；写入前会把两个库
备份到 `_diag/_backup_repair_killed_bindings_<时间戳>/`。

用法：
    python _diag/repair_killed_bindings.py                 # 只报告
    python _diag/repair_killed_bindings.py --stale-days 3  # 顺便报告更老的僵尸
    python _diag/repair_killed_bindings.py --apply         # 真写
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "feishu-generation-agent",
    "data",
)
PROD_DB = os.path.join(DATA_DIR, "production-bitable.sqlite3")
AGENT_DB = os.path.join(DATA_DIR, "agent.sqlite3")

#: 运行状态 → 多维表格任务状态（与 production_service 的 _RELEASED_STATUSES /
#: _ACTIVE_STATUSES 保持一致）。故意写成字面量：这是个一次性修数据脚本，不该
#: 因为业务代码哪天改名而悄悄跟着变。
STATUS_BY_RUN = {
    "succeeded": "已完成",
    "completed_with_errors": "失败",
    "failed": "失败",
    "cancelled": "失败",
    "timed_out": "失败",
    "created": "处理中",
    "running": "处理中",
    "waiting_approval": "待审批",
    "waiting_review": "待确认成片",
    "resuming": "生成中",
    "waiting_provider": "生成中",
    "delivering": "回写中",
    "delivery_failed": "回写失败",
}

NON_TERMINAL = {"created", "running", "waiting_approval", "waiting_review",
                "resuming", "waiting_provider", "delivering"}

KILLED_MARKERS = ("本地运行记录不存在", "本地运行未能创建", "重跑初始化失败")
RETRYABLE_SUFFIX = "，绑定已释放，可重新领取"

KILLED_SQL = " OR ".join(["last_error LIKE ?"] * len(KILLED_MARKERS))


def log(message: str) -> None:
    """这台机器的控制台是 GBK：任何打不出来的字符都要降级，绝不能因此炸掉脚本。"""
    REPORT.append(message)
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("gbk", "replace").decode("gbk", "replace"))


#: 所有输出同时留一份，好写成 UTF-8 报告（控制台是 GBK，中文看不了）。
REPORT: list[str] = []


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_runs() -> dict[str, dict]:
    connection = connect(AGENT_DB)
    try:
        return {
            row["run_id"]: dict(row)
            for row in connection.execute(
                "SELECT run_id, status, created_at, updated_at FROM runs"
            )
        }
    finally:
        connection.close()


def load_bindings() -> list[tuple[str, dict]]:
    connection = connect(PROD_DB)
    try:
        rows: list[tuple[str, dict]] = []
        for table in ("production_tasks", "production_task_history"):
            for row in connection.execute("SELECT * FROM %s" % table):
                rows.append((table, dict(row)))
        return rows
    finally:
        connection.close()


def looks_killed(row: dict) -> bool:
    text = str(row.get("last_error") or "")
    return any(marker in text for marker in KILLED_MARKERS)


def parse_stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def plan_actions(bindings, runs, *, stale_days: float, release_stale: bool):
    """把要做的动作算出来（纯函数，便于先打印再执行）。"""
    revive, relabel, release, keep = [], [], [], []

    # 一个记录只能有一条活跃绑定。先算出哪些记录本来就有活跃绑定。
    active_records = {
        (row["source_app_token"], row["source_table_id"], row["source_record_id"])
        for table, row in bindings
        if table == "production_tasks" and row.get("active")
    }

    now = datetime.now(timezone.utc)
    for table, row in bindings:
        run = runs.get(row["run_id"])
        run_status = run["status"] if run else None
        key = (row["source_app_token"], row["source_table_id"], row["source_record_id"])

        if looks_killed(row):
            if run is not None:
                wanted = STATUS_BY_RUN.get(run_status)
                revive.append(
                    {
                        "table": table,
                        "run_id": row["run_id"],
                        "record": row["display_text"],
                        "was": row.get("status"),
                        "now": wanted,
                        "run_status": run_status,
                        "reactivate": not row.get("active") and key not in active_records,
                    }
                )
            else:
                if not str(row.get("last_error") or "").endswith(RETRYABLE_SUFFIX):
                    relabel.append(
                        {
                            "table": table,
                            "run_id": row["run_id"],
                            "record": row["display_text"],
                            "error": str(row.get("last_error") or ""),
                        }
                    )
            continue

        # 活跃但已经很久没动、run 又不在非终态 —— 疑似僵尸。
        if table == "production_tasks" and row.get("active"):
            age = parse_stamp(row.get("updated_at") or row.get("created_at"))
            stale = age is not None and (now - age) > timedelta(days=stale_days)
            if stale and (run_status is None or run_status not in NON_TERMINAL):
                release.append(
                    {
                        "table": table,
                        "run_id": row["run_id"],
                        "record": row["display_text"],
                        "status": row.get("status"),
                        "run_status": run_status,
                        "updated_at": row.get("updated_at"),
                    }
                )
                continue
            keep.append({"record": row["display_text"], "run_id": row["run_id"]})

    return revive, relabel, release, keep


def backup(tag: str) -> str:
    target = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "_backup_repair_killed_bindings_%s" % tag,
    )
    os.makedirs(target, exist_ok=True)
    for path in (PROD_DB, AGENT_DB):
        source = sqlite3.connect(path)
        try:
            destination = sqlite3.connect(os.path.join(target, os.path.basename(path)))
            try:
                source.backup(destination)   # WAL 下的安全快照
            finally:
                destination.close()
        finally:
            source.close()
    return target


def apply_actions(revive, relabel, release_stale, *, do_release_stale: bool) -> None:
    connection = sqlite3.connect(PROD_DB)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in revive:
            table = item["table"]
            if item["now"] is None:
                continue
            connection.execute(
                "UPDATE %s SET status = ?, last_error = NULL%s WHERE run_id = ?"
                % (table, ", active = 1" if item["reactivate"] else ""),
                (item["now"], item["run_id"]),
            )
        for item in relabel:
            connection.execute(
                "UPDATE %s SET last_error = ? WHERE run_id = ?" % item["table"],
                (item["error"] + RETRYABLE_SUFFIX, item["run_id"]),
            )
        if do_release_stale:
            for item in release_stale:
                connection.execute(
                    "UPDATE production_tasks SET status = ?, last_error = ?, "
                    "active = 0, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                    ("失败", "任务长期没有进展，绑定已释放，可重新领取", item["run_id"]),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    parser.add_argument("--stale-days", type=float, default=3.0)
    parser.add_argument(
        "--release-stale",
        action="store_true",
        help="连僵尸活跃绑定一起释放（默认只报告，因为它们可能是用户在等的审批）",
    )
    parser.add_argument(
        "--report",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "repair_killed_bindings.txt"
        ),
        help="把同一份报告写成 UTF-8 文本（控制台是 GBK，中文会乱码）",
    )
    args = parser.parse_args()

    try:
        return _run(args)
    finally:
        try:
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write("\n".join(REPORT) + "\n")
        except OSError:
            pass


def _run(args) -> int:
    if not os.path.exists(PROD_DB) or not os.path.exists(AGENT_DB):
        log("找不到数据库：%s / %s" % (PROD_DB, AGENT_DB))
        return 2

    runs = load_runs()
    bindings = load_bindings()
    log("绑定行 %d 条，运行 %d 条" % (len(bindings), len(runs)))

    revive, relabel, release_stale, keep = plan_actions(
        bindings,
        runs,
        stale_days=args.stale_days,
        release_stale=args.release_stale,
    )

    log("")
    log("[1] 被误杀的绑定（run 其实存在）—— 判死是错的，应恢复：%d 条" % len(revive))
    for item in revive:
        log("    %-8s run=%s %s -> %s (run=%s) 重新激活=%s"
            % (item["record"], item["run_id"][:8], item["was"], item["now"],
               item["run_status"], item["reactivate"]))

    log("")
    log("[2] 判死判对了（run 确实不存在）—— 只升级文案：%d 条" % len(relabel))
    for item in relabel:
        log("    %-8s run=%s  %s" % (item["record"], item["run_id"][:8], item["error"]))

    log("")
    log("[3] 僵尸活跃绑定（>%.1f 天没动且 run 不在非终态）：%d 条" % (args.stale_days, len(release_stale)))
    for item in release_stale:
        log("    %-8s run=%s status=%s run状态=%s 最后更新=%s  <- 释放=%s"
            % (item["record"], item["run_id"][:8], item["status"], item["run_status"],
               item["updated_at"], args.release_stale))

    log("")
    log("[4] 正常活跃绑定（保持不动）：%d 条" % len(keep))
    for item in keep:
        log("    %-8s run=%s" % (item["record"], item["run_id"][:8]))

    if not args.apply:
        log("")
        log("== 这是 dry-run，什么都没写。确认无误后加 --apply ==")
        return 0

    if not revive and not relabel and not (args.release_stale and release_stale):
        log("")
        log("没有需要写入的改动。")
        return 0

    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup(tag)
    log("")
    log("已备份到 %s" % target)
    apply_actions(revive, relabel, release_stale, do_release_stale=args.release_stale)
    log("已写入：恢复 %d 条、升级文案 %d 条、释放僵尸 %d 条"
        % (len(revive), len(relabel),
           len(release_stale) if args.release_stale else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
