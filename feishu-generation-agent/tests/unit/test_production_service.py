import pytest

from feishu_generation_agent.bitable.production_service import (
    ProductionBitableService,
    ProductionTaskSource,
)
from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import (
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiProd", app_token="appProd", table_id="tblProd",
        view_id="vewProd", source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )


def _task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-no-maker", display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/docA", progress="未开始", task_type="动画类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A", task_type="动画类", requirement_attachment="https://tenant.feishu.cn/docx/docA"
        ),
    )


def _portrait_task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-portrait",
        display_text="真人需求",
        source_url="https://tenant.feishu.cn/docx/docPortrait",
        progress="未开始",
        task_type="真人类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="真人需求",
            task_type="真人类",
            requirement_attachment="https://tenant.feishu.cn/docx/docPortrait",
        ),
    )


def _category_sources() -> dict[str, ProductionTaskSource]:
    return {
        "animation": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewAnimation"}),
            "动画类",
        ),
        "portrait": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewPortrait"}),
            "真人类",
        ),
    }


class _MixedCategoryBitable:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_schema(self, location):
        return object()

    async def list_tasks(self, location, schema, *, include_completed):
        self.calls.append(location.view_id)
        return [_task(), _portrait_task()]


class _Runtime:
    async def start_run(self, request, *, run_id=None, thread_id=None):
        return run_id


async def _production_service(
    tmp_path,
    *,
    bitable,
    sources,
    enabled_task_types=frozenset({"动画类", "真人类"}),
):
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=bitable,
        store=store,
        runtime=_Runtime(),
        sources=sources,
        include_completed_for_test=False,
        enabled_task_types=enabled_task_types,
    )
    return service, store


async def test_service_scans_only_the_requested_category_and_exact_type(
    tmp_path,
) -> None:
    bitable = _MixedCategoryBitable()
    service, store = await _production_service(
        tmp_path,
        bitable=bitable,
        sources=_category_sources(),
    )
    try:
        animation_tasks = await service.scan("animation")
        portrait_tasks = await service.scan("portrait")
    finally:
        await store.close()

    assert [task.task_type for task in animation_tasks] == ["动画类"]
    assert [task.task_type for task in portrait_tasks] == ["真人类"]
    assert bitable.calls == ["vewAnimation", "vewPortrait"]


async def test_completed_with_errors_releases_production_task_as_failed(
    tmp_path,
) -> None:
    class Runtime:
        async def get_run_view(self, run_id):
            return {"run_id": run_id, "status": "completed_with_errors"}

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=_MixedCategoryBitable(),
        store=store,
        runtime=Runtime(),
        sources=_category_sources(),
        include_completed_for_test=False,
        enabled_task_types=frozenset({"动画类", "真人类"}),
    )
    try:
        binding = await store.claim(
            _category_sources()["portrait"].location,
            _portrait_task(),
            run_id="run-with-errors",
            thread_id="thread-with-errors",
        )
        released = await service.sync_once(binding.run_id)
        active = await service.active_runs()
    finally:
        await store.close()

    assert released.status is TableTaskStatus.FAILED
    assert active == []


async def test_service_archive_and_restore_recent_runs(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources=_category_sources(),
    )
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="prime-local",
        )
        await store.release(
            binding.run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="prime-local",
        )

        assert [item.run_id for item in await service.recent_runs()] == ["run-1"]

        await service.archive_run("run-1")
        assert await service.recent_runs() == []
        assert [item.run_id for item in await service.archived_runs()] == ["run-1"]

        await service.restore_run("run-1")
        assert [item.run_id for item in await service.recent_runs()] == ["run-1"]
        assert await service.archived_runs() == []
    finally:
        await store.close()


async def test_portrait_claim_uses_the_portrait_source_location(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources=_category_sources(),
        enabled_task_types=frozenset({"动画类", "真人类"}),
    )
    try:
        run_id = await service.claim("rec-portrait", "portrait")
        binding = await store.get_by_run(run_id)
    finally:
        await store.close()

    assert binding is not None
    assert binding.source_location.view_id == "vewPortrait"
    assert binding.snapshot.task_type == "真人类"


async def test_service_reports_an_unconfigured_portrait_source(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources={"animation": _category_sources()["animation"]},
    )
    try:
        with pytest.raises(RunValidationError, match="真人类视图尚未配置"):
            await service.scan("portrait")
    finally:
        await store.close()


async def test_service_allows_approval_without_maker_for_animation(tmp_path) -> None:
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request: RequirementRequest, *, run_id=None, thread_id=None):
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        run_id = await service.claim("rec-no-maker")
        await service.validate_approval(run_id)
    finally:
        await store.close()


async def test_service_lists_active_production_run_for_browser_restore(tmp_path) -> None:
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        run_id = await service.claim("rec-no-maker")
        active = await service.active_runs()
        scanned_after_claim = await service.scan()
    finally:
        await store.close()

    assert [(item.run_id, item.status.value) for item in active] == [
        (run_id, "处理中")
    ]
    # #1：已领取的记录不能从列表里消失，否则用户一点「开始分析」这条记录就
    # 再也看不见了（要重新进审批页才能找回来）。它应该带着领取信息回来。
    assert [item.record_id for item in scanned_after_claim] == ["rec-no-maker"]
    assert scanned_after_claim[0].claimed_run_id == run_id
    assert scanned_after_claim[0].claim_status is TableTaskStatus.PROCESSING


async def test_service_keeps_scan_global_while_active_runs_are_owner_scoped(
    tmp_path,
) -> None:
    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return [_task()]

    service, store = await _production_service(
        tmp_path,
        bitable=Bitable(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
    )
    try:
        run_id = await service.claim(
            "rec-no-maker", owner_user_id="user-a"
        )
        assert [
            item.run_id
            for item in await service.active_runs(owner_user_id="user-a")
        ] == [run_id]
        assert (
            await service.active_runs(owner_user_id="user-b")
        ) == []
        scanned = await service.scan()
        assert [item.record_id for item in scanned] == ["rec-no-maker"]
        assert scanned[0].claimed_run_id == run_id
    finally:
        await store.close()


async def test_claim_refuses_a_record_that_is_already_claimed(tmp_path) -> None:
    """列表现在会带回已领取的记录，所以领取入口必须自己挡住重复领取。"""

    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return [_task()]

    service, store = await _production_service(
        tmp_path,
        bitable=Bitable(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
    )
    try:
        first_run_id = await service.claim("rec-no-maker")
        with pytest.raises(RunConflict, match="当前不可领取"):
            await service.claim("rec-no-maker")
    finally:
        await store.close()

    assert first_run_id


async def test_service_hides_owned_run_from_wrong_owner_mutations(
    tmp_path,
) -> None:
    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return [_task()]

    class Runtime:
        async def start_run(
            self, request, *, run_id=None, thread_id=None
        ):
            return run_id

        async def delete_run(self, run_id):
            raise AssertionError("wrong owner reached runtime")

        async def retry_delivery(self, run_id):
            raise AssertionError("wrong owner reached runtime")

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(),
        store=store,
        runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        run_id = await service.claim(
            "rec-no-maker", owner_user_id="user-a"
        )
        for operation in (
            lambda: service.validate_approval(
                run_id, owner_user_id="user-b"
            ),
            lambda: service.retry_delivery(
                run_id, owner_user_id="user-b"
            ),
            lambda: service.delete_run(
                run_id, owner_user_id="user-b"
            ),
            lambda: service.rerun(
                run_id, owner_user_id="user-b"
            ),
        ):
            with pytest.raises(RunNotFound):
                await operation()
    finally:
        await store.close()


async def test_service_distinguishes_missing_and_wrong_owner_production_runs(
    tmp_path,
) -> None:
    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return [_task()]

    service, store = await _production_service(
        tmp_path,
        bitable=Bitable(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
    )
    try:
        run_id = await service.claim(
            "rec-no-maker", owner_user_id="user-a"
        )
        assert await service.is_production_run(
            run_id, owner_user_id="user-a"
        )
        assert not await service.is_production_run(
            "missing-run", owner_user_id="user-a"
        )
        with pytest.raises(RunNotFound):
            await service.is_production_run(
                run_id, owner_user_id="user-b"
            )
    finally:
        await store.close()


@pytest.mark.parametrize(
    "runtime_status",
    ["succeeded", "completed_with_errors", "failed", "cancelled", "timed_out"],
)
async def test_terminal_runtime_status_releases_shared_production_lock(
    tmp_path,
    runtime_status: str,
) -> None:
    class Runtime:
        async def get_run_view(self, run_id):
            return {"run_id": run_id, "status": runtime_status}

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=_MixedCategoryBitable(),
        store=store,
        runtime=Runtime(),
        sources=_category_sources(),
        include_completed_for_test=False,
        enabled_task_types=frozenset({"动画类", "真人类"}),
    )
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id=f"run-{runtime_status}",
            thread_id=f"thread-{runtime_status}",
            owner_user_id="user-a",
        )
        await service.sync_once(
            binding.run_id, owner_user_id="user-a"
        )
        replacement = await store.claim(
            _location(),
            _task(),
            run_id=f"replacement-{runtime_status}",
            thread_id=f"replacement-thread-{runtime_status}",
            owner_user_id="user-b",
        )
        assert replacement.owner_user_id == "user-b"
    finally:
        await store.close()


async def test_service_rerun_keeps_successful_original_visible_while_active(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls: list[tuple[str, str, str]] = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(
            self, source_run_id, request, *, run_id, thread_id
        ):
            self.clone_calls.append((source_run_id, run_id, thread_id))
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)

        rerun_id = await service.rerun(original_run_id)
        original = await store.get_by_run(original_run_id)
        recent = await service.recent_runs()
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert original is not None
    assert original.status is TableTaskStatus.COMPLETED
    # A successful rerun must not overwrite the original task: the completed
    # original stays visible as a separate history entry while the rerun runs.
    assert [item.run_id for item in recent] == [original_run_id]
    assert len(runtime.clone_calls) == 1
    cloned_from, cloned_run_id, cloned_thread_id = runtime.clone_calls[0]
    assert cloned_from == original_run_id
    assert cloned_run_id == rerun_id
    assert cloned_thread_id != original.thread_id


async def test_service_rerun_selected_task_creates_branch_and_auto_approves(
    tmp_path,
) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_kwargs = None
            self.resume_calls = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, **kwargs):
            self.clone_kwargs = (source_run_id, request, kwargs)
            return kwargs["run_id"]

        async def resume_run(self, run_id, decision):
            self.resume_calls.append((run_id, decision))

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)

        rerun_id = await service.rerun(
            original_run_id,
            task_ids=["task-1"],
            feedback="动作再慢一点",
            auto_approve=True,
        )
        original = await store.get_by_run(original_run_id)
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert original is not None
    assert original.status is TableTaskStatus.COMPLETED
    assert runtime.clone_kwargs is not None
    _source, _request, clone_kwargs = runtime.clone_kwargs
    assert clone_kwargs["task_ids"] == ["task-1"]
    assert clone_kwargs["feedback"] == "动作再慢一点"
    assert len(runtime.resume_calls) == 1
    resumed_run_id, decision = runtime.resume_calls[0]
    assert resumed_run_id == rerun_id
    assert decision.action == "approve"
    assert decision.selected_task_ids == ["task-1"]


async def test_service_rerun_selected_waits_for_user_approval(tmp_path) -> None:
    """不带 auto_approve 时，重跑必须停在审批页等人放行。"""

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_kwargs = None
            self.resume_calls = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, **kwargs):
            self.clone_kwargs = (source_run_id, request, kwargs)
            return kwargs["run_id"]

        async def resume_run(self, run_id, decision):
            self.resume_calls.append((run_id, decision))

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)

        rerun_id = await service.rerun(
            original_run_id,
            task_ids=["task-1"],
            feedback="动作再慢一点",
        )
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert runtime.clone_kwargs is not None
    _source, _request, clone_kwargs = runtime.clone_kwargs
    assert clone_kwargs["task_ids"] == ["task-1"]
    assert clone_kwargs["feedback"] == "动作再慢一点"
    assert runtime.resume_calls == []

async def test_service_rerun_returns_existing_active_run_for_same_record(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls = 0

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, *args, **kwargs):
            self.clone_calls += 1
            if self.clone_calls > 1:
                raise AssertionError("existing active run should be reused")
            return kwargs["run_id"]

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.FAILED)
        current_run_id = await service.rerun(original_run_id)

        reused_run_id = await service.rerun(original_run_id)
    finally:
        await store.close()

    assert reused_run_id == current_run_id
    assert runtime.clone_calls == 1


async def test_service_rerun_conflicts_when_active_run_waits_for_human(
    tmp_path,
) -> None:
    """记录上卡着一条等人的任务时，重跑必须明确报错。

    旧行为是静默返回那条卡住的任务 id，用户点「重跑」后被直接甩回它所在的
    审批/审片页面，看起来就是「重跑没生效」。
    """

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls = 0

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, *args, **kwargs):
            self.clone_calls += 1
            return kwargs["run_id"]

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.FAILED)
        parked_run_id = await service.rerun(original_run_id)
        # 那条重跑出来的任务停在「待审批」等人处理（生产现场就是这样卡了一天）
        await store.set_status(parked_run_id, TableTaskStatus.WAITING_APPROVAL)

        with pytest.raises(RunConflict) as excinfo:
            await service.rerun(original_run_id)
    finally:
        await store.close()

    assert "待审批" in str(excinfo.value)
    assert runtime.clone_calls == 1


async def test_service_rerun_reuses_active_run_that_is_still_working(
    tmp_path,
) -> None:
    """真正在跑的任务仍然复用（连点两次重跑不该起两个）。"""

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls = 0

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, *args, **kwargs):
            self.clone_calls += 1
            return kwargs["run_id"]

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.FAILED)
        working_run_id = await service.rerun(original_run_id)
        await store.set_status(working_run_id, TableTaskStatus.GENERATING)

        reused_run_id = await service.rerun(original_run_id)
    finally:
        await store.close()

    assert reused_run_id == working_run_id
    assert runtime.clone_calls == 1


async def test_service_rejects_rerun_of_non_animation_task(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

    task = _task().model_copy(
        update={
            "task_type": "真人类",
            "snapshot": _task().snapshot.model_copy(update={"task_type": "真人类"}),
        }
    )
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=False,
    )
    try:
        binding = await store.claim(
            _location(), task, run_id="run-live-action", thread_id="thread-live-action"
        )
        await store.release(binding.run_id, status=TableTaskStatus.COMPLETED)
        with pytest.raises(RunConflict, match="真人类任务暂未启用"):
            await service.rerun(binding.run_id)
    finally:
        await store.close()


async def test_service_successful_rerun_keeps_both_completed_tasks(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
    import asyncio

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls: list[tuple[str, str, str]] = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, *, run_id, thread_id):
            self.clone_calls.append((source_run_id, run_id, thread_id))
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)
        await asyncio.sleep(1.1)

        rerun_id = await service.rerun(original_run_id)
        await store.release(rerun_id, status=TableTaskStatus.COMPLETED)
        await asyncio.sleep(1.1)

        recent = await service.recent_runs()
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert [item.run_id for item in recent] == [rerun_id, original_run_id]


async def test_service_recent_runs_keeps_successful_record_with_active_rerun(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
    import asyncio

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, *, run_id, thread_id):
            self.clone_calls.append((source_run_id, run_id, thread_id))
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)
        await asyncio.sleep(1.1)

        rerun_id = await service.rerun(original_run_id)
        recent = await service.recent_runs()
        active = await service.active_runs()
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert [item.run_id for item in recent] == [original_run_id]
    assert [item.run_id for item in active] == [rerun_id]


async def test_service_failed_rerun_overwrites_and_hides_failed_original(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
    import asyncio

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, *, run_id, thread_id):
            self.clone_calls.append((source_run_id, run_id, thread_id))
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.FAILED)
        await asyncio.sleep(1.1)

        rerun_id = await service.rerun(original_run_id)
        recent = await service.recent_runs()
        active = await service.active_runs()
    finally:
        await store.close()

    assert rerun_id != original_run_id
    # A failed rerun overwrites the failed task: the old failed entry is hidden
    # while the new attempt is the only visible active run.
    assert [item.run_id for item in recent] == []
    assert [item.run_id for item in active] == [rerun_id]
