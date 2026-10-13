import aiosqlite
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


async def test_recent_runs_keeps_every_visible_record_and_all_its_attempts(
    tmp_path,
) -> None:
    """可见记录一条都不许被窗口藏起来；每条任务要带上它的**全部**历次尝试。

    用户口径（2026-09-17）：「以一个任务为一个单位，所有记录都存在该任务里」。
    2026-09-24 追加：窗口外的记录以前**既不在列表、也不在回收站**，等于人间蒸发
    （实测 33 条可见记录只显示 10 条）。「只保留 10 条」改由自动归档负责
    （`auto_archive_overflow`），列表本身不再截断 —— 否则「还没轮到归档」的记录
    会被重新藏起来。

    这个用例的 runtime 没有仓储，自动归档跑不动（查不到谁在跑就不动），所以 12 条
    可见记录必须**一条不少**地出现在列表里。
    """
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location):
            return object()

        async def list_tasks(self, location, schema, *, include_completed):
            return []

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None):
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(),
        store=store,
        runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        # 12 条任务 × 每条 3 次尝试，全部已结束（模拟历史）
        for record_index in range(12):
            for attempt in range(3):
                task = ProductionTaskSummary(
                    record_id=f"rec-{record_index:02d}",
                    display_text=f"需求 {record_index}",
                    source_url=f"https://tenant.feishu.cn/docx/doc{record_index}",
                    progress="已完成",
                    task_type="动画类",
                    snapshot=ProductionSourceSnapshot(
                        requirement_name=f"需求 {record_index}",
                        task_type="动画类",
                        requirement_attachment="https://tenant.feishu.cn/docx/x",
                    ),
                )
                binding = await store.claim(
                    _location(),
                    task,
                    run_id=f"run-{record_index:02d}-{attempt}",
                    thread_id=f"thread-{record_index:02d}-{attempt}",
                    owner_user_id="prime-local",
                )
                await store.release(
                    binding.run_id,
                    status=TableTaskStatus.COMPLETED,
                    owner_user_id="prime-local",
                )
        recent = await service.recent_runs()
    finally:
        await store.close()

    record_order: list[str] = []
    for binding in recent:
        if binding.record_id not in record_order:
            record_order.append(binding.record_id)
    # 「只保留 10 条」由自动归档负责（见 auto_archive_overflow）；列表本身不截断，
    # 免得把「还没轮到归档」的记录重新藏成「列表没有、回收站也没有」。
    assert len(record_order) == 12, "可见记录一条都不许被窗口藏起来"
    assert len(recent) == 36, "每条任务的 3 次尝试都要在，不能被窗口截断"


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


async def test_service_rerun_from_waiting_review_takes_over_the_record(
    tmp_path,
) -> None:
    """「待确认成片」也要能退回重跑 —— 那是成片审核页上的主操作。

    用户 2026-09-21：成片审核页点「重跑选中任务」必然 409（连点 14 次），
    因为绑定状态是「待确认成片」，而 rerun 只认「已完成 / 失败」。
    成片已经生成完、正等人做决定，这时退回重跑必须放行。
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
        run_id = await service.claim("rec-no-maker")
        # 成片生成完，停在审核页等人确认（生产现场就是这个状态）
        await store.set_status(run_id, TableTaskStatus.REVIEWING)

        rerun_id = await service.rerun(
            run_id, task_ids=["task-1"], feedback="南瓜滚落的位置不对"
        )
        active = await store.list_active(
            _location().app_token or "", _location().table_id
        )
    finally:
        await store.close()

    assert rerun_id != run_id
    assert runtime.clone_calls == 1
    # 旧绑定被放掉，新运行接管这条记录（否则 claim 会以「已被领取」拒绝）
    assert [binding.run_id for binding in active] == [rerun_id]


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


async def test_service_failed_attempt_stays_in_recent_after_rerun(tmp_path) -> None:
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
    # 失败的尝试**必须留着**：用户正是在「刚重跑完、想知道上次为什么失败」的时候
    # 看「成品与结果」的历史滑条（2026-09-24：「我失败过几次、为什么失败」看不到）。
    # 以前这里断言的是 []（失败版被 hidden=1 吞掉、又被「记录上还有活跃任务」滤掉）。
    assert [item.run_id for item in recent] == [original_run_id]
    # 新尝试仍然是这条记录唯一活跃的那次。
    assert [item.run_id for item in active] == [rerun_id]


async def test_service_recent_runs_keeps_failed_attempt_but_drops_stale_intermediate(
    tmp_path,
) -> None:
    """记录上还有新尝试在跑时：**失败**的旧尝试留着，旧的中间态残影不塞进来。

    光放开 store 的 `hidden` 还不够 —— service 层还有一道「记录上有活跃任务时跳过
    非终态历史行」的过滤，失败的尝试会被它再吞一次（2026-09-24：重跑之后滑条里
    仍然只剩成功版）。这里把两条规则一起钉住。
    """
    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(self, source_run_id, request, *, run_id, thread_id):
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        # run-failed 失败后被放掉 —— 下一次 claim 会把它搬进历史并打 hidden=1。
        failed_run = await service.claim("rec-no-maker")
        await store.release(failed_run, status=TableTaskStatus.FAILED)

        await store.claim(
            _location(), _task(),
            run_id="run-review", thread_id="thread-review",
            owner_user_id="prime-local",
        )
        # 「待确认成片」被放掉后再接手（审核页点重跑就是这条路）：它进历史时是
        # hidden=0，但**不是终态** —— 这类残影不该占滑条。
        await store.release(
            "run-review", status=TableTaskStatus.REVIEWING,
            owner_user_id="prime-local",
        )
        await store.claim(
            _location(), _task(),
            run_id="run-live", thread_id="thread-live",
            owner_user_id="prime-local",
        )

        recent = await service.recent_runs()
        active = await service.active_runs()
    finally:
        await store.close()

    # 失败的尝试在，中间态的残影不在。
    assert [item.run_id for item in recent] == [failed_run]
    assert [item.run_id for item in active] == ["run-live"]


class _ViewRuntime:
    """只提供 `sync_once` 需要的 `get_run_view`（状态可改）。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.views: list[str] = []

    async def get_run_view(self, run_id: str) -> dict:
        self.views.append(run_id)
        return {"status": self.status}

    async def start_run(self, request, *, run_id=None, thread_id=None):
        return run_id


def _plain_bitable():
    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return []

    return Bitable()


async def test_sync_once_is_read_only_when_nothing_changed(tmp_path) -> None:
    """读接口不该写库：状态没变时 `sync_once` 不碰 `updated_at`。

    用户 2026-09-24：`GET /api/runs/{run_id}` 会调 `sync_once()`；对**停在历史里
    的失败运行**它会走 `release()` → `_update_run`，回落到 `production_task_history`
    刷 `updated_at`。于是「看一眼详情」就把这几条顶到了 recent-runs 最前面
    （实测 4 条失败尝试被读接口刷成 04:16:13）。

    验收口径：连续两次 GET（这里等价于连续两次 `sync_once`），`updated_at` 不变。
    """
    import asyncio

    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = _ViewRuntime("failed")
    service = ProductionBitableService(
        bitable=_plain_bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        await store.claim(
            _location(), _task(), run_id="run-1", thread_id="thread-1",
            owner_user_id="prime-local",
        )
        await store.release("run-1", status=TableTaskStatus.FAILED)
        before = (await store.get_by_run("run-1")).updated_at
        await asyncio.sleep(1.1)
        synced = await service.sync_once("run-1")
        after = (await store.get_by_run("run-1")).updated_at
        # 再读一次（等价于用户又刷新了一次详情页）。
        await service.sync_once("run-1")
        twice = (await store.get_by_run("run-1")).updated_at

        # 第二条尝试接手 → 上面那条被搬进历史并打 hidden=1：这才是出问题的行
        #（老记录停在历史里的失败运行）。
        await store.claim(
            _location(), _task(), run_id="run-2", thread_id="thread-2",
            owner_user_id="prime-local",
        )
        await store.release("run-2", status=TableTaskStatus.FAILED)
        hidden_before = (await store.get_by_run("run-1")).updated_at
        await asyncio.sleep(1.1)
        await service.sync_once("run-1")
        hidden_after = (await store.get_by_run("run-1")).updated_at
    finally:
        await store.close()

    assert synced.status is TableTaskStatus.FAILED
    assert after == before, "状态没变就不该写库（updated_at 被读操作刷新了）"
    assert twice == before, "连续两次读也不能改变 updated_at"
    assert hidden_after == hidden_before, (
        "停在历史里的失败运行被读接口刷了 updated_at —— 正是排序被改变的根因"
    )


async def test_sync_once_still_writes_when_the_state_really_changed(tmp_path) -> None:
    """只去掉**纯读取副作用**：状态真的推进了、行还占着记录，都必须照写。

    否则「详情页永远停在旧状态」和「已结束的任务还挂在进行中列表里」两个
    老毛病都会回来。
    """
    import asyncio

    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = _ViewRuntime("running")
    service = ProductionBitableService(
        bitable=_plain_bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        # ① 行还活着（active=1）、状态没变 → 不写（详情页每秒轮询，不该每秒刷库）。
        await store.claim(
            _location(), _task(), run_id="run-live", thread_id="thread-live",
            owner_user_id="prime-local",
        )
        live_before = (await store.get_by_run("run-live")).updated_at
        await asyncio.sleep(1.1)
        await service.sync_once("run-live")
        live_after = await store.get_by_run("run-live")
        assert live_after.updated_at == live_before
        assert live_after.active is True, "还在跑的行不能被放掉"

        # ② 状态真的推进了（等待审批）→ 必须写。
        runtime.status = "waiting_approval"
        await asyncio.sleep(1.1)
        await service.sync_once("run-live")
        advanced = await store.get_by_run("run-live")
        assert advanced.status is TableTaskStatus.WAITING_APPROVAL
        assert advanced.updated_at != live_before

        # ③ 运行时已经终态、但行还占着记录 → 必须放掉（否则永远挂在「进行中」）。
        runtime.status = "succeeded"
        await service.sync_once("run-live")
        finished = await store.get_by_run("run-live")
        assert finished.status is TableTaskStatus.COMPLETED
        assert finished.active is False
    finally:
        await store.close()


async def test_record_runs_returns_full_history_beyond_the_recent_window(
    tmp_path, monkeypatch
) -> None:
    """按 record_id（或 run_id）取该记录的**全部**尝试，不受「最近任务」扫描窗口限制。

    用户 2026-09-24：「窗口外老记录的失败尝试看不到」——实测 89 条失败尝试里
    45 条属于「最近 10 条任务」之外的老记录。列表本身现在不再按 10 条截断
    （只保留 10 条由自动归档负责，见 `auto_archive_overflow`），但 `recent_runs`
    仍然只扫 `_RECENT_RUN_SCAN_LIMIT` 行 run；比这更老的记录依旧进不了全局列表，
    按记录取历史这条路必须照样一条不落。这里把扫描窗口压到 10 行来复刻那种处境。
    """
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
    from feishu_generation_agent.bitable import production_service as service_module
    import aiosqlite

    monkeypatch.setattr(service_module, "_RECENT_RUN_SCAN_LIMIT", 10)

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=_plain_bitable(), store=store, runtime=_ViewRuntime("failed"),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        # 12 条记录，各 2 次尝试（24 行 run）；扫描窗口只有 10 行，
        # 最老的那条（rec-00）必然落在窗口外。
        for index in range(12):
            task = _task().model_copy(update={"record_id": f"rec-{index:02d}"})
            for attempt in range(2):
                run_id = f"run-{index:02d}-{attempt}"
                await store.claim(
                    _location(), task, run_id=run_id,
                    thread_id=f"thread-{index:02d}-{attempt}",
                    owner_user_id="prime-local",
                )
                await store.release(
                    run_id,
                    status=(
                        TableTaskStatus.FAILED if index == 0
                        else TableTaskStatus.COMPLETED
                    ),
                )
        # `updated_at` 只有秒级精度，同一秒建的 12 条记录排不出先后 —— 直接按记录
        # 铺开时间戳，rec-11 最新、rec-00 最老（复刻生产上「老记录」的处境）。
        connection = await aiosqlite.connect(str(tmp_path / "production.sqlite3"))
        try:
            for index in range(12):
                stamp = f"2026-01-01 00:{index:02d}:00"
                for table in ("production_tasks", "production_task_history"):
                    await connection.execute(
                        f"UPDATE {table} SET updated_at = ? "
                        "WHERE source_record_id = ?",
                        (stamp, f"rec-{index:02d}"),
                    )
            await connection.commit()
        finally:
            await connection.close()

        recent = await service.recent_runs()
        recent_records = {item.record_id for item in recent}
        assert "rec-00" not in recent_records, "前提：这条老记录确实在扫描窗口外"

        record_id, runs = await service.record_runs(record_id="rec-00")
        by_run_id = await service.record_runs(run_id="run-00-0")
    finally:
        await store.close()

    assert record_id == "rec-00"
    assert {item.run_id for item in runs} == {"run-00-0", "run-00-1"}
    # 只给 run_id 也要能用：前端运行详情里没有 record_id。
    assert by_run_id[0] == "rec-00"
    assert {item.run_id for item in by_run_id[1]} == {"run-00-0", "run-00-1"}


async def test_runs_carry_runtime_status_so_cancelled_is_distinguishable(
    tmp_path,
) -> None:
    """payload 要带上**运行时**状态：多维表格的「失败」分不出 cancelled。

    用户 2026-09-24：「有 2 条运行时状态是 cancelled、没有任何错误记录」，
    前端只能如实写「未记录失败原因」。运行时状态带出来后，界面能说
    「这次被取消了」，且**不编具体错误**。
    """
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Repository:
        async def list_runs(self, *, owner_user_id=None):
            return [
                {"run_id": "run-cancelled", "status": "cancelled"},
                {"run_id": "run-failed", "status": "failed"},
            ]

    class Runtime(_ViewRuntime):
        def __init__(self) -> None:
            super().__init__("cancelled")
            self.repository = Repository()

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=_plain_bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        for index, run_id in enumerate(("run-cancelled", "run-failed")):
            task = _task().model_copy(update={"record_id": f"rec-{index}"})
            await store.claim(
                _location(), task, run_id=run_id,
                thread_id=f"thread-{index}", owner_user_id="prime-local",
            )
            await store.release(run_id, status=TableTaskStatus.FAILED)
        recent = await service.recent_runs()
        record_id, runs = await service.record_runs(record_id="rec-0")
    finally:
        await store.close()

    statuses = {item.run_id: item.runtime_status for item in recent}
    assert statuses == {"run-cancelled": "cancelled", "run-failed": "failed"}
    assert runs[0].runtime_status == "cancelled"


async def test_runs_without_runtime_status_stay_none(tmp_path) -> None:
    """取不到运行时状态就留 None —— 前端照旧说「未记录失败原因」，绝不猜。"""
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=_plain_bitable(), store=store, runtime=_ViewRuntime("failed"),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        await store.claim(
            _location(), _task(), run_id="run-1", thread_id="thread-1",
            owner_user_id="prime-local",
        )
        await store.release("run-1", status=TableTaskStatus.FAILED)
        recent = await service.recent_runs()
    finally:
        await store.close()

    assert [item.runtime_status for item in recent] == [None]


# ---------------------------------------------------------------------------
# 「任务记录只保留最近 10 条，第 11 条起自动进回收站」（2026-09-24 用户原话）
# ---------------------------------------------------------------------------


class _StatusRepository:
    """只实现自动归档要用的那一个查询：`runs` 表里谁还在跑。"""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = dict(statuses or {})

    async def list_runs(self, *, owner_user_id=None, statuses=None):
        return [
            {"run_id": run_id, "status": status, "owner_user_id": owner_user_id}
            for run_id, status in self.statuses.items()
        ]


class _StatusRuntime(_ViewRuntime):
    """带 `repository` 的运行时：自动归档要靠它判断谁在跑。"""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        super().__init__("succeeded")
        self.repository = _StatusRepository(statuses)


class _OneTaskBitable:
    """`claim` 走 `scan`，需要一张只返回一条待领取记录的表。"""

    def __init__(self, task) -> None:
        self.task = task

    async def ensure_schema(self, location):
        return object()

    async def list_tasks(self, location, schema, *, include_completed):
        return [self.task]


async def _overflow_service(tmp_path, *, statuses=None, bitable=None, runtime=None):
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=bitable if bitable is not None else _plain_bitable(),
        store=store,
        runtime=runtime if runtime is not None else _StatusRuntime(statuses),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    return service, store


async def _seed_records(store, database, count, *, owner="prime-local", start=1):
    """造 `count` 条「已完成」的记录，并把 `updated_at` 钉成可控的递增时间。

    SQLite 的 `CURRENT_TIMESTAMP` 只到秒：同一秒里建的记录 `updated_at` 完全相同，
    `list_recent` 的排序就成了未定义行为。要判「哪几条被归档」就必须自己钉时间。
    """
    bindings = []
    for offset in range(count):
        index = start + offset
        task = _task().model_copy(
            update={
                "record_id": f"rec-{index:02d}",
                "display_text": f"需求 {index}",
            }
        )
        binding = await store.claim(
            _location(),
            task,
            run_id=f"run-{index:02d}",
            thread_id=f"thread-{index:02d}",
            owner_user_id=owner,
        )
        await store.release(
            binding.run_id, status=TableTaskStatus.COMPLETED, owner_user_id=owner
        )
        bindings.append(binding)
    connection = await aiosqlite.connect(str(database))
    try:
        for offset, binding in enumerate(bindings):
            index = start + offset
            stamp = f"2026-09-01 00:{index // 60:02d}:{index % 60:02d}"
            for table in ("production_tasks", "production_task_history"):
                await connection.execute(
                    f"UPDATE {table} SET updated_at = ? WHERE run_id = ?",
                    (stamp, binding.run_id),
                )
        await connection.commit()
    finally:
        await connection.close()
    return bindings


async def _row_count(database) -> int:
    connection = await aiosqlite.connect(str(database))
    try:
        total = 0
        for table in ("production_tasks", "production_task_history"):
            cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}")
            total += (await cursor.fetchone())[0]
            await cursor.close()
        return total
    finally:
        await connection.close()


async def _visible_records(store, *, owner="prime-local") -> list[str]:
    """按 `list_recent` 的口径列出**所有**可见记录（不受 10 条展示窗口限制）。

    `service.recent_runs()` 自己就截到 10 条，拿它判「窗口外还剩几条」是判不出来的。
    """
    rows = await store.list_recent(
        "appProd", "tblProd", owner_user_id=owner, limit=800
    )
    ordered: list[str] = []
    for binding in rows:
        if binding.record_id not in ordered:
            ordered.append(binding.record_id)
    return ordered


async def test_auto_archive_keeps_only_the_recent_ten(tmp_path) -> None:
    """第 11 条起整条进回收站：列表稳定 10 条，回收站里找得到、可恢复。"""
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(tmp_path)
    try:
        await _seed_records(store, database, 13)
        assert len(await _visible_records(store)) == 13

        archived = await service.auto_archive_overflow()
        assert sorted(archived) == ["rec-01", "rec-02", "rec-03"]

        recent = await service.recent_runs()
        assert [item.record_id for item in recent] == [
            f"rec-{index:02d}" for index in range(13, 3, -1)
        ]

        trash = await service.archived_runs()
        assert sorted(item.record_id for item in trash) == [
            "rec-01", "rec-02", "rec-03"
        ]
        assert all(item.auto_archived and item.deleted for item in trash)
        # 只把 deleted 从 0 改成 1：一行都没被物理删除。
        assert await _row_count(database) == 13
        assert await _visible_records(store) == [
            f"rec-{index:02d}" for index in range(13, 3, -1)
        ]

        # 再跑一次是幂等的：窗口里已经是 10 条，没有新的可归档。
        assert await service.auto_archive_overflow() == []
    finally:
        await store.close()


@pytest.mark.parametrize("busy_status", ["running", "resuming"])
async def test_auto_archive_skips_records_that_are_still_running(
    tmp_path, busy_status
) -> None:
    """`runs` 表里 running / resuming 的记录**绝不归档** —— 宁可多于 10 条。

    只看绑定行的 `active` 挡不住：进程重启后可能留下 `active = 0`、运行时却还在
    `resuming` 的残影（生产库里实测就有这么一条）。
    """
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(
        tmp_path, statuses={"run-01": busy_status}
    )
    try:
        await _seed_records(store, database, 13)
        archived = await service.auto_archive_overflow()
        assert sorted(archived) == ["rec-02", "rec-03"]

        # 在跑的那条留在列表里 —— 列表因此是 11 条，不是 10 条。
        visible = await _visible_records(store)
        assert len(visible) == 11
        assert "rec-01" in visible
        assert "rec-01" not in {
            item.record_id for item in await service.archived_runs()
        }
    finally:
        await store.close()


async def test_auto_archive_does_nothing_when_runtime_status_is_unavailable(
    tmp_path,
) -> None:
    """读不到「谁在跑」就整体不归档 —— 判断不了就不动，绝不误伤。"""
    database = tmp_path / "production.sqlite3"
    # _Runtime 没有 repository：拿不到 runs 表状态。
    service, store = await _overflow_service(tmp_path, runtime=_Runtime())
    try:
        await _seed_records(store, database, 13)
        assert await service.auto_archive_overflow() == []
        assert len(await _visible_records(store)) == 13
        assert await service.archived_runs() == []
    finally:
        await store.close()


async def test_auto_archive_is_scoped_to_the_owner(tmp_path) -> None:
    """归档只影响当前 owner：别人的记录一行都不许动。"""
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(tmp_path)
    try:
        await _seed_records(store, database, 13, owner="user-a")
        await _seed_records(store, database, 13, owner="user-b", start=100)

        archived = await service.auto_archive_overflow(owner_user_id="user-a")
        assert sorted(archived) == ["rec-01", "rec-02", "rec-03"]

        others = await store.list_recent(
            "appProd", "tblProd", owner_user_id="user-b", limit=800
        )
        assert len(others) == 13
        assert all(item.deleted is False for item in others)
    finally:
        await store.close()


async def test_auto_archive_marks_manual_deletion_differently(tmp_path) -> None:
    """手动删除与自动归档在回收站里要能分开（前端据此换文案）。"""
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(tmp_path)
    try:
        await _seed_records(store, database, 12)
        await service.auto_archive_overflow()

        manual = next(
            item for item in await service.recent_runs()
            if item.record_id == "rec-04"
        )
        await service.archive_run(manual.run_id)

        trash = {item.record_id: item for item in await service.archived_runs()}
        assert trash["rec-01"].auto_archived is True
        assert trash["rec-04"].auto_archived is False
    finally:
        await store.close()


async def test_manual_delete_archives_the_whole_record(tmp_path) -> None:
    """手动「删除」也是整条记录归档：删完就真的从列表消失，不再「删了还在」。"""
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(tmp_path)
    try:
        # 同一条记录两次尝试：失败进历史、成功留在当前行。
        await store.claim(
            _location(), _task(), run_id="run-fail", thread_id="thread-fail",
            owner_user_id="prime-local",
        )
        await store.release(
            "run-fail", status=TableTaskStatus.FAILED, owner_user_id="prime-local"
        )
        await store.claim(
            _location(), _task(), run_id="run-ok", thread_id="thread-ok",
            owner_user_id="prime-local",
        )
        await store.release(
            "run-ok", status=TableTaskStatus.COMPLETED, owner_user_id="prime-local"
        )

        await service.archive_run("run-fail")
        assert await service.recent_runs() == []
        assert [
            item.run_id for item in await service.archived_runs()
        ] == ["run-ok"] or [
            item.run_id for item in await service.archived_runs()
        ] == ["run-fail"]

        await service.restore_run("run-fail")
        assert {item.run_id for item in await service.recent_runs()} == {
            "run-fail", "run-ok"
        }
        assert await service.archived_runs() == []
    finally:
        await store.close()


async def test_restoring_an_auto_archived_record_brings_it_back(tmp_path) -> None:
    """从回收站恢复 → 它回到列表；列表暂时 11 条，下次产生新尝试时再收敛回 10。

    恢复刻意**不**顺手归档：否则用户恢复 A 就会看到 B 被塞进回收站 —— 他什么都没做
    却少了一条。宁可暂时 11 条，可见永远比「找不到」安全。
    """
    database = tmp_path / "production.sqlite3"
    service, store = await _overflow_service(tmp_path)
    try:
        await _seed_records(store, database, 13)
        await service.auto_archive_overflow()
        assert len(await _visible_records(store)) == 10

        target = next(
            item for item in await service.archived_runs()
            if item.record_id == "rec-01"
        )
        await service.restore_run(target.run_id)

        # 恢复的那条回到列表最前面；列表如实变成 11 条，没有别的记录被挤掉。
        recent_ids = [item.record_id for item in await service.recent_runs()]
        assert recent_ids[0] == "rec-01"
        assert len(recent_ids) == 11
        visible = await _visible_records(store)
        assert len(visible) == 11
        assert "rec-04" in visible, "恢复一条不该把别人挤进回收站"

        # 13 条记录：11 条可见 + 2 条在回收站，一条都没隐身。
        trash = sorted(item.record_id for item in await service.archived_runs())
        assert trash == ["rec-02", "rec-03"]
        assert set(visible) | set(trash) == {
            f"rec-{index:02d}" for index in range(1, 14)
        }

        # 下一次产生新尝试（领取）时收敛回 10 条。
        bitable = _OneTaskBitable(
            _task().model_copy(
                update={"record_id": "rec-new", "display_text": "新需求"}
            )
        )
        service._bitable = bitable
        run_id = await service.claim("rec-new", "animation")
        # 新记录还没结束（active=1，不算可见），领取时已经把 11 条收敛掉 2 条 → 9 条；
        # 它一结束就正好 10 条。
        assert len(await _visible_records(store)) == 9
        await store.release(
            run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="prime-local",
        )
        assert len(await _visible_records(store)) == 10
    finally:
        await store.close()


async def test_claim_archives_the_oldest_record_when_the_window_is_full(
    tmp_path,
) -> None:
    """触发点是**产生新尝试的写路径**：领取第 11 条时就把最老的归档掉。"""
    database = tmp_path / "production.sqlite3"
    bitable = _OneTaskBitable(
        _task().model_copy(
            update={"record_id": "rec-new", "display_text": "新需求"}
        )
    )
    service, store = await _overflow_service(tmp_path, bitable=bitable)
    try:
        await _seed_records(store, database, 10)
        assert len(await service.recent_runs()) == 10

        await service.claim("rec-new", "animation")

        assert "rec-01" not in {
            item.record_id for item in await service.recent_runs()
        }
        assert [
            item.record_id for item in await service.archived_runs()
        ] == ["rec-01"]
    finally:
        await store.close()


async def test_claim_survives_a_failing_archive_sweep(tmp_path) -> None:
    """归档扫不动也不能影响领取本身（best-effort）。"""
    database = tmp_path / "production.sqlite3"
    bitable = _OneTaskBitable(
        _task().model_copy(
            update={"record_id": "rec-new", "display_text": "新需求"}
        )
    )
    service, store = await _overflow_service(tmp_path, bitable=bitable)

    async def _boom(*args, **kwargs):
        raise RuntimeError("sweep failed")

    try:
        await _seed_records(store, database, 12)
        service.auto_archive_overflow = _boom  # type: ignore[assignment]
        run_id = await service.claim("rec-new", "animation")
        assert run_id
    finally:
        await store.close()

