"""把「最近任务」窗口之外的老记录**整条**移进回收站（一次性数据修复）。

背景：`_RECENT_TASK_LIMIT = 10` 以前只是**展示窗口**（`recent_runs()` 里
`list(grouped)[:10]`），窗口外的记录谁也没动过 —— 它们既不在列表里、也不在回收站
里，等于人间蒸发。2026-09-24 实测：当前 owner 有 33 条可见记录，界面只显示 10 条，
剩下 23 条用户永远找不到（这正是用户最初抱怨「历史记录被吞了」的根因）。

现在 `claim` / `rerun`（产生新尝试的写路径）会自动归档；这个脚本负责把**历史遗留**
的那一批补上。归档规则与线上**完全一致** —— 直接调用
`ProductionBitableService.auto_archive_overflow`，不另写一套：

* 每个 owner 只保留最近 10 条**记录**，更老的整条归档；
* `runs` 表里状态是 `running` / `resuming` 的记录**跳过**（用户可能正盯着它）；
* 只把 `deleted` 从 0 改成 1（回收站，**可恢复**）；**不物理删除**，也不改
  `status` / 提示词快照 / 时间戳。

用法（在 feishu-generation-agent 目录下）：

    .venv\\Scripts\\python.exe scripts\\auto_archive_overflow.py            # 只出计划
    .venv\\Scripts\\python.exe scripts\\auto_archive_overflow.py --apply    # 先备份再执行

`--apply` 会先把 `data/production-bitable.sqlite3` 用 SQLite 在线备份 API 复制一份到
工作区的 `_autoarchive_backup_<时间戳>/`，再动真库。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feishu_generation_agent.bitable.production_service import (  # noqa: E402
    ProductionBitableService,
    ProductionTaskSource,
)
from feishu_generation_agent.domain.bitable import BitableLocation  # noqa: E402
from feishu_generation_agent.storage.production_tasks import (  # noqa: E402
    ProductionTaskStore,
)

PRODUCTION_DB = ROOT / "data" / "production-bitable.sqlite3"
AGENT_DB = ROOT / "data" / "agent.sqlite3"
#: 本机 Portal 用户的 owner id（接口都按 X-Portal-User-Id 隔离）。
DEFAULT_OWNER = "6b34d9e8-3058-41b6-89b2-4cedac2362ee"
#: 备份放到**工作区**根目录（feishu-generation-agent 的上一层）。
WORKSPACE_ROOT = ROOT.parents[1]

_TABLES = ("production_tasks", "production_task_history")
#: 只允许这几个字段变化；其余字段必须逐字节相同。
_MUTABLE_COLUMNS = {"deleted", "archived_auto"}


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def backup_database(source: Path, target: Path) -> None:
    """在线备份（`sqlite3.Connection.backup`）：服务在写也不会拷到半截状态。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = _connect(source, read_only=True)
    try:
        destination = sqlite3.connect(str(target))
        try:
            origin.backup(destination)
        finally:
            destination.close()
    finally:
        origin.close()


def snapshot_rows(database: Path) -> dict[tuple[str, str], dict]:
    """两张表全字段快照，用来证明「只改了 deleted」。"""
    connection = _connect(database, read_only=True)
    try:
        rows: dict[tuple[str, str], dict] = {}
        for table in _TABLES:
            cursor = connection.execute(f"SELECT * FROM {table}")
            for row in cursor.fetchall():
                rows[(table, row["run_id"])] = dict(row)
            cursor.close()
        return rows
    finally:
        connection.close()


def deleted_counts(database: Path, owner: str) -> dict[int, int]:
    connection = _connect(database, read_only=True)
    try:
        cursor = connection.execute(
            f"""SELECT deleted, COUNT(*) AS n FROM (
                    SELECT deleted FROM production_tasks WHERE owner_user_id = ?
                    UNION ALL
                    SELECT deleted FROM production_task_history
                    WHERE owner_user_id = ?
                ) GROUP BY deleted""",
            (owner, owner),
        )
        counts = {int(row["deleted"]): int(row["n"]) for row in cursor.fetchall()}
        cursor.close()
        return counts
    finally:
        connection.close()


def owners(database: Path) -> list[str]:
    connection = _connect(database, read_only=True)
    try:
        cursor = connection.execute(
            f"""SELECT owner_user_id, COUNT(*) AS n FROM (
                    SELECT owner_user_id FROM production_tasks
                    UNION ALL
                    SELECT owner_user_id FROM production_task_history
                ) GROUP BY owner_user_id ORDER BY n DESC"""
        )
        rows = [str(row["owner_user_id"]) for row in cursor.fetchall()]
        cursor.close()
        return rows
    finally:
        connection.close()


def table_location(database: Path) -> BitableLocation:
    connection = _connect(database, read_only=True)
    try:
        row = connection.execute(
            "SELECT source_app_token, source_table_id FROM production_tasks LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit("生产表任务库里一条记录都没有，无需归档")
    return BitableLocation(
        wiki_token="",
        app_token=str(row["source_app_token"]),
        table_id=str(row["source_table_id"]),
        view_id="",
        source_url="",
    )


class _AgentRuns:
    """给自动归档提供 `runs` 表状态（判断「谁在跑」）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def list_runs(self, *, owner_user_id=None, statuses=None):
        connection = _connect(self._path, read_only=True)
        try:
            if owner_user_id is None:
                cursor = connection.execute("SELECT run_id, status FROM runs")
            else:
                cursor = connection.execute(
                    "SELECT run_id, status FROM runs WHERE owner_user_id = ?",
                    (owner_user_id,),
                )
            rows = [
                {"run_id": str(row["run_id"]), "status": str(row["status"])}
                for row in cursor.fetchall()
            ]
            cursor.close()
            return rows
        finally:
            connection.close()


class _Runtime:
    def __init__(self, repository: _AgentRuns) -> None:
        self.repository = repository


async def _state(store, location, owner: str) -> dict:
    recent = await store.list_recent(
        location.app_token or "",
        location.table_id,
        owner_user_id=owner,
        limit=800,
    )
    archived = await store.list_archived(
        location.app_token or "",
        location.table_id,
        owner_user_id=owner,
        limit=5000,
    )
    return {
        "visible_records": len({item.record_id for item in recent}),
        "visible_rows": len(recent),
        "trash_records": len({item.record_id for item in archived}),
        "trash_rows": len(archived),
        "names": [item.display_text for item in recent],
    }


async def sweep(database: Path, owner: str) -> dict:
    location = table_location(database)
    store = await ProductionTaskStore.open(database)
    try:
        service = ProductionBitableService(
            bitable=None,
            store=store,
            runtime=_Runtime(_AgentRuns(AGENT_DB)),
            sources={"animation": ProductionTaskSource(location, "")},
            include_completed_for_test=True,
        )
        before = await _state(store, location, owner)
        archived_record_ids = await service.auto_archive_overflow(
            owner_user_id=owner
        )
        after = await _state(store, location, owner)
        archived_bindings = [
            item
            for item in await store.list_archived(
                location.app_token or "",
                location.table_id,
                owner_user_id=owner,
                limit=5000,
            )
            if item.record_id in set(archived_record_ids)
        ]
        return {
            "location": location,
            "before": before,
            "after": after,
            "archived_record_ids": archived_record_ids,
            "archived_names": sorted(
                {item.display_text for item in archived_bindings}
            ),
        }
    finally:
        await store.close()


def _diff_snapshot(before: dict, after: dict) -> list[str]:
    """逐字段比对：除了 deleted / archived_auto，任何差异都算失败。"""
    problems: list[str] = []
    if set(before) != set(after):
        problems.append("行集合变了（发生了物理删除或新增）")
        return problems
    for key, row in before.items():
        for column, value in row.items():
            if column in _MUTABLE_COLUMNS:
                continue
            if after[key][column] != value:
                problems.append(f"{key} 的 {column} 被改动了")
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库（默认只出计划，在副本上试跑）",
    )
    parser.add_argument(
        "--owner",
        action="append",
        default=None,
        help=f"要处理的 owner_user_id，可重复；默认 {DEFAULT_OWNER}",
    )
    parser.add_argument("--backup-dir", default=None, help="备份目录")
    args = parser.parse_args()

    targets = args.owner or [DEFAULT_OWNER]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.apply:
        backup_dir = (
            Path(args.backup_dir)
            if args.backup_dir
            else WORKSPACE_ROOT / f"_autoarchive_backup_{stamp}"
        )
        backup_path = backup_dir / PRODUCTION_DB.name
        backup_database(PRODUCTION_DB, backup_path)
        print(f"[备份] {backup_path}")
        print(f"[备份] {backup_path.stat().st_size} 字节")
        database = PRODUCTION_DB
    else:
        # 计划模式：在一份副本上试跑，真库一个字都不动。
        import shutil
        import tempfile

        workdir = Path(tempfile.mkdtemp(prefix="autoarchive_dryrun_"))
        database = workdir / PRODUCTION_DB.name
        shutil.copy2(PRODUCTION_DB, database)
        print(f"[计划] 真库未改动；在副本上试跑：{database}")

    before_rows = snapshot_rows(database)
    all_owners = owners(database)

    print("\n== 其它 owner（只读，不动） ==")
    for owner in all_owners:
        if owner in targets:
            continue
        print(f"  {owner}: deleted 分布 {deleted_counts(database, owner)}")

    for owner in targets:
        print(f"\n== owner {owner} ==")
        print(f"  处理前 deleted 分布: {deleted_counts(database, owner)}")
        result = await sweep(database, owner)
        before, after = result["before"], result["after"]
        print(
            f"  可见记录 {before['visible_records']} 条 "
            f"({before['visible_rows']} 行) -> {after['visible_records']} 条 "
            f"({after['visible_rows']} 行)"
        )
        print(
            f"  回收站记录 {before['trash_records']} 条 "
            f"({before['trash_rows']} 行) -> {after['trash_records']} 条 "
            f"({after['trash_rows']} 行)"
        )
        print(f"  本次归档 {len(result['archived_record_ids'])} 条记录：")
        print(f"    record_id: {result['archived_record_ids']}")
        print(f"    示例名称: {result['archived_names'][:8]}")
        print(f"  处理后 deleted 分布: {deleted_counts(database, owner)}")

    after_rows = snapshot_rows(database)
    problems = _diff_snapshot(before_rows, after_rows)
    print("\n== 自检（只允许 deleted / archived_auto 变化） ==")
    if problems:
        print("  失败：")
        for item in problems[:20]:
            print(f"    - {item}")
        return 1
    print("  通过：行数不变、除 deleted / archived_auto 外无任何字段被改动")
    if not args.apply:
        print("\n[计划] 没有写任何真实数据。加 --apply 才会执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
