from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.document import (
    PlanningPromptSnapshot,
    RequirementRequest,
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.domain.plan import ApprovalDecision
from feishu_generation_agent.domain.production_bitable import ProductionTaskSummary
from feishu_generation_agent.graph.runtime import (
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


_RELEASED_STATUSES = {
    "succeeded": TableTaskStatus.COMPLETED,
    "completed_with_errors": TableTaskStatus.FAILED,
    "failed": TableTaskStatus.FAILED,
    "cancelled": TableTaskStatus.FAILED,
    "timed_out": TableTaskStatus.FAILED,
}
_ACTIVE_STATUSES = {
    "created": TableTaskStatus.PROCESSING,
    "running": TableTaskStatus.PROCESSING,
    "waiting_approval": TableTaskStatus.WAITING_APPROVAL,
    "waiting_review": TableTaskStatus.REVIEWING,
    "resuming": TableTaskStatus.GENERATING,
    "waiting_provider": TableTaskStatus.GENERATING,
    "delivering": TableTaskStatus.WRITING_BACK,
    "delivery_failed": TableTaskStatus.WRITEBACK_FAILED,
}
_SHARED_RESULT_TARGET = "__shared_production_result__"

#: 任务记录最多列几条**任务**（每条任务自带它的全部历次尝试）。
_RECENT_TASK_LIMIT = 10
#: 分组前先捞多少条 run —— 要足够大，才能把最近这些任务的历次尝试都捞全。
_RECENT_RUN_SCAN_LIMIT = 500

# 这些绑定状态在 _ACTIVE_STATUSES 里算「活跃」，但其实是在**等人操作**
# （审批计划 / 审核成片），并没有真的在跑。此时复用它们会把用户静默甩回
# 那个卡住的页面——用户体感就是「点了重跑却立刻跳到审核页，什么都没做」
# （2026-09-16 生产事故：一条卡在待审批一天的任务挡掉了该记录所有重跑）。
_RERUN_BLOCKING_STATUSES = {
    TableTaskStatus.WAITING_APPROVAL,
    TableTaskStatus.REVIEWING,
}


@dataclass(frozen=True, slots=True)
class ProductionTaskSource:
    location: BitableLocation
    expected_task_type: str
    # 图片类需求来自另一张表，那张表没有「需求类型」字段，无法靠字段值
    # 判定模式，因此把模式与类型声明在来源上。
    planning_mode: str = "video"
    declared_task_type: str = ""

    def matches_task_type(self, task_type: str) -> bool:
        """expected_task_type 留空表示本表所有行都算数。

        没有「需求类型」字段的表，行上的 task_type 恒为空，不能拿它过滤，
        否则会把整张表的记录全部滤掉。
        """
        if not self.expected_task_type:
            return True
        return task_type == self.expected_task_type


class ProductionBitableService:
    def __init__(
        self,
        *,
        bitable: Any,
        store: ProductionTaskStore,
        runtime: Any,
        sources: Mapping[str, ProductionTaskSource],
        include_completed_for_test: bool,
        enabled_task_types: frozenset[str] = frozenset({"动画类"}),
    ) -> None:
        self._bitable = bitable
        self._store = store
        self._runtime = runtime
        self._sources = dict(sources)
        self._include_completed_for_test = include_completed_for_test
        self._enabled_task_types = enabled_task_types
        self._schemas: dict[tuple[str, str], Any] = {}
        self._closed = False

    async def scan(self, category: str = "animation"):
        source = await self._prepared_source(category)
        schema_key = (
            source.location.app_token or "",
            source.location.table_id,
        )
        schema = self._schemas.get(schema_key)
        if schema is None:
            schema = await self._bitable.ensure_schema(source.location)
            self._schemas[schema_key] = schema
        tasks = await self._bitable.list_tasks(
            source.location,
            schema,
            include_completed=self._include_completed_for_test,
        )
        # 显示前先把绑定状态跟运行时对齐，否则徽章会一直停在领取那一刻的
        # 「处理中」，而任务其实早就到了待审批 —— 单条失败不影响整个列表。
        await self._reconcile_bindings(await self._store.list_active(*schema_key))
        claimed_by_record = {
            binding.record_id: binding
            for binding in await self._store.list_active(*schema_key)
        }
        return [
            self._stamp_claim(
                task,
                claimed_by_record.get(task.record_id),
            )
            for task in (
                self._stamp_declared_type(task, source) for task in tasks
            )
            if source.matches_task_type(task.task_type)
        ]

    @staticmethod
    def _stamp_claim(task, binding):
        """把「这条记录已被领取」挂到摘要上，而不是把记录整条滤掉。

        #1 之前是直接过滤掉已领取的记录，用户一点「开始分析」这条记录就从
        列表里消失，只能进审批页才找得回来。
        """
        if binding is None:
            return task
        return task.model_copy(
            update={
                "claim_status": binding.status,
                "claimed_run_id": binding.run_id,
            }
        )

    @staticmethod
    def _stamp_declared_type(task, source: ProductionTaskSource):
        """给没有「需求类型」字段的表补上来源声明的类型。

        图片需求来自另一张多维表格，那张表没有该字段，行上的 task_type
        恒为空。在这里补齐后，下游的交付白名单、binding 快照、规划模式
        判定全部走既有路径，无需为图片表另开分支。
        """
        if task.task_type or not source.declared_task_type:
            return task
        return task.model_copy(
            update={
                "task_type": source.declared_task_type,
                "snapshot": task.snapshot.model_copy(
                    update={"task_type": source.declared_task_type}
                ),
            }
        )

    async def claim(
        self,
        record_id: str,
        category: str = "animation",
        *,
        owner_user_id: str = "prime-local",
        planning_prompt: PlanningPromptSnapshot | None = None,
    ) -> str:
        source = await self._prepared_source(category)
        task = next(
            (item for item in await self.scan(category) if item.record_id == record_id),
            None,
        )
        if task is None or task.claimed_run_id is not None:
            raise RunConflict("该生产表记录当前不可领取")
        if task.task_type not in self._enabled_task_types:
            raise RunConflict(f"{task.task_type or '未分类'}任务暂未启用")
        binding = await self._store.claim(
            source.location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=owner_user_id,
        )
        if planning_prompt is None:
            planning_prompt = build_planning_prompt_snapshot(
                owner_user_id=owner_user_id,
                source="prime",
                version=0,
                prompt_text=planner_system_prompt(),
            )
        with self._runtime_owner_scope(owner_user_id):
            return await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url,
                    trigger_type="production_bitable",
                    planning_prompt=planning_prompt,
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )

    async def active_runs(self, *, owner_user_id: str = "prime-local"):
        location = await self._table_location()
        app_token = location.app_token or ""
        table_id = location.table_id
        bindings = await self._store.list_active(
            app_token,
            table_id,
            owner_user_id=owner_user_id,
        )
        # Reconcile persisted table bindings before displaying them.  A process
        # restart or an old cancellation can leave production_tasks at 待审批
        # while the runtime is already terminal; those rows otherwise render as
        # dead tasks whose buttons all return conflicts.
        await self._reconcile_bindings(bindings)
        return await self._store.list_active(
            app_token,
            table_id,
            owner_user_id=owner_user_id,
        )

    async def _reconcile_bindings(self, bindings) -> None:
        """把绑定状态跟运行时对齐；单条失败不能拖垮整个列表。"""
        for binding in bindings:
            try:
                await self.sync_once(
                    binding.run_id,
                    owner_user_id=binding.owner_user_id,
                )
            except RunNotFound:
                try:
                    await self._store.release(
                        binding.run_id,
                        status=TableTaskStatus.FAILED,
                        last_error="本地运行记录不存在",
                        owner_user_id=binding.owner_user_id,
                    )
                except Exception:
                    pass
            except Exception:
                # A transient runtime/provider error must not prevent the task
                # list from loading.  Keep the last persisted status for now.
                continue

    async def recent_runs(self, *, owner_user_id: str = "prime-local"):
        """最近的任务记录：**以任务为单位**，最多 `_RECENT_TASK_LIMIT` 条任务，
        每条任务带上它的**全部**历次尝试（成片预览的历史滑条就靠这个）。

        以前是「取最近 10 条 run」——任务一多，同一条任务的历史就被挤出窗口，
        预览里只剩一两条。改成按记录分组后，外面的条数仍是 10 条**任务**，
        任务内部的尝试不再被截断。
        """
        location = await self._table_location()
        app_token = location.app_token or ""
        table_id = location.table_id
        recent = await self._store.list_recent(
            app_token,
            table_id,
            owner_user_id=owner_user_id,
            limit=_RECENT_RUN_SCAN_LIMIT,
        )
        active_record_ids = {
            binding.record_id
            for binding in await self._store.list_active(
                app_token,
                table_id,
                owner_user_id=owner_user_id,
            )
        }
        grouped: dict[str, list] = {}
        for binding in recent:
            if (
                binding.record_id in active_record_ids
                and binding.status is not TableTaskStatus.COMPLETED
            ):
                continue
            grouped.setdefault(binding.record_id, []).append(binding)
        # recent 已按 updated_at 倒序 → 记录第一次出现的顺序就是任务的新旧顺序。
        return [
            binding
            for record_id in list(grouped)[:_RECENT_TASK_LIMIT]
            for binding in grouped[record_id]
        ]

    async def archived_runs(self, *, owner_user_id: str = "prime-local"):
        location = await self._table_location()
        return await self._store.list_archived(
            location.app_token or "",
            location.table_id,
            owner_user_id=owner_user_id,
        )

    async def archive_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        updated = await self._store.archive(
            run_id, owner_user_id=owner_user_id
        )
        if updated == 0:
            raise RunNotFound("多维表格运行不存在")

    async def restore_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        updated = await self._store.restore(
            run_id, owner_user_id=owner_user_id
        )
        if updated == 0:
            raise RunNotFound("多维表格运行不存在")

    async def result_table_url(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str | None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            return None
        target = await self._store.get_result_target(_SHARED_RESULT_TARGET)
        return target.url if target is not None else None

    async def is_production_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> bool:
        state = await self._store.run_owner_state(
            run_id, owner_user_id=owner_user_id
        )
        if state == "other":
            raise RunNotFound("多维表格运行不存在")
        return state == "owned"

    async def rerun(
        self,
        run_id: str,
        *,
        owner_user_id: str = "prime-local",
        task_ids: list[str] | None = None,
        feedback: str | None = None,
        auto_approve: bool = False,
    ) -> str:
        source = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if source is None:
            raise RunNotFound("多维表格运行不存在")
        if source.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            raise RunConflict("只有已经结束的多维表格任务可以重跑")
        if source.snapshot.task_type not in self._enabled_task_types:
            raise RunConflict(f"{source.snapshot.task_type or '未分类'}任务暂未启用")
        active_for_record = next(
            (
                binding
                for binding in await self._store.list_active(
                    source.source_location.app_token or "",
                    source.source_location.table_id,
                    owner_user_id=owner_user_id,
                )
                if binding.record_id == source.record_id
            ),
            None,
        )
        if active_for_record is not None:
            if active_for_record.status in _RERUN_BLOCKING_STATUSES:
                raise RunConflict(
                    f"该记录上还有一条「{active_for_record.status}」的任务，"
                    "请先处理（审批通过或取消）它再重跑。"
                )
            return active_for_record.run_id
        task = ProductionTaskSummary(
            record_id=source.record_id,
            display_text=source.display_text,
            source_url=source.source_url,
            progress=source.progress,
            task_type=source.snapshot.task_type,
            maker_open_id=source.maker_open_id,
            maker_name=source.maker_name,
            snapshot=source.snapshot,
        )
        rerun = await self._store.claim(
            source.source_location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=owner_user_id,
        )
        try:
            with self._runtime_owner_scope(owner_user_id):
                clone_kwargs = {
                    "run_id": rerun.run_id,
                    "thread_id": rerun.thread_id,
                }
                if task_ids is not None:
                    clone_kwargs["task_ids"] = task_ids
                if feedback is not None:
                    clone_kwargs["feedback"] = feedback
                new_run_id = await self._runtime.clone_run_for_approval(
                    run_id,
                    RequirementRequest(
                        source_url=rerun.source_url,
                        trigger_type="production_bitable",
                    ),
                    **clone_kwargs,
                )
                if auto_approve:
                    await self._runtime.resume_run(
                        new_run_id,
                        ApprovalDecision(
                            action="approve",
                            selected_task_ids=list(task_ids or []),
                        ),
                    )
                return new_run_id
        except Exception:
            await self._store.release(
                rerun.run_id,
                status=TableTaskStatus.FAILED,
                last_error="重跑初始化失败",
                owner_user_id=owner_user_id,
            )
            raise

    async def sync_once(
        self, run_id: str, *, owner_user_id: str | None = None
    ):
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        with self._runtime_owner_scope(binding.owner_user_id):
            view = await self._runtime.get_run_view(run_id)
        runtime_status = view.get("status")
        if not isinstance(runtime_status, str):
            raise RunConflict("运行状态无效")
        released = _RELEASED_STATUSES.get(runtime_status)
        if released is not None:
            return await self._store.release(
                run_id,
                status=released,
                owner_user_id=binding.owner_user_id,
            )
        active = _ACTIVE_STATUSES.get(runtime_status)
        if active is None:
            raise RunConflict(f"无法同步运行状态：{runtime_status}")
        return await self._store.set_status(
            run_id, active, owner_user_id=binding.owner_user_id
        )

    async def retry_delivery(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        if binding.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
            TableTaskStatus.WRITEBACK_FAILED,
        }:
            raise RunConflict("只有已生成完成的运行可以导出结果表")
        with self._runtime_owner_scope(owner_user_id):
            await self._runtime.retry_delivery(run_id)
        await self._store.set_status(
            run_id,
            TableTaskStatus.WRITING_BACK,
            owner_user_id=owner_user_id,
        )

    async def delete_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        with self._runtime_owner_scope(owner_user_id):
            await self._runtime.delete_run(run_id)
        if binding.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            await self._store.release(
                run_id,
                status=TableTaskStatus.FAILED,
                last_error="本地运行已删除",
                owner_user_id=owner_user_id,
            )

    async def resume_incomplete(self) -> list[str]:
        location = await self._table_location()
        # Startup recovery is intentionally cross-owner and never exposed by
        # a user-facing route.
        bindings = await self._store.list_active(
            location.app_token or "", location.table_id
        )
        for binding in bindings:
            with self._runtime_owner_scope(binding.owner_user_id):
                await self._runtime.start_run(
                    RequirementRequest(
                        source_url=binding.source_url,
                        trigger_type="production_bitable",
                    ),
                    run_id=binding.run_id,
                    thread_id=binding.thread_id,
                )
        await self._runtime.resume_pending_runs()
        return [binding.run_id for binding in bindings]

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._store.close()

    async def validate_approval(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        if binding.snapshot.task_type not in self._enabled_task_types:
            raise RunValidationError(f"{binding.snapshot.task_type or '未分类'}任务暂未启用")

    async def _prepared_source(self, category: str) -> ProductionTaskSource:
        if self._closed:
            raise RunConflict("多维表格服务正在关闭")
        source = self._sources.get(category)
        if source is None:
            label = {
                "animation": "动画类",
                "portrait": "真人类",
                "image": "图片类",
            }.get(category, category)
            raise RunValidationError(f"{label}视图尚未配置")
        if source.location.app_token is None:
            resolved = await self._bitable.resolve_location(source.location)
            source = ProductionTaskSource(resolved, source.expected_task_type)
            self._sources[category] = source
        return source

    async def _table_location(self) -> BitableLocation:
        return (await self._prepared_source("animation")).location

    def _runtime_owner_scope(self, owner_user_id: str):
        repository = getattr(self._runtime, "repository", None)
        owner_scope = getattr(repository, "owner_scope", None)
        if callable(owner_scope):
            return owner_scope(owner_user_id)
        return nullcontext()
