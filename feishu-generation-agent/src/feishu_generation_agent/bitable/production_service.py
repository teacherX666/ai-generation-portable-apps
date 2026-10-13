import logging
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


_LOGGER = logging.getLogger(__name__)


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
#: 失败尝试现在也进窗口（见 store.list_recent），同样 10 条任务占用的行数变多了，
#: 所以从 500 提到 800，免得窗口被撑满后把老一点的尝试挤出滑条。
_RECENT_RUN_SCAN_LIMIT = 800

#: `runs` 表里这些状态的运行**正在跑**：对应的记录绝不自动归档。用户可能正盯着
#: 它。宁可列表暂时多于 10 条，也不能把在跑的任务藏进回收站。
_BUSY_RUNTIME_STATUSES = frozenset({"running", "resuming"})

# 这些绑定状态在 _ACTIVE_STATUSES 里算「活跃」，但其实是在**等人操作**
# （审批计划 / 审核成片），并没有真的在跑。此时复用它们会把用户静默甩回
# 那个卡住的页面——用户体感就是「点了重跑却立刻跳到审核页，什么都没做」
# （2026-09-16 生产事故：一条卡在待审批一天的任务挡掉了该记录所有重跑）。
_RERUN_BLOCKING_STATUSES = {
    TableTaskStatus.WAITING_APPROVAL,
    TableTaskStatus.REVIEWING,
}


def _group_recent_attempts(recent: list, active_record_ids: set[str]) -> dict[str, list]:
    """把 `list_recent` 的 run 行按 `record_id` 分组，保持「最近 → 最老」的顺序。

    字典的插入顺序就是任务的新旧顺序（`recent` 已按 `updated_at` 倒序），所以
    `list(grouped)` 前 `_RECENT_TASK_LIMIT` 个就是「最近 10 条任务」。
    「最近任务」列表与自动归档共用这一份规则，两边的窗口口径不会漂移。
    """
    grouped: dict[str, list] = {}
    for binding in recent:
        # 记录上还有一条在跑的新尝试时，旧的**中间态**历史行（生成中/待审批…）
        # 不往滑条里塞 —— 它们只是被打断的残影。但**失败**的尝试必须留下：
        # 用户正是在「刚重跑完、想知道上次为什么失败」的时候看这里
        # （2026-09-24）。以前只放行「已完成」，失败版一重跑就被吞掉。
        if (
            binding.record_id in active_record_ids
            and binding.status
            not in {TableTaskStatus.COMPLETED, TableTaskStatus.FAILED}
        ):
            continue
        grouped.setdefault(binding.record_id, []).append(binding)
    return grouped


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
            run_id = await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url,
                    trigger_type="production_bitable",
                    planning_prompt=planning_prompt,
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )
        # 新尝试已经真的产生了，这时候才按窗口归档（见 auto_archive_overflow）。
        await self._auto_archive_best_effort(owner_user_id, record_id)
        return run_id

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
        """最近的任务记录：**以任务为单位**，每条任务带上它的**全部**历次尝试
        （成片预览的历史滑条就靠这个）。

        以前是「取最近 10 条 run」——任务一多，同一条任务的历史就被挤出窗口，
        预览里只剩一两条。改成按记录分组后，任务内部的尝试不再被截断。

        **「只保留 10 条」现在由自动归档（`auto_archive_overflow`）负责，这里不再
        截断。** 以前这里 `list(grouped)[:10]` 是一道硬窗口：窗口外的记录既不在列表
        里、也不在回收站里，等于人间蒸发（2026-09-24 实测 33 条可见记录只显示 10 条，
        23 条谁也找不到 —— 用户最初的抱怨）。归档接管预算之后，再截断只会把「还没
        轮到归档」的记录重新藏起来（并发领好几条任务时尤其明显：领取时每条各归档
        一条，几条一起完成就会多出两三条）。
        宁可列表暂时多于 10 条（用户口径：「多余的进回收站」；任务作者口径：
        「宁可暂时多于 10 条」），也不能让任何一条记录既不在列表、又不在回收站。

        **失败的尝试也算一次尝试**：它们以前在 store 里被 `hidden=1` 挡掉，
        滑条上只剩成功版，用户看不到「失败过几次、为什么失败」。
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
        grouped = _group_recent_attempts(recent, active_record_ids)
        # recent 已按 updated_at 倒序 → 记录第一次出现的顺序就是任务的新旧顺序。
        # 每条记录内部的尝试同样是 updated_at 倒序（最新的一次在最前）。
        return await self._with_runtime_status(
            [binding for bindings in grouped.values() for binding in bindings],
            owner_user_id,
        )

    async def record_runs(
        self,
        *,
        record_id: str | None = None,
        run_id: str | None = None,
        owner_user_id: str = "prime-local",
    ) -> tuple[str, list]:
        """**某一条记录**的全部历次尝试（不受「最近任务」的窗口限制）。

        全局「最近任务」列表刻意保持窗口不变（最多 10 条任务 / 扫 800 条 run）——
        撑大它只会让 payload 变肥。但用户点开某条记录时要的是**它自己**的完整
        历史：老记录的失败尝试以前永远看不到（2026-09-24 实测 89 条失败尝试里
        45 条属于窗口外的老记录）。前端在打开某条运行时改调这里。

        只给 `run_id` 也能用：先按 run_id 找到它属于哪条记录，再取那条记录的
        全部尝试 —— 前端手上不一定有 record_id（运行详情里没有这个字段）。
        """
        location = await self._table_location()
        app_token = location.app_token or ""
        table_id = location.table_id
        if record_id is None:
            if run_id is None:
                raise RunValidationError("需要 record_id 或 run_id")
            binding = await self._store.get_by_run(
                run_id, owner_user_id=owner_user_id
            )
            if binding is None:
                raise RunNotFound("多维表格运行不存在")
            record_id = binding.record_id
        bindings = await self._store.list_record_runs(
            app_token,
            table_id,
            record_id,
            owner_user_id=owner_user_id,
        )
        return record_id, await self._with_runtime_status(
            bindings, owner_user_id
        )

    async def _with_runtime_status(
        self, bindings: list, owner_user_id: str
    ) -> list:
        """给每条绑定挂上**运行时**状态（cancelled / failed / succeeded…）。

        前端「失败原因」在拿不到具体错误时要能说清「这次是被取消的」—— 多维表格
        那边的 `status` 只有「失败」，cancelled 与 failed 区分不出来（2026-09-24
        用户：「有 2 条运行时状态是 cancelled，界面只说未记录失败原因」）。
        取不到运行时状态就原样返回，前端照旧显示「未记录失败原因」，绝不猜。
        """
        if not bindings:
            return bindings
        statuses = await self._runtime_statuses(owner_user_id)
        if not statuses:
            return bindings
        return [
            binding.model_copy(
                update={"runtime_status": statuses[binding.run_id]}
            )
            if binding.run_id in statuses
            else binding
            for binding in bindings
        ]

    async def _runtime_statuses(self, owner_user_id: str) -> dict[str, str]:
        """run_id → 运行时状态。取不到就返回空表（列表不该因此打不开）。"""
        repository = getattr(self._runtime, "repository", None)
        list_runs = getattr(repository, "list_runs", None)
        if not callable(list_runs):
            return {}
        try:
            rows = await list_runs(owner_user_id=owner_user_id)
        except TypeError:
            try:
                rows = await list_runs()
            except Exception:
                return {}
        except Exception:
            return {}
        statuses: dict[str, str] = {}
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            run_id = row.get("run_id")
            if run_id:
                statuses[str(run_id)] = str(row.get("status") or "")
        return statuses

    async def archived_runs(self, *, owner_user_id: str = "prime-local"):
        """回收站列表：**每条记录一行**。

        归档（手动删除与自动归档）都是按整条记录做的，所以一条记录的历次尝试会一起
        变成 `deleted = 1`。原样透传会让回收站里同一条记录出现好几行（名字一样、
        只是状态不同），用户根本分不清；去重放在 store 的 SQL 里做（必须在 LIMIT
        截断**之前**，否则多尝试的老记录会被挤出窗口）。恢复时整条记录一起回来。
        """
        location = await self._table_location()
        return await self._store.list_archived_records(
            location.app_token or "",
            location.table_id,
            owner_user_id=owner_user_id,
        )

    async def archive_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        """把这条运行**所属的整条记录**移进回收站。

        以前是按 run_id 只归档那一次尝试，于是「删了还在」：同一条记录的其它尝试
        仍然 `deleted = 0`，列表照旧显示这条记录（用户 2026-09-24 的原始抱怨之一）。
        现在与自动归档用同一粒度（整条记录），删一次就真的从列表消失、整体进回收站、
        恢复时也整体回来。
        """
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        updated = await self._store.archive_record(
            binding.source_location.app_token or "",
            binding.source_location.table_id,
            binding.record_id,
            owner_user_id=owner_user_id,
        )
        if updated == 0:
            raise RunNotFound("多维表格运行不存在")

    async def restore_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        """整条记录出回收站（回到列表）。

        **这里刻意不顺手归档**：恢复一条会让可见记录变成 11 条，列表如实显示 11 条
        （`recent_runs` 不再截断），等下一次产生新尝试时再按窗口收敛回 10 条。若在这
        里立刻归档，用户恢复 A 就会看到 B 被塞进回收站 —— 他没做任何事却少了一条。
        宁可暂时 11 条：可见永远比「找不到」安全。
        """
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        updated = await self._store.restore_record(
            binding.source_location.app_token or "",
            binding.source_location.table_id,
            binding.record_id,
            owner_user_id=owner_user_id,
        )
        if updated == 0:
            raise RunNotFound("多维表格运行不存在")

    async def auto_archive_overflow(
        self,
        *,
        owner_user_id: str = "prime-local",
        pinned_record_id: str | None = None,
    ) -> list[str]:
        """把「最近任务」窗口之外的老记录**整条**移进回收站（`deleted=1`，可恢复）。

        用户原话：「我不是只保留十个任务记录吗，多余的进回收站」。以前
        `_RECENT_TASK_LIMIT` 只是**展示窗口** —— 窗口外的记录既不在列表里、也不在
        回收站里，等于人间蒸发（实测 33 条可见记录只显示 10 条，剩下 23 条谁也找不到）。

        触发点是**写路径**：`claim` / `rerun`（产生新尝试）与 `restore_run`（恢复一条
        会把另一条挤出窗口）。不放读接口里 —— `GET /api/bitable/recent-runs` 这类读
        接口不该写库。

        `pinned_record_id` 是「必须保住的那条记录」，它会被算进窗口并排到最前：
        * 刚领取的新尝试 —— 它 `active = 1`，在 `list_recent` 里还看不见，但它**必然**
          会占掉最近 10 条的一个位置；算进来，领取第 11 条时就把最老的那条归档掉，
          列表不会先涨到 11 再回落。
        * 刚从回收站恢复的记录 —— 它刚被顶到最新，不能被自己触发的那次归档吃回去。

        跳过规则（宁可暂时多于 10 条，也不误伤）：
        * `runs` 表里状态是 `running` / `resuming` 的记录**绝不归档**；
        * 记录上还挂着 active 绑定的跳过；
        * 查不到运行时状态（仓储不可用）时**整体不归档** —— 判断不了谁在跑就不动；
        * 单条记录归档失败不影响其它记录，也不影响调用方。
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
        ordered = list(_group_recent_attempts(recent, active_record_ids))
        if pinned_record_id:
            ordered = [pinned_record_id] + [
                record_id
                for record_id in ordered
                if record_id != pinned_record_id
            ]
        overflow = ordered[_RECENT_TASK_LIMIT:]
        if not overflow:
            return []
        busy_record_ids = await self._busy_record_ids(owner_user_id)
        if busy_record_ids is None:
            _LOGGER.warning(
                "读不到运行时状态，跳过自动归档（宁可多于 %s 条也不误伤在跑的任务）",
                _RECENT_TASK_LIMIT,
            )
            return []
        archived: list[str] = []
        for record_id in overflow:
            if record_id in active_record_ids or record_id in busy_record_ids:
                continue
            try:
                updated = await self._store.archive_record(
                    app_token,
                    table_id,
                    record_id,
                    owner_user_id=owner_user_id,
                    auto=True,
                )
            except Exception:  # noqa: BLE001 - 归档失败不能拖垮领取/重跑
                _LOGGER.warning(
                    "自动归档记录失败 record=%s", record_id, exc_info=True
                )
                continue
            if updated:
                archived.append(record_id)
        return archived

    async def _busy_record_ids(self, owner_user_id: str) -> set[str] | None:
        """正在跑（`running` / `resuming`）的记录集合；`None` = 查不到。"""
        repository = getattr(self._runtime, "repository", None)
        list_runs = getattr(repository, "list_runs", None)
        if not callable(list_runs):
            return None
        try:
            rows = await list_runs(owner_user_id=owner_user_id)
        except TypeError:
            try:
                rows = await list_runs()
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        busy_run_ids = [
            str(row.get("run_id"))
            for row in rows or []
            if isinstance(row, Mapping)
            and row.get("run_id")
            and str(row.get("status") or "") in _BUSY_RUNTIME_STATUSES
        ]
        if not busy_run_ids:
            return set()
        return await self._store.record_ids_for_runs(busy_run_ids)

    async def _auto_archive_best_effort(
        self, owner_user_id: str, pinned_record_id: str | None
    ) -> None:
        """自动归档的调用壳：**任何**失败都不能影响领取 / 重跑 / 恢复本身。"""
        try:
            await self.auto_archive_overflow(
                owner_user_id=owner_user_id,
                pinned_record_id=pinned_record_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("自动归档超窗任务失败", exc_info=True)

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
        duration: int | None = None,
        auto_approve: bool = False,
    ) -> str:
        source = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if source is None:
            raise RunNotFound("多维表格运行不存在")
        # 「待确认成片」也允许重跑：成片已经生成完、正等人在审核页做决定，而
        # 审核页上的「重跑选中任务」正是这个状态下的主操作。以前只认「已完成/
        # 失败」，于是审核页点这个按钮**必然** 409（用户 2026-09-21 连点 14 次
        # 全失败，第 15 次才过 —— 因为那时绑定已被同步成「已完成」）。
        parked_in_review = source.status is TableTaskStatus.REVIEWING
        if source.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        } and not parked_in_review:
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
            if active_for_record.run_id != run_id:
                if active_for_record.status in _RERUN_BLOCKING_STATUSES:
                    raise RunConflict(
                        f"该记录上还有一条「{active_for_record.status}」的任务，"
                        "请先处理（审批通过或取消）它再重跑。"
                    )
                return active_for_record.run_id
            # 活动绑定就是源自己（只可能是上面那个「待确认成片」）：退回重跑要
            # 接管这条记录，先把旧绑定放掉，否则 claim() 会以「已被领取」拒绝。
            await self._store.release(
                run_id,
                status=active_for_record.status,
                owner_user_id=owner_user_id,
            )
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
                if duration is not None:
                    # ②-b（2026-10-10）：本次时长。与上一版计划不同时，克隆分支不会把
                    # 提示词原样保留（会置 blocking_issues 让人在审批页看到原因）。
                    clone_kwargs["duration"] = duration
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
            # 重跑是一次**新尝试**，同样按窗口归档（放在 try 外面：归档失败不该
            # 触发上面那段「重跑初始化失败」的回收）。
            await self._auto_archive_best_effort(owner_user_id, source.record_id)
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
            # 已经是同一个终态、并且已经放掉（active=0）的行，这次同步**没有任何
            # 需要落库的变化** —— 再 UPDATE 一次只会把 updated_at 刷成当前时间。
            # 那是纯读副作用：用户点开一条失败详情，它就被顶到「最近任务」最前面
            #（2026-09-24 实测 4 条失败尝试被读接口刷成 04:16:13，排到了最前）。
            # 状态真的推进了、或者行还占着记录（active=1），才必须写。
            if binding.status is released and not binding.active:
                return binding
            return await self._store.release(
                run_id,
                status=released,
                owner_user_id=binding.owner_user_id,
            )
        active = _ACTIVE_STATUSES.get(runtime_status)
        if active is None:
            raise RunConflict(f"无法同步运行状态：{runtime_status}")
        # 同理：状态没变、也没有遗留的错误要清掉时，不写库（读接口不该改
        # updated_at，更不该把任务记录的排序读一次动一次）。
        if binding.status is active and binding.last_error is None:
            return binding
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
