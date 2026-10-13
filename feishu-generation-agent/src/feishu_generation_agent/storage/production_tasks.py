import asyncio
import json
from pathlib import Path
from typing import Literal

import aiosqlite

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.production_bitable import (
    ProductionBinding,
    ProductionDelivery,
    ProductionTaskSummary,
    ResultTableTarget,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_tasks (
  source_app_token TEXT NOT NULL,
  source_table_id TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL DEFAULT 'prime-local',
  source_location_json TEXT NOT NULL,
  source_url TEXT NOT NULL,
  display_text TEXT NOT NULL,
  progress TEXT NOT NULL,
  maker_open_id TEXT,
  maker_name TEXT,
  snapshot_json TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  last_error TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  deleted INTEGER NOT NULL DEFAULT 0,
  archived_auto INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source_app_token, source_table_id, source_record_id)
);
CREATE TABLE IF NOT EXISTS production_task_history (
  source_app_token TEXT NOT NULL,
  source_table_id TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL DEFAULT 'prime-local',
  source_location_json TEXT NOT NULL,
  source_url TEXT NOT NULL,
  display_text TEXT NOT NULL,
  progress TEXT NOT NULL,
  maker_open_id TEXT,
  maker_name TEXT,
  snapshot_json TEXT NOT NULL,
  run_id TEXT NOT NULL PRIMARY KEY,
  thread_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  last_error TEXT,
  active INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  archived_auto INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maker_result_tables (
  maker_open_id TEXT PRIMARY KEY,
  maker_name TEXT NOT NULL,
  app_token TEXT NOT NULL,
  table_id TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_deliveries (
  run_id TEXT PRIMARY KEY,
  result_record_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
_BINDING_COLUMNS = """
source_app_token, source_table_id, source_record_id, owner_user_id,
source_location_json, source_url, display_text, progress, maker_open_id,
maker_name, snapshot_json, run_id, thread_id, status, last_error, active,
deleted, archived_auto, created_at, updated_at
"""

#: 生产表任务的两张表（当前绑定 / 历史尝试）。按记录归档、按记录恢复、以及
#: run_id → record_id 反查都要同时覆盖这两张，否则历史尝试会漏在列表里。
_PRODUCTION_TABLES = ("production_tasks", "production_task_history")


class ProductionTaskAlreadyClaimed(RuntimeError):
    pass


class ProductionTaskStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> "ProductionTaskStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(database_path), isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.executescript(_SCHEMA)
            await connection.execute("BEGIN IMMEDIATE")
            await cls._migrate_owners(connection)
            await cls._migrate_deleted(connection)
            await cls._migrate_hidden(connection)
            await cls._migrate_archived_auto(connection)
            await connection.commit()
        except BaseException:
            await connection.rollback()
            await connection.close()
            raise
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def claim(
        self,
        location: BitableLocation,
        task: ProductionTaskSummary,
        *,
        run_id: str,
        thread_id: str,
        owner_user_id: str = "prime-local",
    ) -> ProductionBinding:
        app_token = _app_token(location)
        _validate_owner(owner_user_id)
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(
                    "SELECT active FROM production_tasks WHERE source_app_token = ? AND source_table_id = ? AND source_record_id = ?",
                    (app_token, location.table_id, task.record_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None and existing["active"] == 1:
                    raise ProductionTaskAlreadyClaimed("生产表任务已被领取")
                if existing is not None:
                    await self._connection.execute(
                        """INSERT OR IGNORE INTO production_task_history (
                          source_app_token, source_table_id, source_record_id,
                          owner_user_id, source_location_json, source_url,
                          display_text, progress, maker_open_id, maker_name,
                          snapshot_json, run_id, thread_id, status, last_error,
                          active, deleted, hidden, archived_auto,
                          created_at, updated_at
                        )
                        SELECT
                          source_app_token, source_table_id, source_record_id,
                          owner_user_id, source_location_json, source_url,
                          display_text, progress, maker_open_id, maker_name,
                          snapshot_json, run_id, thread_id, status, last_error,
                          active, deleted,
                          CASE WHEN status = '失败' THEN 1 ELSE 0 END,
                          archived_auto,
                          created_at, updated_at
                        FROM production_tasks
                        WHERE source_app_token = ? AND source_table_id = ?
                        AND source_record_id = ?""",
                        (app_token, location.table_id, task.record_id),
                    )
                payload = (
                    owner_user_id,
                    json.dumps(location.model_dump(mode="json"), ensure_ascii=False),
                    task.source_url,
                    task.display_text,
                    task.progress,
                    task.maker_open_id,
                    task.maker_name,
                    json.dumps(task.snapshot.model_dump(mode="json"), ensure_ascii=False),
                    run_id,
                    thread_id,
                    TableTaskStatus.PROCESSING.value,
                )
                if existing is None:
                    await self._connection.execute(
                        """INSERT INTO production_tasks (
                          source_app_token, source_table_id, source_record_id,
                          owner_user_id, source_location_json, source_url,
                          display_text, progress, maker_open_id, maker_name,
                          snapshot_json, run_id, thread_id, status,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (app_token, location.table_id, task.record_id, *payload),
                    )
                else:
                    await self._connection.execute(
                        """UPDATE production_tasks SET
                          owner_user_id = ?, source_location_json = ?,
                          source_url = ?, display_text = ?, progress = ?,
                          maker_open_id = ?, maker_name = ?, snapshot_json = ?,
                          run_id = ?, thread_id = ?, status = ?,
                          last_error = NULL, active = 1,
                          deleted = 0, hidden = 0, archived_auto = 0,
                          updated_at = CURRENT_TIMESTAMP
                        WHERE source_app_token = ? AND source_table_id = ? AND source_record_id = ?""",
                        (*payload, app_token, location.table_id, task.record_id),
                    )
                binding = await self._get_by_run_locked(
                    run_id, owner_user_id=owner_user_id
                )
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        assert binding is not None
        return binding

    async def get_by_run(
        self, run_id: str, *, owner_user_id: str | None = None
    ) -> ProductionBinding | None:
        async with self._lock:
            return await self._get_by_run_locked(
                run_id, owner_user_id=owner_user_id
            )

    async def run_owner_state(
        self, run_id: str, *, owner_user_id: str
    ) -> Literal["missing", "owned", "other"]:
        _validate_owner(owner_user_id)
        async with self._lock:
            cursor = await self._connection.execute(
                """
                SELECT owner_user_id FROM production_tasks WHERE run_id = ?
                UNION ALL
                SELECT owner_user_id FROM production_task_history
                WHERE run_id = ?
                LIMIT 1
                """,
                (run_id, run_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return "missing"
        return "owned" if row["owner_user_id"] == owner_user_id else "other"

    async def list_active(
        self,
        app_token: str,
        table_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> list[ProductionBinding]:
        owner_clause = (
            "AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        parameters: tuple[object, ...] = (
            (app_token, table_id, owner_user_id)
            if owner_user_id is not None
            else (app_token, table_id)
        )
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT * FROM production_tasks
                WHERE source_app_token = ? AND source_table_id = ? AND active = 1
                {owner_clause}
                ORDER BY created_at ASC""",
                parameters,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_binding_from_row(row) for row in rows]

    async def list_recent(
        self,
        app_token: str,
        table_id: str,
        *,
        owner_user_id: str | None = None,
        limit: int = 10,
    ) -> list[ProductionBinding]:
        if limit < 1:
            return []
        owner_clause = (
            "AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        owner_parameters: tuple[str, ...] = (
            (owner_user_id,) if owner_user_id is not None else ()
        )
        # `hidden` 只有一个写入点：claim() 把上一条**失败**的尝试搬进历史时按
        # `CASE WHEN status = '失败'` 打的标记。它的原意是「别让失败版占着最近列表」，
        # 但用户要的是「我失败过几次、为什么失败」都看得到（2026-09-24：「『成品与结果』
        # 区少了某些记录」，记录 recvvWdIXpk2uE 的 4 条失败尝试全被吞掉）。
        # 这里只放开「因失败而被隐藏」这一种：hidden=0 的正常行，加上 hidden=1 但状态
        # 就是失败的行；其它隐藏语义（将来若有别的写入点）照旧生效。
        # `deleted=1`（用户主动删除 / 回收站）与它无关，一行都不放开。
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT {_BINDING_COLUMNS} FROM (
                      SELECT {_BINDING_COLUMNS} FROM production_tasks
                      WHERE source_app_token = ? AND source_table_id = ?
                        AND active = 0 AND deleted = 0
                      {owner_clause}
                      UNION ALL
                      SELECT {_BINDING_COLUMNS} FROM production_task_history
                      WHERE source_app_token = ? AND source_table_id = ?
                        AND active = 0 AND deleted = 0
                        AND (hidden = 0 OR status = ?)
                      {owner_clause}
                ) ORDER BY updated_at DESC LIMIT ?""",
                (
                    app_token,
                    table_id,
                    *owner_parameters,
                    app_token,
                    table_id,
                    TableTaskStatus.FAILED.value,
                    *owner_parameters,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_binding_from_row(row) for row in rows]

    async def list_record_runs(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        *,
        owner_user_id: str | None = None,
        limit: int = 500,
    ) -> list[ProductionBinding]:
        """某一条多维表格记录的**全部**历次尝试（不受「最近任务」窗口限制）。

        `list_recent` 是给全局「最近任务」列表用的：先扫最近 N 条 run，再按记录
        只留 10 条任务。更老的记录因此永远进不了那份列表（2026-09-24 实测：89 条
        失败尝试里有 45 条属于窗口外的老记录）。用户点开**某一条记录**时要的是它
        自己的完整历史，所以这里按 record_id 直接查，一条都不截。

        `hidden` 在这里**不过滤**：隐藏的原意是「别让失败版占着最近列表」，而按
        记录看历史时，失败的那几次正是用户要找的东西（`deleted=1` 是用户主动
        删除，仍然一行都不给）。
        """
        if limit < 1:
            return []
        owner_clause = (
            "AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        owner_parameters: tuple[str, ...] = (
            (owner_user_id,) if owner_user_id is not None else ()
        )
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT {_BINDING_COLUMNS} FROM (
                      SELECT {_BINDING_COLUMNS} FROM production_tasks
                      WHERE source_app_token = ? AND source_table_id = ?
                        AND source_record_id = ? AND deleted = 0
                      {owner_clause}
                      UNION ALL
                      SELECT {_BINDING_COLUMNS} FROM production_task_history
                      WHERE source_app_token = ? AND source_table_id = ?
                        AND source_record_id = ? AND deleted = 0
                      {owner_clause}
                ) ORDER BY updated_at DESC LIMIT ?""",
                (
                    app_token,
                    table_id,
                    record_id,
                    *owner_parameters,
                    app_token,
                    table_id,
                    record_id,
                    *owner_parameters,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_binding_from_row(row) for row in rows]

    async def list_archived(
        self,
        app_token: str,
        table_id: str,
        *,
        owner_user_id: str | None = None,
        limit: int = 200,
    ) -> list[ProductionBinding]:
        if limit < 1:
            return []
        owner_clause = (
            "AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        owner_parameters: tuple[str, ...] = (
            (owner_user_id,) if owner_user_id is not None else ()
        )
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT * FROM (
                    SELECT {_BINDING_COLUMNS} FROM production_tasks
                    WHERE source_app_token = ? AND source_table_id = ?
                      AND active = 0 AND deleted = 1
                    {owner_clause}
                    UNION ALL
                    SELECT {_BINDING_COLUMNS} FROM production_task_history
                    WHERE source_app_token = ? AND source_table_id = ?
                      AND active = 0 AND deleted = 1
                    {owner_clause}
                ) ORDER BY updated_at DESC LIMIT ?""",
                (
                    app_token,
                    table_id,
                    *owner_parameters,
                    app_token,
                    table_id,
                    *owner_parameters,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_binding_from_row(row) for row in rows]

    async def list_archived_records(
        self,
        app_token: str,
        table_id: str,
        *,
        owner_user_id: str | None = None,
        limit: int = 200,
    ) -> list[ProductionBinding]:
        """回收站列表：**每条记录一行**（取它最新那一次尝试）。

        归档是按整条记录做的，一条记录的历次尝试会一起变成 `deleted = 1`。按 run
        逐行返回，回收站里同一条记录会重复出现好几次；而**先按 run 截断再去重**，
        多尝试的老记录会被整个挤出窗口 —— 实测 327 行归档数据共 42 条记录，按 run
        取前 200 行只剩 29 条，另外 13 条在回收站里根本看不到（正是这次要修的
        「找不到」）。所以去重必须发生在**截断之前**，放在 SQL 里做。

        `GROUP BY source_record_id` + `MAX(updated_at)`：SQLite 保证「恰好一个
        `max()` 聚合」的查询里裸列取的就是那条 max 行 —— 即这条记录最新的一次
        尝试，与 `list_archived` 的排序口径一致。
        """
        if limit < 1:
            return []
        owner_clause = (
            "AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        owner_parameters: tuple[str, ...] = (
            (owner_user_id,) if owner_user_id is not None else ()
        )
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT {_BINDING_COLUMNS} FROM (
                    SELECT {_BINDING_COLUMNS} FROM production_tasks
                    WHERE source_app_token = ? AND source_table_id = ?
                      AND active = 0 AND deleted = 1
                    {owner_clause}
                    UNION ALL
                    SELECT {_BINDING_COLUMNS} FROM production_task_history
                    WHERE source_app_token = ? AND source_table_id = ?
                      AND active = 0 AND deleted = 1
                    {owner_clause}
                ) GROUP BY source_record_id
                ORDER BY MAX(updated_at) DESC LIMIT ?""",
                (
                    app_token,
                    table_id,
                    *owner_parameters,
                    app_token,
                    table_id,
                    *owner_parameters,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_binding_from_row(row) for row in rows]

    async def archive(
        self,
        run_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> int:
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        parameters: tuple[object, ...] = (
            run_id,
            *((owner_user_id,) if owner_user_id is not None else ()),
        )
        updated = 0
        async with self._lock:
            for table in _PRODUCTION_TABLES:
                cursor = await self._connection.execute(
                    f"UPDATE {table} SET deleted = 1, archived_auto = 0, "
                    "updated_at = CURRENT_TIMESTAMP "
                    f"WHERE run_id = ? AND active = 0{owner_clause}",
                    parameters,
                )
                updated += cursor.rowcount
                await cursor.close()
        return updated

    async def restore(
        self,
        run_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> int:
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        parameters: tuple[object, ...] = (
            run_id,
            *((owner_user_id,) if owner_user_id is not None else ()),
        )
        updated = 0
        async with self._lock:
            for table in _PRODUCTION_TABLES:
                cursor = await self._connection.execute(
                    f"UPDATE {table} SET deleted = 0, archived_auto = 0, "
                    "updated_at = CURRENT_TIMESTAMP "
                    f"WHERE run_id = ? AND active = 0{owner_clause}",
                    parameters,
                )
                updated += cursor.rowcount
                await cursor.close()
        return updated

    async def archive_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        *,
        owner_user_id: str | None = None,
        auto: bool = False,
    ) -> int:
        """把**一整条记录**的历次尝试一起移进回收站（`deleted=1`），绝不物理删除。

        为什么按记录而不是按 run：列表里的「一行」是一条**记录**，它可不可见由
        这条记录**任意一次尝试**决定（`list_recent` 先按 run 取窗口、再按 record
        分组）。只归档当前那一次尝试，剩下的尝试照样把记录留在列表里 —— 用户点
        「删除」看着没反应（「删了还在」）。整条记录一起归档，列表才会真的少一条。

        只动 `active = 0` 的行：正在跑的那一行（`production_tasks.active = 1`）留着。
        宁可列表暂时多于 10 条，也不能把用户正盯着的任务藏进回收站。

        **刻意不刷新 `updated_at`**：归档不是一次「活动」，改时间戳等于篡改这条记录
        的真实时间，也会让恢复后的排序失真。回收站里因此显示的是它真正的最后活动
        时间（`auto=1` 的行前端按这个口径显示成「最后更新」）。
        """
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        predicate = (
            "source_app_token = ? AND source_table_id = ? "
            f"AND source_record_id = ? AND active = 0{owner_clause}"
        )
        parameters: tuple[object, ...] = (
            app_token,
            table_id,
            record_id,
            *((owner_user_id,) if owner_user_id is not None else ()),
        )
        updated = 0
        async with self._lock:
            for table in _PRODUCTION_TABLES:
                cursor = await self._connection.execute(
                    f"UPDATE {table} SET deleted = 1, archived_auto = ? "
                    f"WHERE {predicate}",
                    (1 if auto else 0, *parameters),
                )
                updated += cursor.rowcount
                await cursor.close()
        return updated

    async def restore_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> int:
        """把整条记录从回收站恢复（`deleted=0`），并把它顶回列表可见的位置。

        `list_recent` 是按 `updated_at` 倒序取前 10 条**记录**的：不把时间戳顶到
        当前，一条老记录恢复完仍然落在窗口之外 —— 用户点了「恢复」却什么都没发生
        （那正是这次要修的「隐身」问题）。

        只顶**最新那一次尝试**的 `updated_at`：把整条记录的时间戳全顶掉会把同一条
        记录内部历次尝试的顺序抹平，历史滑条的顺序就乱了。
        """
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        predicate = (
            "source_app_token = ? AND source_table_id = ? "
            f"AND source_record_id = ? AND active = 0{owner_clause}"
        )
        parameters: tuple[object, ...] = (
            app_token,
            table_id,
            record_id,
            *((owner_user_id,) if owner_user_id is not None else ()),
        )
        updated = 0
        async with self._lock:
            for table in _PRODUCTION_TABLES:
                cursor = await self._connection.execute(
                    f"UPDATE {table} SET deleted = 0, archived_auto = 0 "
                    f"WHERE {predicate}",
                    parameters,
                )
                updated += cursor.rowcount
                await cursor.close()
            if not updated:
                return 0
            cursor = await self._connection.execute(
                f"""SELECT run_id FROM (
                      SELECT run_id, updated_at FROM production_tasks
                      WHERE {predicate}
                      UNION ALL
                      SELECT run_id, updated_at FROM production_task_history
                      WHERE {predicate}
                    ) ORDER BY updated_at DESC LIMIT 1""",
                (*parameters, *parameters),
            )
            newest = await cursor.fetchone()
            await cursor.close()
            if newest is not None:
                for table in _PRODUCTION_TABLES:
                    cursor = await self._connection.execute(
                        f"UPDATE {table} SET updated_at = CURRENT_TIMESTAMP "
                        "WHERE run_id = ?",
                        (newest["run_id"],),
                    )
                    await cursor.close()
        return updated

    async def record_ids_for_runs(self, run_ids) -> set[str]:
        """run_id 集合 → 它们所属的 record_id（两张表都查）。

        自动归档要靠它把「`runs` 表里 running / resuming 的运行」翻译成「哪几条
        记录正在跑」，从而整条跳过 —— 那些运行对应的绑定行可能已经 `active = 0`
        （进程重启留下的残影），只看 `active` 挡不住。
        """
        values = [str(run_id) for run_id in run_ids if run_id]
        if not values:
            return set()
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            cursor = await self._connection.execute(
                f"""SELECT source_record_id FROM production_tasks
                    WHERE run_id IN ({placeholders})
                    UNION
                    SELECT source_record_id FROM production_task_history
                    WHERE run_id IN ({placeholders})""",
                (*values, *values),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {str(row["source_record_id"]) for row in rows}

    async def set_status(
        self,
        run_id: str,
        status: TableTaskStatus,
        last_error: str | None = None,
        *,
        owner_user_id: str | None = None,
    ) -> ProductionBinding:
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        return await self._update_run(
            run_id,
            f"""UPDATE production_tasks
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE run_id = ?{owner_clause}""",
            (
                status.value,
                last_error,
                run_id,
                *((owner_user_id,) if owner_user_id is not None else ()),
            ),
        )

    async def release(
        self,
        run_id: str,
        *,
        status: TableTaskStatus,
        last_error: str | None = None,
        owner_user_id: str | None = None,
    ) -> ProductionBinding:
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        return await self._update_run(
            run_id,
            f"""UPDATE production_tasks
            SET status = ?, last_error = ?, active = 0,
            updated_at = CURRENT_TIMESTAMP WHERE run_id = ?{owner_clause}""",
            (
                status.value,
                last_error,
                run_id,
                *((owner_user_id,) if owner_user_id is not None else ()),
            ),
        )

    async def get_result_target(self, maker_open_id: str) -> ResultTableTarget | None:
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM maker_result_tables WHERE maker_open_id = ?", (maker_open_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return ResultTableTarget(
            maker_open_id=row["maker_open_id"], maker_name=row["maker_name"],
            app_token=row["app_token"], table_id=row["table_id"], url=row["url"],
        )

    async def upsert_result_target(self, target: ResultTableTarget) -> None:
        async with self._lock:
            await self._connection.execute(
                """INSERT INTO maker_result_tables (
                  maker_open_id, maker_name, app_token, table_id, url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(maker_open_id) DO UPDATE SET
                  maker_name = excluded.maker_name, app_token = excluded.app_token,
                  table_id = excluded.table_id, url = excluded.url,
                  updated_at = CURRENT_TIMESTAMP""",
                (target.maker_open_id, target.maker_name, target.app_token, target.table_id, target.url),
            )
            await self._connection.commit()

    async def reserve_delivery(self, run_id: str) -> ProductionDelivery:
        async with self._lock:
            await self._connection.execute(
                """INSERT INTO production_deliveries (run_id, status, created_at, updated_at)
                VALUES (?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(run_id) DO NOTHING""",
                (run_id,),
            )
            await self._connection.commit()
            return await self._get_delivery_locked(run_id)

    async def complete_delivery(self, run_id: str, *, result_record_id: str) -> ProductionDelivery:
        async with self._lock:
            await self._connection.execute(
                """UPDATE production_deliveries SET result_record_id = ?, status = 'succeeded',
                updated_at = CURRENT_TIMESTAMP WHERE run_id = ?""",
                (result_record_id, run_id),
            )
            await self._connection.commit()
            return await self._get_delivery_locked(run_id)

    async def get_delivery(self, run_id: str) -> ProductionDelivery | None:
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM production_deliveries WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _delivery_from_row(row) if row is not None else None

    async def _get_by_run_locked(
        self, run_id: str, *, owner_user_id: str | None = None
    ) -> ProductionBinding | None:
        owner_clause = (
            " AND owner_user_id = ?" if owner_user_id is not None else ""
        )
        parameters: tuple[object, ...] = (
            (run_id, owner_user_id)
            if owner_user_id is not None
            else (run_id,)
        )
        cursor = await self._connection.execute(
            f"SELECT * FROM production_tasks WHERE run_id = ?{owner_clause}",
            parameters,
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            cursor = await self._connection.execute(
                f"SELECT * FROM production_task_history "
                f"WHERE run_id = ?{owner_clause}",
                parameters,
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _binding_from_row(row) if row is not None else None

    async def _get_delivery_locked(self, run_id: str) -> ProductionDelivery:
        cursor = await self._connection.execute(
            "SELECT * FROM production_deliveries WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        return _delivery_from_row(row)

    async def _update_run(
        self, run_id: str, statement: str, parameters: tuple[object, ...]
    ) -> ProductionBinding:
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(statement, parameters)
                updated = cursor.rowcount == 1
                await cursor.close()
                if not updated:
                    cursor = await self._connection.execute(
                        statement.replace("production_tasks", "production_task_history"),
                        parameters,
                    )
                    updated = cursor.rowcount == 1
                    await cursor.close()
                if not updated:
                    raise KeyError(f"unknown run_id: {run_id}")
                binding = await self._get_by_run_locked(run_id)
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        assert binding is not None
        return binding

    @staticmethod
    async def _migrate_owners(connection: aiosqlite.Connection) -> None:
        for table in ("production_tasks", "production_task_history"):
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            columns = {str(row[1]) for row in rows}
            if "owner_user_id" not in columns:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_user_id "
                    "TEXT NOT NULL DEFAULT 'prime-local'"
                )

    @staticmethod
    async def _migrate_deleted(connection: aiosqlite.Connection) -> None:
        for table in ("production_tasks", "production_task_history"):
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            columns = {str(row[1]) for row in rows}
            if "deleted" not in columns:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN deleted "
                    "INTEGER NOT NULL DEFAULT 0"
                )


    @staticmethod
    async def _migrate_hidden(connection: aiosqlite.Connection) -> None:
        for table in _PRODUCTION_TABLES:
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            columns = {str(row[1]) for row in rows}
            if "hidden" not in columns:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN hidden "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    async def _migrate_archived_auto(connection: aiosqlite.Connection) -> None:
        for table in _PRODUCTION_TABLES:
            cursor = await connection.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            columns = {str(row[1]) for row in rows}
            if "archived_auto" not in columns:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN archived_auto "
                    "INTEGER NOT NULL DEFAULT 0"
                )


def _app_token(location: BitableLocation) -> str:
    if not location.app_token:
        raise ValueError("production location is unresolved")
    return location.app_token


def _validate_owner(owner_user_id: str) -> None:
    if (
        not isinstance(owner_user_id, str)
        or not owner_user_id
        or owner_user_id != owner_user_id.strip()
        or len(owner_user_id) > 255
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in owner_user_id
        )
    ):
        raise ValueError("invalid owner_user_id")


def _binding_from_row(row: aiosqlite.Row) -> ProductionBinding:
    return ProductionBinding(
        source_location=BitableLocation.model_validate(json.loads(row["source_location_json"])),
        record_id=row["source_record_id"], owner_user_id=row["owner_user_id"],
        source_url=row["source_url"],
        display_text=row["display_text"], progress=row["progress"],
        maker_open_id=row["maker_open_id"], maker_name=row["maker_name"],
        snapshot=json.loads(row["snapshot_json"]), run_id=row["run_id"],
        thread_id=row["thread_id"], status=TableTaskStatus(row["status"]),
        last_error=row["last_error"], created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted=bool(row["deleted"]),
        auto_archived=bool(row["archived_auto"]),
        active=bool(row["active"]),
    )


def _delivery_from_row(row: aiosqlite.Row) -> ProductionDelivery:
    return ProductionDelivery(
        run_id=row["run_id"], result_record_id=row["result_record_id"], status=row["status"]
    )
