"""运行级归档（回收站）：`runs.archived_at` / `archived_auto` 的迁移与读写。

用户 2026-10-30：「直接分析的任务没有删除按钮，并且，不受超过十条自动回收的限制，
两者改成一套接口」。

多维表格那边的「删除 = 整条记录进回收站」「超出 10 条自动回收」写在绑定表
（`production_tasks` 的 `deleted` / `archived_auto`）。直连任务（左上角「生成计划」）
没有绑定，只能落在运行表上 —— 这个文件盯的就是那张表的三个口子：
`archive_runs` / `restore_runs` / `list_runs(exclude_archived=True)`，
以及**老库升级**（ALTER TABLE）后行为是否一致。
"""

from __future__ import annotations

import aiosqlite
import pytest

from feishu_generation_agent.storage.repository import Repository


async def _open(tmp_path) -> Repository:
    """真实仓储（`Repository.open` 会建表 + 跑迁移）。"""
    return await Repository.open(tmp_path / "agent.sqlite3")


async def _seed(repo: Repository, run_id: str, *, owner: str = "prime-local") -> None:
    await repo.create_run(
        run_id,
        f"thread-{run_id}",
        f"https://tenant.feishu.cn/wiki/{run_id}",
        owner_user_id=owner,
    )


async def test_archive_moves_run_out_of_the_list_and_into_trash(tmp_path) -> None:
    repo = await _open(tmp_path)
    try:
        await _seed(repo, "run-a")
        await _seed(repo, "run-b")

        assert await repo.archive_runs(["run-a"]) == 1
        # 列表默认照旧能看到（别的链路按状态查询，不该被归档状态影响）
        assert {row["run_id"] for row in await repo.list_runs()} == {
            "run-a", "run-b",
        }
        # 任务记录列表要的正是「跳过已归档」
        visible = await repo.list_runs(exclude_archived=True)
        assert [row["run_id"] for row in visible] == ["run-b"]
        # 回收站里能看到，且能区分自动回收 vs 用户自己删的
        trashed = await repo.list_archived_runs()
        assert [row["run_id"] for row in trashed] == ["run-a"]
        assert trashed[0]["archived_auto"] == 0
        assert trashed[0]["archived_at"]

        # 自动回收打上标记
        assert await repo.archive_runs(["run-b"], auto=True) == 1
        assert [
            row["archived_auto"] for row in await repo.list_archived_runs()
        ] == [0, 1] or [
            row["archived_auto"] for row in await repo.list_archived_runs()
        ] == [1, 0]
    finally:
        await repo.close()


async def test_restore_brings_the_whole_task_back(tmp_path) -> None:
    repo = await _open(tmp_path)
    try:
        await _seed(repo, "run-a")
        await _seed(repo, "run-a2")          # 同一份文档的第二次尝试
        await repo.archive_runs(["run-a", "run-a2"], auto=True)
        assert await repo.list_runs(exclude_archived=True) == []

        assert await repo.restore_runs(["run-a", "run-a2"]) == 2
        assert {
            row["run_id"] for row in await repo.list_runs(exclude_archived=True)
        } == {"run-a", "run-a2"}
        assert await repo.list_archived_runs() == []
        # 恢复必须把自动标记也清掉，否则回收站文案会继续写「自动归档」
        assert all(
            row["archived_auto"] == 0 for row in await repo.list_runs()
        )
    finally:
        await repo.close()


async def test_archive_is_idempotent_and_owner_scoped(tmp_path) -> None:
    repo = await _open(tmp_path)
    try:
        await _seed(repo, "run-mine")
        await _seed(repo, "run-theirs", owner="someone-else")

        assert await repo.archive_runs(["run-mine"]) == 1
        # 再归档一次不该重复计数（回收站不是"每次点多一条"）
        assert await repo.archive_runs(["run-mine"]) == 0
        # 别人的运行一条都不许出现
        assert [
            row["run_id"] for row in await repo.list_archived_runs(
                owner_user_id="prime-local"
            )
        ] == ["run-mine"]
        theirs = await repo.list_archived_runs(owner_user_id="someone-else")
        assert theirs == []
        assert await repo.restore_runs(["run-theirs"]) == 0
    finally:
        await repo.close()


async def test_legacy_database_gets_the_archive_columns(tmp_path) -> None:
    """老库（没有 archived_at / archived_auto 的 runs 表）必须能就地升级。

    生产这台机器的 `agent.sqlite3` 里已经有 1300+ 个运行，重建表等于把用户的
    任务记录全清空 —— 只能 ALTER TABLE 加列。
    """
    path = tmp_path / "legacy.sqlite3"
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.executescript(
        """
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          thread_id TEXT NOT NULL UNIQUE,
          source_url TEXT NOT NULL,
          owner_user_id TEXT NOT NULL DEFAULT 'prime-local',
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    await connection.execute(
        """
        INSERT INTO runs (run_id, thread_id, source_url, owner_user_id,
                          status, created_at, updated_at)
        VALUES ('run-old', 'thread-old', 'https://x/y', 'prime-local',
                'succeeded', '2026-10-01T00:00:00+00:00',
                '2026-10-01T00:00:00+00:00')
        """
    )
    await connection.commit()
    await connection.close()

    repo = await Repository.open(path)      # open 会跑迁移
    try:
        assert await repo.list_archived_runs() == []
        assert await repo.archive_runs(["run-old"], auto=True) == 1
        trashed = await repo.list_archived_runs()
        assert [row["run_id"] for row in trashed] == ["run-old"]
        assert trashed[0]["archived_auto"] == 1
        assert await repo.list_runs(exclude_archived=True) == []
        # 老数据本身没丢
        assert (await repo.get_run("run-old"))["status"] == "succeeded"
    finally:
        await repo.close()


@pytest.mark.parametrize("run_ids", [[], [""], ["   "]])
async def test_empty_archive_calls_are_noops(tmp_path, run_ids) -> None:
    repo = await _open(tmp_path)
    try:
        await _seed(repo, "run-a")
        assert await repo.archive_runs(run_ids) == 0
        assert await repo.restore_runs(run_ids) == 0
        assert [
            row["run_id"] for row in await repo.list_runs(exclude_archived=True)
        ] == ["run-a"]
    finally:
        await repo.close()
