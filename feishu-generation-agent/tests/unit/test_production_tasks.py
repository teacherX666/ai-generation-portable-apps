from pathlib import Path

import aiosqlite
import pytest

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
    ResultTableTarget,
)
from feishu_generation_agent.storage.production_tasks import (
    ProductionTaskAlreadyClaimed,
    ProductionTaskStore,
)


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiProd",
        app_token="appProd",
        table_id="tblProd",
        view_id="vewProd",
        source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )


def _task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="recProd",
        display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/docA",
        progress="未开始",
        maker_open_id="ou-maker",
        maker_name="制作人",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A",
            requirement_attachment="https://tenant.feishu.cn/docx/docA",
            project_names=["项目 A"],
            requester_open_ids=["ou-requester"],
            requester_names=["发起人"],
            maker_open_ids=["ou-maker"],
            maker_names=["制作人"],
        ),
    )


async def test_claim_is_unique_per_source_record_and_persists_snapshot(
    tmp_path: Path,
) -> None:
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="user-a",
        )

        assert binding.owner_user_id == "user-a"
        assert binding.snapshot.requirement_name == "需求 A"
        with pytest.raises(ProductionTaskAlreadyClaimed):
            await store.claim(
                _location(),
                _task(),
                run_id="run-2",
                thread_id="thread-2",
                owner_user_id="user-b",
            )
        assert [
            item.run_id
            for item in await store.list_active(
                "appProd", "tblProd", owner_user_id="user-a"
            )
        ] == ["run-1"]
        assert (
            await store.list_active(
                "appProd", "tblProd", owner_user_id="user-b"
            )
            == []
        )
    finally:
        await store.close()


async def test_result_target_and_delivery_row_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(path)
    try:
        await store.claim(_location(), _task(), run_id="run-1", thread_id="thread-1")
        await store.upsert_result_target(
            ResultTableTarget(
                maker_open_id="ou-maker",
                maker_name="制作人",
                app_token="app-result",
                table_id="tbl-result",
                url="https://tenant.feishu.cn/base/app-result",
            )
        )
        await store.reserve_delivery("run-1")
        await store.complete_delivery("run-1", result_record_id="rec-result")
    finally:
        await store.close()

    reopened = await ProductionTaskStore.open(path)
    try:
        assert (await reopened.get_result_target("ou-maker")).table_id == "tbl-result"
        assert (await reopened.get_delivery("run-1")).result_record_id == "rec-result"
    finally:
        await reopened.close()


async def test_production_store_migrates_legacy_active_and_history_owners(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-production.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            """
            CREATE TABLE production_tasks (
              source_app_token TEXT NOT NULL,
              source_table_id TEXT NOT NULL,
              source_record_id TEXT NOT NULL,
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
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (
                source_app_token, source_table_id, source_record_id
              )
            );
            CREATE TABLE production_task_history (
              source_app_token TEXT NOT NULL,
              source_table_id TEXT NOT NULL,
              source_record_id TEXT NOT NULL,
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
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )

    store = await ProductionTaskStore.open(path)
    await store.close()
    reopened = await ProductionTaskStore.open(path)
    await reopened.close()

    async with aiosqlite.connect(path) as connection:
        for table in ("production_tasks", "production_task_history"):
            rows = await (
                await connection.execute(f"PRAGMA table_info({table})")
            ).fetchall()
            owner = next(row for row in rows if row[1] == "owner_user_id")
            assert owner[3] == 1
            assert owner[4] == "'prime-local'"


async def test_recent_history_survives_mixed_legacy_and_fresh_column_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-production.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            """
            CREATE TABLE production_tasks (
              source_app_token TEXT NOT NULL,
              source_table_id TEXT NOT NULL,
              source_record_id TEXT NOT NULL,
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
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (
                source_app_token, source_table_id, source_record_id
              )
            )
            """
        )

    store = await ProductionTaskStore.open(path)
    try:
        first = await store.claim(
            _location(),
            _task(),
            run_id="run-first",
            thread_id="thread-first",
            owner_user_id="user-a",
        )
        from feishu_generation_agent.domain.bitable import TableTaskStatus

        await store.release(
            first.run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )
        await store.claim(
            _location(),
            _task(),
            run_id="run-second",
            thread_id="thread-second",
            owner_user_id="user-b",
        )

        recent = await store.list_recent(
            "appProd", "tblProd", owner_user_id="user-a"
        )
        assert [(item.run_id, item.owner_user_id) for item in recent] == [
            ("run-first", "user-a")
        ]
    finally:
        await store.close()


async def test_reclaiming_an_archived_record_makes_it_visible_again(
    tmp_path: Path,
) -> None:
    """归档过的记录被重新领取后必须回到列表。

    2026-09-17 生产：超级大床的当前行被 archive 标成 `deleted=1`，而 claim 的
    UPDATE 只设 `active=1`、**没清 `deleted`** —— 之后每次重跑都复用同一行，新尝试
    全部继承 `deleted=1`，于是用户「一直重跑但看不到记录」（列表里只剩历史里的旧
    尝试，当前那次永远不出现）。
    """
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="user-a",
        )
        await store.release(
            binding.run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )
        assert await store.archive("run-1") == 1
        assert await store.list_recent("appProd", "tblProd") == []

        # 重新领取（＝开始分析 / 重跑）后再结束：这一行必须重新可见。
        await store.claim(
            _location(),
            _task(),
            run_id="run-2",
            thread_id="thread-2",
            owner_user_id="user-a",
        )
        await store.release(
            "run-2",
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )

        recent = await store.list_recent("appProd", "tblProd")
        assert [item.run_id for item in recent] == ["run-2"], recent
    finally:
        await store.close()


async def test_archive_and_restore_move_recent_run_between_trash(
    tmp_path: Path,
) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="user-a",
        )
        await store.release(
            binding.run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )

        assert [
            item.run_id for item in await store.list_recent("appProd", "tblProd")
        ] == ["run-1"]

        assert await store.archive("run-1") == 1
        assert await store.list_recent("appProd", "tblProd") == []
        assert [
            item.run_id
            for item in await store.list_archived("appProd", "tblProd")
        ] == ["run-1"]

        assert await store.restore("run-1") == 1
        assert [
            item.run_id for item in await store.list_recent("appProd", "tblProd")
        ] == ["run-1"]
        assert await store.list_archived("appProd", "tblProd") == []
    finally:
        await store.close()


async def test_recent_keeps_failed_attempts_without_breaking_other_hidden_semantics(
    tmp_path: Path,
) -> None:
    """失败的尝试要留在「最近」里；`hidden` 的其它语义一行都不许放开。

    用户 2026-09-24：「『成品与结果』区少了某些记录」—— 记录 recvvWdIXpk2uE 在库里
    有 21 条历史 + 1 条当前行，接口只给 18 条，差的 4 条**全部**是
    `status=失败 AND hidden=1`（实锤：hidden=1 共 89 行，全是失败）。

    `hidden` 只有一个写入点：`claim()` 把上一条尝试搬进历史时按
    `CASE WHEN status = '失败'` 打的标记。所以这里放行的条件是
    **「hidden=0 或 status=失败」**，而不是把 `hidden` 过滤整个删掉：
    「非失败原因被隐藏」的行、以及用户主动删除（deleted=1）的行，仍然不出现。
    """
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    database = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(database)
    try:
        # run-fail 失败 → run-ok 接手：claim 把 run-fail 搬进历史并打 hidden=1。
        await store.claim(
            _location(),
            _task(),
            run_id="run-fail",
            thread_id="thread-fail",
            owner_user_id="user-a",
        )
        await store.release(
            "run-fail", status=TableTaskStatus.FAILED, owner_user_id="user-a"
        )
        await store.claim(
            _location(),
            _task(),
            run_id="run-ok",
            thread_id="thread-ok",
            owner_user_id="user-a",
        )
        await store.release(
            "run-ok", status=TableTaskStatus.COMPLETED, owner_user_id="user-a"
        )
        # 再来一次，把**成功**的 run-ok 也搬进历史（它拿到 hidden=0）。
        await store.claim(
            _location(),
            _task(),
            run_id="run-next",
            thread_id="thread-next",
            owner_user_id="user-a",
        )
        await store.release(
            "run-next", status=TableTaskStatus.COMPLETED, owner_user_id="user-a"
        )

        connection = await aiosqlite.connect(str(database))
        try:
            cursor = await connection.execute(
                "SELECT run_id, status, hidden FROM production_task_history"
            )
            history = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
            await cursor.close()
        finally:
            await connection.close()
        # 失败的那条确实是 hidden=1（就是被吞掉的那一类），成功的那条是 hidden=0。
        assert history["run-fail"] == (TableTaskStatus.FAILED.value, 1)
        assert history["run-ok"] == (TableTaskStatus.COMPLETED.value, 0)

        assert {
            item.run_id
            for item in await store.list_recent("appProd", "tblProd")
        } == {"run-fail", "run-ok", "run-next"}

        # 把**成功**的历史行也标成 hidden=1（模拟「非失败原因被隐藏」）：
        # 它必须继续隐藏 —— 放开的只是「因失败而被隐藏」这一种。
        connection = await aiosqlite.connect(str(database))
        try:
            await connection.execute(
                "UPDATE production_task_history SET hidden = 1 WHERE run_id = ?",
                ("run-ok",),
            )
            await connection.commit()
        finally:
            await connection.close()
        assert {
            item.run_id
            for item in await store.list_recent("appProd", "tblProd")
        } == {"run-fail", "run-next"}

        # 用户主动删除（deleted=1）的失败尝试进回收站，不进「最近」。
        assert await store.archive("run-fail") == 1
        assert [
            item.run_id for item in await store.list_recent("appProd", "tblProd")
        ] == ["run-next"]
        assert [
            item.run_id
            for item in await store.list_archived("appProd", "tblProd")
        ] == ["run-fail"]
    finally:
        await store.close()


async def test_list_record_runs_returns_every_attempt_of_that_record_only(
    tmp_path: Path,
) -> None:
    """按 record_id 取某条记录的**全部**历次尝试：不受「最近」窗口限制。

    用户 2026-09-24：「窗口外老记录的失败尝试看不到」——实测 89 条失败尝试里
    45 条属于「最近 10 条任务」窗口之外的老记录，它们永远进不了 recent-runs。
    这里钉住按记录取历史的语义：一条都不截、不掺别的记录、hidden 的失败也照给，
    只有用户主动删除（deleted=1）的不给。
    """
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        # 老记录 rec-old：第一次就失败，之后又试了 4 次（每次都被搬进历史）。
        old = _task().model_copy(update={"record_id": "rec-old"})
        for attempt in range(5):
            run_id = f"run-old-{attempt}"
            binding = await store.claim(
                _location(),
                old,
                run_id=run_id,
                thread_id=f"thread-old-{attempt}",
                owner_user_id="user-a",
            )
            # 第一次失败 → 下次 claim 会把它搬进历史并打 hidden=1。
            status = (
                TableTaskStatus.FAILED
                if attempt == 0
                else TableTaskStatus.COMPLETED
            )
            await store.release(
                run_id, status=status, owner_user_id="user-a"
            )
        # 另一条记录：不能被 rec-old 的历史带出来。
        other = _task().model_copy(update={"record_id": "rec-other"})
        await store.claim(
            _location(),
            other,
            run_id="run-other",
            thread_id="thread-other",
            owner_user_id="user-a",
        )
        await store.release(
            "run-other",
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )

        # 窗口只留 2 条：rec-old 的历史必然被截断（这就是老记录的处境）。
        assert len(
            await store.list_recent("appProd", "tblProd", limit=2)
        ) == 2

        runs = await store.list_record_runs("appProd", "tblProd", "rec-old")
        assert {item.run_id for item in runs} == {
            f"run-old-{attempt}" for attempt in range(5)
        }
        # 失败的尝试确实被打了 hidden=1（就是 recent-runs 里被吞掉的那一类），
        # 但按记录取历史时照样给出来，且状态如实。
        connection = await aiosqlite.connect(str(tmp_path / "production.sqlite3"))
        try:
            cursor = await connection.execute(
                "SELECT hidden FROM production_task_history WHERE run_id = ?",
                ("run-old-0",),
            )
            hidden = (await cursor.fetchone())[0]
            await cursor.close()
        finally:
            await connection.close()
        assert hidden == 1
        failed = next(item for item in runs if item.run_id == "run-old-0")
        assert failed.status is TableTaskStatus.FAILED

        # 用户主动删除的那次不给。
        assert await store.archive("run-old-0") == 1
        assert {
            item.run_id
            for item in await store.list_record_runs("appProd", "tblProd", "rec-old")
        } == {f"run-old-{attempt}" for attempt in range(1, 5)}
    finally:
        await store.close()


async def test_binding_exposes_whether_the_row_is_still_active(tmp_path: Path) -> None:
    """`active` 要能读出来：读接口靠它判断「这次同步有没有真要落库的变化」。

    已经是同一个终态、又已经放掉（active=0）的行，再 UPDATE 一次只会刷新
    `updated_at` —— 用户点开一条失败详情就把它顶到「最近任务」最前面。
    """
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="user-a",
        )
        assert binding.active is True
        released = await store.release(
            "run-1", status=TableTaskStatus.FAILED, owner_user_id="user-a"
        )
        assert released.active is False
        assert (await store.get_by_run("run-1")).active is False
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# 按**整条记录**归档 / 恢复（「最近任务只保留 10 条，多余的进回收站」的底座）
# ---------------------------------------------------------------------------


async def _snapshot_rows(database: Path) -> dict[tuple[str, str], dict]:
    """两张表全字段快照，用来证明归档只改了 `deleted` / `archived_auto`。"""
    connection = await aiosqlite.connect(str(database))
    connection.row_factory = aiosqlite.Row
    try:
        rows: dict[tuple[str, str], dict] = {}
        for table in ("production_tasks", "production_task_history"):
            cursor = await connection.execute(f"SELECT * FROM {table}")
            for row in await cursor.fetchall():
                rows[(table, row["run_id"])] = dict(row)
            await cursor.close()
        return rows
    finally:
        await connection.close()


async def _two_attempts(store: ProductionTaskStore, *, owner: str = "user-a") -> None:
    """同一条记录跑两次：run-1 失败进历史，run-2 成为当前行。"""
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    await store.claim(
        _location(), _task(), run_id="run-1", thread_id="thread-1",
        owner_user_id=owner,
    )
    await store.release(
        "run-1", status=TableTaskStatus.FAILED, owner_user_id=owner
    )
    await store.claim(
        _location(), _task(), run_id="run-2", thread_id="thread-2",
        owner_user_id=owner,
    )
    await store.release(
        "run-2", status=TableTaskStatus.COMPLETED, owner_user_id=owner
    )


async def test_archive_record_moves_every_attempt_and_only_flips_deleted(
    tmp_path: Path,
) -> None:
    """整条记录归档 = 它的**全部**尝试一起进回收站，且一个字段都不许改。

    只归档「当前那一次尝试」会让记录因为其它尝试继续可见（「删了还在」），所以
    归档粒度是整条记录；同时按用户要求只允许 `deleted` 从 0 变 1 —— 状态、提示词
    快照、时间戳全部原样，回收站才能如实还原它。
    """
    database = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(database)
    try:
        await _two_attempts(store)
        before = await _snapshot_rows(database)

        updated = await store.archive_record(
            "appProd", "tblProd", "recProd", owner_user_id="user-a", auto=True
        )
        assert updated == 2

        after = await _snapshot_rows(database)
        assert set(after) == set(before), "归档不许物理删除任何一行"
        for key, row in before.items():
            for column, value in row.items():
                if column in {"deleted", "archived_auto"}:
                    continue
                assert after[key][column] == value, (key, column)

        assert await store.list_recent(
            "appProd", "tblProd", owner_user_id="user-a"
        ) == []
        archived = await store.list_archived(
            "appProd", "tblProd", owner_user_id="user-a"
        )
        assert {item.run_id for item in archived} == {"run-1", "run-2"}
        assert all(item.deleted and item.auto_archived for item in archived)
    finally:
        await store.close()


async def test_archive_record_keeps_the_active_row_out_of_trash(
    tmp_path: Path,
) -> None:
    """正在跑的那一行（`active = 1`）绝不归档 —— 宁可列表多于 10 条。"""
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    database = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(database)
    try:
        await store.claim(
            _location(), _task(), run_id="run-1", thread_id="thread-1",
            owner_user_id="user-a",
        )
        await store.release(
            "run-1", status=TableTaskStatus.COMPLETED, owner_user_id="user-a"
        )
        # run-2 正在跑：这一行还是 active=1。
        await store.claim(
            _location(), _task(), run_id="run-2", thread_id="thread-2",
            owner_user_id="user-a",
        )

        assert await store.archive_record(
            "appProd", "tblProd", "recProd", owner_user_id="user-a"
        ) == 1
        assert {
            item.run_id
            for item in await store.list_archived(
                "appProd", "tblProd", owner_user_id="user-a"
            )
        } == {"run-1"}
        active = await store.list_active("appProd", "tblProd", owner_user_id="user-a")
        assert [item.run_id for item in active] == ["run-2"]
        assert active[0].deleted is False
    finally:
        await store.close()


async def test_archive_record_is_scoped_to_the_owner(tmp_path: Path) -> None:
    """同一个 record_id 属于不同 owner 时互不影响（接口都按 owner 隔离）。"""
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        for owner, run_id in (("user-a", "run-a"), ("user-b", "run-b")):
            await store.claim(
                _location(), _task(), run_id=run_id, thread_id=f"thread-{run_id}",
                owner_user_id=owner,
            )
            await store.release(
                run_id, status=TableTaskStatus.COMPLETED, owner_user_id=owner
            )

        assert await store.archive_record(
            "appProd", "tblProd", "recProd", owner_user_id="user-a"
        ) == 1
        assert await store.list_recent(
            "appProd", "tblProd", owner_user_id="user-a"
        ) == []
        assert [
            item.run_id
            for item in await store.list_recent(
                "appProd", "tblProd", owner_user_id="user-b"
            )
        ] == ["run-b"]
    finally:
        await store.close()


async def test_restore_record_bumps_only_the_newest_attempt(tmp_path: Path) -> None:
    """恢复整条记录，并且只把**最新那次尝试**顶到当前时间。

    列表按 `updated_at` 取前 10 条记录：不顶时间戳，一条老记录恢复完仍在窗口外
    （点了「恢复」却什么都没发生）。但整条记录全顶掉会把内部历次尝试的顺序抹平。
    """
    database = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(database)
    try:
        await _two_attempts(store)
        await store.archive_record(
            "appProd", "tblProd", "recProd", owner_user_id="user-a"
        )
        connection = await aiosqlite.connect(str(database))
        try:
            for table in ("production_tasks", "production_task_history"):
                await connection.execute(
                    f"UPDATE {table} SET updated_at = ? WHERE run_id = ?",
                    ("2026-09-01 00:00:01", "run-1"),
                )
                await connection.execute(
                    f"UPDATE {table} SET updated_at = ? WHERE run_id = ?",
                    ("2026-09-01 00:00:02", "run-2"),
                )
            await connection.commit()
        finally:
            await connection.close()

        assert await store.restore_record(
            "appProd", "tblProd", "recProd", owner_user_id="user-a"
        ) == 2

        assert {
            item.run_id
            for item in await store.list_record_runs("appProd", "tblProd", "recProd")
        } == {"run-1", "run-2"}
        assert await store.list_archived("appProd", "tblProd") == []
        rows = await _snapshot_rows(database)
        run_one = next(
            row for (table, run_id), row in rows.items()
            if run_id == "run-1" and table == "production_task_history"
        )
        run_two = next(
            row for (table, run_id), row in rows.items()
            if run_id == "run-2" and table == "production_tasks"
        )
        assert run_one["updated_at"] == "2026-09-01 00:00:01"
        assert run_two["updated_at"] != "2026-09-01 00:00:02"
        assert run_one["deleted"] == 0 and run_two["deleted"] == 0
        assert run_one["archived_auto"] == 0 and run_two["archived_auto"] == 0
    finally:
        await store.close()


async def test_record_ids_for_runs_spans_both_tables(tmp_path: Path) -> None:
    """`runs` 表的 run_id 要能反查回记录，历史尝试（旧 run）也得认。"""
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        await _two_attempts(store)
        assert await store.record_ids_for_runs(["run-1", "run-2"]) == {"recProd"}
        assert await store.record_ids_for_runs([]) == set()
        assert await store.record_ids_for_runs(["run-unknown"]) == set()
    finally:
        await store.close()


async def test_list_archived_records_dedupes_before_truncating(tmp_path: Path) -> None:
    """回收站的去重必须在 LIMIT 截断**之前**，否则多尝试的老记录整条看不见。

    2026-09-24 实测：自动归档后有 327 行归档数据、共 42 条记录；按 run 逐行取前
    200 行只剩 29 条记录 —— 另外 13 条在回收站里根本找不到，和用户最初抱怨的
    「历史记录被吞了、找不到」是同一类 bug。所以 `list_archived_records` 在 SQL 里
    先 `GROUP BY source_record_id` 再 LIMIT。
    """
    from feishu_generation_agent.domain.bitable import TableTaskStatus

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        # 5 条记录 × 每条 3 次尝试 = 15 行归档数据。
        for index in range(5):
            record_id = f"rec-{index}"
            for attempt in range(3):
                run_id = f"run-{index}-{attempt}"
                task = _task().model_copy(update={"record_id": record_id})
                await store.claim(
                    _location(), task, run_id=run_id,
                    thread_id=f"thread-{run_id}", owner_user_id="user-a",
                )
                await store.release(
                    run_id,
                    status=TableTaskStatus.COMPLETED,
                    owner_user_id="user-a",
                )
            assert await store.archive_record(
                "appProd", "tblProd", record_id, owner_user_id="user-a", auto=True
            ) == 3

        # 按 run 取 3 行可能全落在同一条记录上；按记录去重后取 3 行必须是 3 条。
        assert len(await store.list_archived("appProd", "tblProd", limit=3)) == 3
        records = await store.list_archived_records(
            "appProd", "tblProd", limit=3
        )
        assert len(records) == 3
        assert len({item.record_id for item in records}) == 3
        assert all(item.auto_archived and item.deleted for item in records)
        # 不设小窗口时 5 条记录一条不少；代表行是这条记录最新那次尝试。
        all_records = await store.list_archived_records(
            "appProd", "tblProd", limit=200
        )
        assert len(all_records) == 5
        for item in all_records:
            index = item.record_id.split("-")[1]
            assert item.run_id == f"run-{index}-2"
    finally:
        await store.close()

