import logging
from pathlib import Path
from types import SimpleNamespace

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.document import PlanningPromptSnapshot
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import (
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskAlreadyClaimed
from feishu_generation_agent.web.app import create_app


class _Runtime:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            outputs_dir=tmp_path / "outputs",
            business_db_path=tmp_path / "business.sqlite3",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        )
        self.resume_calls: list[str] = []
        self.start_requests = []
        self.run_views: dict[str, dict] = {}

    async def get_run_view(self, run_id: str) -> dict:
        return dict(
            self.run_views.get(run_id, {"run_id": run_id, "status": "succeeded"})
        )

    async def close(self) -> None:
        pass

    async def resume_run(self, run_id, decision) -> None:
        del decision
        self.resume_calls.append(run_id)

    async def start_run(self, request) -> str:
        self.start_requests.append(request)
        return f"run-{len(self.start_requests)}"


class _ProductionService:
    def __init__(self, *, task_type: str = "动画类") -> None:
        self.rerun_calls: list[str] = []
        self.rerun_selected_calls: list[tuple[str, list[str], str, bool]] = []
        #: ②-b：本次时长（只记显式传过来的非空值）。
        self.rerun_durations: list[int] = []
        self.archive_calls: list[str] = []
        self.restore_calls: list[str] = []
        self.rerun_error: Exception | None = None
        self.scan_error: Exception | None = None
        self.task_type = task_type
        self.scan_categories: list[str] = []
        self.claim_categories: list[tuple[str, str]] = []
        self.owner_calls: list[tuple[str, str]] = []
        self.planning_prompts: list[PlanningPromptSnapshot] = []
        self.run_owners = {
            "run-no-maker": "prime-local",
            "run-old": "prime-local",
        }
        self.tasks_by_category = {
            "animation": [
                ProductionTaskSummary(
                    record_id="rec-no-maker",
                    display_text="需求 A",
                    source_url="https://tenant.feishu.cn/docx/docA",
                    progress="未开始",
                    task_type=self.task_type,
                    snapshot=ProductionSourceSnapshot(
                        requirement_name="需求 A",
                        task_type=self.task_type,
                        requirement_attachment="https://tenant.feishu.cn/docx/docA",
                    ),
                )
            ],
            "portrait": [
                ProductionTaskSummary(
                    record_id="rec-portrait",
                    display_text="真人需求 A",
                    source_url="https://tenant.feishu.cn/docx/docPortrait",
                    progress="未开始",
                    task_type="真人类",
                    snapshot=ProductionSourceSnapshot(
                        requirement_name="真人需求 A",
                        task_type="真人类",
                        requirement_attachment="https://tenant.feishu.cn/docx/docPortrait",
                    ),
                )
            ],
        }

    async def scan(self, category: str = "animation"):
        self.scan_categories.append(category)
        if self.scan_error is not None:
            raise self.scan_error
        return self.tasks_by_category[category]

    async def claim(
        self,
        record_id: str,
        category: str = "animation",
        *,
        owner_user_id: str = "prime-local",
        planning_prompt: PlanningPromptSnapshot | None = None,
    ) -> str:
        self.claim_categories.append((record_id, category))
        self.owner_calls.append(("claim", owner_user_id))
        assert planning_prompt is not None
        self.planning_prompts.append(planning_prompt)
        self.run_owners["run-no-maker"] = owner_user_id
        if category == "portrait":
            assert record_id == "rec-portrait"
            return "run-no-maker"
        assert record_id == "rec-no-maker"
        if self.task_type != "动画类":
            raise RunConflict(f"{self.task_type}任务暂未启用")
        return "run-no-maker"

    async def validate_approval(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)
        assert run_id == "run-no-maker"
        if self.task_type != "动画类":
            raise RunValidationError(f"{self.task_type}任务暂未启用")

    async def active_runs(self, *, owner_user_id: str = "prime-local"):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                run_id=run_id,
                record_id=f"rec-{run_id}",
                display_text=run_id,
                status=TableTaskStatus.PROCESSING,
            )
            for run_id, owner in self.run_owners.items()
            if owner == owner_user_id and run_id != "run-old"
        ]

    async def recent_runs(self, *, owner_user_id: str = "prime-local"):
        from types import SimpleNamespace

        items = [
            SimpleNamespace(
                run_id="run-old",
                # 同一条多维表格记录的历次尝试共用 record_id：前端据此把重跑
                # 叠到同一条任务记录下。
                record_id="rec-old",
                display_text="需求 A",
                status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
            )
        ]
        return items if self.run_owners["run-old"] == owner_user_id else []

    async def archived_runs(self, *, owner_user_id: str = "prime-local"):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                run_id="run-archived",
                display_text="已删需求",
                status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
            )
        ]

    async def archive_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)
        self.archive_calls.append(run_id)

    async def restore_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)
        self.restore_calls.append(run_id)

    async def rerun(
        self,
        run_id: str,
        *,
        owner_user_id: str = "prime-local",
        task_ids: list[str] | None = None,
        feedback: str | None = None,
        #: ②-b（2026-10-10）：本次时长。与真实 `ProductionService.rerun` 的签名保持一致。
        duration: int | None = None,
        auto_approve: bool = False,
    ) -> str:
        self._require_owner(run_id, owner_user_id)
        self.rerun_calls.append(run_id)
        if duration is not None:
            self.rerun_durations.append(duration)
        if self.rerun_error is not None:
            raise self.rerun_error
        if task_ids is not None or feedback is not None or auto_approve or duration is not None:
            self.rerun_selected_calls.append(
                (run_id, list(task_ids or []), feedback or "", auto_approve)
            )
            return "run-selected"
        return "run-new"

    async def result_table_url(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str | None:
        self._require_owner(run_id, owner_user_id)
        assert run_id == "run-old"
        return "https://tenant.feishu.cn/base/result-table"

    async def retry_delivery(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    async def delete_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    async def sync_once(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    def _require_owner(self, run_id: str, owner_user_id: str) -> None:
        if self.run_owners.get(run_id) != owner_user_id:
            raise RunNotFound("多维表格运行不存在")

    async def close(self) -> None:
        pass


class _PromptStore:
    def __init__(self, prompt_text: str, version: int) -> None:
        self.profile = SimpleNamespace(
            prompt_text=prompt_text,
            version=version,
        )
        self.get_calls: list[str] = []

    async def get(self, portal_user_id: str):
        self.get_calls.append(portal_user_id)
        return self.profile


async def test_claim_snapshots_profile_once_and_new_claim_uses_new_version(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    prompt_store = _PromptStore("个人版本 v2", 2)
    app = create_app(
        runtime=runtime,
        bitable_service=production,
        planner_prompt_store=prompt_store,
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"X-Portal-User-Id": "user-a"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim", headers=headers
        )
        prompt_store.profile = SimpleNamespace(
            prompt_text="个人版本 v3",
            version=3,
        )
        second = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim", headers=headers
        )
        production.run_owners["run-old"] = "user-a"
        before_rerun = list(prompt_store.get_calls)
        rerun = await client.post(
            "/api/bitable/runs/run-old/rerun", headers=headers
        )

    assert first.status_code == second.status_code == 202
    assert rerun.status_code == 202
    assert [item.prompt_text for item in production.planning_prompts] == [
        "个人版本 v2",
        "个人版本 v3",
    ]
    assert [item.version for item in production.planning_prompts] == [2, 3]
    assert all(
        item.owner_user_id == "user-a"
        and item.source == "personal"
        for item in production.planning_prompts
    )
    assert prompt_store.get_calls == before_rerun == ["user-a", "user-a"]
    assert "个人版本 v2" not in caplog.text
    assert "个人版本 v3" not in caplog.text
    assert "owner_user_id=user-a" in caplog.text
    assert "source=personal" in caplog.text
    assert "version=2" in caplog.text
    assert production.planning_prompts[0].prompt_sha256 in caplog.text


async def test_local_and_portal_direct_run_creation_have_explicit_snapshots(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    prompt_store = _PromptStore("个人规划提示词", 4)
    app = create_app(runtime=runtime, planner_prompt_store=prompt_store)
    transport = httpx.ASGITransport(app=app)
    payload = {"source_url": "https://tenant.feishu.cn/docx/docA"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        local = await client.post("/api/runs", json=payload)
        portal = await client.post(
            "/api/runs",
            json=payload,
            headers={"X-Portal-User-Id": "user-a"},
        )

    assert local.status_code == portal.status_code == 202
    local_prompt = runtime.start_requests[0].planning_prompt
    portal_prompt = runtime.start_requests[1].planning_prompt
    assert local_prompt is not None
    assert local_prompt.owner_user_id == "prime-local"
    assert local_prompt.source == "prime"
    assert local_prompt.version == 0
    assert local_prompt.prompt_sha256 == (
        # 2026-10-12：视频契约按「原图保真」口径改过（水印不再触发 usable=false），
        # 哈希随之更新 —— 有意改动，见 tests/unit/test_reference_fidelity_rules.py。
        "65fb914f8005792653c2ec688ad0c704dae50769dc90893d265db7fb88ed2f8b"
    )
    assert portal_prompt is not None
    assert portal_prompt.owner_user_id == "user-a"
    assert portal_prompt.source == "personal"
    assert portal_prompt.version == 4
    assert portal_prompt.prompt_text == "个人规划提示词"


async def test_portal_creation_fails_when_prompt_store_is_unavailable(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/runs",
            json={"source_url": "https://tenant.feishu.cn/docx/docA"},
            headers={"X-Portal-User-Id": "user-a"},
        )
        claim = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim",
            headers={"X-Portal-User-Id": "user-a"},
        )

    assert response.status_code == 503
    assert claim.status_code == 503
    assert runtime.start_requests == []
    assert production.planning_prompts == []


async def test_scan_exposes_animation_type_and_allows_approval_without_maker(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get("/api/bitable/tasks")
        claimed = await client.post("/api/bitable/tasks/rec-no-maker/claim")
        approved = await client.post(
            f"/api/runs/{claimed.json()['run_id']}/decision",
            json={"action": "approve", "selected_task_ids": ["task-1"]},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["progress"] == "未开始"
    assert scanned.json()[0]["task_type"] == "动画类"
    assert scanned.json()[0]["deliverable"] is True
    assert "snapshot" not in scanned.json()[0]
    assert "maker_open_id" not in scanned.json()[0]
    assert approved.status_code == 202
    assert runtime.resume_calls == ["run-no-maker"]


async def test_scan_marks_live_action_as_unavailable_and_rejects_claim(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(
        runtime=runtime,
        bitable_service=_ProductionService(task_type="真人类"),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get("/api/bitable/tasks")
        rejected = await client.post("/api/bitable/tasks/rec-no-maker/claim")

    assert scanned.status_code == 200
    assert scanned.json()[0]["task_type"] == "真人类"
    assert scanned.json()[0]["deliverable"] is True
    assert scanned.json()[0]["delivery_block_reason"] is None
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "真人类任务暂未启用"


async def test_api_routes_scan_and_claim_to_portrait_category(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    service = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get(
            "/api/bitable/tasks", params={"category": "portrait"}
        )
        claimed = await client.post(
            "/api/bitable/tasks/rec-portrait/claim",
            params={"category": "portrait"},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["task_type"] == "真人类"
    assert claimed.status_code == 202
    assert service.scan_categories == ["portrait"]
    assert service.claim_categories == [("rec-portrait", "portrait")]


async def test_api_rejects_unknown_production_category(tmp_path) -> None:
    app = create_app(
        runtime=_Runtime(tmp_path),
        bitable_service=_ProductionService(),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/tasks", params={"category": "other"}
        )

    assert response.status_code == 422


async def test_api_returns_validation_error_for_missing_production_category(tmp_path) -> None:
    service = _ProductionService()
    service.scan_error = RunValidationError("未配置 portrait 类别")
    app = create_app(runtime=_Runtime(tmp_path), bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/tasks", params={"category": "portrait"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "未配置 portrait 类别"


async def test_recent_runs_and_rerun_endpoints(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        recent = await client.get("/api/bitable/recent-runs")
        rerun = await client.post("/api/bitable/runs/run-old/rerun")

    assert recent.status_code == 200
    assert recent.json()[0]["run_id"] == "run-old"
    # record_id 是前端把重跑叠到同一条任务记录下的依据。
    assert recent.json()[0]["record_id"] == "rec-old"
    assert recent.json()[0]["rerunnable"] is True
    # 失败尝试的原因要能直接下发（没有就是 null，前端再去那次运行里找）。
    assert recent.json()[0]["last_error"] is None
    assert rerun.status_code == 202
    assert rerun.json() == {"run_id": "run-new"}
    assert production.rerun_calls == ["run-old"]


async def test_recent_runs_exposes_failed_attempt_with_its_reason(tmp_path) -> None:
    """失败的尝试也要出现在 recent-runs 里，并带上落库的失败原因。

    用户 2026-09-24：「『成品与结果』区少了某些记录」—— 失败的尝试以前在 store 里
    被 `hidden=1` 挡掉，前端滑条上一条都没有。
    """
    from types import SimpleNamespace

    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async def failed_recent_runs(*, owner_user_id: str = "prime-local"):
        assert owner_user_id == "prime-local"
        return [
            SimpleNamespace(
                run_id="run-failed",
                record_id="rec-old",
                display_text="需求 A",
                status=TableTaskStatus.FAILED,
                updated_at="2026-07-22T12:30:00+00:00",
                last_error="生成参数不符合供应商要求",
                runtime_status="failed",
            ),
            SimpleNamespace(
                run_id="run-old",
                record_id="rec-old",
                display_text="需求 A",
                status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
                last_error=None,
            ),
        ]

    async def shared_result_table_url(
        run_id: str, *, owner_user_id: str = "prime-local"
    ):
        return "https://tenant.feishu.cn/base/appResult?table=tblResult"

    production.recent_runs = failed_recent_runs
    production.result_table_url = shared_result_table_url

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        recent = await client.get("/api/bitable/recent-runs")

    assert recent.status_code == 200
    payload = {item["run_id"]: item for item in recent.json()}
    assert set(payload) == {"run-failed", "run-old"}
    assert payload["run-failed"]["status"] == TableTaskStatus.FAILED.value
    assert payload["run-failed"]["last_error"] == "生成参数不符合供应商要求"
    # **运行时**状态单独下发：多维表格的「失败」把 cancelled / failed 压成同一个
    # 值，前端要靠它才能说清「这次是被取消的」（2026-09-24）。
    assert payload["run-failed"]["runtime_status"] == "failed"
    assert payload["run-failed"]["result_table_url"].endswith("tblResult")
    assert payload["run-failed"]["rerunnable"] is True
    # 成功的版本一点没变：仍可重跑，原因字段为空。
    assert payload["run-old"]["status"] == TableTaskStatus.COMPLETED.value
    assert payload["run-old"]["last_error"] is None
    assert payload["run-old"]["runtime_status"] is None
    assert payload["run-old"]["rerunnable"] is True


async def test_record_runs_endpoint_returns_full_history_of_that_record(
    tmp_path,
) -> None:
    """按 record_id（或 run_id）取某条记录的全部尝试，不受「最近任务」窗口限制。

    用户 2026-09-24：「窗口外老记录的失败尝试看不到」——实测 89 条失败尝试里 45 条
    属于窗口外的老记录。全局 recent-runs 保持窗口不变，前端打开某条记录时改用它。
    """
    from types import SimpleNamespace

    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)
    calls: list[tuple[str | None, str | None]] = []

    async def record_runs(
        *, record_id: str | None = None, run_id: str | None = None,
        owner_user_id: str = "prime-local",
    ):
        assert owner_user_id == "prime-local"
        calls.append((record_id, run_id))
        return "rec-old", [
            SimpleNamespace(
                run_id="run-cancelled",
                record_id="rec-old",
                display_text="需求 A",
                status=TableTaskStatus.FAILED,
                updated_at="2026-07-22T12:30:00+00:00",
                last_error=None,
                runtime_status="cancelled",
            ),
            SimpleNamespace(
                run_id="run-old",
                record_id="rec-old",
                display_text="需求 A",
                status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
                last_error=None,
                runtime_status="succeeded",
            ),
        ]

    async def shared_result_table_url(
        run_id: str, *, owner_user_id: str = "prime-local"
    ):
        return "https://tenant.feishu.cn/base/appResult?table=tblResult"

    production.record_runs = record_runs
    production.result_table_url = shared_result_table_url

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        by_record = await client.get(
            "/api/bitable/record-runs", params={"record_id": "rec-old"}
        )
        by_run = await client.get(
            "/api/bitable/record-runs", params={"run_id": "run-cancelled"}
        )
        missing = await client.get("/api/bitable/record-runs")

    assert by_record.status_code == 200
    body = by_record.json()
    assert body["record_id"] == "rec-old"
    payload = {item["run_id"]: item for item in body["runs"]}
    assert set(payload) == {"run-cancelled", "run-old"}
    assert payload["run-cancelled"]["status"] == TableTaskStatus.FAILED.value
    assert payload["run-cancelled"]["runtime_status"] == "cancelled"
    assert payload["run-cancelled"]["last_error"] is None
    assert payload["run-old"]["runtime_status"] == "succeeded"
    # 只给 run_id 也要能用（运行详情里没有 record_id）。
    assert by_run.status_code == 200
    assert calls == [("rec-old", None), (None, "run-cancelled")]
    # 两个都不给：说清楚缺什么，别返回一个空列表让人以为「这条记录没有历史」。
    assert missing.status_code == 422


async def test_record_runs_endpoint_is_absent_when_service_lacks_it(tmp_path) -> None:
    """MVP 模式没有这个能力 → 404，而不是 500 或「读取多维表格失败」。"""
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    assert not hasattr(production, "record_runs")
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/record-runs", params={"record_id": "rec-old"}
        )

    assert response.status_code == 404


async def test_run_detail_read_does_not_write_or_reorder(tmp_path) -> None:
    """验收口径：连续两次 `GET /api/runs/{run_id}`，`updated_at` 不变、顺序不变。

    用户 2026-09-24：读接口会调 `sync_once()`；对**停在历史里的失败运行**它会走
    `release()` → `_update_run`，回落到 `production_task_history` 刷 `updated_at`。
    于是上一个子代理只是「读那 4 条失败详情」，它们就被刷成 04:16:13、排到了
    `recent-runs` 最前面 —— 状态和 last_error 都没变，**顺序被读操作改变了**。

    这里用真 store + 真 service 走完整 HTTP 路径：读两次，行的时间戳与列表顺序
    都必须纹丝不动；同时钉住「状态真的推进了仍然会写」不被这次改动改坏。
    """
    import asyncio

    import aiosqlite

    from feishu_generation_agent.bitable.production_service import (
        ProductionBitableService,
        ProductionTaskSource,
    )
    from feishu_generation_agent.domain.bitable import BitableLocation
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    location = BitableLocation(
        wiki_token="wikiProd",
        app_token="appProd",
        table_id="tblProd",
        view_id="vewProd",
        source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd",
    )

    def task(record_id: str) -> ProductionTaskSummary:
        return ProductionTaskSummary(
            record_id=record_id,
            display_text=f"需求 {record_id}",
            source_url="https://tenant.feishu.cn/docx/docA",
            progress="已完成",
            task_type="动画类",
            snapshot=ProductionSourceSnapshot(
                requirement_name=f"需求 {record_id}",
                task_type="动画类",
                requirement_attachment="https://tenant.feishu.cn/docx/docA",
            ),
        )

    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return []

    class Repository:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        async def get_run(self, run_id: str, *, owner_user_id=None):
            return self.rows.get(run_id)

        async def list_runs(self, *, owner_user_id=None):
            return [dict(row) for row in self.rows.values()]

    class Runtime:
        def __init__(self) -> None:
            self.repository = Repository()

        async def get_run_view(self, run_id: str) -> dict:
            row = self.repository.rows[run_id]
            return {
                "run_id": run_id,
                "status": row["status"],
                "artifacts": [],
            }

        async def close(self) -> None:
            pass

    def run_row(run_id: str, status: str) -> dict:
        return {
            "run_id": run_id,
            "thread_id": f"thread-{run_id}",
            "source_url": "https://tenant.feishu.cn/docx/docA",
            "owner_user_id": "prime-local",
            "status": status,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    # rec-old 的两次尝试（第一次被取消 → 失败后停进历史），rec-new 更新。
    runtime.repository.rows["run-cancelled"] = run_row("run-cancelled", "cancelled")
    runtime.repository.rows["run-newest"] = run_row("run-newest", "succeeded")
    service = ProductionBitableService(
        bitable=Bitable(),
        store=store,
        runtime=runtime,
        sources={"animation": ProductionTaskSource(location, "动画类")},
        include_completed_for_test=True,
    )
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)
    try:
        for run_id in ("run-cancelled", "run-newest"):
            await store.claim(
                location, task("rec-old" if run_id == "run-cancelled" else "rec-new"),
                run_id=run_id, thread_id=f"thread-{run_id}",
                owner_user_id="prime-local",
            )
            await store.release(run_id, status=TableTaskStatus.FAILED)
        # 再接手一次 → run-cancelled 被搬进历史并打 hidden=1（就是出问题的那些行）。
        await store.claim(
            location, task("rec-old"), run_id="run-cancelled-2",
            thread_id="thread-run-cancelled-2", owner_user_id="prime-local",
        )
        await store.release("run-cancelled-2", status=TableTaskStatus.FAILED)
        # 铺开时间：rec-new 更新 → 它该排在前面。
        connection = await aiosqlite.connect(str(tmp_path / "production.sqlite3"))
        try:
            for table in ("production_tasks", "production_task_history"):
                await connection.execute(
                    f"UPDATE {table} SET updated_at = '2026-01-01 00:00:00' "
                    "WHERE source_record_id = 'rec-old'"
                )
                await connection.execute(
                    f"UPDATE {table} SET updated_at = '2026-01-02 00:00:00' "
                    "WHERE source_record_id = 'rec-new'"
                )
            await connection.commit()
        finally:
            await connection.close()

        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            before = (await store.get_by_run("run-cancelled")).updated_at
            order_before = [
                item["run_id"]
                for item in (await client.get("/api/bitable/recent-runs")).json()
            ]
            await asyncio.sleep(1.1)
            first = await client.get("/api/runs/run-cancelled")
            after_first = (await store.get_by_run("run-cancelled")).updated_at
            await asyncio.sleep(1.1)
            second = await client.get("/api/runs/run-cancelled")
            after_second = (await store.get_by_run("run-cancelled")).updated_at
            order_after = [
                item["run_id"]
                for item in (await client.get("/api/bitable/recent-runs")).json()
            ]
            # 状态真的推进时仍然要落库（别把必要写入一起砍掉）。
            runtime.repository.rows["run-newest"]["status"] = "failed"
            await client.get("/api/runs/run-newest")
            advanced = (await store.get_by_run("run-newest")).status
    finally:
        await service.close()

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert after_first == before, "第一次读就写了库（updated_at 被刷新）"
    assert after_second == before, "连续两次读改变了 updated_at"
    assert order_after == order_before, "读操作改变了「最近任务」的顺序"
    assert order_before[0] == "run-newest"
    # 必要写入还在：运行时变成 failed，落库状态跟着走到「失败」。
    assert advanced is TableTaskStatus.FAILED


async def test_rerun_selected_endpoint_creates_new_branch(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/bitable/runs/run-old/rerun-selected",
            json={
                "action": "adjust",
                "feedback": "动作再慢一点",
                "task_ids": ["task-1"],
            },
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-selected"}
    # 重跑只重写提示词，必须停在审批页让用户过目、改参数与模型；
    # 自动通过会把这一步直接跳掉（用户体感：重跑后立刻开始生成、改不了东西）。
    assert production.rerun_selected_calls == [
        ("run-old", ["task-1"], "动作再慢一点", False)
    ]


async def test_rerun_endpoint_forwards_new_duration(tmp_path) -> None:
    """②-b：`duration` 能被 API 一路传到服务层（与上一版不同就不许原样保留提示词）。

    审批页改时长走的是任务 PATCH + 提交校验（②-a 拦）；这里是给 API / 脚本调用方的
    对称入口 —— 传了它，克隆分支就会去比对「本次时长 vs 被克隆计划的时长」。
    """
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        ok = await client.post(
            "/api/bitable/runs/run-old/rerun",
            json={"duration": 8},
        )
        too_long = await client.post(
            "/api/bitable/runs/run-old/rerun",
            json={"duration": 999},
        )

    assert ok.status_code == 202
    assert production.rerun_durations == [8]
    # schema 有上下界（4..60）：明显不合理的时长要在入口就拒，不要传进来再炸。
    assert too_long.status_code == 422


async def test_rerun_endpoint_forwards_rework_feedback(tmp_path) -> None:
    """「重新运行」也能带返工要求 —— 与「重跑选中任务」同一条逻辑。

    用户口径 2026-09-24：「重新运行按钮逻辑跟这个一模一样」。以前 `/rerun` 收不到
    body，返工要求根本传不进去，用户填了也白填。
    """
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/bitable/runs/run-old/rerun",
            json={"feedback": "动作再慢一点"},
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-selected"}
    # 不带勾选 = 整份计划；仍然必须停在待审批（auto_approve 恒为 False）。
    assert production.rerun_selected_calls == [
        ("run-old", [], "动作再慢一点", False)
    ]


async def test_rerun_endpoint_without_body_stays_verbatim(tmp_path) -> None:
    """不带 body = 一个字都不改地重跑（老前端/老脚本的调用形状必须继续有效）。"""
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/bitable/runs/run-old/rerun")

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-new"}
    # 没有返工要求就不该把 task_ids/feedback 传给服务（服务端据此走原样分支）。
    assert production.rerun_selected_calls == []


async def test_rerun_selected_allows_empty_feedback(tmp_path) -> None:
    """返工要求为空是**合法**的：不改提示词，照样进审批页重新审核。

    用户口径 2026-09-24：「如果返工要求里没内容则是直接不改提示词原封不动让我
    重新审核」。以前这里直接 422「请填写返工要求」，用户连重跑都点不了。
    """
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/bitable/runs/run-old/rerun-selected",
            json={"action": "adjust", "feedback": "", "task_ids": ["task-1"]},
        )

    assert response.status_code == 202
    assert production.rerun_selected_calls == [
        ("run-old", ["task-1"], "", False)
    ]


async def test_rerun_selected_rejects_bad_payloads(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        confirm = await client.post(
            "/api/bitable/runs/run-old/rerun-selected",
            json={"action": "confirm", "task_ids": ["task-1"]},
        )
        duplicated = await client.post(
            "/api/bitable/runs/run-old/rerun-selected",
            json={"action": "adjust", "task_ids": ["task-1", "task-1"]},
        )

    # 只支持返工重跑；重复勾选同一个任务没有意义。
    assert confirm.status_code == 422
    assert duplicated.status_code == 422
    assert production.rerun_selected_calls == []


async def test_run_view_exposes_shared_result_table_url(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    runtime.run_views["run-old"] = {
        "run_id": "run-old",
        "status": "succeeded",
        "artifacts": [{"artifact_id": "artifact-1", "kind": "video"}],
    }
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        view = await client.get("/api/runs/run-old")

    assert view.status_code == 200
    assert view.json()["result_table_url"] == (
        "https://tenant.feishu.cn/base/result-table"
    )


class _ServiceWithoutResultTable:
    async def sync_once(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        del run_id, owner_user_id

    async def close(self) -> None:
        pass


async def test_run_view_tolerates_service_without_result_table_url(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    runtime.run_views["run-old"] = {"run_id": "run-old", "status": "succeeded"}
    app = create_app(
        runtime=runtime, bitable_service=_ServiceWithoutResultTable()
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        view = await client.get("/api/runs/run-old")

    assert view.status_code == 200
    assert "result_table_url" not in view.json()


async def test_archive_restore_and_archived_runs_endpoints(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.run_owners["run-archived"] = "prime-local"
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        archived = await client.get("/api/bitable/archived-runs")
        archive = await client.post("/api/bitable/runs/run-old/archive")
        restore = await client.post("/api/bitable/runs/run-archived/restore")

    assert archived.status_code == 200
    assert archived.json()[0]["run_id"] == "run-archived"
    assert archive.status_code == 200
    assert archive.json() == {"run_id": "run-old", "status": "archived"}
    assert restore.status_code == 200
    assert restore.json() == {
        "run_id": "run-archived",
        "status": "restored",
    }
    assert production.archive_calls == ["run-old"]
    assert production.restore_calls == ["run-archived"]


async def test_production_routes_filter_lists_and_hide_wrong_owner(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.run_owners.update(
        {"run-a": "user-a", "run-b": "user-b", "run-old": "user-a"}
    )
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)
    user_a = {"X-Portal-User-Id": "user-a"}
    user_b = {"X-Portal-User-Id": "user-b"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned_a = await client.get(
            "/api/bitable/tasks", headers=user_a
        )
        scanned_b = await client.get(
            "/api/bitable/tasks", headers=user_b
        )
        active_a = await client.get(
            "/api/bitable/active-runs", headers=user_a
        )
        recent_a = await client.get(
            "/api/bitable/recent-runs", headers=user_a
        )
        wrong_owner = [
            await client.post(
                "/api/bitable/runs/run-old/rerun", headers=user_b
            ),
            await client.post(
                "/api/bitable/runs/run-old/retry-delivery",
                headers=user_b,
            ),
            await client.delete("/api/runs/run-old", headers=user_b),
        ]

    assert scanned_a.json() == scanned_b.json()
    assert [item["run_id"] for item in active_a.json()] == ["run-a"]
    assert [item["run_id"] for item in recent_a.json()] == ["run-old"]
    assert [response.status_code for response in wrong_owner] == [404, 404, 404]


async def test_rerun_of_locked_production_task_returns_a_conflict(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.rerun_error = ProductionTaskAlreadyClaimed("生产表任务已被领取")
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/bitable/runs/run-old/rerun")

    assert response.status_code == 409
    assert response.json()["detail"] == "该任务已被领取或当前不可处理"


async def test_rerun_conflict_keeps_its_actionable_message(tmp_path) -> None:
    """RunConflict 的文案是写给用户看的，不能被压成通用提示。

    用户 2026-09-21 反馈「重新运行点了直接没反应」：后端返回 409，前端却只拿到
    「该任务已被领取或当前不可处理」，既看不出发生了什么，也不知道该做什么。
    """
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.rerun_error = RunConflict(
        "该记录上还有一条「生成中」的任务，请先处理（审批通过或取消）它再重跑。"
    )
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/bitable/runs/run-old/rerun")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "该记录上还有一条「生成中」的任务，请先处理（审批通过或取消）它再重跑。"
    )


async def test_static_assets_are_not_cached_between_local_updates(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/static/review-state.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
