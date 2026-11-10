import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.runnables.config import set_config_context
from langgraph.types import Command
from langsmith import tracing_context
from pydantic import ValidationError

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    IngestIssueRecord,
    IngestIssueCode,
    IngestIssueSeverity,
    MediaAsset,
    NormalizedDocument,
    PlanningPromptSnapshot,
    RequirementRequest,
    VisionDescription,
    build_planning_prompt_snapshot,
    make_ingest_issue_record,
    resolve_ingest_issue_records,
)
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.ports import DeliveryWriter, DocumentSource, VisionAnalyzer
from feishu_generation_agent.domain.video_models import (
    DEFAULT_VIDEO_MODEL_KEY,
    normalize_video_task_payload,
    resolve_video_model_key,
)
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    ArtifactReviewDecision,
    AuditReport,
    GenerationTask,
    ImageReference,
    TaskPlan,
    reconcile_asset_coverage,
    reconcile_task_asset_coverage,
)
from feishu_generation_agent.domain.reference_contract import (
    ReferenceRemapError,
    canonicalize_references,
    dangling_reference_mentions,
    duration_consistency_issues,
    reference_tokens,
    refresh_duration_blocking_issues,
    remap_prompt_references,
)
from feishu_generation_agent.integrations.planner import (
    language_validation_message,
    planner_system_prompt,
    reconcile_negative_constraints,
    reconcile_storyboard_sources,
    validate_plan,
)
from feishu_generation_agent.integrations.planning_media import MediaUploadCache
from feishu_generation_agent.integrations.rework_prompt import (
    CARRIED_LOOKBACK_RUNS,
    CARRIED_REQUIREMENTS_LIMIT,
    build_rework_prompt,
    collect_rework_requirements,
    fusion_requirements,
    merge_negative_constraints,
    merge_requirements,
    rework_inputs,
)
from feishu_generation_agent.integrations.video_insight import (
    build_take_media_parts,
    describe_output_videos,
)
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository

from .nodes import _execution_units, approved_plan_from_state, base_task_id


_LOGGER = logging.getLogger(__name__)


def _read_watermark_cleanup(settings: Any, run_id: str) -> dict[str, Any]:
    """读参考图去水印的后台进度台账（读不到返回空 dict）。

    2026-10-20 用户口径：规划完先把界面显示出来、去水印在后台跑，**每张参考图要给前端反馈**。
    台账由后台任务实时写（`integrations/reference_watermark_clean.py`），这里只读。
    """
    try:
        from feishu_generation_agent.integrations.reference_watermark_clean import (
            read_cleanup_status,
        )

        return read_cleanup_status(run_id, Path(settings.outputs_dir))
    except Exception:  # noqa: BLE001
        return {}


def _adopted_cleanup_path(settings: Any, run_id: str, asset_id: str) -> Path | None:
    """这张参考图**当前该用哪个清洗版**；`None` = 用原图。

    2026-10-23 起判定交给 `reference_watermark_clean.effective_cleanup_path` —— 它会认
    **用户的选择**（`decision`）：`cleaned` = 用户看过图、决定用清洗版（**即使护栏判了
    `kept` 也照用**）；`original` = 用户决定用原图（即使护栏采用过也不用）；`auto` = 沿用
    护栏判定。预览与**提交**必须走同一个判据，否则就是「审批页看到的那版 ≠ 真正提交的那版」。

    ⚠️ **必须返回绝对路径**：台账里存的是 `data\\runs\\...-nowm.png` 这种**相对路径**
    （`asset.local_path` 本身就是相对的，清洗版就写在它旁边）。下面是拿它跟
    `data_dir`/`outputs_dir` 的**绝对**允许根做 `is_relative_to` 校验的 —— 相对路径永远
    判否 → `break` → 抛「参考素材不存在」→ 缩略图 404。

    用户 2026-10-20 现象就是这条：「我飞书模块去水印但是预览没了」——凡是
    **已采用清洗版**的参考图，预览全空白（1 张没检测到水印的照旧正常，所以看着像"部分丢了"）。
    """
    from feishu_generation_agent.integrations.reference_watermark_clean import (
        effective_cleanup_path,
    )

    entry = _read_watermark_cleanup(settings, run_id).get(asset_id) or {}
    return effective_cleanup_path(entry)


def _cleanup_version_path(
    settings: Any, run_id: str, asset_id: str, version: int
) -> Path | None:
    """台账里**第 N 版**清洗版的路径（前端"来回切历史版本"用）。

    ⚠️ **只认台账里记过的版本**：不接受调用方给任意路径 —— 否则就成了"拿 run 的权限
    去读盘上任何文件"。（用户 2026-10-23 口径：重洗要保留历史版本、能来回对比。）
    """
    from feishu_generation_agent.integrations.reference_watermark_clean import (
        resolve_cleaned_path,
    )

    entry = _read_watermark_cleanup(settings, run_id).get(asset_id) or {}
    for item in entry.get("versions") or []:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("version") or 0) != int(version):
                continue
        except (TypeError, ValueError):
            continue
        return resolve_cleaned_path(item.get("path"))
    return None


def _artifact_shot_meta(
    state: dict[str, Any],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """执行单元 task_id -> （展示位次, 镜头/序号信息）。

    成片卡片的顺序**必须跟分镜顺序一致**。`list_artifacts` 是按 artifact_id
    （sha256 哈希）排的，等于随机序 —— 用户分不清哪张卡是哪一镜，也就没法准确
    勾选要重跑的那几条（2026-09-23 反馈：「卡片顺序对不上分镜顺序，不知道该勾
    哪一条」）。这里按已批准计划的镜头顺序 + 产出序号给出稳定位次，并把镜头号、
    镜头总数、第几条/共几条、镜头标题一起交给前端做标注。
    """
    raw = (
        state.get("approved_plan")
        or state.get("draft_plan")
        or state.get("task_plan")
    )
    try:
        plan = TaskPlan.model_validate(raw)
    except (TypeError, ValueError):
        return {}, {}
    order: dict[str, int] = {}
    meta: dict[str, dict[str, Any]] = {}
    position = 0
    shot_total = len(plan.tasks)
    for shot_index, task in enumerate(plan.tasks, start=1):
        units = _execution_units(task)
        for slot_index, unit in enumerate(units, start=1):
            position += 1
            order[unit.task_id] = position
            meta[unit.task_id] = {
                "shot_index": shot_index,
                "shot_total": shot_total,
                "slot_index": slot_index,
                "slot_total": len(units),
                "task_title": task.title,
            }
    return order, meta


def plan_has_reference_video(plan: Any) -> bool:
    """计划里有没有**要提交给方舟的参考视频**（决定用哪一档单价）。

    判据取两处、只要命中一处就算（都是提交侧真正会用的引用）：
    * `tasks[].reference_plan[]` —— 规划器给每个素材的可用性判断，视频素材的 id 形如
      `video-1`，role 为 `video`；
    * `tasks[].reference_images[]` —— 提交时用的引用清单，role 为 `reference_video`
      （本地新上传的素材 id 形如 `upload-<hash>`，靠 role 才认得出来）。

    `usable=False` 的素材会被排除、不提交，所以不算数。
    """
    if not isinstance(plan, dict):
        return False
    for task in (plan.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        for asset in (task.get("reference_plan") or []):
            if isinstance(asset, dict) and asset.get("usable") is not False:
                if "video" in f"{asset.get('asset_id') or ''} {asset.get('role') or ''}".lower():
                    return True
        for ref in (task.get("reference_images") or []):
            if not isinstance(ref, dict):
                continue
            if "video" in f"{ref.get('asset_id') or ''} {ref.get('role') or ''}".lower():
                return True
    return False


class RunNotFound(LookupError):
    pass


class RunConflict(RuntimeError):
    pass


class RunValidationError(ValueError):
    pass


#: 「正文自述时长 ≠ 任务时长」阻断项的重算逻辑**已下沉到 domain 层**
#: （`domain.reference_contract.refresh_duration_blocking_issues`）——
#: 2026-10-21：它必须挂在**所有合并路径**共用的
#: `plan.reconcile_task_asset_coverage` 上，否则客户端回传的过期阻断项
#: 会在 `human_approval` 节点里把用户改对的计划重新判死。
#: 这里保留同名再导出，历史调用点（patch_task / _validate_decision / 测试）不受影响。
class GraphRuntime:
    _RECOVERABLE_STATUSES = frozenset(
        {"created", "running", "resuming", "waiting_provider", "delivering"}
    )
    _TERMINAL_STATUSES = frozenset(
        {
            "succeeded",
            "completed_with_errors",
            "delivery_failed",
            "failed",
            "cancelled",
        }
    )
    _CANCELLABLE_STATUSES = frozenset(
        {
            "created", "running", "resuming",
            "waiting_provider", "delivering",
            "waiting_approval", "waiting_review",
        }
    )
    #: operations.phase 里「这个执行单元已经有结果了」的取值（成功或彻底失败都算
    #: 完成 —— 进度条要能走到头，不能卡在 N-1/N）。
    _FINISHED_OPERATION_PHASES = frozenset(
        {
            "succeeded",
            "completed",
            "success",
            "failed",
            "cancelled",
            "expired",
            "timed_out",
            "submission_uncertain",
        }
    )

    def __init__(
        self,
        *,
        graph: Any,
        repository: Repository,
        file_store: FileStore,
        settings: Settings,
        delivery_writer: DeliveryWriter | None = None,
        document_source: DocumentSource | None = None,
        vision_analyzer: VisionAnalyzer | None = None,
        rework_fuser: Any | None = None,
        #: 只重生成某一段分镜图的回调（bootstrap 注入，运行时没有图片生成器）。
        storyboard_regenerator: Any | None = None,
        video_analyzer: Any | None = None,
        #: 公开图床：把成片传成 https 链接，才能作为 video_url 附进多模态请求。
        public_media_host: Any | None = None,
        #: 清洗范围探针（2026-10-28）：重洗前先确认"洗哪儿、哪儿不许动"。
        cleanup_scope_probe: Any | None = None,
    ) -> None:
        self.graph = graph
        self.repository = repository
        self.file_store = file_store
        self.settings = settings
        self.delivery_writer = delivery_writer
        self.document_source = document_source
        self.vision_analyzer = vision_analyzer
        # AI 融合器（`planner.fuse_rework_prompt`）。缺省 None 时重跑走安全拼接，
        # 与图节点共用同一套语义——两条重跑路径不允许再各写一份。
        self.rework_fuser = rework_fuser
        self._storyboard_regenerator = storyboard_regenerator
        # 能**直接看视频**的分析器（ds4.1 多模态）：返工时把上一版成片送进去，
        # 让融合模型看到实际画面（不抽帧）。
        self.video_analyzer = video_analyzer
        self.public_media_host = public_media_host
        # 清洗范围探针（清洗前确认范围）—— 重洗路径也走它，见 `reclean_reference`。
        self.cleanup_scope_probe = cleanup_scope_probe
        # 成片公开链接缓存：同一条成片只上传一次（sha256 缺失时用 artifact_id 顶）。
        self._take_media_cache = MediaUploadCache(
            Path(settings.data_dir) / "take-media-links.json"
        )
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        self._closed = False

    async def _take_media_parts(
        self, artifacts: list[Any] | None
    ) -> list[dict[str, Any]]:
        """上一版成片的视频 content parts；拿不到就返回空（调用方回落文字看片）。

        用户 2026-09-28：「把调用改成一次多模态调用就行了，不要纯文本」。
        """
        if self.video_analyzer is None or self.public_media_host is None:
            return []
        return await build_take_media_parts(
            artifacts,
            public_media_host=self.public_media_host,
            cache=self._take_media_cache,
        )

    async def _previous_take_artifacts(
        self, *, source_url: str, exclude_run_id: str
    ) -> list[Any]:
        """同一条需求**上一个跑完的**运行的成片。

        审批页「AI 改写提示词」时，当前 run 还没生成（artifacts=0），上一版成片在
        那个已完成的 run 里 —— 不把它取出来，融合就只能凭文字改词（这正是用户
        2026-09-28 那段病句的来源：那条路完全不看视频）。
        """
        if not source_url:
            return []
        succeeded = {"succeeded", "completed", "success"}
        try:
            rows = await self.repository.list_runs()
        except Exception:  # noqa: BLE001 —— 查不到就当没有上一版
            _LOGGER.warning("查找上一版成片失败 source_url=%s", source_url, exc_info=True)
            return []
        candidates = [
            row
            for row in rows
            if str(row.get("source_url") or "") == source_url
            and str(row.get("run_id") or "") != exclude_run_id
            and str(row.get("status") or "").lower() in succeeded
        ]
        candidates.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        for row in candidates:
            try:
                snapshot = await self.graph.aget_state(
                    self._config(str(row.get("thread_id") or ""))
                )
            except Exception:  # noqa: BLE001 —— 单个 run 读不到就试下一个
                continue
            artifacts = list((getattr(snapshot, "values", None) or {}).get("artifacts") or [])
            if artifacts:
                return artifacts
        return []

    def _remember_requirements(self, text: str, *, doc: str = "") -> None:
        """把用户这次说的话**自动沉淀**成长期口径（2026-10-13 用户：「没有记忆功能吗」）。

        为什么要有：规划器每次规划都是一次性的，用户反馈给「铲」的口径不会带到
        「钱袋子」—— 于是他同一类问题要反复说。这里在他每次提要求时就记下来，
        同一件事说第二次自动升为长期口径，之后**每次规划都会带上**（见
        `nodes._knowledge_context_for_plan`）。

        写**共享桶**：规划跑在后台任务里时归属人可能是空的，写个人桶＝记了没人读。
        任何失败都只记日志 —— 记忆是加分项，绝不能让这次返工失败。
        """
        try:
            from feishu_generation_agent.integrations.user_rules import (
                UserRuleStore,
                split_requirement_items,
            )

            items = split_requirement_items(text)
            if not items:
                return
            store = UserRuleStore(Path(self.settings.data_dir) / "user_rules.json")
            for item in items:
                store.record("", item, source="rework", doc=doc, shared=True)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("沉淀用户长期口径失败", exc_info=True)

    async def _carried_rework_requirements(
        self,
        *,
        source_url: str,
        exclude_run_id: str,
    ) -> list[str]:
        """同一条需求**历次运行**累积的返工要求（整篇重新规划时要继承，别清零）。

        2026-10-10「铲」任务实测：用户点「重新生成计划」→ 新 run 从
        `ingest_source → plan_requirements` 重跑，`_history_context_for_plan` 只能从
        **本 run 自己**的 `draft_plan` / `approved_plan` 取历史 —— 新 run 两条都是空的，
        于是上一轮攒下的「蜡是比较硬化状态的」被彻底丢掉，用户两小时后不得不再说一遍
        「蜡的质感是明显硬化的」。这条链路本身没问题（同 run 退回修改时是有效的），
        缺的是**跨 run** 这一段。

        查不到就当没有：继承历史绝不能让新 run 起不来。
        """
        if not source_url:
            return []
        try:
            rows = await self.repository.list_runs()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "继承返工要求时查不到运行列表 source_url=%s", source_url, exc_info=True
            )
            return []
        candidates = [
            row
            for row in rows
            if str(row.get("source_url") or "") == source_url
            and str(row.get("run_id") or "") != exclude_run_id
        ]
        # 由新到旧取最近若干条，再翻成由旧到新 —— 截断时才留得住最近的 requirement。
        candidates.sort(
            key=lambda row: str(row.get("created_at") or ""), reverse=True
        )
        candidates = candidates[:CARRIED_LOOKBACK_RUNS]
        candidates.reverse()
        states: list[dict[str, Any]] = []
        for row in candidates:
            thread_id = str(row.get("thread_id") or "")
            if not thread_id:
                continue
            try:
                snapshot = await self.graph.aget_state(self._config(thread_id))
            except Exception:  # noqa: BLE001 —— 单条读不到就跳过
                continue
            values = getattr(snapshot, "values", None) or {}
            if values:
                states.append(dict(values))
        return collect_rework_requirements(
            states, limit=CARRIED_REQUIREMENTS_LIMIT
        )

    def _start_background(self, coroutine: Any, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def start_run(
        self,
        request: RequirementRequest,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        if self._closed:
            raise RunConflict("运行时正在关闭")
        reserved_ids = run_id is not None and thread_id is not None
        run_id = run_id or str(uuid4())
        thread_id = thread_id or str(uuid4())
        async with self._start_lock:
            existing = await self.repository.get_run(run_id)
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id
                    or existing["source_url"] != request.source_url
                ):
                    raise RunConflict("预留的运行 ID 与现有运行不一致")
                snapshot = await self.graph.aget_state(
                    self._config(thread_id)
                )
                state = dict(snapshot.values or {})
                if state and self._planning_prompt_from_state(state) is None:
                    await self._fail_missing_planning_prompt(run_id)
                    return run_id
                if not state and request.planning_prompt is None:
                    await self._fail_missing_planning_prompt(run_id)
                    return run_id
                approval_name = f"approval-run-{run_id}"
                if (
                    existing["status"] in {"created", "running"}
                    and not self._has_background(approval_name)
                ):
                    if not state:
                        self._start_background(
                            self._run_to_approval(run_id, thread_id, request),
                            name=approval_name,
                        )
                return run_id
            if (
                reserved_ids
                and request.trigger_type != "local_link"
                and request.planning_prompt is None
            ):
                snapshot = await self.graph.aget_state(
                    self._config(thread_id)
                )
                state = dict(snapshot.values or {})
                planning_prompt = self._planning_prompt_from_state(state)
                if planning_prompt is None:
                    raise RunConflict("无法恢复运行：提示词快照不存在")
                if (
                    state.get("run_id") != run_id
                    or state.get("thread_id") != thread_id
                    or state.get("source_url") != request.source_url
                ):
                    raise RunConflict("恢复运行与 checkpoint 不一致")
                await self.repository.create_run(
                    run_id,
                    thread_id,
                    request.source_url,
                    status=self._safe_status(state.get("status"), "failed"),
                    owner_user_id=planning_prompt.owner_user_id,
                )
                return run_id
            await self.repository.create_run(
                run_id,
                thread_id,
                request.source_url,
                status="created",
            )
            if request.planning_prompt is None:
                request = request.model_copy(
                    update={
                        "planning_prompt": build_planning_prompt_snapshot(
                            owner_user_id="prime-local",
                            source="prime",
                            version=0,
                            prompt_text=planner_system_prompt(),
                        )
                    }
                )
            self._start_background(
                self._run_to_approval(run_id, thread_id, request),
                name=f"approval-run-{run_id}",
            )
        return run_id

    async def clone_run_for_approval(
        self,
        source_run_id: str,
        request: RequirementRequest,
        *,
        run_id: str,
        thread_id: str,
        task_ids: list[str] | None = None,
        feedback: str | None = None,
        duration: int | None = None,
    ) -> str:
        """Create a rerun from a prior run.

        Reuse the approved plan when one exists. Runs cancelled before approval do
        not have an approved plan, so re-read and re-plan the original document
        instead of exposing a rerun action that can only fail.

        `duration`（2026-10-10，②-b）：本次重跑要用的时长。给了它且与被复用计划的
        时长不同时，**不允许**把上一版提示词原样保留 —— 见下面 `_reuse_guard`。
        """
        if self._closed:
            raise RunConflict("运行时正在关闭")
        async with self._start_lock:
            source = await self.repository.get_run(source_run_id)
            if source is None:
                raise RunNotFound("原运行不存在")
            if source["source_url"] != request.source_url:
                raise RunConflict("重跑来源与原运行不一致")
            existing = await self.repository.get_run(run_id)
            if existing is not None:
                raise RunConflict("重跑运行 ID 已存在")
            snapshot = await self.graph.aget_state(self._config(source["thread_id"]))
            source_state = dict(snapshot.values or {})
            planning_prompt = self._planning_prompt_from_state(source_state)
            if planning_prompt is None:
                raise RunValidationError("原运行缺少有效提示词快照")
            try:
                approved_plan = approved_plan_from_state(
                    source_state,
                    max_output_count=self.settings.max_output_count,
                )
            except (TypeError, ValueError):
                approved_plan = None
            # Preserve a plan that was already generated before the source run
            # was cancelled/failed——only in terminated states. A source run still
            # waiting_approval keeps the original semantics: cloning it means
            # "重新生成计划"（fresh analysis, rerun_source event）.
            if approved_plan is None or not approved_plan.tasks:
                source_status = str(source.get("status") or "").lower()
                if source_status in {"cancelled", "canceled", "failed", "error"}:
                    try:
                        draft_plan = TaskPlan.model_validate(
                            source_state.get("draft_plan") or source_state.get("task_plan")
                        )
                    except (TypeError, ValueError):
                        draft_plan = None
                    if draft_plan is not None and draft_plan.tasks:
                        approved_plan = draft_plan
            if approved_plan is None or not approved_plan.tasks:
                await self.repository.create_run(
                    run_id,
                    thread_id,
                    request.source_url,
                    status="created",
                    owner_user_id=planning_prompt.owner_user_id,
                )
                fresh_request = request.model_copy(
                    update={"planning_prompt": planning_prompt}
                )
                await self.repository.append_event(
                    run_id,
                    "rerun_source",
                    "started",
                    "Original document queued for fresh analysis",
                )
                self._start_background(
                    self._run_to_approval(run_id, thread_id, fresh_request),
                    name=f"approval-run-{run_id}",
                )
                return run_id
            selected_task_ids = list(dict.fromkeys(task_ids or []))
            if selected_task_ids:
                known_task_ids = {task.task_id for task in approved_plan.tasks}
                # 成片审核页是按**每条成片**勾选的，传进来的是 artifact 级 id
                # （形如 task-1::output:2）。以前直接拿它跟计划里的 task_id 比，
                # 选中「一个镜头的第 2 条产出」必然报「重跑任务不存在」
                #（2026-09-23 反馈：多条视频时选不中要重跑的那条）。
                selected_task_ids = list(
                    dict.fromkeys(
                        base_task_id(task_id) for task_id in selected_task_ids
                    )
                )
                unknown_task_ids = set(selected_task_ids) - known_task_ids
                if unknown_task_ids:
                    raise RunValidationError(
                        "\u91cd\u8dd1\u4efb\u52a1\u4e0d\u5b58\u5728\uff1a"
                        + "\u3001".join(sorted(unknown_task_ids))
                    )
                selected_task_id_set = set(selected_task_ids)
            else:
                # 「重新运行」不带勾选：返工要求要作用在**整份计划**上。
                # 以前这段整体包在 `if selected_task_ids:` 里，于是
                # `POST /rerun` 带返工要求时会被静默忽略 —— 用户看到提示词一个字
                # 没变（2026-09-24）。
                selected_task_id_set = {task.task_id for task in approved_plan.tasks}
            feedback_text = (feedback or "").strip()
            # 用户这次说的话先**自动沉淀**成长期口径（同一件事说第二次就长期生效）。
            # 放在前面：哪怕后面没走成返工，他说过的话也已经记住了。
            if feedback_text:
                self._remember_requirements(feedback_text)
            # 正文到底有没有被改动（下面写事件时要如实回报）。
            # 2026-10-10「铲」任务：融合只往声音段塞了 4 个字、画面段一字未动，
            # 事件却说「已按本次返工要求改写提示词」—— 用户以为改了、其实没改。
            rewrote_prompt = False
            #: 本次返工里「融合没生效」的真实原因（超长被拒 / 上游报错 / 模型原样返回），
            #: 写事件时如实转述（2026-11-05）。
            last_fusion_reason: str | None = None
            # 只有「有返工要求」才改写提示词；要求为空 = **一个字都不改**
            # （不调融合模型、不产生费用），只把已批准计划原样搬进待审批。
            if feedback_text and selected_task_id_set:
                updated_tasks = []
                # 上一版成片只送模型看**一次**：以前这段在逐任务循环里，
                # 选 3 条就白调 3 次视频理解模型、还写 3 条一模一样的
                # clone_approved_plan 事件（生产 run 80fa3a3f / b05c54d2 实锤）。
                # 一次多模态调用优先（用户 2026-09-28）：把上一版成片**视频**直接附进
                # 融合请求，模型自己看片 —— 不再先跑一遍文本看片再让融合去读转述。
                artifacts = list(source_state.get("artifacts") or [])
                media_parts = await self._take_media_parts(artifacts)
                if media_parts:
                    visual_context = ""
                    event_text = (
                        "已把上一版成片作为视频附进融合请求（一次多模态调用）"
                    )
                else:
                    # 回落：成片传不上去（没图床 / 上传失败 / 产物缺失）时仍走文字看片。
                    visual_context = await describe_output_videos(
                        self.video_analyzer,
                        artifacts,
                        # 2026-09-28：看片必须拿到**上一版正文**才能逐条核对
                        # 「要求 X，实际 Y」。以前一个字都没传，模型只能描述画面，
                        # 融合拿不到差距 → 要么原样退回、要么自己发明问题。
                        prompts={
                            task.task_id: task.prompt
                            for task in approved_plan.tasks
                        },
                    )
                    text = " ".join((visual_context or "").split())
                    event_text = (
                        f"已让模型看过上一版成片，融合时带上画面：{text[:600]}"
                        if text
                        else "本次返工没带上一版画面（没有成片文件，或看片失败）"
                    )
                # 把"模型看了什么"记进事件，否则用户看不出返工到底有没有看片
                #（用户 2026-09-18：「这样看不出问题啊」）。
                try:
                    await self.repository.append_event(
                        run_id, "clone_approved_plan", "running", event_text
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "记录画面上下文事件失败 run=%s", run_id, exc_info=True
                    )
                for task in approved_plan.tasks:
                    if task.task_id not in selected_task_id_set:
                        updated_tasks.append(task)
                        continue
                    # 与图节点 review_artifacts 共用同一套语义：历次返工要求
                    # 只累积不覆盖、优先 AI 融合、永不因超长失败。
                    base_prompt, requirements = rework_inputs(task, feedback_text)
                    # 融合为什么没生效（超长被拒 / 上游报错 / 模型原样返回）由它带回，
                    # 下面写事件时如实转述 —— 以前只能写「一致或不合契约」，用户
                    # 看到的是「没给我改提示词」却说不出原因（2026-11-05 MM day16 视频3）。
                    fusion_diagnostics: dict[str, Any] = {}
                    prompt, _truncated, _must_avoid = await build_rework_prompt(
                        base_prompt,
                        # 只喂**本轮**要求；`requirements`（累积）仍写回任务做记录。
                        fusion_requirements(feedback_text),
                        fuse=self.rework_fuser,
                        visual_context=visual_context,
                        media_parts=media_parts,
                        diagnostics=fusion_diagnostics,
                    )
                    # 与图节点 `review_artifacts` 的判定口径保持一致（只差空白 = 没改）。
                    if prompt.strip() != (base_prompt or "").strip():
                        rewrote_prompt = True
                        last_fusion_reason = None
                    else:
                        last_fusion_reason = str(
                            fusion_diagnostics.get("rejection_reason") or ""
                        ).strip() or "原因未记录"
                    updated_tasks.append(
                        task.model_copy(
                            update={
                                "prompt": prompt,
                                "rework_requirements": requirements,
                                # 冻结锚点只写第一次；融合基准/改前是 previous。
                                "rework_base_prompt": (
                                    task.rework_base_prompt or base_prompt
                                ),
                                # 展示用的「改前」是这一版被返工前的样子。
                                "rework_previous_prompt": task.prompt,
                                # **不往约束里加东西**（用户口径 2026-09-17）：要求由融合
                                # 写进正文；克隆按契约逐字保留计划，存量的合并+限量
                                # 清理在「编辑落盘」与「提交」两处生效。
                            }
                        )
                    )
                approved_plan = approved_plan.model_copy(
                    update={"tasks": updated_tasks}
                )
            # ②-b（2026-10-10，P0）：复用提示词的**守卫** —— 不许把一个与本次时长
            # 不一致、或锚点已经空洞的旧提示词原样搬进新 run。
            #
            # 为什么必须在这里而不是只在审批页兜：①「点返工重做但不填返工要求」走的就是
            # 下面这条「一个字都不改」的快路径，会把**修复前**的旧提示词（含 @视频N
            # 空洞、含上一版改错的时长表述）逐字节搬回来；②用户拿到的是一个已经停在
            # 审批页的 run，如果只在提交时拦，他会以为「重跑成功了」。
            #
            # 处置方式选的是**置 blocking_issues 拦住**（另一条路是"按新时长重写时间轴"）：
            #   - 机械按比例缩放秒数会写出 `0.0-0.62s` 这种没法用的时间轴，而"动作怎么
            #     重新分配"需要理解内容，只能交给模型；
            #   - 强制多调一次融合模型与产品口径冲突（用户 2026-09-24 明确"没内容就
            #     一个字都不改、不产生费用"）；
            #   - 拦住时给的是**人话 + 出口**：点「重新运行」并填写返工要求，融合链路
            #     会按新时长重写时间轴与全部时长表述。
            guard_reasons, guard_warnings = self._reuse_guard_reasons(
                approved_plan,
                target_duration=duration,
                # checkpoint 里的 media_assets 是 **dict**，不是 MediaAsset 对象。
                mime_types={
                    str(asset.get("asset_id")): str(asset.get("mime_type") or "")
                    for asset in (
                        source_state.get("media_assets")
                        if isinstance(source_state.get("media_assets"), list)
                        else []
                    )
                    if isinstance(asset, dict) and asset.get("asset_id")
                },
            )
            if guard_reasons or guard_warnings:
                guarded_tasks = []
                for task in approved_plan.tasks:
                    reasons = (guard_reasons or {}).get(task.task_id) or []
                    warns = (guard_warnings or {}).get(task.task_id) or []
                    if not reasons and not warns:
                        guarded_tasks.append(task)
                        continue
                    updates: dict[str, Any] = {}
                    if reasons:
                        updates["blocking_issues"] = list(
                            dict.fromkeys([*task.blocking_issues, *reasons])
                        )
                    if warns:
                        # 格式类的提示只进 warnings：审批页看得见，但不挡路
                        #（2026-10-30：用户明确要求这类阻塞删掉）。
                        updates["warnings"] = list(
                            dict.fromkeys([*task.warnings, *warns])
                        )
                    guarded_tasks.append(task.model_copy(update=updates))
                approved_plan = approved_plan.model_copy(
                    update={"tasks": guarded_tasks}
                )
            # 分镜行 id 确定性补齐：复制来的计划没有经过模型，缺的行会一直缺
            # （实测 2026-09-17 超级大床：重跑多少次都报同样 3 个 id）。
            resolved_document = self._document_assets(
                source_state.get("normalized_document"),
                source_state.get("media_assets")
                if isinstance(source_state.get("media_assets"), list)
                else [],
            )
            if resolved_document is not None:
                try:
                    approved_plan = reconcile_storyboard_sources(
                        approved_plan,
                        NormalizedDocument.model_validate(resolved_document),
                    )
                except Exception:
                    pass
            approved_plan_json = approved_plan.model_dump(mode="json")
            approved_tasks = [
                task.model_dump(mode="json") for task in approved_plan.tasks
            ]
            if selected_task_ids:
                selected_task_id_set = set(selected_task_ids)
                approved_tasks = [
                    task for task in approved_tasks
                    if task["task_id"] in selected_task_id_set
                ]

            state = deepcopy(source_state)
            state.update(
                run_id=run_id,
                thread_id=thread_id,
                source_url=request.source_url,
                status="waiting_approval",
                draft_plan=deepcopy(approved_plan_json),
                task_plan=deepcopy(approved_plan_json),
                approval_decision=None,
                approval_revision=None,
                approved_tasks=deepcopy(approved_tasks),
                approved_plan=None,
                execution_records=[],
                artifacts=[],
                delivery_record=None,
                last_error=None,
                artifact_review_decision=None,
                artifact_review_feedback=None,
            )
            # 分镜候选图必须跟着一起搬，否则复制来的计划里那些已写进
            # reference_images 的关键帧在新 run 下解析不到（预览 404、生成报
            # 「图片引用无效」）。
            source_storyboards = source_state.get("storyboard_assets")
            state["storyboard_assets"] = (
                deepcopy(source_storyboards)
                if isinstance(source_storyboards, list)
                else []
            )
            state.pop("error", None)
            await self.repository.create_run(
                run_id, thread_id, request.source_url, status="created"
            )
            # **素材不变量**（2026-10-28 用户口径：「素材变了是绝对不允许的」）：
            # 重跑复用上一版计划时**不重新入库**，于是新 run 的清洗台账是空的 ——
            # 而预览与提交都只看**本 run 的台账**，结果审批页会把素材显示成
            # **带水印的原图**（用户看到的"素材又变了"就是这个）。
            # 直接把源运行的台账整份继承过来：清洗版文件在文档级 inputs 目录里，路径照样有效。
            await self._inherit_cleanup_ledger(source_run_id, run_id)
            config = self._config(thread_id)
            try:
                await self._graph_aupdate_state(
                    config, state, as_node="validate_plan"
                )
                result = await self._graph_ainvoke(None, config=config)
            except Exception:
                await self.repository.update_run_status(run_id, "failed")
                raise RunConflict("重跑审批 checkpoint 初始化失败") from None
            if not self._has_interrupt(result):
                await self.repository.update_run_status(run_id, "failed")
                raise RunConflict("重跑未进入等待审批状态")
            # 用户口径 2026-09-24：有返工要求 → 按融合结果改写提示词；没内容 →
            # 一个字都不改。事件里如实写清是哪种，否则审批页上分不出「模型没改」
            # 和「根本没送模型」。
            if rewrote_prompt:
                clone_summary = "已复用上一版已批准计划，并按本次返工要求改写提示词"
            elif not feedback_text:
                clone_summary = "已复用上一版已批准计划，未填返工要求，提示词原样保留"
            else:
                # 2026-10-10（「铲」任务）：送了模型、但**正文一个字没改**（融合结果与
                # 原文一致，或被落点契约拦下后回退）。必须如实说 —— 否则用户会以为
                # 「提示词改了、画面却没变」，把力气白花在一轮轮重跑上。
                # 2026-11-05（MM day16 视频3）：还要带上**真实原因**。那次的真相是
                # 「4 次改写都只超 19~38 字被整条拒掉」，而这里只写「与原文一致或
                # 不合契约」，用户看到的就是「没给我改提示词」却说不出为什么。
                clone_summary = (
                    "已复用上一版已批准计划，但本次返工没有改变提示词正文"
                    "（"
                    + (last_fusion_reason or "融合结果与原文一致或不合契约")
                    + "，已按原样保留）"
                )
            await self.repository.append_event(
                run_id,
                "clone_approved_plan",
                "completed",
                clone_summary,
            )
            if guard_reasons:
                # 如实说明「原样保留」这条路上有哪几条被守卫拦下了，否则用户在审批页
                # 看到红色阻塞却不知道是这一轮加进去的。
                blocked = "；".join(
                    f"{task_id}: {reason}"
                    for task_id, reasons in guard_reasons.items()
                    for reason in reasons
                )
                await self.repository.append_event(
                    run_id,
                    "clone_approved_plan",
                    "blocked",
                    "复用上一版提示词被拦下（提示词与本次参数不符）：" + blocked,
                )
            await self.repository.append_event(
                run_id,
                "clone_approved_plan",
                "waiting",
                "已进入待审批：请核对提示词/参数后再点「批准并开始生成」",
            )
            await self.repository.update_run_status(run_id, "waiting_approval")
        return run_id

    def _reuse_guard_reasons(
        self,
        plan: TaskPlan,
        *,
        target_duration: int | None,
        mime_types: Mapping[str, str],
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """②-b：复用上一版计划时，哪些任务**不能原样保留**（task_id → 人话原因）。

        返回 `(要拦住审批的, 只提示不拦的)` 两份；四类判据：

        1. **本次时长 ≠ 被克隆计划的时长**（调用方显式给了 `target_duration` 时）：
           提示词里的时间轴与「预计 N 秒」全是按旧时长写的，原样保留必然对不上
           —— 实测后果是成片前 4.08 秒画面完全冻结（REPORT_YARN §4.1）。
        2. **提示词正文自述时长 ≠ 本任务时长**：上一版提交时就带着这个缺陷（r5 的
           形态：时长 8、正文「预计5秒」），原样搬到新 run 只是把缺陷往前传。
        3. **锚点空洞**：提示词里出现了 `@视频N` / `@音频N` / 超出范围的 `@图片N`，
           而本计划的 `reference_images` 里没有对应素材（上一版修复时被摘掉了），
           或者已经留下「参考  中…」这种被削掉主语的空洞。用户已知口径：点返工不填
           要求会把修复前的旧提示词搬回来、带出 `@视频N` 空洞。

        ⚠️ 第 3、4 类（锚点空洞 / 悬空引用）**只进 warnings，不再拦住审批**
        （2026-10-30 用户口径：「本来就没有图片6，他还是给我阻塞了，把这个阻塞的删了」）：
        它们是格式层问题，补一句或删一个 token 就能继续，没有理由把用户卡在审批页；
        更要紧的是这两类判据都从**当前** prompt 推出来，写成 blocking_issues 之后就变成
        **存量字符串** —— 提示词后来修好了、旧字符串还留着，`TaskPlan.approve()` 见非空
        即抛错，用户怎么改都批不了（实测 run `e92f45bb`：prompt 里只有 @图片1~@图片4
        与 @视频1，阻塞项却写着「引用了 @图片6」）。
        """
        blocking: dict[str, list[str]] = {}
        warnings: dict[str, list[str]] = {}
        for task in plan.tasks:
            # token 按**实际提交顺序**编号（`reference_tokens` 同一套规则），否则
            # 「@图片2 有没有对应素材」会判错。
            try:
                valid_tokens = set(
                    reference_tokens(
                        sorted(task.reference_images, key=lambda item: item.order),
                        mime_types,
                    ).values()
                )
            except ValueError:
                valid_tokens = set()

            task_reasons: list[str] = []
            #: 只提示、不拦审批的那些（锚点空洞 / 悬空引用）。
            task_warnings: list[str] = []
            effective_duration = (
                target_duration if isinstance(target_duration, int) else task.duration
            )
            if (
                isinstance(target_duration, int)
                and not isinstance(target_duration, bool)
                and task.duration is not None
                and target_duration != task.duration
            ):
                task_reasons.append(
                    f"本次时长是 {target_duration} 秒，而上一版计划里的提示词是按 "
                    f"{task.duration} 秒写的（时间轴与「预计 N 秒」都没跟着改）。"
                    "请点「重新运行」并填写返工要求，让模型按新时长重写时间轴与全部"
                    "时长表述；或把时长改回上一版的秒数。"
                )
            task_reasons.extend(
                duration_consistency_issues(task.prompt, effective_duration)
            )
            holes = dangling_reference_mentions(task.prompt)
            if holes:
                task_warnings.append(
                    "提示词里有被削掉主语的空洞（" + "、".join(holes[:3]) + "）："
                    "上一版摘素材时把句子打断了，原样保留会让模型拿到一句缺主语的话。"
                    "建议填写返工要求让模型重写这一句（不拦审批）。"
                )
            for mention in re.findall(r"@(图片|视频|音频)(\d+)", task.prompt):
                token = f"@{mention[0]}{mention[1]}"
                if token in valid_tokens:
                    continue
                task_warnings.append(
                    f"提示词引用了 {token}，但本次计划的参考素材里没有对应的"
                    f"{mention[0]}（上一版可能已被摘掉）。模型会忽略这个引用；"
                    f"要改的话请填写返工要求让模型重写，或手动把 {token} 改成实际存在的"
                    f"素材引用（不拦审批）。"
                )
            if task_reasons:
                blocking[task.task_id] = list(dict.fromkeys(task_reasons))
            if task_warnings:
                warnings[task.task_id] = list(dict.fromkeys(task_warnings))
        return blocking, warnings

    def _has_background(self, name: str) -> bool:
        return any(
            task.get_name() == name
            for task in self._background_tasks
            if not task.done()
        )

    async def resume_pending_runs(self) -> None:
        if self._closed:
            raise RunConflict("运行时正在关闭")
        runs = await self.repository.list_runs(
            statuses=self._RECOVERABLE_STATUSES
        )
        for run in runs:
            run_id = run["run_id"]
            lock = self._run_locks.setdefault(run_id, asyncio.Lock())
            if lock.locked() or any(
                task.get_name() == f"recovery-run-{run_id}"
                for task in self._background_tasks
                if not task.done()
            ) or self._has_background(f"approval-run-{run_id}"):
                continue
            self._start_background(
                self._recover_run(run_id), name=f"recovery-run-{run_id}"
            )

    async def _recover_run(self, run_id: str) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None or run["status"] not in self._RECOVERABLE_STATUSES:
                return
            try:
                snapshot = await self.graph.aget_state(
                    self._config(run["thread_id"])
                )
                state = dict(snapshot.values or {})
                if (
                    not state
                    or self._planning_prompt_from_state(state) is None
                ):
                    await self._fail_missing_planning_prompt(run_id)
                    return
                if run["status"] == "delivering":
                    previous_status = self._safe_status(
                        state.get("status"), "succeeded"
                    )
                    await self._retry_delivery_locked(
                        run_id, run["thread_id"], previous_status
                    )
                    return
                await self.repository.update_run_status(run_id, "running")
                result = await self._graph_ainvoke(
                    None, config=self._config(run["thread_id"])
                )
                final_status = self._waiting_status(result) or self._safe_status(
                    result.get("status"), "failed"
                )
                await self.repository.update_run_status(run_id, final_status)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.repository.append_event(
                    run_id,
                    "runtime",
                    "failed",
                    "Workflow recovery failed",
                )
                await self.repository.update_run_status(run_id, "failed")

    async def wait_for_terminal(
        self, run_id: str, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            if run["status"] in self._TERMINAL_STATUSES:
                return await self.get_run_view(run_id)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"run {run_id} did not reach a terminal status")
            await asyncio.sleep(0.01)

    async def retry_delivery(self, run_id: str) -> None:
        """Export a finished run's artifacts to the result table on demand.

        Generation success is already terminal. This is a best-effort,
        user-triggered export and must never downgrade a successful run.
        """
        if self.delivery_writer is None:
            raise RunConflict("导出结果表未配置")
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在处理中，请稍后重试")
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            if run["status"] not in {
                "succeeded", "completed_with_errors", "delivery_failed",
            }:
                raise RunConflict("只有已生成完成的运行可以导出结果表")
            if not await self._checkpoint_has_valid_planning_prompt(
                run["thread_id"]
            ):
                await self._fail_missing_planning_prompt(run_id)
                raise RunValidationError("运行缺少有效提示词快照")
            if run["status"] != "delivery_failed":
                artifacts = await self.repository.list_artifacts(run_id)
                if not artifacts:
                    raise RunConflict("没有可导出的成片")
            previous_status = run["status"]
            await self.repository.update_run_status(run_id, "delivering")
            self._start_background(
                self._retry_delivery_worker(
                    run_id, run["thread_id"], previous_status
                ),
                name=f"delivery-retry-{run_id}",
            )

    async def _retry_delivery_worker(
        self, run_id: str, thread_id: str, previous_status: str
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            await self._retry_delivery_locked(
                run_id, thread_id, previous_status
            )

    async def _retry_delivery_locked(
        self, run_id: str, thread_id: str, previous_status: str = "succeeded"
    ) -> None:
        try:
            if not await self._checkpoint_has_valid_planning_prompt(thread_id):
                await self._fail_missing_planning_prompt(run_id)
                return
            if self.delivery_writer is None:
                raise RunConflict("导出结果表未配置")
            if previous_status == "delivery_failed":
                # Legacy re-export path: the delivery context already exists.
                record = await self.delivery_writer.retry_delivery(run_id)
                artifacts = await self.repository.list_artifacts(run_id)
                final_status = "succeeded" if artifacts else "failed"
            else:
                # Fresh manual export of a successful run: reconstruct the
                # document/plan from the checkpoint and write the result table.
                snapshot = await self.graph.aget_state(self._config(thread_id))
                state = dict(snapshot.values or {})
                document = NormalizedDocument.model_validate(
                    state.get("normalized_document")
                )
                plan = approved_plan_from_state(
                    state, max_output_count=self.settings.max_output_count
                )
                artifacts = await self.repository.list_artifacts(run_id)
                record = await self.delivery_writer.deliver(
                    run_id, document, plan, artifacts
                )
                final_status = "succeeded"
            await self._graph_aupdate_state(
                self._config(thread_id),
                {
                    "delivery_record": record.model_dump(mode="json"),
                    "status": final_status,
                    "last_error": None,
                },
                as_node="deliver_to_feishu",
            )
            await self.repository.append_event(
                run_id,
                "deliver_to_feishu",
                "completed",
                "Feishu result-table export completed",
            )
            await self.repository.update_run_status(run_id, final_status)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.append_event(
                run_id,
                "deliver_to_feishu",
                "failed",
                "Feishu result-table export failed",
            )
            # A failed export must not downgrade the already-successful
            # generation run; legacy delivery_failed runs stay delivery_failed.
            await self.repository.update_run_status(run_id, previous_status)
    async def delete_run(self, run_id: str) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在处理中，不能删除")
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            allowed = self._TERMINAL_STATUSES | {"waiting_approval", "waiting_review"}
            if run["status"] not in allowed:
                raise RunConflict("只有等待审批或已结束的运行可以删除")
            checkpointer = getattr(self.graph, "checkpointer", None)
            if checkpointer is not None:
                await checkpointer.adelete_thread(run["thread_id"])
            self.file_store.delete_run(run_id)
            await self.repository.delete_run(run_id)
        self._run_locks.pop(run_id, None)

    async def cancel_run(self, run_id: str) -> None:
        """Cancel an active run immediately while preserving its plan."""
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        if run["status"] in self._TERMINAL_STATUSES:
            raise RunConflict("运行已经结束")
        if run["status"] not in self._CANCELLABLE_STATUSES:
            raise RunConflict("当前状态不支持取消")

        prefixes = (
            "approval-run",
            "approval-resume",
            "artifact-review-resume",
            "delivery-retry",
            "recovery-run",
        )
        workers = tuple(
            task
            for task in self._background_tasks
            if not task.done()
            and any(
                task.get_name() == f"{prefix}-{run_id}"
                for prefix in prefixes
            )
        )
        for task in workers:
            task.cancel()

        # Mark the database first so the cancel request returns immediately and
        # the UI can stop polling as an active run.  Do not await worker shutdown:
        # provider HTTP calls may take time to observe asyncio cancellation.
        await self.repository.append_event(
            run_id,
            "runtime_cancel",
            "completed",
            "Run cancelled by user",
        )
        await self.repository.update_run_status(run_id, "cancelled")
        try:
            await asyncio.wait_for(
                self._graph_aupdate_state(
                    self._config(run["thread_id"]),
                    {"status": "cancelled", "cancel_requested": True},
                    as_node="runtime_cancel",
                ),
                timeout=2.0,
            )
        except (TimeoutError, Exception):
            _LOGGER.exception("取消运行时更新 checkpoint 失败: %s", run_id)
    async def _inherit_cleanup_ledger(
        self, source_run_id: str, run_id: str
    ) -> None:
        """把源运行的**参考图清洗台账**整份继承给新 run（重跑复用计划时用）。

        为什么必须继承（2026-10-28 用户报「我点重新运行素材还是变了」）：
        重跑复用上一版计划时**不重新入库**，也就没有清洗这一环；而预览
        （`get_reference_file`）与提交（`nodes._clean_assets`）都只看**本 run** 的台账 ——
        台账为空 ⇒ 回退成**带水印的原图**，用户看到的就是"素材变了"。
        清洗版文件写在**文档级** `data/runs/<doc_token>/inputs/` 下，与 run 无关，所以直接搬台账即可。

        任何失败都只警告、不阻断重跑（素材回退原图是可用的，虽然不理想）。
        """
        try:
            from feishu_generation_agent.integrations.reference_watermark_clean import (
                cleanup_status_path,
                read_cleanup_status,
            )

            outputs_dir = Path(self.settings.outputs_dir)
            source = read_cleanup_status(source_run_id, outputs_dir)
            if not source:
                return
            target = cleanup_status_path(run_id, outputs_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(source, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, target)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("继承参考图清洗台账失败，按原图继续", exc_info=True)

    async def _run_to_approval(
        self,
        run_id: str,
        thread_id: str,
        request: RequirementRequest,
    ) -> None:
        try:
            await self.repository.update_run_status(run_id, "running")
            # 整篇重新规划（新 run）要继承**同一条需求历次运行**里的返工要求
            # （2026-10-10「铲」任务）。放在这里而不是节点里，因为只有 runtime
            # 能跨 run 读到别的 thread 的 checkpoint。
            carried_rework_requirements = await self._carried_rework_requirements(
                source_url=request.source_url,
                exclude_run_id=run_id,
            )
            result = await self._graph_ainvoke(
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "source_url": request.source_url,
                    "requester_open_id": request.requester_open_id,
                    "trigger_type": request.trigger_type,
                    "reply_context": request.reply_context,
                    "planning_prompt": (
                        request.planning_prompt.model_dump(mode="json")
                        if request.planning_prompt is not None
                        else None
                    ),
                    "planning_mode": request.planning_mode,
                    "carried_rework_requirements": carried_rework_requirements,
                    "status": "created",
                },
                config=self._config(thread_id),
            )
            status = self._waiting_status(result) or self._safe_status(
                result.get("status"), "completed"
            )
            await self.repository.update_run_status(run_id, status)
        except asyncio.CancelledError:
            raise
        except AgentError as exc:
            await self._record_last_error(run_id, thread_id, exc)
            await self.repository.append_event(
                run_id,
                "runtime",
                "failed",
                "Workflow background execution failed",
            )
            await self.repository.update_run_status(run_id, "failed")
        except Exception:
            await self.repository.append_event(
                run_id,
                "runtime",
                "failed",
                "Workflow background execution failed",
            )
            await self.repository.update_run_status(run_id, "failed")

    @staticmethod
    def _planning_prompt_from_state(
        state: dict[str, Any],
    ) -> PlanningPromptSnapshot | None:
        try:
            return PlanningPromptSnapshot.model_validate(
                state.get("planning_prompt")
            )
        except ValidationError:
            return None

    async def plan_portal_view(self, thread_id: str) -> dict[str, Any]:
        """Portal 用的一次性摘要：`{"prompt": 正文, "has_reference_video": 有没有挂参考视频, "title": 文档标题}`。

        为什么要「有没有挂参考视频」这一位（2026-10-29 用户报「计算费用应该按挂没挂参考
        视频判断单价」）：方舟对「有/无输入视频」是两档单价（Seedance 2.5 差 1.6 倍），
        而**方舟自己的任务列表/详情只返回输出内容**（`content.content.video_url` = 成片，
        实测 100/100 条如此），从它那儿看不出输入侧有没有视频。唯一知道真相的是**提交方**，
        也就是这里：计划的 `reference_plan` / `reference_images` 里 role 带 video 的素材
        就是这次提交给方舟的参考视频。

        读不到（没有 thread_id / 库读失败 / 状态里没有计划）就返回空视图 —— 调用方回落
        占位串与「无输入视频」档，绝不因为读不到而让历史列表失败。
        """
        view: dict[str, Any] = {
            "prompt": "",
            "has_reference_video": False,
            # 文档标题：没有多维表格绑定的运行（「直接分析需求文档」）在列表里没有
            # display_text 可用，只能拿标题当那一行的名字（2026-10-30）。
            "title": "",
            # 本条运行的产出类型（video / image）：直连任务重跑时要用它，否则
            # 源运行没有已批准计划、退回重新规划那一步会按默认的 video 重做
            # （图片需求的直连任务会被规划成视频）。
            "planning_mode": "",
        }
        if not thread_id:
            return view
        try:
            snapshot = await self.graph.aget_state(self._config(thread_id))
        except Exception:  # noqa: BLE001 - 历史列表不该因读 checkpoint 失败而报错
            return view
        state = dict(snapshot.values or {})
        view["title"] = str(state.get("document_title") or "").strip()
        view["planning_mode"] = str(state.get("planning_mode") or "").strip()
        for key in ("approved_plan", "draft_plan"):
            plan = state.get(key)
            if not isinstance(plan, dict):
                continue
            if not view["has_reference_video"]:
                view["has_reference_video"] = plan_has_reference_video(plan)
            if view["prompt"]:
                continue
            prompts = [
                str(task.get("prompt") or "").strip()
                for task in (plan.get("tasks") or [])
                if isinstance(task, dict) and str(task.get("prompt") or "").strip()
            ]
            if prompts:
                view["prompt"] = "\n\n".join(prompts)[:2000]
        return view

    async def plan_prompt_text(self, thread_id: str) -> str:
        """这条运行实际使用的**生成提示词**（Portal 的「创作记录」正文）。

        ⚠️ 别取错字段：state 里的 `planning_prompt` 是**规划器自己的系统提示词模板**
        （带 version + sha256，source 为 prime/personal），不是用户需求。第一版取的就是它，
        真机验证时发现 Portal 上每条飞书任务都显示「你是 AI 图片与视频生成需求规划器…」。
        真正的正文在 `approved_plan`（已批准）/ `draft_plan`（草稿）的 `tasks[].prompt` 里。

        取不到返回空串，由调用方回落占位文案 —— 绝不因为读不到正文而让历史列表失败。
        正文与「有没有参考视频」都来自同一次 checkpoint 读取，见 `plan_portal_view`。
        """
        return str((await self.plan_portal_view(thread_id)).get("prompt") or "")

    async def _checkpoint_has_valid_planning_prompt(
        self,
        thread_id: str,
    ) -> bool:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        state = dict(snapshot.values or {})
        return (
            bool(state)
            and self._planning_prompt_from_state(state) is not None
        )

    async def _fail_missing_planning_prompt(self, run_id: str) -> None:
        await self.repository.append_event(
            run_id,
            "planning_prompt",
            "failed",
            "Planning prompt snapshot unavailable",
        )
        await self.repository.update_run_status(run_id, "failed")

    async def get_run_view(self, run_id: str) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        state = dict(snapshot.values or {})
        events = await self.repository.list_events(run_id)
        operations = await self.repository.list_operations(run_id)
        artifacts = await self.repository.list_artifacts(run_id)
        shot_order, shot_meta = _artifact_shot_meta(state)
        # 按分镜顺序展示；计划外的产物（旧版遗留）排在最后，再按 id 稳定排序。
        artifacts.sort(
            key=lambda artifact: (
                shot_order.get(artifact.task_id, len(shot_order) + 1),
                artifact.artifact_id,
            )
        )
        repository_status = self._safe_status(run.get("status"), "created")
        # Terminal repository state wins over an older checkpoint.  This is
        # important after cancellation: the graph may still point at the
        # waiting_approval interrupt even though the run is already cancelled.
        if repository_status in self._TERMINAL_STATUSES:
            status = repository_status
        elif repository_status in {
            "created",
            "running",
            "resuming",
            "waiting_provider",
            "delivering",
            "failed",
        }:
            status = repository_status
        else:
            status = self._safe_status(state.get("status"), repository_status)
        plan = state.get("draft_plan")
        if plan is None:
            plan = state.get("task_plan")
        if not isinstance(plan, dict):
            plan = {
                "tasks": [],
                "document_summary": "",
                "excluded_assets": [],
            }
        try:
            plan_model = TaskPlan.model_validate(plan)
            plan = plan_model.model_dump(mode="json")
        except Exception:
            plan_model = None
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            tasks = []
        tasks = self._storyboard_view(run_id, tasks)
        # 审批页展示的阻断项也要按**当前** prompt/duration 重算：这条是存量字符串，
        # 用户把时长/提示词改一致之后它还留在计划里，界面上会一直显示红色的
        # 「阻塞：…」（2026-10-13 小猫钓鱼：其实已经一致，只差没人重算）。
        for task in tasks:
            if isinstance(task, dict):
                refresh_duration_blocking_issues(task)
        media_assets = self._safe_media_assets(
            run_id, state.get("media_assets", [])
        )
        coverage = self._asset_coverage(
            plan_model,
            state.get("media_assets", []),
        )
        ingest_issue_records = self._document_ingest_issue_records(state)
        ingest_issues = [
            record.display_message for record in ingest_issue_records
        ]
        view = {
            "run_id": run_id,
            "thread_id": run["thread_id"],
            "source_url": run["source_url"],
            "status": status,
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "events": self._event_view(events),
            "interrupt": self._interrupt_view(snapshot, state),
            "nodes": self._node_view(events),
            "operations": [
                {
                    "task_id": operation.get("task_id"),
                    "operation": operation.get("operation"),
                    "provider": operation.get("provider"),
                    "phase": operation.get("phase"),
                    "provider_task_id": operation.get("official_id"),
                    "updated_at": operation.get("updated_at"),
                }
                for operation in operations
            ],
            "execution_records": state.get("execution_records", []),
            "progress": self._generation_progress(state, operations, artifacts),
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "task_id": artifact.task_id,
                    "kind": artifact.kind,
                    "mime_type": artifact.mime_type,
                    "size": artifact.size,
                    "status": artifact.status,
                    "provider_task_id": artifact.provider_task_id,
                    "delivered": artifact.feishu_file_token is not None,
                    "preview_url": (
                        f"/api/runs/{run_id}/artifacts/"
                        f"{artifact.artifact_id}/content"
                    ),
                    # 镜头号 / 第几条：卡片要标出来，用户才知道该勾哪一条重跑。
                    **shot_meta.get(artifact.task_id, {}),
                }
                for artifact in artifacts
            ],
            "delivery": state.get("delivery_record"),
            "artifact_review": {
                "feedback": state.get("artifact_review_feedback"),
                "decision": state.get("artifact_review_decision"),
            },
            "last_error": state.get("last_error"),
            "privacy": {
                "langsmith_tracing": self.settings.langsmith_tracing,
            },
            "approval": {
                "document_id": state.get("document_id"),
                "document_title": state.get("document_title"),
                "document_revision": state.get(
                    "document_revision", state.get("source_revision")
                ),
                "revision": state.get(
                    "draft_revision",
                    state.get("document_revision", state.get("source_revision")),
                ),
                "tasks": tasks,
                "document_summary": plan.get("document_summary", ""),
                "media_assets": media_assets,
                # 参考图去水印的后台进度台账（2026-10-20 用户口径：每张参考图要有前端反馈）。
                # 形如 {"image-1": {"status": "running", "message": "正在去水印…"}}；
                # 没有这份文件时返回空 dict，前端就不显示状态。
                "watermark_cleanup": _read_watermark_cleanup(self.settings, run_id),
                "excluded_assets": plan.get("excluded_assets", []),
                "coverage": coverage,
                "vision_descriptions": state.get("vision_descriptions", []),
                "vision_issues": state.get("vision_issues", []),
                "ingest_issue_records": [
                    record.model_dump(
                        mode="json",
                        exclude_none=True,
                        include={
                            "severity",
                            "code",
                            "display_message",
                            "source_block_id",
                            "asset_id",
                            "asset_kind",
                            "failure_reason",
                        },
                    )
                    for record in ingest_issue_records
                ],
                "ingest_issues": ingest_issues,
                "blocking_ingest_issues": [
                    record.display_message
                    for record in ingest_issue_records
                    if record.severity is IngestIssueSeverity.BLOCKING
                ],
                "asset_ingest_issues": [
                    record.display_message
                    for record in ingest_issue_records
                    if record.severity is IngestIssueSeverity.ASSET
                ],
                "validation_issues": self._rebuild_validation_issues(
                    state,
                    plan_model,
                    ingest_issue_records,
                ),
                "audit_issues": self._audit_issue_view(state),
                "selected_task_ids": [
                    task.get("task_id")
                    for task in state.get("approved_tasks", [])
                    if isinstance(task, dict)
                ],
            },
        }
        return view

    @staticmethod
    def _generation_progress(
        state: dict[str, Any],
        operations: list[dict[str, Any]],
        artifacts: list[Any],
    ) -> dict[str, int] | None:
        """「已完成 / 总数」（执行单元级），给界面显示「生成中 2/3」。

        用户 2026-09-23：「3 个任务完成 2 个，界面还是只显示『生成中』，看不出在动」。
        执行阶段的进度**不在 checkpoint 里** —— `execution_records` 要等
        `execute_selected_tasks` 全部跑完才写回。所以这里用两条边写边更新的通道算：

        - `operations`：每个执行单元一条（`phase` 变成 succeeded 就是出结果了）；
        - `artifacts`：每个执行单元落盘后立刻 `replace_task_artifacts`。

        拿不到已批准任务（还没审批 / 老数据）时返回 None，界面就不显示进度。
        """
        approved = [
            task
            for task in (state.get("approved_tasks") or [])
            if isinstance(task, dict)
        ]
        unit_of: dict[str, str] = {}
        for task in approved:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            try:
                count = max(1, int(task.get("output_count") or 1))
            except (TypeError, ValueError):
                count = 1
            if count == 1:
                unit_of[task_id] = task_id
                continue
            for index in range(1, count + 1):
                unit_of[f"{task_id}::output:{index}"] = task_id
        total = len(unit_of)
        if total <= 0:
            return None
        finished = {
            unit
            for unit in (
                getattr(artifact, "task_id", None) for artifact in artifacts
            )
            if isinstance(unit, str) and unit in unit_of
        }
        for operation in operations:
            unit = operation.get("task_id")
            if not isinstance(unit, str) or unit not in unit_of:
                continue
            phase = str(operation.get("phase") or "").lower()
            if phase in GraphRuntime._FINISHED_OPERATION_PHASES:
                finished.add(unit)
        return {"completed": len(finished), "total": total}

    @staticmethod
    def _audit_issue_view(state: dict[str, Any]) -> list[str]:
        """审查员（audit_plan）的全部风险提示，原样交给审批页。

        以前只把「技术阻断 / 人工处理」两类并进 validation_issues，其余**建议性
        风险**在审批页完全没有出口。实测（用户 2026-09-21 巨型南瓜）：审查员早就
        写了「3 个分镜合并成 8 秒、单镜头时长被压缩、三组事件可能表达不完整」，
        但这句话既不叫「技术阻断」也不含「人工处理」，被静默丢掉 —— 用户批准了
        一份注定出废片的计划，然后反复「重新运行」（重跑复用同一份计划），
        得到同样的废片。风险提示是给人看的，不该只有两个关键词能上浮。
        """
        raw = state.get("audit_report")
        if not raw:
            return []
        try:
            report = AuditReport.model_validate(raw)
        except (TypeError, ValueError):
            return []
        return [
            issue.strip()
            for issue in report.issues
            if isinstance(issue, str) and issue.strip()
        ]

    @staticmethod
    def _storyboard_view(
        run_id: str, tasks: list[Any]
    ) -> list[Any]:
        """把每个时间段的候选图 asset_id 换成带预览地址的对象，给审批页选图用。

        `candidates` 存的是 asset_id 字符串（提交时直接用）；视图里换成
        `{asset_id, preview_url, selected}`，前端不用自己拼 URL。

        「没选就取第一张候选」只在**用户没删过**的时候生效：`selection_cleared`
        的段是「用户显式删掉了这一段的关键帧」，这里再高亮一张，前端批准时就会
        把它回传回来 —— 删了等于没删（用户 2026-09-23 报的正是这个）。
        """
        enriched: list[Any] = []
        for task in tasks:
            if not isinstance(task, dict):
                enriched.append(task)
                continue
            frames = task.get("storyboard_frames")
            if not isinstance(frames, list) or not frames:
                enriched.append(task)
                continue
            view_frames: list[dict[str, Any]] = []
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                candidates = frame.get("candidates") or []
                selected = frame.get("selected")
                if not selected and candidates and not frame.get("selection_cleared"):
                    # 展示「默认第一张」，与实际提交保持一致。
                    selected = candidates[0]
                view_frames.append(
                    {
                        **frame,
                        "candidates": [
                            {
                                "asset_id": asset_id,
                                "preview_url": (
                                    f"/api/runs/{run_id}/references/"
                                    f"{asset_id}/content"
                                ),
                                "selected": asset_id == selected,
                            }
                            for asset_id in candidates
                            if isinstance(asset_id, str)
                        ],
                    }
                )
            enriched.append({**task, "storyboard_frames": view_frames})
        return enriched

    @staticmethod
    def _document_ingest_issue_records(
        state: dict[str, Any],
    ) -> list[IngestIssueRecord]:
        for key in ("normalized_document", "source_document"):
            document = state.get(key)
            if not isinstance(document, dict):
                continue
            try:
                return resolve_ingest_issue_records(document)
            except ValidationError:
                return [
                    make_ingest_issue_record(
                        IngestIssueCode.LEGACY_UNKNOWN
                    )
                ]
        return []

    def _rebuild_validation_issues(
        self,
        state: dict[str, Any],
        plan: TaskPlan | None,
        records: list[IngestIssueRecord],
    ) -> list[str]:
        document: NormalizedDocument | None = None
        for key in ("normalized_document", "source_document"):
            raw = state.get(key)
            if raw is None:
                continue
            try:
                document = NormalizedDocument.model_validate(raw)
                break
            except Exception:
                _LOGGER.exception("审批文档解析失败")
                break
        if document is None or plan is None:
            # 失败任务没有文档（或计划无效），属于预期情况，静默返回兜底，
            # 避免每次读取历史失败任务都打印一条完整 traceback 刷屏。
            return ["审批校验状态无效，请重新读取后再审批"]
        try:
            issues = validate_plan(
                plan,
                document,
                max_output_count=self.settings.max_output_count,
            )
            audit = AuditReport.model_validate(state.get("audit_report", {}))
            if audit.corrections_required:
                issues.extend(
                    f"audit: {issue}"
                    for issue in audit.issues
                    if issue.startswith("技术阻断")
                    or "人工处理" in issue
                )
            issues.extend(
                record.display_message
                for record in records
                if record.severity is IngestIssueSeverity.BLOCKING
            )
        except Exception:
            _LOGGER.exception("重建审批校验问题失败，无法生成校验列表")
            return ["审批校验状态无效，请重新读取后再审批"]
        return list(dict.fromkeys(issues))

    async def resume_run(
        self,
        run_id: str,
        decision: ApprovalDecision,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("审批正在处理中，请勿重复提交")
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            if run["status"] != "waiting_approval":
                raise RunConflict("只有等待审批的运行可以提交决定")
            snapshot = await self.graph.aget_state(
                self._config(run["thread_id"])
            )
            state = dict(snapshot.values or {})
            if self._planning_prompt_from_state(state) is None:
                await self._fail_missing_planning_prompt(run_id)
                raise RunValidationError("运行缺少有效提示词快照")
            self._validate_decision(state, decision)
            await self.repository.update_run_status(run_id, "resuming")
            # 审批决定立即返回，生成与交付放到后台执行，避免 decision 接口
            # 阻塞到整条生成链路结束（视频生成动辄数分钟），前端请求长时间
            # 挂起被网关/浏览器判超时。
            self._start_background(
                self._resume_approved_run(
                    run_id, run["thread_id"], decision
                ),
                name=f"approval-resume-{run_id}",
            )

    async def _resume_approved_run(
        self,
        run_id: str,
        thread_id: str,
        decision: ApprovalDecision,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            try:
                result = await self._graph_ainvoke(
                    Command(resume=decision.model_dump(mode="json")),
                    config=self._config(thread_id),
                )
            except AgentError as exc:
                await self._record_last_error(run_id, thread_id, exc)
                await self.repository.update_run_status(run_id, "failed")
                return
            except Exception:
                await self.repository.append_event(
                    run_id,
                    "runtime",
                    "failed",
                    "Workflow approval resume failed",
                )
                await self.repository.update_run_status(run_id, "failed")
                return
            status = self._waiting_status(result) or self._safe_status(
                result.get("status"), "completed"
            )
            await self.repository.update_run_status(run_id, status)

    async def resume_artifact_review(
        self,
        run_id: str,
        decision: ArtifactReviewDecision,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("成片确认正在处理中，请勿重复提交")
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            status = str(run["status"] or "").lower()
            if status != "waiting_review":
                if status not in {
                    "succeeded",
                    "completed_with_errors",
                    "delivery_failed",
                }:
                    raise RunConflict("只有可审核的成片运行可以提交决定")
                config = self._config(run["thread_id"])
                snapshot = await self.graph.aget_state(config)
                state = dict(snapshot.values or {})
                if not state.get("artifacts"):
                    raise RunValidationError("当前没有可审核的成片")
                await self.graph.aupdate_state(
                    config,
                    {"status": "waiting_review"},
                    as_node="verify_and_download_artifacts",
                )
                reopened = await self.graph.ainvoke(None, config=config)
                if not self._has_interrupt(reopened):
                    raise RunConflict("成片审核门禁无法重新打开")
                await self.repository.update_run_status(run_id, "waiting_review")
            snapshot = await self.graph.aget_state(
                self._config(run["thread_id"])
            )
            state = dict(snapshot.values or {})
            if self._planning_prompt_from_state(state) is None:
                await self._fail_missing_planning_prompt(run_id)
                raise RunValidationError("运行缺少有效提示词快照")
            self._validate_artifact_review(state, decision)
            await self.repository.update_run_status(run_id, "resuming")
            self._start_background(
                self._resume_artifact_review_run(
                    run_id, run["thread_id"], decision
                ),
                name=f"artifact-review-resume-{run_id}",
            )

    async def _resume_artifact_review_run(
        self,
        run_id: str,
        thread_id: str,
        decision: ArtifactReviewDecision,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            try:
                result = await self._graph_ainvoke(
                    Command(resume=decision.model_dump(mode="json")),
                    config=self._config(thread_id),
                )
            except AgentError as exc:
                await self._record_last_error(run_id, thread_id, exc)
                await self.repository.update_run_status(run_id, "failed")
                return
            except Exception:
                await self.repository.append_event(
                    run_id,
                    "runtime",
                    "failed",
                    "Workflow artifact review resume failed",
                )
                await self.repository.update_run_status(run_id, "failed")
                return
            status = self._waiting_status(result) or self._safe_status(
                result.get("status"), "completed"
            )
            await self.repository.update_run_status(run_id, status)

    @staticmethod
    def _validate_artifact_review(
        state: dict[str, Any],
        decision: ArtifactReviewDecision,
    ) -> None:
        if len(decision.task_ids) != len(set(decision.task_ids)):
            raise RunValidationError("重跑任务不能重复选择")
        if decision.action == "adjust" and (
            not isinstance(decision.feedback, str)
            or not decision.feedback.strip()
        ):
            raise RunValidationError("退回调整时必须填写调整意见")
        if decision.action != "adjust" and (
            decision.feedback is not None or decision.task_ids
        ):
            raise RunValidationError("确认或取消时不能携带调整意见或任务")
        if decision.action == "confirm" and not state.get("artifacts"):
            raise RunValidationError("当前没有可确认的成片")

    async def _record_last_error(
        self,
        run_id: str,
        thread_id: str,
        exc: AgentError,
    ) -> None:
        del run_id
        await self._graph_aupdate_state(
            self._config(thread_id),
            {"last_error": exc.detail.model_dump(mode="json")},
            as_node="revalidate_approval",
        )

    async def add_reference(
        self,
        run_id: str,
        *,
        task_id: str,
        role: str,
        order: int,
        filename: str,
        content: bytes,
        replaces_asset_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            references = list(task.reference_images)
            if replaces_asset_id is not None:
                replace_index = next(
                    (
                        index
                        for index, reference in enumerate(references)
                        if reference.asset_id == replaces_asset_id
                    ),
                    None,
                )
                if replace_index is None:
                    raise RunValidationError(
                        f"替换素材 {replaces_asset_id} 不属于任务 {task_id}"
                    )
            else:
                replace_index = None

            try:
                verified = self.file_store.validate(content)
            except (TypeError, ValueError):
                raise RunValidationError("上传内容不是有效图片或音视频参考素材") from None
            allowed_roles = (
                {"reference_image", "first_frame", "last_frame"}
                if verified.mime_type.startswith("image/")
                else {"reference_video"}
                if verified.mime_type.startswith("video/")
                else {"reference_audio"}
                if verified.mime_type.startswith("audio/")
                else set()
            )
            if role not in allowed_roles:
                raise RunValidationError("参考素材类型与用途不匹配")
            try:
                stored = self.file_store.save_input(run_id, filename, content)
            except (TypeError, ValueError):
                raise RunValidationError("参考素材保存失败") from None
            asset_id = f"upload-{uuid4().hex}"
            asset = MediaAsset(
                asset_id=asset_id,
                source_block_id=f"local-upload:{asset_id}",
                origin="local_upload",
                file_token=None,
                local_path=stored.local_path,
                mime_type=stored.mime_type,
                size=stored.size,
                sha256=stored.sha256,
                width=stored.width,
                height=stored.height,
            )
            reference = ImageReference(
                asset_id=asset_id,
                role=role,
                order=order,
            )
            if replace_index is None:
                references.append(reference)
                replacement_asset_ids = None
            else:
                replaced_asset_id = references[replace_index].asset_id
                # 「替换」= **原地**换掉那一格的素材，槽位跟着被替换的那一格走。
                # 客户端给的 `order` 在替换场景下**不可信**：前端提交的是下拉框里
                # 「同类型内的序号」（视频1 的序号是 1），而它在任务里的全局槽位是 2
                # （图片1 占 1）—— 照实采用就与图片1 撞车，`canonicalize_references`
                # 判 order 重复 → 422「图片引用无效」（2026-10-30 用户报「替换视频会失败」，
                # 替换图片没事正因为图片的同类型序号恰好等于全局槽位）。
                # 槽位由服务端说了算，就不必指望每个客户端都算对。
                reference = reference.model_copy(
                    update={"order": references[replace_index].order}
                )
                references[replace_index] = reference
                replacement_asset_ids = {replaced_asset_id: asset_id}
            assets = self._state_assets(state)
            assets.append(asset)
            updated_task = self._task_with_references(
                task,
                references,
                assets,
                replacement_asset_ids=replacement_asset_ids,
            )
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)
            return {
                "asset_id": asset_id,
                "mime_type": stored.mime_type,
                "size": stored.size,
                "width": stored.width,
                "height": stored.height,
            }

    async def set_references(
        self,
        run_id: str,
        *,
        task_id: str,
        references: list[ImageReference],
        reference_mode: str | None = None,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            assets = self._state_assets(state)
            updated_task = self._task_with_references(
                task,
                references,
                assets,
                reference_mode=reference_mode,
            )
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)

    async def unlink_reference(
        self,
        run_id: str,
        *,
        task_id: str,
        asset_id: str,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            references = [
                reference
                for reference in task.reference_images
                if reference.asset_id != asset_id
            ]
            if len(references) == len(task.reference_images):
                raise RunValidationError(
                    f"素材 {asset_id} 不属于任务 {task_id}"
                )
            assets = self._state_assets(state)
            updated_task = self._task_with_references(task, references, assets)
            updated_task = self._drop_storyboard_candidate(updated_task, asset_id)
            if updated_task.storyboard_frames != task.storyboard_frames:
                # 帧被改过 → 提示词里的「关键帧参考：」也要按最新选择重算，
                # 否则草稿里会留下一句指向已删图的话（批准时会清掉，但用户看得见）。
                updated_task = self._reapply_storyboard_selection(
                    updated_task, state
                )
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)

    @staticmethod
    def _drop_storyboard_candidate(
        task: GenerationTask, asset_id: str
    ) -> GenerationTask:
        """把被删掉的素材从分镜帧里摘干净 —— 用户删了就不许再被加回来。

        用户 2026-09-23：「分镜图我明明已经删掉他了不需要了还是能影响我」。审批页把
        每段选中的关键帧也当普通参考图列在「参考素材」里，用户在那里删掉它时，这里
        过去**只摘了 `reference_images`**，帧上的 `selected` 还指着它 —— 批准时
        `_with_server_storyboard_frames` → `apply_selection` 按服务端帧重算参考图，
        又把它加了回来，删了等于没删。

        所以，删掉的素材若属于某一段的 `candidates`：

        - 从 `candidates` 里移除 —— 否则「没选就取第一张候选」的默认规则会把它
          重新选回来（`_storyboard_view` 也照此展示）；
        - 它正好是那一段的 `selected` 时一并清空，并打上 `selection_cleared`
          标记 —— **这一段就没有关键帧了**。标记是必须的：光把 `selected` 清空，
          「默认第一张」还会把**剩下的候选**补上来，用户「删了不需要」的那一段
          依然会带着一张他没挑过的关键帧进视频。

        删全局图（不在任何帧的候选里）时原样返回，行为不变。
        """
        frames = task.storyboard_frames or []
        if not any(asset_id in frame.candidates for frame in frames):
            return task
        updated = task.model_copy(
            update={
                "storyboard_frames": [
                    frame.model_copy(
                        update={
                            "candidates": [
                                candidate
                                for candidate in frame.candidates
                                if candidate != asset_id
                            ],
                            "selected": (
                                None
                                if frame.selected == asset_id
                                else frame.selected
                            ),
                            "selection_cleared": (
                                True
                                if frame.selected == asset_id
                                else frame.selection_cleared
                            ),
                        }
                    )
                    for frame in frames
                ]
            }
        )
        return updated

    def _reapply_storyboard_selection(
        self, task: GenerationTask, state: dict[str, Any]
    ) -> GenerationTask:
        """按最新的帧重拼参考图与提示词里的「关键帧参考：」那一句（就地算）。"""
        payload = task.model_dump(mode="json")
        frames = [
            frame.model_dump(mode="json") for frame in (task.storyboard_frames or [])
        ]
        self._apply_storyboard_selection(
            payload,
            frames,
            self._storyboard_reference_ids(state, frames),
        )
        return GenerationTask.model_validate(payload)

    async def exclude_asset(
        self,
        run_id: str,
        *,
        asset_id: str,
    ) -> None:
        """把一个**用不到**的素材放进 `excluded_assets`。

        覆盖门要求「每个素材要么被任务引用、要么被排除」，而审批页过去只有引用的
        增删、没有排除入口 —— 计划没引用的素材只能靠手改提示词绕开（2026-09-17
        超级大床：image-4..image-8 未覆盖，批准按钮一直是灰的）。
        """
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            referenced = {
                reference.asset_id
                for task in plan.tasks
                for reference in task.reference_images
            }
            if asset_id in referenced:
                raise RunValidationError(
                    f"素材 {asset_id} 已被任务引用，不能同时排除"
                )
            assets = self._state_assets(state)
            if asset_id not in {asset.asset_id for asset in assets}:
                raise RunValidationError(f"素材 {asset_id} 不存在")
            updated_plan = reconcile_asset_coverage(
                plan, removed_asset_ids={asset_id}
            )
            await self._persist_draft(run, state, updated_plan, assets)

    async def remove_asset(
        self,
        run_id: str,
        *,
        asset_id: str,
    ) -> None:
        """把一个素材从**这次运行**里移除（服务器上的文件保留）。

        用户 2026-11-04：「已排除的素材可不可以加一个删除和重新挂素材的功能」，
        并自己选定了语义：**删除 = 从本次运行移除、文件保留**（不是删文件，也不是
        "从排除里拿回来"）。

        与 `exclude_asset` 的分工：
          * **排除** = 这次不用它，但仍然登记在案（覆盖门要求「每个素材要么被任务引用、
            要么被排除」，所以排除是"留个说法"）；
          * **删除** = 当它跟本次任务无关，直接从素材集合里拿掉 —— 覆盖门不再管它，
            「已排除」里也跟着消失。

        这是人做的决定，所以同样走 `_waiting_state`（非可编辑状态直接拒）。还被任务
        引用的素材**拒绝删除**：要人先去任务上摘掉，绝不偷偷改计划。
        只读 `media_assets`（分镜候选在 `storyboard_assets`，另一条通道，别混进来 ——
        混进来就会触发「未覆盖素材」门禁，见 `_state_assets` 的说明）。
        """
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            referenced = {
                reference.asset_id
                for task in plan.tasks
                for reference in task.reference_images
            }
            if asset_id in referenced:
                raise RunValidationError(
                    f"素材 {asset_id} 还被任务引用，请先从任务上摘掉再删除"
                )
            media_assets: list[MediaAsset] = []
            found = False
            for item in state.get("media_assets") or []:
                try:
                    asset = MediaAsset.model_validate(item)
                except Exception:  # noqa: BLE001 —— 坏素材不该挡住这次删除
                    continue
                if asset.asset_id == asset_id:
                    found = True
                    continue
                media_assets.append(asset)
            if not found:
                raise RunValidationError(f"素材 {asset_id} 不存在")
            updated_plan = TaskPlan(
                tasks=plan.tasks,
                document_summary=plan.document_summary,
                excluded_assets=[
                    item
                    for item in plan.excluded_assets
                    if item.asset_id != asset_id
                ],
            )
            await self._persist_draft(run, state, updated_plan, media_assets)

    _PATCHABLE_TASK_FIELDS = {
        "prompt",
        "negative_constraints",
        "aspect_ratio",
        "output_count",
        "image_size",
        "size_variants",
        "safe_area",
        "image_provider",
        "video_provider",
        "duration",
        "resolution",
        "generate_audio",
        "user_intent",
        "delivery_crop",
        # 审批页选分镜关键帧（只改 selected，候选图按 index 并回来）。
        "storyboard_frames",
    }

    @staticmethod
    def _apply_storyboard_selection(
        task: dict[str, Any],
        frames: list[dict[str, Any]],
        storyboard_ids: set[str],
    ) -> None:
        """把选中的关键帧变成任务的参考图 + 提示词里的一句点名（见 storyboard 模块）。"""
        from feishu_generation_agent.integrations.storyboard import (
            apply_selection,
        )

        apply_selection(task, frames, storyboard_ids)

    @staticmethod
    def _merge_storyboard_selection(
        current: list[Any], incoming: Any
    ) -> list[dict[str, Any]]:
        """按 index 合并前端回传的选图结果，保留服务端生成的候选图。"""
        from feishu_generation_agent.integrations.storyboard import (
            merge_selection,
        )

        return merge_selection(list(current or []), incoming)

    @staticmethod
    def _storyboard_reference_ids(
        state: dict[str, Any],
        frames: list[dict[str, Any]],
    ) -> set[str]:
        """`apply_selection` 要摘掉再追加的 asset_id（生成候选 ∪ 每段选中的图）。

        2026-09-23 实测：这里原先只算 `storyboard_assets`，于是**复用的分镜图**
        （比如 image-1 直接当第 1 段的关键帧）不在集合里 → 它既留在基础参考图里、
        又被当作该段画面追加一次 → 「同一任务不能重复引用同一图片」直接 400。
        注意**不能**把 `reference_plan` 里所有 `role=shot` 的图都并进来：没被任何
        一段选中的 shot 图会被摘掉又没人加回来（参考图静默少一张）。
        """
        from feishu_generation_agent.integrations.storyboard import (
            reference_ids_for_selection,
        )

        generated = {
            item.get("asset_id")
            for item in state.get("storyboard_assets", [])
            if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
        }
        return reference_ids_for_selection(frames, generated)

    async def regenerate_storyboard_frame(
        self,
        run_id: str,
        *,
        task_id: str,
        index: int,
    ) -> dict[str, Any]:
        """**只重跑某一段**的分镜图（用户 2026-09-22 选的方案 A）。

        同一段文本、同一参考图，重新采样 K 张替换该段候选 —— **不重新规划、不改提示词、
        不动其它段、不调 LLM**，只花 1 张图（×K）的生图费用。生成器由 bootstrap 注入
        （`storyboard_regenerator`），运行时自己没有图片生成器。
        """
        from feishu_generation_agent.domain.plan import StoryboardFrame
        from feishu_generation_agent.integrations.storyboard import (
            apply_selection,
            reference_ids_for_selection,
        )

        if self._storyboard_regenerator is None:
            raise RunValidationError("分镜重生成不可用")
        # **生成阶段不持锁**：一次生图要 80~110 秒，如果整段都占着运行锁，用户同时点
        # 第二段就会被顶成 409「运行正在更新，请稍后重试」（2026-09-23 用户实测：
        # 「我想两张分镜图一起并发都不行吗」）。所以：读状态 → **放开锁去生成** →
        # 拿到锁只做「重读最新状态 + 落盘」这一小段。
        run, state = await self._waiting_state(run_id)
        plan = self._state_plan(state)
        task_index, task = self._task(plan, task_id)
        frames = [
            StoryboardFrame.model_validate(item)
            for item in (task.storyboard_frames or [])
        ]
        frame = next((item for item in frames if item.index == index), None)
        if frame is None:
            raise RunValidationError(f"没有第 {index} 段的分镜图")
        created, updated_frame = await self._storyboard_regenerator(
            run_id=run_id,
            task=task,
            frame=frame,
            assets=self._state_assets(state),
        )
        # 必须真的换了一批图：以前只看「候选非空」，而 K=0 时生成函数空转返回、
        # 候选还是原来那张 → 接口 200 但界面毫无变化（用户 2026-09-23：「点了没用」）。
        if (
            not updated_frame.candidates
            or updated_frame.candidates == frame.candidates
        ):
            raise RunValidationError(
                "重新生成没有产生新图（生图服务未返回结果），请稍后重试"
            )
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            # 生成期间别的段可能已经落过盘，所以**重读最新状态**再合并，别覆盖它。
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, latest_task = self._task(plan, task_id)
            latest_frames = [
                StoryboardFrame.model_validate(item)
                for item in (latest_task.storyboard_frames or [])
            ]
            updated = dict(latest_task.model_dump(mode="json"))
            updated["storyboard_frames"] = [
                (
                    updated_frame.model_dump(mode="json")
                    if item.index == index
                    else item.model_dump(mode="json")
                )
                for item in latest_frames
            ]
            # 新素材要登记进 storyboard_assets，否则预览 404、执行时解析不到。
            existing = [
                item
                for item in state.get("storyboard_assets", [])
                if isinstance(item, dict)
            ]
            known = {item.get("asset_id") for item in existing}
            merged_assets = existing + [
                asset.model_dump(mode="json")
                for asset in created
                if asset.asset_id not in known
            ]
            storyboard_ids = {
                item.get("asset_id")
                for item in merged_assets
                if isinstance(item.get("asset_id"), str)
            }
            # 摘掉再追加的集合还要算上「每段当前选中的图」：复用的分镜图不在
            # storyboard_assets 里，漏掉它会同时以「基础参考」和「该段画面」的身份
            # 在参考图里出现两次（2026-09-23 实测：image-2 出现两遍）。
            apply_selection(
                updated,
                updated["storyboard_frames"],
                reference_ids_for_selection(
                    updated["storyboard_frames"], storyboard_ids
                ),
            )
            updated_task = GenerationTask.model_validate(updated)
            await self._persist_draft(
                run,
                state,
                self._replace_task(plan, task_index, updated_task),
                self._state_assets(state),
                {"storyboard_assets": merged_assets},
            )
        return {"status": "updated", "frame": updated_frame.model_dump(mode="json")}

    async def patch_task(
        self,
        run_id: str,
        *,
        task_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """热修改：把任务字段编辑直接持久化进审批草稿。

        与 set_references 同模式，但针对提示词等任务字段。提示词被手工修改
        后以人的版本为准：清空 prompt_slots，避免后续校验用槽位拼装覆盖手工
        内容；同时沿用确定性补齐，把漏写的 @图片N 补回 prompt 尾部。
        """
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            unknown = set(patch) - self._PATCHABLE_TASK_FIELDS
            if unknown:
                raise RunValidationError(
                    "不支持修改的字段：" + "、".join(sorted(unknown))
                )
            assets = self._state_assets(state)
            updated = dict(task.model_dump(mode="json"))
            updated.update(patch)
            if "storyboard_frames" in patch:
                # 前端选图只回传 {index, selected}（候选图是服务端生成的，前端不
                # 该也无法重传）。按 index 把候选并回来，避免把图弄丢；选中的图
                # 立刻变成任务的参考图 + 提示词里的一句点名，审批与执行都不用改。
                merged_frames = self._merge_storyboard_selection(
                    task.storyboard_frames, patch["storyboard_frames"]
                )
                updated["storyboard_frames"] = merged_frames
                self._apply_storyboard_selection(
                    updated,
                    merged_frames,
                    self._storyboard_reference_ids(state, merged_frames),
                )
            default_model_key = resolve_video_model_key(
                getattr(self.settings, "video_provider", None),
                fallback=resolve_video_model_key(
                    getattr(self.settings, "seedance_model", None),
                    fallback=DEFAULT_VIDEO_MODEL_KEY,
                ) or DEFAULT_VIDEO_MODEL_KEY,
            )
            updated, policy_warnings = normalize_video_task_payload(
                updated,
                default_model_key=default_model_key,
                max_output_count=self.settings.max_output_count,
            )
            if policy_warnings:
                warnings = list(updated.get("warnings") or [])
                warnings.extend(policy_warnings)
                updated["warnings"] = list(dict.fromkeys(warnings))
            # 改时长/改提示词之后必须**重算**「时长不一致」阻断项：这条是存量字符串，
            # 不重算的话用户把两边改一致了也永远批不了（2026-10-13 小猫钓鱼 422 根因）。
            # 这是**用户显式改时长**的路径，所以守卫那条「时长变了」也按「已改回上一版」判。
            refresh_duration_blocking_issues(
                updated, explicit_duration_edit=True
            )
            if "prompt" in patch:
                updated["prompt_slots"] = None
                # 只对图片任务做 token 补齐：视频 prompt 的 @图片N 必须出现在
                # 具体镜头段落里，尾部追加反而会被判「只罗列未用于镜头」。
                if updated.get("task_type") == "image_to_image":
                    mime_types = {
                        asset.asset_id: asset.mime_type for asset in assets
                    }
                    references = [
                        ImageReference.model_validate(item)
                        for item in updated.get("reference_images", [])
                    ]
                    ordered = sorted(references, key=lambda item: item.order)
                    missing_tokens = [
                        token
                        for token in reference_tokens(
                            ordered, mime_types
                        ).values()
                        if token not in updated["prompt"]
                    ]
                    if missing_tokens:
                        updated["prompt"] = (
                            f"{updated['prompt']}，画面风格严格参考 "
                            f"{'、'.join(missing_tokens)}"
                        )
            try:
                updated_task = GenerationTask.model_validate(updated)
            except ValidationError as exc:
                compact = "; ".join(
                    ".".join(str(part) for part in item["loc"])
                    + ": "
                    + str(item["msg"])
                    for item in exc.errors(
                        include_url=False, include_input=False
                    )[:6]
                )
                raise RunValidationError(f"修改后的任务参数无效：{compact}") from None
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)
            return {
                "status": "updated",
                "task": updated_task.model_dump(mode="json"),
            }

    async def optimize_prompts(
        self,
        run_id: str,
        *,
        task_ids: list[str],
        feedback: str,
    ) -> dict[str, Any]:
        """按用户要求**原地**用 AI 重写选中任务的提示词，仍停在审批页。

        与「退回修改计划」的区别：不重新规划、不新建运行、不动分镜结构与其它任务，
        也不覆盖用户手工改过的其它字段 —— 只把勾中那几条任务的提示词按这次要求
        改写一遍，改完还停在审批页给用户过目。

        融合语义与成片返工、多维表格重跑**完全一致**（共用 `rework_inputs` +
        `build_rework_prompt`）：历次要求只累积不覆盖、优先 AI 融合、永不因超长
        失败（超长只截断并挂 warning）。
        """
        text = (feedback or "").strip()
        if not text:
            raise RunValidationError("请填写优化要求")
        # 审批页的原地优化也是「用户说的话」→ 同样自动沉淀成长期口径。
        self._remember_requirements(text)
        wanted = list(dict.fromkeys(task_ids or []))
        if not wanted:
            raise RunValidationError("请至少选择一条需要优化的任务")

        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            assets = self._state_assets(state)
            known = {task.task_id for task in plan.tasks}
            unknown = [task_id for task_id in wanted if task_id not in known]
            if unknown:
                raise RunValidationError("未知任务：" + "、".join(sorted(unknown)))

            wanted_set = set(wanted)
            tasks: list[GenerationTask] = []
            optimized: list[str] = []
            unchanged: list[str] = []
            #: 「没改动」的**真实原因**（2026-11-05）：以前只说「多半是模型限流或
            #: 上游报错」，把「超长被契约拒掉」也蒙在里面。
            unchanged_reasons: dict[str, str] = {}
            # 审批页优化**也要看上一版成片**（用户 2026-09-28）：这条路以前完全不看
            # 视频，才会把「气球从手上方滑走」改成一段自相矛盾的病句。当前 run 还没
            # 生成（artifacts=0），成片在**同一条需求上一个跑完的 run** 里。
            take_artifacts = await self._previous_take_artifacts(
                source_url=str(state.get("source_url") or ""),
                exclude_run_id=run_id,
            )
            media_parts = await self._take_media_parts(take_artifacts)
            visual_context = ""
            if media_parts:
                event_text = "已把上一版成片作为视频附进改写请求（一次多模态调用）"
            else:
                # 回落：成片传不上去时至少给一段带提示词的文字核对结论。
                visual_context = await describe_output_videos(
                    self.video_analyzer,
                    take_artifacts,
                    prompts={task.task_id: task.prompt for task in plan.tasks},
                )
                text_preview = " ".join((visual_context or "").split())
                event_text = (
                    f"已让模型看过上一版成片，改写时带上画面：{text_preview[:600]}"
                    if text_preview
                    else "本次改写没带上一版画面（没有成片文件，或看片失败）"
                )
            try:
                await self.repository.append_event(
                    run_id, "optimize_prompts", "running", event_text
                )
            except Exception:  # noqa: BLE001 —— 记事件失败不该挡住优化本身
                _LOGGER.warning("记录优化画面事件失败 run=%s", run_id, exc_info=True)
            for task in plan.tasks:
                if task.task_id not in wanted_set:
                    tasks.append(task)
                    continue
                base_prompt, requirements = rework_inputs(task, text)
                fusion_diagnostics: dict[str, Any] = {}
                prompt, truncated, _must_avoid = await build_rework_prompt(
                    base_prompt,
                    # 只喂**本轮**要求；`requirements`（累积）仍写回任务做记录。
                    fusion_requirements(text),
                    fuse=self.rework_fuser,
                    visual_context=visual_context,
                    media_parts=media_parts,
                    diagnostics=fusion_diagnostics,
                )
                updates: dict[str, Any] = {
                    "prompt": prompt,
                    "rework_requirements": requirements,
                    # 冻结锚点只写第一次；展示用的「改前」是这一版被优化前的样子。
                    "rework_base_prompt": task.rework_base_prompt or base_prompt,
                    "rework_previous_prompt": task.prompt,
                    "negative_constraints": merge_negative_constraints(
                        task.negative_constraints, []
                    ),
                }
                if truncated:
                    trimmed_chars = int(fusion_diagnostics.get("trimmed_chars") or 0)
                    updates["warnings"] = [
                        *task.warnings,
                        (
                            # 2026-11-05：新路径——融合结果只多出几十字，裁掉尾部后
                            # 照样采纳（旧口径是整条丢弃、正文一个字不改）。
                            f"融合结果超长，已按句子边界裁掉结尾 {trimmed_chars} 字后采纳"
                            if trimmed_chars
                            else "优化要求过多，提示词已截断；历史要求仍完整保留"
                        ),
                    ]
                # 手工编辑过的提示词以人的版本为准：清空槽位，避免后续校验用槽位
                # 拼装把这次改写覆盖掉（与 patch_task 同一口径）。
                updates["prompt_slots"] = None
                tasks.append(task.model_copy(update=updates))
                if prompt.strip() == (task.prompt or "").strip():
                    unchanged.append(task.task_id)
                    unchanged_reasons[task.task_id] = str(
                        fusion_diagnostics.get("rejection_reason") or ""
                    ).strip()
                else:
                    optimized.append(task.task_id)

            updated_plan = plan.model_copy(update={"tasks": tasks})
            await self._persist_draft(run, state, updated_plan, assets)
            try:
                await self.repository.append_event(
                    run_id,
                    "optimize_prompts",
                    "completed",
                    (
                        f"已按「{text[:80]}」用 AI 重写提示词："
                        + ("、".join(optimized) if optimized else "无")
                        + (
                            "；融合未生效（"
                            + "；".join(
                                f"{task_id}：{unchanged_reasons.get(task_id) or '原因未记录'}"
                                for task_id in unchanged
                            )
                            + "）"
                            if unchanged
                            else ""
                        )
                    ),
                )
            except Exception:  # noqa: BLE001 —— 记事件失败不该挡住优化本身
                _LOGGER.warning("记录提示词优化事件失败 run=%s", run_id, exc_info=True)
            # 2026-10-22 用户要求：「优化所选提示词」也要看到改动差异，并与「重做」同一个接口口径。
            # 对比数据在这里一次给出（**只含正文真被改写过的任务**），前端拿到就能立刻渲染 diff，
            # 不必等 poll 回来的那一版视图 —— 轮询慢一拍时用户也已经看到「改了哪几句」。
            # 原样保留（融合没生效）的任务不给对比：那是「没改」，给了反而误导。
            comparisons = self._rework_comparisons(
                tasks, only=set(optimized)
            )
            return {
                "status": "updated",
                "optimized_task_ids": optimized,
                "unchanged_task_ids": unchanged,
                "comparisons": comparisons,
            }

    async def set_reference_cleanup_decision(
        self,
        run_id: str,
        asset_id: str,
        decision: str,
        selected: str | None = None,
    ) -> dict[str, Any]:
        """记录用户对这张参考图去水印结果的选择（`auto` / `cleaned` / `original`）。

        用户 2026-10-23 口径：「可以让用户看到清洗后的图片自由选择原图或者继续重新清洗」。
        存进台账后，**预览与提交都按它生效**（`_adopted_cleanup_path` 是唯一收口点）。
        非法取值抛 `RunValidationError`（前端只可能传这三个）。
        """
        from feishu_generation_agent.integrations.reference_watermark_clean import (
            read_cleanup_status,
            set_cleanup_decision,
        )

        outputs_dir = Path(self.settings.outputs_dir)
        entry = read_cleanup_status(run_id, outputs_dir).get(asset_id)
        if not entry:
            # 台账里没有这张图 → 说明它压根没参与过去水印（没水印 / 不是图片 / 清洗没开）。
            # 不写一个"空条目"进去：那会让前端以为它清洗过（而且 `effective_cleanup_path`
            # 会把它当"有结论"从而跳过兜底清洗）。
            raise RunValidationError(
                f"参考图 {asset_id} 没有去水印记录（可能未检测到水印），无需选择"
            )
        if not await set_cleanup_decision(
            run_id,
            outputs_dir,
            asset_id,
            decision,
            selected=selected,
            # 2026-10-28：把这个选择按**原图内容 sha** 记进跨运行记忆 —— 下一版运行
            # （新 run）自动继承，不再被重置成 auto 后回退原图。
            data_dir=Path(self.settings.data_dir),
            source_path=(
                await self.get_reference_file(run_id, asset_id, prefer_cleaned=False)
            )[0],
        ):
            raise RunValidationError(
                "这一版清洗把画面里的文字/内容洗掉了，不能选用；"
                "请重新清洗，或选择用原图"
            )
        return read_cleanup_status(run_id, outputs_dir).get(asset_id) or {}

    async def reclean_reference(self, run_id: str, asset_id: str) -> dict[str, Any]:
        """**重新清洗**这张参考图：产出新版本（`-nowm-2`…），旧版本保留。

        重绘有随机性 —— 重洗是拿到更干净一版的最实际手段（**刻意不做局部重绘**：
        用户 2026-10-23「局部重绘拼接的时候会有明显漏洞」）。
        """
        from feishu_generation_agent.integrations.reference_watermark_clean import (
            read_cleanup_status,
            reclean_reference_image,
        )

        # `prefer_cleaned=False` 拿到的是**原图**（清洗版是另存的文件，不改 `local_path`）。
        original, _mime = await self.get_reference_file(
            run_id, asset_id, prefer_cleaned=False
        )
        outputs_dir = Path(self.settings.outputs_dir)
        probe = self.cleanup_scope_probe
        await reclean_reference_image(
            original,
            run_id=run_id,
            outputs_dir=outputs_dir,
            asset_id=asset_id,
            # 重洗 = 重新来过 → 顺手抹掉跨运行记忆，免得更早那次的旧选择又被搬回来。
            data_dir=Path(self.settings.data_dir),
            # 重洗前同样先确认清洗范围（不然重洗会再把字洗掉一次）。
            scope_probe=(
                (lambda asset: probe.probe(asset)) if probe is not None else None
            ),
        )
        return read_cleanup_status(run_id, outputs_dir).get(asset_id) or {}

    async def get_reference_file(
        self,
        run_id: str,
        asset_id: str,
        *,
        prefer_cleaned: bool = True,
        version: int | None = None,
    ) -> tuple[Path, str]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        # 分镜关键帧候选图单独一条通道（不放 media_assets，免得触发未覆盖素材门禁），
        # 但预览走同一个端点，所以这里也要查一遍。
        for collection in ("media_assets", "storyboard_assets"):
            for item in snapshot.values.get(collection, []):
                if not isinstance(item, dict) or item.get("asset_id") != asset_id:
                    continue
                try:
                    asset = MediaAsset.model_validate(item)
                except Exception:
                    break
                resolved_path = asset.local_path.resolve()
                # 参考图去水印（2026-10-20 用户口径：「对应参考图给出前端反馈」）：
                # 后台清洗**完成并采用**之后，缩略图直接给**清洗版** —— 界面上的素材图
                # 会自动变成干净的那张；没洗成/没水印就仍然是原图。
                # `prefer_cleaned=False` 时强制给原图：前端「原图 / 已去水印版」切换用
                # （同一个端点 + `?original=1`，不新增端点）。**原图永远可看**是硬要求 ——
                # 用户 2026-10-10 撤掉旧版去水印功能的理由就是「它把我的原图改掉了」。
                if version is not None:
                    # 指定版本（前端列历史版本、来回切用）；只认台账里记过的版本。
                    picked = _cleanup_version_path(
                        self.settings, run_id, asset_id, int(version)
                    )
                    if picked is not None and picked.is_file():
                        resolved_path = picked.resolve()
                elif prefer_cleaned:
                    cleaned = _adopted_cleanup_path(self.settings, run_id, asset_id)
                    if cleaned is not None and cleaned.is_file():
                        #: 再 resolve 一次不是多余的：下面拿它跟**绝对**允许根比 is_relative_to，
                        #: 任何来源的相对路径都会判否 → break → 预览 404（2026-10-20 实测踩到）。
                        resolved_path = cleaned.resolve()
                # 文档素材落在 data/ 下，而分镜候选图是 provider 结果、落在 outputs/
                # 下（materialize_provider_result 的落盘位置）。两个根都是本应用自己
                # 的目录，都要放行 —— 只认 data/ 会让预览 404（2026-09-22 实跑踩到）。
                allowed_roots = [
                    self.settings.data_dir.resolve(),
                    self.settings.outputs_dir.resolve(),
                ]
                if (
                    not asset.mime_type.startswith(("image/", "video/", "audio/"))
                    or not any(
                        resolved_path.is_relative_to(root)
                        for root in allowed_roots
                    )
                    or not resolved_path.is_file()
                ):
                    break
                return resolved_path, asset.mime_type
        raise RunNotFound("参考素材不存在")

    async def get_artifact_file(
        self,
        run_id: str,
        artifact_id: str,
    ) -> tuple[Path, str]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        for artifact in await self.repository.list_artifacts(run_id):
            if artifact.artifact_id != artifact_id:
                continue
            resolved_path = artifact.local_path.resolve()
            outputs_root = self.settings.outputs_dir.resolve()
            if (
                artifact.kind not in {"image", "video"}
                or not artifact.mime_type.startswith(("image/", "video/"))
                or not resolved_path.is_relative_to(outputs_root)
                or not resolved_path.is_file()
            ):
                break
            return resolved_path, artifact.mime_type
        raise RunNotFound("成片产物不存在")

    async def retry_failed_assets(self, run_id: str) -> dict[str, int]:
        if self.document_source is None:
            raise RunConflict("飞书素材读取服务尚未配置")
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在处理其他操作")
        async with lock:
            run, state = await self._waiting_state(run_id)
            try:
                document = NormalizedDocument.model_validate(
                    state.get("normalized_document")
                )
            except Exception:
                raise RunValidationError("当前文档素材状态无效") from None
            failed_before = {
                asset.asset_id
                for asset in document.media_assets
                if asset.download_error is not None
                and asset.asset_id.startswith(("image-", "video-"))
            }
            if not failed_before:
                raise RunValidationError("当前没有可重新读取的失败素材")
            await self.repository.append_event(
                run_id,
                "retry_failed_assets",
                "started",
                f"Retrying {len(failed_before)} failed assets",
            )
            try:
                refreshed = await self.document_source.retry_failed_assets(document)
            except AgentError as exc:
                await self.repository.append_event(
                    run_id,
                    "retry_failed_assets",
                    "failed",
                    exc.detail.message,
                )
                raise RunConflict(exc.detail.message) from None
            except Exception:
                await self.repository.append_event(
                    run_id,
                    "retry_failed_assets",
                    "failed",
                    "Failed asset retry did not complete",
                )
                raise RunConflict("重新读取失败素材未完成，请稍后重试") from None

            remaining = {
                asset.asset_id
                for asset in refreshed.media_assets
                if asset.download_error is not None
                and asset.asset_id in failed_before
            }
            recovered = failed_before - remaining
            plan = self._state_plan(state)
            recovered_video_assets = [
                asset
                for asset in refreshed.media_assets
                if asset.asset_id in recovered and asset.mime_type.startswith("video/")
            ]
            if recovered_video_assets:
                for task in plan.tasks:
                    if task.video_provider is None or task.reference_mode != "multi_reference":
                        continue
                    existing_ids = {reference.asset_id for reference in task.reference_images}
                    next_order = max(
                        (reference.order for reference in task.reference_images),
                        default=0,
                    ) + 1
                    for asset in sorted(
                        recovered_video_assets,
                        key=lambda item: item.asset_id,
                    ):
                        if asset.asset_id in existing_ids:
                            continue
                        task.reference_images.append(
                            ImageReference(
                                asset_id=asset.asset_id,
                                role="reference_video",
                                order=next_order,
                            )
                        )
                        existing_ids.add(asset.asset_id)
                        next_order += 1
            descriptions = [
                VisionDescription.model_validate(item)
                for item in state.get("vision_descriptions", [])
            ]
            descriptions = [
                item for item in descriptions if item.asset_id not in recovered
            ]
            vision_issues = []
            if self.vision_analyzer is not None:
                for asset in refreshed.media_assets:
                    if asset.asset_id not in recovered or not asset.mime_type.startswith("image/"):
                        continue
                    try:
                        descriptions.append(await self.vision_analyzer.analyze(asset))
                    except Exception:
                        continue

            checkpoint_document = refreshed.model_copy(
                update={
                    "media_assets": [
                        asset.model_copy(update={"file_token": None})
                        for asset in refreshed.media_assets
                    ]
                }
            )
            refreshed_json = checkpoint_document.model_dump(mode="json")
            validation_issues = [
                record.display_message
                for record in resolve_ingest_issue_records(refreshed)
                if record.severity is IngestIssueSeverity.BLOCKING
            ]
            validation_issues.extend(
                validate_plan(
                    plan,
                    refreshed,
                    max_output_count=self.settings.max_output_count,
                )
            )
            revision = state.get("draft_revision")
            if not isinstance(revision, int):
                revision = state.get(
                    "document_revision", state.get("source_revision", 0)
                )
            updates = {
                "normalized_document": refreshed_json,
                "source_document": refreshed_json,
                "media_assets": refreshed_json["media_assets"],
                "draft_plan": plan.model_dump(mode="json"),
                "vision_descriptions": [
                    item.model_dump(mode="json") for item in descriptions
                ],
                "vision_issues": vision_issues,
                "validation_issues": validation_issues,
                "draft_revision": revision + 1,
                "status": "waiting_approval",
            }
            config = self._config(run["thread_id"])
            try:
                await self._graph_aupdate_state(
                    config, updates, as_node="validate_plan"
                )
                result = await self._graph_ainvoke(None, config=config)
            except Exception:
                raise RunConflict("更新审批 checkpoint 失败") from None
            if not self._has_interrupt(result):
                raise RunConflict("更新后未恢复到等待审批状态")
            await self.repository.update_run_status(run_id, "waiting_approval")
            await self.repository.append_event(
                run_id,
                "retry_failed_assets",
                "completed",
                f"Recovered {len(recovered)} assets; {len(remaining)} still failed",
            )
            return {
                "recovered_count": len(recovered),
                "remaining_count": len(remaining),
            }

    async def _waiting_state(
        self,
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        if run["status"] != "waiting_approval":
            # 文案要说清"现在是哪个阶段、该怎么做"。旧文案「只有等待审批的运行可以修改
            # 素材」把两个阶段混在一起 —— 界面里 `waiting_approval` 叫「等待你审核」、
            # `waiting_review` 叫「成片与结果」，用户 2026-09-29 正是在这个歧义上卡住
            # （「等待我审核这个阶段就传不了」），而且他并没有做错什么：参考素材的增删改
            # **天生只能在审批阶段**做（`_persist_draft` 会把运行推回审批中断、状态改回
            # waiting_approval），审核阶段本来就落不了地。
            stage = {
                "waiting_review": "成片与结果",
                "succeeded": "已完成",
                "running": "生成中",
                "resuming": "恢复中",
            }.get(run["status"], run["status"])
            raise RunConflict(
                f"当前阶段（{stage}）不能改计划与参考素材："
                "请先点「退回修改计划」回到审批页，改好后再「批准并开始生成」"
            )
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        state = dict(snapshot.values or {})
        if state.get("status") != "waiting_approval":
            raise RunConflict("当前 checkpoint 不在等待审批状态")
        return run, state

    @staticmethod
    def _state_plan(state: dict[str, Any]) -> TaskPlan:
        try:
            return TaskPlan.model_validate(
                state.get("draft_plan") or state.get("task_plan")
            )
        except Exception:
            raise RunValidationError("当前审批计划无效") from None

    @staticmethod
    def _state_assets(state: dict[str, Any]) -> list[MediaAsset]:
        """运行里所有可以作为参考图的素材：文档素材 + 规划阶段生成的分镜关键帧。

        分镜候选单独一条通道（**故意不进 media_assets**，否则会触发「未覆盖素材」
        门禁把审批页卡死），但它们会被写进任务的 reference_images —— 所以**每一处
        引用校验都必须认得它们**，漏一处就报「图片引用无效」（2026-09-22 实跑踩到：
        选完分镜图后再动参考图/批准，直接报这个错，视频生成不了）。
        """
        assets: list[MediaAsset] = []
        for item in [
            *(state.get("media_assets") or []),
            *(state.get("storyboard_assets") or []),
        ]:
            try:
                assets.append(MediaAsset.model_validate(item))
            except Exception:  # noqa: BLE001 —— 坏素材不该挡住其它素材
                continue
        return assets

    @staticmethod
    def _rework_comparisons(
        tasks: list[GenerationTask],
        *,
        only: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """给「改了哪几句」用的对比数据（2026-10-22 用户要求：优化提示词也要看得到差异）。

        两条返工链路（原地优化 / 退回重跑）都要能展示对比，所以口径收在这一处：

        - `previous_prompt` = **本次改写之前**那一版，是 diff 的左边（与前端
          `PromptDiff.reworkBasePrompt` 的优先字段一致）；
        - `base_prompt` = 冻结锚点（首次返工之前那版），只在没有 previous 时兜底；
        - `current_prompt` = 改写后的正文，diff 的右边。

        只回正文，不回 segments —— 句子级 diff 由前端 `prompt-diff.js` 算，
        这里**不重复实现**，也不产生任何额外模型调用。
        """
        result: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if only is not None and task.task_id not in only:
                continue
            previous = (task.rework_previous_prompt or "").strip()
            frozen = (task.rework_base_prompt or "").strip()
            current = (task.prompt or "").strip()
            base = previous or frozen
            result[task.task_id] = {
                "previous_prompt": task.rework_previous_prompt or "",
                "base_prompt": base,
                "current_prompt": task.prompt or "",
                "requirements": list(task.rework_requirements or []),
                "changed": bool(base) and current != previous,
            }
        return result

    @staticmethod
    def _task(plan: TaskPlan, task_id: str) -> tuple[int, GenerationTask]:
        for index, task in enumerate(plan.tasks):
            if task.task_id == task_id:
                return index, task
        raise RunValidationError(f"未知任务：{task_id}")

    def _task_with_references(
        self,
        task: GenerationTask,
        references: list[ImageReference],
        assets: list[MediaAsset],
        *,
        reference_mode: str | None = None,
        replacement_asset_ids: dict[str, str] | None = None,
    ) -> GenerationTask:
        try:
            canonical_references = canonicalize_references(references)
            mime_types = {
                asset.asset_id: asset.mime_type for asset in assets
            }
            prompt = remap_prompt_references(
                task.prompt,
                task.reference_images,
                canonical_references,
                mime_types,
                replacement_asset_ids=replacement_asset_ids,
            )
            updated = GenerationTask.model_validate(
                task.model_dump(mode="json")
                | {
                    "reference_images": [
                        reference.model_dump(mode="json")
                        for reference in canonical_references
                    ],
                    "reference_mode": reference_mode or task.reference_mode,
                    "prompt": prompt,
                }
            )
            self._validate_references(
                updated.task_type.value,
                updated.reference_images,
                {asset.asset_id: asset for asset in assets},
                updated.reference_mode,
            )
            return updated
        except RunValidationError:
            raise
        except ReferenceRemapError as exc:
            raise RunValidationError(str(exc)) from None
        except Exception as exc:
            message = str(exc)
            if "reference_images" in message:
                raise RunValidationError("任务必须保留至少一张参考图片") from None
            raise RunValidationError("图片引用无效") from None

    @staticmethod
    def _replace_task(
        plan: TaskPlan,
        task_index: int,
        task: GenerationTask,
    ) -> TaskPlan:
        tasks = list(plan.tasks)
        tasks[task_index] = task
        return reconcile_task_asset_coverage(plan, tasks)

    async def _persist_draft(
        self,
        run: dict[str, Any],
        state: dict[str, Any],
        plan: TaskPlan,
        assets: list[MediaAsset],
        extra_updates: dict[str, Any] | None = None,
    ) -> None:
        asset_json = [asset.model_dump(mode="json") for asset in assets]
        normalized = self._document_assets(state.get("normalized_document"), asset_json)
        source_document = self._document_assets(state.get("source_document"), asset_json)
        validation_issues: list[str] = []
        if normalized is not None:
            try:
                document = NormalizedDocument.model_validate(normalized)
                # 分镜行 id 确定性补齐：重跑走的是「复制上一份已批准计划」，
                # 模型没有机会重抄，缺的行会一直缺（实测 2026-09-17 超级大床）。
                plan = reconcile_storyboard_sources(plan, document)
                validation_issues = [
                    record.display_message
                    for record in resolve_ingest_issue_records(document)
                    if record.severity is IngestIssueSeverity.BLOCKING
                ]
                validation_issues.extend(
                    validate_plan(
                        plan,
                        document,
                        max_output_count=self.settings.max_output_count,
                    )
                )
            except Exception:
                raise RunValidationError("更新后的任务计划无法验证") from None
        revision = state.get("draft_revision")
        if not isinstance(revision, int):
            revision = state.get("document_revision", state.get("source_revision", 0))
        # 补齐后再落盘（上面的 plan 可能已被 reconcile 过）。
        # 「必须避免」里的重复写法也顺手清一遍（去重上线前的残留会被一直复制下去）。
        plan = reconcile_negative_constraints(plan)
        plan_json = plan.model_dump(mode="json")
        updates: dict[str, Any] = {
            "draft_plan": plan_json,
            "task_plan": plan_json,
            "media_assets": asset_json,
            "draft_revision": revision + 1,
            "approval_decision": None,
            "approved_tasks": [],
            "approved_plan": None,
            "validation_issues": validation_issues,
            "status": "waiting_approval",
        }
        if normalized is not None:
            updates["normalized_document"] = normalized
        if source_document is not None:
            updates["source_document"] = source_document
        if extra_updates:
            updates.update(extra_updates)
        config = self._config(run["thread_id"])
        try:
            await self._graph_aupdate_state(
                config,
                updates,
                as_node="validate_plan",
            )
            result = await self._graph_ainvoke(None, config=config)
        except Exception:
            raise RunConflict("更新审批 checkpoint 失败") from None
        if not self._has_interrupt(result):
            raise RunConflict("更新后未恢复到等待审批状态")
        await self.repository.update_run_status(run["run_id"], "waiting_approval")

    @staticmethod
    def _document_assets(value: Any, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        updated = dict(value)
        updated["media_assets"] = assets
        return updated

    def _validate_decision(
        self,
        state: dict[str, Any],
        decision: ApprovalDecision,
    ) -> None:
        if decision.action != "approve":
            return
        raw_plan = state.get("draft_plan") or state.get("task_plan")
        try:
            original = TaskPlan.model_validate(raw_plan)
            candidate = (
                reconcile_task_asset_coverage(original, decision.tasks)
                if decision.tasks
                else original
            )
            original_ids = {task.task_id for task in original.tasks}
            if any(task.task_id not in original_ids for task in candidate.tasks):
                raise ValueError("编辑结果包含未知任务")
            assets: dict[str, MediaAsset] = {}
            # 分镜关键帧候选图也要进这张表：选中的关键帧会被写进任务的
            # reference_images，引用校验必须能解析到它们（否则审批直接报
            # 「参考素材不存在」）。
            for item in [
                *(state.get("media_assets", [])),
                *(state.get("storyboard_assets", [])),
            ]:
                try:
                    asset = MediaAsset.model_validate(item)
                except Exception:
                    continue
                assets[asset.asset_id] = asset
            for task in candidate.tasks:
                self._validate_references(
                    task.task_type.value,
                    task.reference_images,
                    assets,
                    task.reference_mode,
                )
            # 审批前按当前 prompt/duration **重算**「时长不一致」阻断项 —— 这是兜底：
            # 存量计划的这条文案可能是在旧时长下写的，用户改对之后（改时长或改提示词）
            # 它不会自己消失，会让审批**永久 422**（2026-10-13 小猫钓鱼 run 788ab355）。
            # 真不一致的会在这一步被**补回来**，所以不会放过真的问题。
            refreshed_tasks = []
            for task in candidate.tasks:
                payload = task.model_dump(mode="json")
                refresh_duration_blocking_issues(payload)
                refreshed_tasks.append(GenerationTask.model_validate(payload))
            candidate = candidate.model_copy(update={"tasks": refreshed_tasks})
            approved = candidate.approved_subset(
                decision.selected_task_ids,
                self.settings.max_output_count,
            )
            if not approved.tasks:
                raise ValueError("没有可批准的任务")
            normalized = state.get("normalized_document")
            if isinstance(normalized, dict):
                document = NormalizedDocument.model_validate(normalized)
                issue_records = resolve_ingest_issue_records(document)
                if any(
                    record.severity is IngestIssueSeverity.BLOCKING
                    for record in issue_records
                ):
                    raise RunValidationError(
                        "文档存在阻断性读取问题，请修复源文档后重试"
                    )
                issues = validate_plan(
                    approved,
                    document,
                    max_output_count=self.settings.max_output_count,
                )
                if issues:
                    if any("asset_coverage" in issue for issue in issues):
                        raise RunValidationError("素材覆盖不完整，请先处理未使用素材")
                    raise RunValidationError(
                        language_validation_message(issues)
                        or "审批计划未通过校验"
                    )
        except RunValidationError:
            raise
        except Exception as exc:
            message = str(exc)
            if "unknown selected task_id" in message:
                missing = message.rsplit(":", 1)[-1].strip()
                raise RunValidationError(f"未知任务：{missing}") from None
            raise RunValidationError("审批任务无效") from None

    @staticmethod
    def _validate_references(
        task_type: str,
        references: Any,
        known_assets: dict[str, MediaAsset],
        reference_mode: str | None = None,
    ) -> None:
        asset_ids = [reference.asset_id for reference in references]
        if any(asset_id not in known_assets for asset_id in asset_ids):
            raise RunValidationError("编辑结果引用了未知素材")
        if any(
            known_assets[asset_id].download_error is not None
            for asset_id in asset_ids
        ):
            raise RunValidationError("编辑结果引用了下载失败素材")
        if len(asset_ids) != len(set(asset_ids)):
            raise RunValidationError("同一任务不能重复引用同一图片")
        orders = [reference.order for reference in references]
        if len(orders) != len(set(orders)):
            raise RunValidationError("同一任务的图片顺序不能重复")
        roles = [reference.role for reference in references]
        allowed_roles = {
            "reference_image",
            "first_frame",
            "last_frame",
            "reference_video",
            "reference_audio",
        }
        if any(role not in allowed_roles for role in roles):
            raise RunValidationError("参考素材用途无效")
        for reference in references:
            mime_type = known_assets[reference.asset_id].mime_type
            valid = (
                (mime_type.startswith("image/") and reference.role in {"reference_image", "first_frame", "last_frame"})
                or (mime_type.startswith("video/") and reference.role == "reference_video")
                or (mime_type.startswith("audio/") and reference.role == "reference_audio")
            )
            if not valid:
                raise RunValidationError("参考素材类型与用途不匹配")
        ordered_roles = [
            reference.role
            for reference in sorted(references, key=lambda reference: reference.order)
        ]
        if task_type == "image_to_image":
            if reference_mode == "first_last_frame":
                raise RunValidationError("图生图只能使用多参考模式")
            if any(role != "reference_image" for role in roles):
                raise RunValidationError("图生图只接受普通参考图")
            return
        if reference_mode == "first_last_frame":
            if ordered_roles != ["first_frame", "last_frame"]:
                raise RunValidationError(
                    "首尾帧模式必须且只能按顺序指定一张首帧和一张尾帧"
                )
            return
        if reference_mode == "multi_reference":
            if any(role in {"first_frame", "last_frame"} for role in roles):
                raise RunValidationError("多参考模式不能使用首帧或尾帧")
            return
        if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
            raise RunValidationError("首帧或尾帧用途不能重复")
        frame_roles = {"first_frame", "last_frame"}.intersection(roles)
        if "reference_image" in roles and frame_roles:
            raise RunValidationError("普通参考图不能与首尾帧混用")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _graph_ainvoke(
        self,
        value: Any,
        *,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        private_config = {**config, "callbacks": []}
        with tracing_context(enabled=False, parent=False):
            with set_config_context(private_config) as context:
                task = context.run(
                    asyncio.create_task,
                    self.graph.ainvoke(value, config=private_config),
                )
                return await task

    async def _graph_aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str,
    ) -> None:
        private_config = {**config, "callbacks": []}
        with tracing_context(enabled=False, parent=False):
            with set_config_context(private_config) as context:
                task = context.run(
                    asyncio.create_task,
                    self.graph.aupdate_state(
                        private_config,
                        values,
                        as_node=as_node,
                    ),
                )
                await task

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _has_interrupt(result: dict[str, Any]) -> bool:
        interrupts = result.get("__interrupt__")
        return isinstance(interrupts, (list, tuple)) and bool(interrupts)

    @staticmethod
    def _interrupt_actions(result: dict[str, Any]) -> set[str]:
        interrupts = result.get("__interrupt__")
        actions: set[str] = set()
        if not isinstance(interrupts, (list, tuple)):
            return actions
        for item in interrupts:
            value = getattr(item, "value", None)
            if isinstance(value, dict) and isinstance(value.get("action"), str):
                actions.add(value["action"])
        return actions

    def _waiting_status(self, result: dict[str, Any]) -> str | None:
        if not self._has_interrupt(result):
            return None
        actions = self._interrupt_actions(result)
        if "review_artifacts" in actions:
            return "waiting_review"
        if "review_plan" in actions:
            return "waiting_approval"
        return None

    @staticmethod
    def _safe_status(value: Any, fallback: str) -> str:
        return value if isinstance(value, str) and value else fallback

    @staticmethod
    def _safe_media_assets(run_id: str, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        assets: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            asset_id = item.get("asset_id")
            if not isinstance(asset_id, str):
                continue
            assets.append(
                {
                    "asset_id": asset_id,
                    "origin": item.get("origin"),
                    "mime_type": item.get("mime_type"),
                    "size": item.get("size"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "download_failed": item.get("download_error") is not None,
                    "preview_url": (
                        f"/api/runs/{run_id}/references/{asset_id}/content"
                        if item.get("download_error") is None
                        else None
                    ),
                }
            )
        return assets

    @staticmethod
    def _asset_coverage(
        plan: TaskPlan | None,
        media_assets: Any,
    ) -> dict[str, int]:
        successful_ids: set[str] = set()
        failed_ids: set[str] = set()
        if isinstance(media_assets, list):
            for item in media_assets:
                if not isinstance(item, dict):
                    continue
                asset_id = item.get("asset_id")
                if not isinstance(asset_id, str) or not asset_id:
                    continue
                if item.get("download_error") is None:
                    successful_ids.add(asset_id)
                else:
                    failed_ids.add(asset_id)
        referenced_ids = (
            {
                reference.asset_id
                for task in plan.tasks
                for reference in task.reference_images
            }
            if plan is not None
            else set()
        )
        excluded_ids = (
            {item.asset_id for item in plan.excluded_assets}
            if plan is not None
            else set()
        )
        referenced = successful_ids.intersection(referenced_ids)
        excluded = successful_ids.intersection(excluded_ids) - referenced
        uncovered = successful_ids - referenced - excluded
        return {
            "successful_total": len(successful_ids),
            "referenced_count": len(referenced),
            "excluded_count": len(excluded),
            "uncovered_count": len(uncovered),
            "failed_count": len(failed_ids),
        }

    @staticmethod
    def _event_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        started: dict[str, datetime] = {}
        result: list[dict[str, Any]] = []
        for event in events:
            created_at = event.get("created_at")
            timestamp: datetime | None = None
            if isinstance(created_at, str):
                try:
                    timestamp = datetime.fromisoformat(created_at).astimezone(UTC)
                except ValueError:
                    timestamp = None
            node = event.get("node")
            status = event.get("status")
            if isinstance(node, str) and status == "started" and timestamp:
                started[node] = timestamp
            duration_ms: int | None = None
            if (
                isinstance(node, str)
                and status in {"completed", "failed"}
                and timestamp
                and node in started
            ):
                duration_ms = max(
                    0, int((timestamp - started.pop(node)).total_seconds() * 1000)
                )
            result.append(
                {
                    "node": node,
                    "status": status,
                    "summary": event.get("summary", ""),
                    "created_at": created_at,
                    "duration_ms": duration_ms,
                }
            )
        return result

    @staticmethod
    def _node_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for event in GraphRuntime._event_view(events):
            node = event.get("node")
            if not isinstance(node, str):
                continue
            item = grouped.setdefault(
                node,
                {
                    "node": node,
                    "status": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "duration_ms": None,
                    "retry_count": -1,
                    "summary": "",
                },
            )
            status = event.get("status")
            if status == "started":
                item["retry_count"] += 1
                item["started_at"] = event.get("created_at")
                item["finished_at"] = None
            elif status in {"completed", "failed"}:
                item["finished_at"] = event.get("created_at")
                item["duration_ms"] = event.get("duration_ms")
            item["status"] = status
            item["summary"] = event.get("summary", "")
        for item in grouped.values():
            item["retry_count"] = max(0, item["retry_count"])
        return list(grouped.values())

    @staticmethod
    def _interrupt_view(snapshot: Any, state: dict[str, Any]) -> dict[str, str] | None:
        for task in getattr(snapshot, "tasks", ()):
            for pending in getattr(task, "interrupts", ()):
                value = getattr(pending, "value", None)
                if isinstance(value, dict) and value.get("action") == "review_plan":
                    return {
                        "action": "review_plan",
                        "status": GraphRuntime._safe_status(
                            state.get("status"), "waiting_approval"
                        ),
                    }
                if isinstance(value, dict) and value.get("action") == "review_artifacts":
                    return {
                        "action": "review_artifacts",
                        "status": GraphRuntime._safe_status(
                            state.get("status"), "waiting_review"
                        ),
                    }
        return None
