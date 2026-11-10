"""「直接分析需求文档」（左上角「生成计划」）的任务也要出现在「任务记录」里。

用户 2026-10-30：「我飞书左上角的生成计划的任务怎么在下面历史里找不到」。

根因：那个列表读的是 `/api/bitable/recent-runs` —— 只列**多维表格绑定**的运行，
而「生成计划」走 `POST /api/runs`、不建绑定，于是这类运行在界面上完全不可见
（全库 1302 个运行里只有 6 个是这种，其中一条就卡在「待审批」等人操作）。

这一版把它们补进列表（前端用 `run:<run_id>` 各自成行、打「直接分析」标签），
并补上同口径的重跑入口 `POST /api/runs/{id}/rerun`。
"""

from types import SimpleNamespace

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.graph.runtime import RunConflict
from feishu_generation_agent.web.app import create_app


class _Repository:
    def __init__(self, runs: list[dict]) -> None:
        self.runs = runs
        self.cloned: list[dict] = []
        self.archive_calls: list[dict] = []
        for run in self.runs:
            run.setdefault("archived_at", None)
            run.setdefault("archived_auto", 0)

    async def list_runs(
        self,
        *,
        owner_user_id: str | None = None,
        exclude_archived: bool = False,
        statuses: set[str] | None = None,
    ):
        rows = [
            dict(run)
            for run in self.runs
            if owner_user_id is None or run["owner_user_id"] == owner_user_id
        ]
        if exclude_archived:
            rows = [row for row in rows if not row.get("archived_at")]
        return rows

    async def list_archived_runs(self, *, owner_user_id: str | None = None):
        return [
            dict(run)
            for run in self.runs
            if run.get("archived_at")
            and (owner_user_id is None or run["owner_user_id"] == owner_user_id)
        ]

    async def archive_runs(self, run_ids: list[str], *, auto: bool = False) -> int:
        wanted = {str(run_id) for run_id in run_ids}
        updated = 0
        for run in self.runs:
            if run["run_id"] in wanted and not run.get("archived_at"):
                run["archived_at"] = "2026-10-30T09:00:00+00:00"
                run["archived_auto"] = 1 if auto else 0
                updated += 1
        self.archive_calls.append({"run_ids": sorted(wanted), "auto": auto})
        return updated

    async def restore_runs(self, run_ids: list[str]) -> int:
        wanted = {str(run_id) for run_id in run_ids}
        updated = 0
        for run in self.runs:
            if run["run_id"] in wanted and run.get("archived_at"):
                run["archived_at"] = None
                run["archived_auto"] = 0
                updated += 1
        return updated

    async def get_run(self, run_id: str, *, owner_user_id: str | None = None):
        for run in self.runs:
            if run["run_id"] != run_id:
                continue
            if owner_user_id is not None and run["owner_user_id"] != owner_user_id:
                return None
            return dict(run)
        return None

    async def count_artifacts_by_run(self, run_ids: list[str]) -> dict[str, int]:
        return {run_id: 2 for run_id in run_ids if run_id == "run-direct"}


class _Runtime:
    """只提供这几个接口用到的那几位：list_runs / get_run / 产物计数 / 计划摘要。"""

    def __init__(self, tmp_path, runs: list[dict]) -> None:
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            outputs_dir=tmp_path / "outputs",
            business_db_path=tmp_path / "business.sqlite3",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        )
        self.repository = _Repository(runs)
        self.clone_calls: list[dict] = []

    async def plan_portal_view(self, thread_id: str) -> dict:
        titles = {
            "thread-direct": "【剧】测试需求-AI视频美术需求",
            "thread-direct-done": "已完成的直连需求",
        }
        modes = {"thread-direct": "video", "thread-direct-done": "image"}
        return {
            "prompt": "提示词正文",
            "has_reference_video": False,
            "title": titles.get(thread_id, ""),
            "planning_mode": modes.get(thread_id, ""),
        }

    async def clone_run_for_approval(self, source_run_id, request, **kwargs):
        self.clone_calls.append(
            {"source_run_id": source_run_id, "request": request, **kwargs}
        )
        # 克隆出来的新版本要真的进仓储 —— 自动回收按"同一份文档 = 一条任务"分组，
        # 新版本不存在的话分组数不涨，测不出窗口收敛。
        source = await self.repository.get_run(source_run_id)
        if source is not None:
            self.repository.runs.append(
                {
                    **source,
                    "run_id": kwargs["run_id"],
                    "thread_id": kwargs["thread_id"],
                    "status": "waiting_approval",
                    "created_at": "2026-07-22T16:00:00+00:00",
                    "updated_at": "2026-07-22T16:05:00+00:00",
                    "archived_at": None,
                    "archived_auto": 0,
                }
            )
        return kwargs["run_id"]

    async def close(self) -> None:
        pass


class _ProductionService:
    """多维表格那一侧：只有一条已完成的绑定运行。"""

    def __init__(self) -> None:
        self.known_run_ids_calls: list[str] = []
        self.archived: list[str] = []
        self.restored: list[str] = []

    async def close(self) -> None:
        pass

    async def is_production_run(self, run_id: str, **kwargs) -> bool:
        """真实服务有这条能力（`record-runs` / 统一归档接口都靠它分通道）。"""
        return run_id == "run-bound"

    async def archive_run(self, run_id: str, **kwargs) -> None:
        self.archived.append(run_id)

    async def restore_run(self, run_id: str, **kwargs) -> None:
        self.restored.append(run_id)

    async def known_run_ids(self, *, owner_user_id: str = "prime-local"):
        self.known_run_ids_calls.append(owner_user_id)
        return {"run-bound"}

    async def recent_runs(self, *, owner_user_id: str = "prime-local"):
        return [
            SimpleNamespace(
                run_id="run-bound",
                record_id="rec-bound",
                display_text="多维表格里的需求",
                status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
                last_error=None,
                runtime_status="succeeded",
            )
        ]

    async def result_table_url(self, run_id: str, **kwargs) -> str | None:
        return None


def _runs() -> list[dict]:
    return [
        {
            "run_id": "run-bound",
            "thread_id": "thread-bound",
            "owner_user_id": "prime-local",
            "status": "succeeded",
            "source_url": "https://tenant.feishu.cn/wiki/bound",
            "created_at": "2026-07-22T12:00:00+00:00",
            "updated_at": "2026-07-22T12:00:00+00:00",
        },
        {
            "run_id": "run-direct",
            "thread_id": "thread-direct",
            "owner_user_id": "prime-local",
            "status": "waiting_approval",
            "source_url": "https://tenant.feishu.cn/wiki/direct",
            "created_at": "2026-07-22T13:00:00+00:00",
            "updated_at": "2026-07-22T13:05:00+00:00",
        },
        {
            "run_id": "run-direct-done",
            "thread_id": "thread-direct-done",
            "owner_user_id": "prime-local",
            "status": "waiting_review",
            "source_url": "https://tenant.feishu.cn/wiki/direct-done",
            "created_at": "2026-07-22T11:00:00+00:00",
            "updated_at": "2026-07-22T11:10:00+00:00",
        },
        {
            "run_id": "run-other-owner",
            "thread_id": "thread-other",
            "owner_user_id": "someone-else",
            "status": "succeeded",
            "source_url": "https://tenant.feishu.cn/wiki/other",
            "created_at": "2026-07-22T14:00:00+00:00",
            "updated_at": "2026-07-22T14:00:00+00:00",
        },
    ]


async def test_recent_runs_include_direct_document_runs(tmp_path) -> None:
    """没有多维表格绑定的运行要出现在列表里，且不重复、不带结果表链接。"""
    runtime = _Runtime(tmp_path, _runs())
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/bitable/recent-runs")

    assert response.status_code == 200
    items = {item["run_id"]: item for item in response.json()}
    # 绑定那条照旧在，且没有被直连那份覆盖
    assert items["run-bound"]["record_id"] == "rec-bound"
    assert "mode" not in items["run-bound"]
    # 直连那条补进来了：分组合成键 `direct:<文档token>`、带标签用的 mode
    direct = items["run-direct"]
    assert direct["record_id"] == "direct:direct"
    assert direct["mode"] == "direct"
    assert direct["display_text"] == "【剧】测试需求-AI视频美术需求"
    # 状态必须是前端认的那套中文词汇（与多维表格任务同一套文案）
    assert direct["status"] == TableTaskStatus.WAITING_APPROVAL.value
    assert direct["runtime_status"] == "waiting_approval"
    assert direct["artifact_count"] == 2
    assert direct["result_table_url"] is None
    # 「待审批」不能重跑（还没生成过），「待确认成片」可以
    assert direct["rerunnable"] is False
    assert items["run-direct-done"]["status"] == TableTaskStatus.REVIEWING.value
    assert items["run-direct-done"]["rerunnable"] is True
    # 别人的运行一条都不许露面
    assert "run-other-owner" not in items
    assert production.known_run_ids_calls == ["prime-local"]


async def test_recent_runs_group_direct_versions_by_document(tmp_path) -> None:
    """同一份文档的历次重跑必须叠成**同一条**任务记录（与多维表格任务同逻辑）。

    用户 2026-10-30：「我重跑任务应该和其它正常任务一样的逻辑，记录都和到一起的」
    —— 前端就是按 `record_id` 分组的（`BitableState.latestRunsByRecord`），
    所以直连运行必须给出一个稳定的分组键：`direct:<文档token>`。
    """
    runs = _runs()
    runs.append({
        "run_id": "run-direct-retry",
        "thread_id": "thread-direct",
        "owner_user_id": "prime-local",
        "status": "failed",
        "source_url": "https://tenant.feishu.cn/wiki/direct",     # 同一份文档
        "created_at": "2026-07-22T15:00:00+00:00",
        "updated_at": "2026-07-22T15:05:00+00:00",
    })
    runtime = _Runtime(tmp_path, runs)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/bitable/recent-runs")

    items = {item["run_id"]: item for item in response.json()}
    assert items["run-direct"]["record_id"] == "direct:direct"
    # 重跑出来的那一版必须与源运行同组（前端据此叠成一行）
    assert items["run-direct-retry"]["record_id"] == "direct:direct"
    # 不同文档不能混进同一组
    assert items["run-direct-done"]["record_id"] == "direct:direct-done"
    # 多维表格任务照旧用飞书记录 id
    assert items["run-bound"]["record_id"] == "rec-bound"


async def test_direct_task_delete_archives_whole_task_and_restores(tmp_path) -> None:
    """直连任务：删除 = **整条任务**进回收站、可恢复（与多维表格任务同一套接口）。

    用户 2026-10-30：「直接分析的任务没有删除按钮，并且不受超过十条自动回收的限制，
    两者改成一套接口」。经前直连任务只有硬删（`DELETE /api/runs/{id}`，逐版本、不可
    恢复），删除按钮还被多维表格那套「待审批不许删」的判据挡掉了。
    """
    runs = _runs()
    runs.append({
        "run_id": "run-direct-retry",
        "thread_id": "thread-direct",
        "owner_user_id": "prime-local",
        "status": "waiting_review",
        "source_url": "https://tenant.feishu.cn/wiki/direct",     # 同一份文档
        "created_at": "2026-07-22T15:00:00+00:00",
        "updated_at": "2026-07-22T15:05:00+00:00",
    })
    runtime = _Runtime(tmp_path, runs)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        removed = await client.post("/api/runs/run-direct/archive")
        listed = await client.get("/api/bitable/recent-runs")
        trash = await client.get("/api/bitable/archived-runs")
        restored = await client.post("/api/runs/run-direct/restore")
        listed_again = await client.get("/api/bitable/recent-runs")

    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "run_id": "run-direct", "status": "archived", "mode": "direct",
    }
    # 整条任务一起走：同一份文档的两个版本都不在列表里了
    after = {item["run_id"] for item in listed.json()}
    assert "run-direct" not in after
    assert "run-direct-retry" not in after
    # 别的文档照旧可见（只删了那一条任务）
    assert "run-direct-done" in after
    # 回收站里**同一个列表**能看到它，并标注是直连通道
    entries = trash.json()
    assert [item["run_id"] for item in entries] == ["run-direct-retry"]
    assert entries[0]["record_id"] == "direct:direct"
    assert entries[0]["mode"] == "direct"
    assert entries[0]["auto_archived"] is False

    # 恢复 → 整条任务回来
    assert restored.status_code == 200
    assert restored.json()["mode"] == "direct"
    back = {item["run_id"] for item in listed_again.json()}
    assert {"run-direct", "run-direct-retry"} <= back


async def test_shared_task_window_counts_both_kinds_together(tmp_path) -> None:
    """**两类任务共用 10 条额度**（不是各 10 条）—— 用户 2026-10-30 定稿口径。

    「两者应该共用十条的次数限制，或者说以后所有任务都共用十条次数限制」。
    这条测试把多维表格那条设成最老的：它必须被**直连任务挤出去**（进回收站），
    这样才证明额度是共用的，而不是各算各的（旧实现：多维表格 10 + 直连 10 = 20 条）。

    触发点在写路径（重跑）：读接口绝不写库。刚重跑那条必须被 pin 住。
    """
    runs = [_runs()[0]]        # 只留 run-bound（生产侧），直连任务按下表自造
    # 11 条各自独立的直连任务（11 份不同文档 → 11 条任务），时间严格递增
    for index in range(11):
        stamp = f"2026-07-22T{10 + index:02d}:00:00+00:00"
        runs.append({
            "run_id": f"run-doc{index}",
            "thread_id": f"thread-doc{index}",
            "owner_user_id": "prime-local",
            "status": "waiting_review",
            "source_url": f"https://tenant.feishu.cn/wiki/doc{index}",
            "created_at": stamp,
            "updated_at": stamp,
        })
    runtime = _Runtime(tmp_path, runs)

    class _Service(_ProductionService):
        async def recent_runs(self, *, owner_user_id: str = "prime-local"):
            """多维表格那条设成**最老**的（09:00）→ 它会被挤进回收站。"""
            return [
                SimpleNamespace(
                    run_id="run-bound",
                    record_id="rec-bound",
                    display_text="多维表格里的需求",
                    status=TableTaskStatus.COMPLETED,
                    updated_at="2026-07-22T09:00:00+00:00",
                    last_error=None,
                    runtime_status="succeeded",
                )
            ]

    production = _Service()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        rerun = await client.post("/api/runs/run-doc10/rerun", json={})
        listed = await client.get("/api/bitable/recent-runs")
        trash = await client.get("/api/bitable/archived-runs")

    assert rerun.status_code == 202, rerun.text
    visible = {item["record_id"] for item in listed.json()}
    assert "direct:doc10" in visible          # 刚重跑那条被 pin 住
    # 12 条任务共用 10 条额度 → 溢出 2 条被回收：
    #   * 直连最老的 run-doc0（10:00）→ 运行表归档
    #   * **多维表格那条（09:00）被直连任务挤出去** → 走绑定表归档
    # 后者正是"额度共用"的证据：旧的按通道各算 10 条时，多维表格那条永远不会被挤掉。
    direct_in_trash = sorted(
        item["record_id"] for item in trash.json() if item["auto_archived"]
    )
    assert direct_in_trash == ["direct:doc0"]
    assert production.archived == ["run-bound"]
    # 直连侧只剩 10 条（11 条里被回收 1 条）；多维表格那条由替身自己决定是否还返回，
    # 所以这里只数直连那一半。
    direct_visible = {
        item["record_id"]
        for item in listed.json()
        if item.get("mode") == "direct"
    }
    assert len(direct_visible) == 10


async def test_direct_overflow_auto_archives_oldest_task(tmp_path) -> None:
    """直连任务自己超窗口时，最老的整条进回收站（可恢复，带 auto 标记）。"""
    runs = [_runs()[0]]        # 只留 run-bound（生产侧），直连任务按下表自造
    # 11 条各自独立的直连任务（11 份不同文档 → 11 条任务），时间严格递增
    for index in range(11):
        stamp = f"2026-07-22T{10 + index:02d}:00:00+00:00"
        runs.append({
            "run_id": f"run-doc{index}",
            "thread_id": f"thread-doc{index}",
            "owner_user_id": "prime-local",
            "status": "waiting_review",
            "source_url": f"https://tenant.feishu.cn/wiki/doc{index}",
            "created_at": stamp,
            "updated_at": stamp,
        })
    runtime = _Runtime(tmp_path, runs)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # 重跑最新那条（写路径）→ 触发窗口收敛
        rerun = await client.post("/api/runs/run-doc10/rerun", json={})
        listed = await client.get("/api/bitable/recent-runs")
        trash = await client.get("/api/bitable/archived-runs")

    assert rerun.status_code == 202, rerun.text
    direct = {
        item["record_id"]
        for item in listed.json()
        if item.get("mode") == "direct"
    }
    # 11 条直连任务 + 1 条多维表格 = 12 条，共用 10 条额度 → 直连只留 9 条
    assert len(direct) == 9
    # 12 条任务共用 10 条额度 → 回收**最老的两条**：run-doc0（10:00）与 run-doc1（11:00）
    #（这条用例里的多维表格任务是 12:00，比它俩新，所以留下来了）
    auto = sorted(
        item["record_id"] for item in trash.json() if item["auto_archived"]
    )
    assert auto == ["direct:doc0", "direct:doc1"]
    # 刚重跑出来的那条必须还在（pin 住，不能被自己的那次归档吃掉）
    assert "direct:doc10" in direct


async def test_unified_archive_endpoint_dispatches_by_kind(tmp_path) -> None:
    """同一对口子按类型分发：生产运行走多维表格归档，直连运行走运行时归档。

    两边的**接口**现在是一套（`/api/runs/{id}/archive|restore`），旧的多维表格口子
    保留为等价别名，外部脚本不会因为这次统一而失效。
    """
    runtime = _Runtime(tmp_path, _runs())

    class _Service(_ProductionService):
        async def is_production_run(self, run_id: str, **kwargs) -> bool:
            return run_id == "run-bound"

    production = _Service()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        bitable = await client.post("/api/runs/run-bound/archive")
        legacy = await client.post("/api/bitable/runs/run-bound/archive")
        bitable_restore = await client.post("/api/runs/run-bound/restore")
        direct = await client.post("/api/runs/run-direct/archive")

    assert bitable.json()["mode"] == "bitable"
    assert legacy.json()["mode"] == "bitable"
    # 生产运行不许走运行时归档（那会让绑定与运行状态脱节）
    assert production.archived == ["run-bound", "run-bound"]
    assert production.restored == ["run-bound"]
    assert direct.json()["mode"] == "direct"
    assert runtime.repository.archive_calls == [
        {"run_ids": ["run-direct"], "auto": False}
    ]


async def test_record_runs_returns_all_direct_versions_of_a_document(tmp_path) -> None:
    """按记录取历史：直连任务要能拿到**同一份文档的全部历次尝试**（成片滑条用）。"""
    runs = _runs()
    runs.append({
        "run_id": "run-direct-retry",
        "thread_id": "thread-direct",
        "owner_user_id": "prime-local",
        "status": "succeeded",
        "source_url": "https://tenant.feishu.cn/wiki/direct",
        "created_at": "2026-07-22T15:00:00+00:00",
        "updated_at": "2026-07-22T15:05:00+00:00",
    })
    runtime = _Runtime(tmp_path, runs)

    class _Service(_ProductionService):
        async def is_production_run(self, run_id: str, **kwargs) -> bool:
            return run_id == "run-bound"

    app = create_app(runtime=runtime, bitable_service=_Service())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        by_run = await client.get(
            "/api/bitable/record-runs", params={"run_id": "run-direct"}
        )
        by_record = await client.get(
            "/api/bitable/record-runs", params={"record_id": "direct:direct"}
        )

    assert by_run.status_code == 200
    assert by_run.json()["record_id"] == "direct:direct"
    assert [run["run_id"] for run in by_run.json()["runs"]] == [
        "run-direct-retry", "run-direct",     # 按 updated_at 倒序
    ]
    # 用合成 record_id 直接查也要能查到（前端缓存用的是它）
    assert by_record.status_code == 200
    assert [run["run_id"] for run in by_record.json()["runs"]] == [
        "run-direct-retry", "run-direct",
    ]


async def test_bitable_rerun_endpoint_falls_back_for_direct_runs(tmp_path) -> None:
    """前端万一判错通道，直连任务从 `/api/bitable/.../rerun` 进来也要能重跑。

    2026-10-30 实测：同一个直连任务，用户点「重新运行」时对时错 —— 「往次成片」那个
    按钮漏传通道，界面就把直连运行当成多维表格运行，报「多维表格运行不存在」。
    后端这条兜底保证：只要有绑定关系判否，就按直连那套口径重跑。
    """
    runtime = _Runtime(tmp_path, _runs())

    class _Service(_ProductionService):
        async def is_production_run(self, run_id: str, **kwargs) -> bool:
            return run_id == "run-bound"

        async def rerun(self, run_id: str, **kwargs):     # 不该被调到
            raise AssertionError("直连任务不该走多维表格重跑")

    app = create_app(runtime=runtime, bitable_service=_Service())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/bitable/runs/run-direct-done/rerun",
            json={"feedback": "结尾台词改掉"},
        )

    assert response.status_code == 202, response.text
    assert runtime.clone_calls, "应当走 clone_run_for_approval（直连重跑口径）"
    assert runtime.clone_calls[0]["source_run_id"] == "run-direct-done"
    assert runtime.clone_calls[0]["feedback"] == "结尾台词改掉"


async def test_bitable_rerun_fallback_keeps_the_direct_error(tmp_path) -> None:
    """转调直连重跑后抛出的 409/404 必须原样透出，不能被压成 502。

    2026-10-30 实测：多维表格入口转调直连重跑后，那边抛的 409「只有已经结束…的
    任务可以重跑」被 `except Exception` 兜住压成了 502「读取多维表格失败」——
    用户看到的原因跟真实原因完全无关。
    """
    runtime = _Runtime(tmp_path, _runs())

    class _Service(_ProductionService):
        async def is_production_run(self, run_id: str, **kwargs) -> bool:
            return False          # 全部当直连运行

    app = create_app(runtime=runtime, bitable_service=_Service())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # run-direct 还是 waiting_approval（没生成过）→ 直连口径应当 409
        response = await client.post("/api/bitable/runs/run-direct/rerun")

    assert response.status_code == 409, response.text
    assert "重跑" in response.json()["detail"]
    assert runtime.clone_calls == []


async def test_record_runs_keeps_the_production_path_for_bitable_runs(tmp_path) -> None:
    """多维表格运行仍走原来的 `production_service.record_runs`（不许被新分支抢走）。"""
    runtime = _Runtime(tmp_path, _runs())

    class _Service(_ProductionService):
        def __init__(self) -> None:
            super().__init__()
            self.record_runs_calls: list[str] = []

        async def is_production_run(self, run_id: str, **kwargs) -> bool:
            return True

        async def record_runs(self, *, record_id=None, run_id=None, owner_user_id="prime-local"):
            self.record_runs_calls.append(str(run_id))
            return "rec-bound", []

    service = _Service()
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/record-runs", params={"run_id": "run-bound"}
        )

    assert response.status_code == 200
    assert response.json() == {"record_id": "rec-bound", "runs": []}
    assert service.record_runs_calls == ["run-bound"]


async def test_recent_runs_survive_a_service_without_known_run_ids(tmp_path) -> None:
    """取不到绑定集合（MVP 模式 / 旧服务）时不许把整个列表带崩，也不许瞎补。"""
    runtime = _Runtime(tmp_path, _runs())

    class _Legacy:
        async def close(self) -> None:
            pass

        async def recent_runs(self, *, owner_user_id: str = "prime-local"):
            return []

    app = create_app(runtime=runtime, bitable_service=_Legacy())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/bitable/recent-runs")

    assert response.status_code == 200
    # 没有 known_run_ids 就不知道哪些是「没有绑定的」—— 宁可一条不补，也不瞎标
    assert response.json() == []


async def test_direct_rerun_clones_the_approved_plan_to_approval(tmp_path) -> None:
    """直连任务的「重新运行」：复用已批准计划 → 停在待审批（与多维表格那条同口径）。"""
    runtime = _Runtime(tmp_path, _runs())
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/runs/run-direct-done/rerun",
            json={"feedback": "扇子要整个泡进水里"},
        )

    assert response.status_code == 202
    new_run_id = response.json()["run_id"]
    assert new_run_id != "run-direct-done"
    assert runtime.clone_calls, "必须走 clone_run_for_approval（复用已批准计划）"
    call = runtime.clone_calls[0]
    assert call["source_run_id"] == "run-direct-done"
    assert call["run_id"] == new_run_id
    assert call["feedback"] == "扇子要整个泡进水里"
    # 产出类型要跟着源运行走：图片需求不能按默认的 video 重做
    assert call["request"].planning_mode == "image"
    assert call["request"].source_url == "https://tenant.feishu.cn/wiki/direct-done"


async def test_direct_rerun_rejects_a_run_that_is_still_going(tmp_path) -> None:
    """还在等人审批、没生成过的运行不给重跑（没有「上一版成片」可复用）。"""
    runtime = _Runtime(tmp_path, _runs())
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/runs/run-direct/rerun")

    assert response.status_code == 409
    assert "重跑" in response.json()["detail"]
    assert runtime.clone_calls == []


async def test_direct_rerun_hides_other_owners_runs(tmp_path) -> None:
    """别人的运行既看不到、也重跑不了（owner 隔离不能被新入口绕过）。"""
    runtime = _Runtime(tmp_path, _runs())
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/runs/run-other-owner/rerun")

    assert response.status_code == 404
    assert runtime.clone_calls == []


def test_direct_status_label_maps_runtime_states_to_frontend_vocabulary() -> None:
    """运行时状态 → 前端那套中文状态：认不出来的一律「处理中」。"""
    from feishu_generation_agent.web.app import _direct_status_label

    assert _direct_status_label("waiting_review") == TableTaskStatus.REVIEWING.value
    assert _direct_status_label("waiting_approval") == TableTaskStatus.WAITING_APPROVAL.value
    assert _direct_status_label("succeeded") == TableTaskStatus.COMPLETED.value
    assert _direct_status_label("cancelled") == TableTaskStatus.FAILED.value
    assert _direct_status_label("planning") == TableTaskStatus.PROCESSING.value
    assert _direct_status_label("") == TableTaskStatus.PROCESSING.value
