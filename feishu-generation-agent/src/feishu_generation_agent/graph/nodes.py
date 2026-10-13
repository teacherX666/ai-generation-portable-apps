import asyncio
import logging
import re
import tempfile
import time
from inspect import Parameter, signature
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, TypeVar
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from feishu_generation_agent.config import Settings
from io import BytesIO
from pathlib import Path

from PIL import Image

from feishu_generation_agent.domain.asset_library import normalize_alias
from feishu_generation_agent.domain.character_matcher import match_characters
from feishu_generation_agent.integrations.image_resize import (
    cover_crop,
    parse_size_variant,
)
from feishu_generation_agent.domain.document import (
    MediaAsset,
    NormalizedDocument,
    PlanningPromptSnapshot,
    RequirementRequest,
    VideoReferenceAnalysis,
    VideoReferenceKind,
    VisionDescription,
    IngestIssueSeverity,
    build_planning_prompt_snapshot,
    resolve_ingest_issue_records,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.video_models import (
    DEFAULT_VIDEO_MODEL_KEY,
    VIDEO_MODEL_BY_KEY,
    normalize_video_task_payload,
    resolve_video_model_key,
)
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    ArtifactReviewDecision,
    AuditReport,
    GenerationTask,
    ImageReference,
    StoryboardFrame,
    TaskPlan,
    TaskType,
    reconcile_task_asset_coverage,
)
from feishu_generation_agent.domain.artifact import (
    Artifact,
    ExecutionRecord,
    ProviderSubmission,
)
from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    language_validation_message,
    planner_system_prompt,
    validate_plan,
)
from feishu_generation_agent.ports import (
    DeliveryWriter,
    DocumentSource,
    ImageGenerator,
    RequirementPlanner,
    VideoGenerator,
    VisionAnalyzer,
)
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    remap_prompt_references,
)
from feishu_generation_agent.integrations.video_reference import (
    choose_reference_frame,
    compress_video,
    count_decoded_frames,
    extract_video_frames,
    probe_video_duration,
    probe_video_info,
    trim_video,
)
from feishu_generation_agent.integrations.planning_media import (
    MediaUploadCache,
    build_planning_media_parts,
)
from feishu_generation_agent.integrations.planning_media import MediaUploadCache
from feishu_generation_agent.integrations.video_insight import (
    build_take_media_parts,
    describe_output_videos,
)
from feishu_generation_agent.storage.repository import Repository

from feishu_generation_agent.integrations.rag_prompt_optimizer import (
    fetch_knowledge_rules,
    format_knowledge_context,
)
from feishu_generation_agent.integrations.rework_prompt import (
    build_rework_prompt,
    fusion_requirements,
    inherit_rework_requirements,
    merge_negative_constraints,
    merge_requirements,
    rework_inputs,
)
from feishu_generation_agent.integrations.storyboard import (
    STORYBOARD_ORIGIN,
    StoryboardConfig,
    apply_selection as storyboard_apply_selection,
    build_storyboard_frames,
    default_selection as storyboard_default_selection,
    keyframe_prompt,
    merge_selection as storyboard_merge_selection,
    reference_ids_for_selection,
)
from .state import AgentState


@dataclass(frozen=True, slots=True)
class GraphServices:
    document_source: DocumentSource
    vision_analyzer: VisionAnalyzer | None
    planner: RequirementPlanner
    image_generator: ImageGenerator | None
    video_generator: VideoGenerator
    delivery_writer: DeliveryWriter
    repository: Repository
    file_store: FileStore
    settings: Settings
    portrait_video_generator: Any | None = None
    production_task_store: Any | None = None
    #: 能**直接看视频**的分析器（ds4.1 多模态）。参考视频只作分镜参考，不再抽帧。
    video_analyzer: Any | None = None
    #: 公开图床（多模态规划要把原图/视频传上去拿 https 链接给模型看）。
    public_media_host: Any | None = None
    # 图片 provider registry：{"banana": gen, "seedream": gen, "gpt-image2": gen}。
    # 为 None 时回落到单实例 image_generator，保持存量调用方零改动。
    image_providers: Mapping[str, ImageGenerator] | None = None
    # 角色素材库与语义匹配器，图片模式下用于自动挂载角色参考图。
    # 未配置时跳过自动挂载，人工仍可在审核界面手动挂。
    asset_library_store: Any | None = None
    character_matcher: Any | None = None
    # 本地 AI Port 视频 provider（minimax H3 all-reference，走 ComfyUI）。
    # Keep both video providers alive so each task can choose independently.
    seedance_video_generator: Any | None = None
    seedance_video_generators: Mapping[str, Any] | None = None
    # Local AI Port video provider (MiniMax H3 via ComfyUI).
    aiport_video_generator: Any | None = None
    # Global defaults; task-level provider fields always take precedence.
    provider_preferences: Any | None = None


_Result = TypeVar("_Result")
_NODE_SUMMARIES = {
    "ingest_source": "Source ingestion",
    "normalize_document": "Document normalization",
    "analyze_images": "Image analysis",
    "plan_requirements": "Requirement planning",
    "audit_plan": "Plan audit",
    "validate_plan": "Plan validation",
    "human_approval": "Human approval",
    "revalidate_approval": "Approval revalidation",
    "check_source_revision": "Source revision check",
    "execute_selected_tasks": "Approved task execution",
    "verify_and_download_artifacts": "Artifact verification",
    "review_artifacts": "Artifact review",
    "deliver_to_feishu": "Feishu delivery",
}

_PENDING_PROVIDER_STATUSES = frozenset(
    {"submitted", "pending", "queued", "running", "processing"}
)
_SUCCESS_PROVIDER_STATUSES = frozenset({"succeeded", "completed", "success"})
_TERMINAL_PROVIDER_PHASES = frozenset(
    {"submission_uncertain", "failed", "cancelled", "expired", "timed_out"}
)
async_sleep = asyncio.sleep

#: 分镜候选的生图重试次数与退避基数。并发调用时瞬时连接错误很常见（实测 6 个并发
#: 里 4 个中招），不重试就会出现「每段只剩一张」甚至整段消失。
_STORYBOARD_ATTEMPTS = 3
_STORYBOARD_RETRY_SECONDS = 4.0


def _validation_error(message: str = "The request is invalid") -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.VALIDATION,
            message=message,
            technical_detail="Input validation failed",
            retryable=False,
        )
    )


def _safe_error(exc: BaseException) -> AgentError:
    if isinstance(exc, AgentError):
        category = exc.detail.category
        retryable = exc.detail.retryable
        if category is ErrorCategory.VALIDATION:
            message = (
                exc.detail.message
                if exc.detail.message.startswith(
                    ("模型三次返回的 JSON 均未通过", "以下字段必须包含中文主体说明")
                )
                else "The request is invalid"
            )
        elif (
            category is ErrorCategory.PERMISSION
            and "GET /open-apis/wiki/v2/spaces/get_node"
            in exc.detail.technical_detail
            and "code=131006" in exc.detail.technical_detail
        ):
            # 131006 是飞书明确返回的 Wiki 节点读取权限错误。这里使用固定文案，
            # 既给用户可执行的处理建议，也不会把接口响应或凭证暴露到页面。
            message = (
                "飞书应用无权读取该 Wiki 文档。请在知识库中授予应用读取权限；"
                "如果链接来自其他飞书企业，请先将文档复制到当前企业后重试。"
            )
        else:
            message = "The workflow node could not be completed"
    elif isinstance(exc, ValidationError):
        category = ErrorCategory.VALIDATION
        retryable = False
        message = "The request is invalid"
    else:
        category = ErrorCategory.TRANSIENT
        retryable = False
        message = "The workflow node could not be completed"
    # 保留原始 technical_detail：planner 会把具体校验失败项写进去，
    # 无条件覆盖成通用文案会让「三次校验未通过」这类故障完全不可诊断，
    # 每次都得另写脚本复现。message 仍走上面的脱敏逻辑。
    inner_detail = (
        exc.detail.technical_detail
        if isinstance(exc, AgentError) and exc.detail.technical_detail
        else None
    )
    technical_detail = f"{category.value} in workflow node"
    if inner_detail:
        technical_detail = f"{technical_detail}; {inner_detail}"
    else:
        technical_detail = (
            f"{technical_detail}; exception_type={type(exc).__name__}"
        )
    return AgentError(
        ErrorDetail(
            category=category,
            message=message,
            technical_detail=technical_detail,
            retryable=retryable,
        )
    )


async def _run_node(
    state: AgentState,
    node: str,
    services: GraphServices,
    operation: Callable[[], Awaitable[_Result]],
) -> _Result:
    run_id = state.get("run_id", "unknown-run")
    summary = _NODE_SUMMARIES[node]
    await services.repository.append_event(
        run_id, node, "started", f"{summary} started"
    )
    failure: AgentError | None = None
    try:
        result = await operation()
    except Exception as exc:
        _LOGGER.exception(
            "workflow node failed node=%s run_id=%s exception_type=%s",
            node,
            run_id,
            type(exc).__name__,
        )
        failure = _safe_error(exc)
    if failure is not None:
        await services.repository.append_event(
            run_id,
            node,
            "failed",
            f"{summary} failed ({failure.detail.category.value})",
        )
        raise failure
    await services.repository.append_event(
        run_id, node, "completed", f"{summary} completed"
    )
    return result


def _configured_thread_id(config: Mapping[str, Any]) -> str | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get("thread_id")
    return value if isinstance(value, str) and value else None


def _ensure_thread_id(state: AgentState, config: Mapping[str, Any]) -> None:
    state_thread_id = state.get("thread_id")
    config_thread_id = _configured_thread_id(config)
    if (
        not isinstance(state_thread_id, str)
        or not state_thread_id
        or config_thread_id != state_thread_id
    ):
        raise _validation_error("The workflow thread is invalid")


def _document_for_checkpoint(document: NormalizedDocument) -> NormalizedDocument:
    assets = [
        asset.model_copy(update={"file_token": None})
        for asset in document.media_assets
    ]
    return document.model_copy(update={"media_assets": assets})


def _json_model(model: Any) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    json.dumps(payload, ensure_ascii=False)
    return payload


def _draft_plan(state: AgentState) -> Any:
    plan = state.get("draft_plan")
    return plan if plan is not None else state.get("task_plan")


def approved_plan_from_state(
    state: AgentState,
    *,
    max_output_count: int,
) -> TaskPlan:
    saved = state.get("approved_plan")
    if isinstance(saved, dict):
        return TaskPlan.model_validate(saved)

    draft = TaskPlan.model_validate(_draft_plan(state))
    approved_tasks = [
        GenerationTask.model_validate(task)
        for task in state.get("approved_tasks", [])
    ]
    selected_ids = [
        task.task_id
        for task in approved_tasks
    ]
    reconciled = reconcile_task_asset_coverage(draft, approved_tasks)
    return reconciled.approved_subset(
        selected_ids,
        max_output_count,
    )


def _document_revision(state: AgentState) -> Any:
    revision = state.get("document_revision")
    return revision if revision is not None else state.get("source_revision")


def _planning_prompt(state: AgentState) -> PlanningPromptSnapshot:
    value = state.get("planning_prompt")
    if value is not None:
        return PlanningPromptSnapshot.model_validate(value)
    # 快照必须按 mode 选契约：直连 run 走 prime-local 分支，快照会通过
    # exact_system_prompt 传给 planner，而该分支排在 image_mode 判断之前，
    # 直接压过图片契约。快照装错内容，图片模板就永远不会被执行。
    prompt_text = (
        image_planner_system_prompt()
        if state.get("planning_mode") == "image"
        else planner_system_prompt()
    )
    return build_planning_prompt_snapshot(
        owner_user_id="prime-local",
        source="prime",
        version=0,
        prompt_text=prompt_text,
    )


_IMAGE_REQUIREMENT_TYPE = "图片类"
_LOGGER = logging.getLogger(__name__)
# 同步型出图 provider：submit 即终态、结果直接落盘，没有异步轮询阶段。
# 下面两类判定原先写死 provider == "chiyun"，registry 拆出 banana /
# gpt-image2 / seedream 后必须一起覆盖，否则它们会绕过标识校验与
# 「落盘失败 → 提交状态未定」的判定。
_SYNC_IMAGE_PROVIDERS = frozenset(
    {"chiyun", "banana", "gpt-image2", "seedream"}
)


async def _resolve_character_assets(
    document: NormalizedDocument,
    services: GraphServices,
) -> list[Any]:
    """把文档里出现的角色对应到素材库条目。

    两级匹配：先按名字/别名字面命中，未覆盖的描述式指代交给语义匹配层。
    任一环节失败都退回已有结果，不让素材库问题拖垮整个 run——挂不上参考图
    只是少了自动化，人工仍可在审核界面手动挂。
    """
    store = getattr(services, "asset_library_store", None)
    if store is None:
        return []
    try:
        library = await store.list_all(limit=500)
    except Exception:
        _LOGGER.warning("素材库读取失败，跳过角色自动挂载", exc_info=True)
        return []
    if not library:
        return []

    by_id = {asset.asset_id: asset for asset in library}
    exact_matches = match_characters(list(document.blocks), library)
    exact_ids = [anchor.asset_id for anchor in exact_matches]
    semantic_ids: list[str] = []

    # 只有单一变体的角色才作为锚点。锚点的含义是「已确定、不要再输出」，
    # 而同名多变体恰恰需要语义层按上下文挑一个，把它们锁成锚点会剥夺
    # 这个判断机会。
    grouped: dict[str, list[Any]] = {}
    for anchor in exact_matches:
        grouped.setdefault(normalize_alias(anchor.name), []).append(anchor)
    anchors = [group[0] for group in grouped.values() if len(group) == 1]

    matcher = getattr(services, "character_matcher", None)
    if matcher is not None:
        try:
            result = await matcher.match(
                document.text_view, library, anchors=anchors
            )
        except Exception:
            _LOGGER.warning("角色语义匹配失败，仅使用精确匹配", exc_info=True)
        else:
            semantic_ids = [item.asset_id for item in result.matches]
            # 文档里出现但库里没有的角色，首次遇到即自动建档并一并挂上。
            for record in await _auto_ingest_characters(
                document, list(result.unresolved_candidates), store
            ):
                by_id[record.asset_id] = record
                semantic_ids.append(record.asset_id)

    return _collapse_variants(exact_ids, semantic_ids, by_id)


def _collapse_variants(
    exact_ids: list[str],
    semantic_ids: list[str],
    by_id: dict[str, Any],
) -> list[Any]:
    """同一角色只保留一个着装变体。

    角色名字面命中时，库里该角色的所有变体都会被精确匹配挑出来。把两套
    冲突服装同时作为参考图会让模型不知道该用哪套——角色一致性正是要避免
    这个。因此按角色名收敛：语义层明确挑过的变体优先，否则取精确匹配的
    第一个。
    """
    chosen: dict[str, str] = {}
    order: list[str] = []
    for asset_id in [*exact_ids, *semantic_ids]:
        asset = by_id.get(asset_id)
        if asset is None:
            continue
        key = normalize_alias(asset.name)
        if key not in chosen:
            chosen[key] = asset_id
            order.append(key)
        elif asset_id in semantic_ids and chosen[key] not in semantic_ids:
            # 语义层的选择覆盖精确匹配的任意变体。
            chosen[key] = asset_id
    return [by_id[chosen[key]] for key in order]


async def _auto_ingest_characters(
    document: NormalizedDocument,
    candidates: list[Any],
    store: Any | None,
) -> list[Any]:
    """把文档里出现、但素材库还没有的角色自动建档。

    参考图取「候选人物所在 block 之后最近的一张图」——需求文档惯例是
    角色名在前、设定图紧随其后。找不到图就跳过，宁可少建也不建错。

    自动入库的条目打 auto-ingested 标签并记录来源文档，方便人工事后复核；
    variant 一律先用「默认」，同人不同着装由人工在素材库界面拆分改名。
    """
    if store is None or not candidates:
        return []

    image_blocks = [
        block
        for block in sorted(document.blocks, key=lambda item: item.order)
        if block.image_asset_id
    ]
    if not image_blocks:
        return []
    assets_by_id = {
        asset.asset_id: asset for asset in document.media_assets
    }
    block_order = {
        block.block_id: block.order for block in document.blocks
    }

    created: list[Any] = []
    for candidate in candidates:
        name = candidate.proposed_name.strip()
        if not name:
            continue
        asset = _nearest_image_asset(
            candidate.block_ids, image_blocks, block_order, assets_by_id
        )
        if asset is None:
            continue
        try:
            content = asset.local_path.read_bytes()
        except OSError:
            _LOGGER.warning("自动入库读取参考图失败 name=%s", name)
            continue
        try:
            record = await store.create(
                name=name,
                variant="默认",
                content=content,
                mime_type=asset.mime_type,
                description=candidate.reason or "",
                tags=["auto-ingested", f"需求文档:{document.document_id}"],
            )
        except Exception:
            # 重名冲突或落盘失败都只跳过该候选，不影响其它候选与整个 run。
            _LOGGER.warning("自动入库失败 name=%s", name, exc_info=True)
            continue
        created.append(record)
    return created


def _nearest_image_asset(
    block_ids: tuple[str, ...],
    image_blocks: list[Any],
    block_order: dict[str, int],
    assets_by_id: dict[str, Any],
) -> Any | None:
    anchors = [
        block_order[block_id]
        for block_id in block_ids
        if block_id in block_order
    ]
    if not anchors:
        return None
    anchor = min(anchors)
    following = [
        block for block in image_blocks if block.order >= anchor
    ]
    chosen = following[0] if following else image_blocks[-1]
    return assets_by_id.get(chosen.image_asset_id)


async def _character_media_assets(
    resolved: list[Any],
    store: Any | None,
) -> list[MediaAsset]:
    """把素材库条目转成 MediaAsset，使其能被下游按普通参考图消费。

    转换后追加进 document.media_assets，_task_assets / provider / 校验
    全部走既有路径，无需为素材库单开分支。
    """
    if store is None or not resolved:
        return []
    media: list[MediaAsset] = []
    for asset in resolved:
        try:
            path = store.local_path(asset)
            content = path.read_bytes()
        except (OSError, AttributeError):
            _LOGGER.warning(
                "素材库文件不可读，跳过挂载 asset_id=%s", asset.asset_id
            )
            continue
        media.append(
            MediaAsset(
                asset_id=asset.asset_id,
                source_block_id=f"asset-library:{asset.asset_id}",
                origin="asset_library",
                local_path=path,
                mime_type=asset.mime_type,
                size=len(content),
                sha256=sha256(content).hexdigest(),
            )
        )
    return media


def _character_context_argument(
    planner: RequirementPlanner,
    resolved: list[Any],
) -> dict[str, str]:
    """把已解析的角色素材描述成 planner 可读的上下文。

    与 _planner_prompt_argument / _planner_mode_argument 同样的签名探测做法：
    老 planner 没有该参数时省略，避免 TypeError。
    """
    if not resolved:
        return {}
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        return {}
    parameter = parameters.get("character_context")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        return {}

    lines = [
        f"- {asset.name} / {asset.variant}（asset_id={asset.asset_id}）"
        f"：{asset.prompt_fragment or asset.description or '无附加描述'}"
        for asset in resolved
    ]
    return {"character_context": "\n".join(lines)}


async def _planning_mode_for_run(
    run_id: str,
    services: GraphServices,
    state: Mapping[str, Any] | None = None,
) -> str:
    """推导规划模式。

    优先级：
    1. state 里的显式声明——直连文档创建的 run 没有多维表格 binding，
       模式在创建时声明；人工改过模式时也以此为准。
    2. binding.planning_mode——图片需求来自另一张表，那张表没有
       「需求类型」字段，无法靠字段值判定。
    3. 需求类型字段——存量 binding 没有 planning_mode 时的回落。

    读取失败一律回落 video，不让一次多维表格抖动把整个 run 拖挂。
    """
    declared_state = (state or {}).get("planning_mode")
    if declared_state in {"image", "video"}:
        return declared_state
    store = getattr(services, "production_task_store", None)
    if store is None:
        # MVP 多维表格没有「需求类型」字段，也没有生产表 binding，
        # 按需求文档正文推断图片/视频，避免图片需求被硬塞进视频规划。
        normalized = (state or {}).get("normalized_document")
        text = ""
        if isinstance(normalized, Mapping):
            text = str(normalized.get("text_view") or "")
        elif normalized is not None:
            text = str(getattr(normalized, "text_view", "") or "")
        return _infer_planning_mode(text)
    try:
        binding = await store.get_by_run(run_id)
    except Exception:
        return "video"
    if binding is None:
        return "video"
    declared = getattr(binding, "planning_mode", None)
    if declared in {"image", "video"}:
        return declared
    task_type = getattr(getattr(binding, "snapshot", None), "task_type", None)
    return "image" if task_type == _IMAGE_REQUIREMENT_TYPE else "video"


def _infer_planning_mode(text: str) -> str:
    """从需求文档正文推断图片/视频模式；默认视频，明确是图片时返回 image。"""
    content = text or ""
    image_keywords = (
        "图片", "一张图", "生成图", "海报", "插画", "壁纸",
        "头像", "设计图", "绘画", "静帧", "成图",
    )
    video_keywords = (
        "视频", "动画", "短片", "镜头", "运镜", "分镜", "片段", "动态",
    )
    wants_image = any(keyword in content for keyword in image_keywords)
    wants_video = any(keyword in content for keyword in video_keywords)
    return "image" if wants_image and not wants_video else "video"


#: 抽帧兜底时抽几帧（只用于「上游拒绝视频」的降级路径）。
#:
#: 2026-10-08 从 3 提到 5：3 帧时采样点已经是 25%/50%/75%，再排掉首尾 20% 就只剩
#: 中间一帧，「跳过带平台水印的帧」这条规则等于没有。5 帧的采样点是 16.7%~83.3%，
#: 排掉首尾后还有 3 个候选，选帧逻辑（避开水印 + 靠近中段）才真正起作用。
_VIDEO_FRAME_COUNT = 5


async def _analyze_video_reference(
    services: GraphServices,
    document_id: str,
    video: MediaAsset,
) -> VideoReferenceAnalysis | None:
    """把文档里的参考视频交给**能看视频的模型**分析，产出分镜参考用的文字描述。

    2026-09-17 用户要求：「不要抽帧，直接上传视频」+「视频基本上都没有能作为参考图
    的，只能作为分镜参考」。实测火山 ds4.1 接受 `video_url` 且真的看懂了内容，所以
    这里**不再抽帧、也不再伪造 `video-N-frame` 图片素材** —— 那张"参考图"会被模型
    当成人物形象锚点，正是「让它不要参考人物形象它还是参考」的来源。

    分析失败返回 None（不猜内容）：视频保持原样，只作分镜参考。
    """
    del document_id  # 兼容旧签名：不再落帧文件，用不到
    analyzer = getattr(services, "video_analyzer", None)
    if analyzer is None:
        return None
    try:
        return await analyzer.analyze_video(video, [])
    except Exception:
        _LOGGER.warning(
            "参考视频分析失败，本段只作分镜参考（不抽帧、不当参考图） video=%s",
            video.asset_id,
            exc_info=True,
        )
        return None


async def _materialize_video_references(
    document: NormalizedDocument,
    services: GraphServices,
) -> NormalizedDocument:
    """给文档里的参考视频补上「分镜参考」描述（不再抽帧、不再替换素材）。

    视频保持 `[video:*]` 标记留在文档里，模型据此理解动作/运镜/节奏；
    它**不会**被挂成参考图（planner 那边有确定性规则把它排除）。
    """
    video_assets = [
        asset
        for asset in document.media_assets
        if asset.mime_type.startswith("video/")
    ]
    if not video_assets:
        return document

    semantics: list[VideoReferenceAnalysis] = list(document.video_semantics)
    for video in video_assets:
        insight = await _analyze_video_reference(
            services,
            document.document_id,
            video,
        )
        if insight is not None:
            semantics.append(insight)

    return document.model_copy(update={"video_semantics": semantics})


def _planner_mode_argument(
    planner: RequirementPlanner,
    mode: str,
) -> dict[str, str]:
    """只在 planner 支持 mode 且需要非默认值时才传。

    存量测试里的 fake planner 大多没有 mode 参数，直接传会 TypeError；
    video 是默认值，省略可进一步减少对存量调用方的干扰。
    """
    if mode == "video":
        return {}
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        return {}
    parameter = parameters.get("mode")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        return {}
    return {"mode": mode}


def _knowledge_context_argument(
    planner: RequirementPlanner,
    knowledge_context: str,
) -> dict[str, str]:
    """把命中的知识库规则作为上下文传给 planner。

    与 _planner_mode_argument 同样的签名探测做法：存量测试里的 fake planner
    大多没有该参数，直接传会 TypeError；而 planner 本来就支持时，
    这条上下文会让它**一次**就把知识库经验写进提示词，不再需要事后改写。
    """
    if not knowledge_context:
        return {}
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        return {}
    if "knowledge_context" not in parameters:
        return {}
    return {"knowledge_context": knowledge_context}


async def _knowledge_context_for_plan(
    document: NormalizedDocument,
    services: GraphServices,
) -> str:
    """取知识库命中的规则文本；服务不可用或没命中都返回空串。

    实测真实文档 1.7k~7.6k 字符（最长约 23KB UTF-8），而 planner 本来就把
    document.text_view 全文当输入，所以这里直接用全文，不做截断。
    """
    rag_url = getattr(services.settings, "rag_preflight_url", None)
    if not (isinstance(rag_url, str) and rag_url.strip()):
        return ""
    text = getattr(document, "text_view", "") or ""
    if not text.strip():
        return ""
    rules = await fetch_knowledge_rules(text, rag_url)
    return format_knowledge_context(rules)


def _planner_prompt_argument(
    planner: RequirementPlanner,
    planning_prompt: PlanningPromptSnapshot,
) -> dict[str, str | None]:
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        parameters = {}
    exact_parameter = parameters.get("exact_system_prompt")
    is_local_prime = (
        planning_prompt.owner_user_id == "prime-local"
        and planning_prompt.source == "prime"
    )
    if (
        is_local_prime
        and exact_parameter is not None
        and exact_parameter.kind is not Parameter.POSITIONAL_ONLY
    ):
        return {"exact_system_prompt": planning_prompt.prompt_text}
    if is_local_prime:
        raise _validation_error(
            "The planner cannot replay the Prime prompt snapshot exactly"
        )
    parameter = parameters.get("system_prompt")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        raise _validation_error(
            "The planner cannot accept the planning prompt snapshot"
        )
    return {"system_prompt": planning_prompt.prompt_text}


async def ingest_source(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        source_url = state.get("source_url")
        if not isinstance(source_url, str) or not source_url:
            raise _validation_error("A source URL is required")
        planning_prompt = _planning_prompt(state)
        request = RequirementRequest(
            source_url=source_url,
            requester_open_id=state.get("requester_open_id"),
            trigger_type=state.get("trigger_type", "local_link"),
            reply_context=state.get("reply_context", {}),
            planning_prompt=planning_prompt,
        )
        document = _document_for_checkpoint(
            await services.document_source.ingest(request)
        )
        document_json = _json_model(document)
        return {
            "status": "running",
            "requirement_request": _json_model(request),
            "planning_prompt": _json_model(planning_prompt),
            "source_document": document_json,
            "source_type": document.source_type.value,
            "source_token": document.source_token,
            "document_id": document.document_id,
            "document_title": document.title,
            "document_revision": document.revision,
            "media_assets": document_json["media_assets"],
            "approval_decision": None,
            "approved_tasks": [],
            "approved_plan": None,
            "execution_records": [],
            "artifacts": [],
            "delivery_record": None,
            "last_error": None,
        }

    return await _run_node(state, "ingest_source", services, operation)


async def normalize_document(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(state.get("source_document"))
        document_json = _json_model(document)
        return {
            "normalized_document": document_json,
            "source_type": document.source_type.value,
            "source_token": document.source_token,
            "document_id": document.document_id,
            "document_title": document.title,
            "document_revision": document.revision,
            "media_assets": document_json["media_assets"],
            "source_revision": document.revision,
        }

    return await _run_node(state, "normalize_document", services, operation)


_VISION_MAX_CONCURRENCY = 5


async def analyze_images(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        mode = await _planning_mode_for_run(state["run_id"], services, state)
        if mode != "image":
            document = await _materialize_video_references(document, services)
        document_json = _json_model(document)
        # 多模态规划：**不再单独跑一遍图片分析**（用户 2026-09-23：「把原本 claude 干的
        # 工作换成 ds 就不能干了吗，而且 ds 一次性查文档看图片，生成的提示词就是效果
        # 更好、不会在转述中效果受损」）。原图和文档一起交给 ds4.1 看，少一次模型调用、
        # 少一层跨模型转述损耗。文本模式（`planning_pipeline=text`）仍走原来的
        # Claude 逐图分析 —— 那条路需要结构化描述当输入。
        if _planning_pipeline(services) == "multimodal":
            return {
                "vision_descriptions": [],
                "vision_issues": [],
                "normalized_document": document_json,
                "media_assets": document_json["media_assets"],
            }
        if services.vision_analyzer is None:
            return {
                "vision_descriptions": [],
                "vision_issues": [],
                "normalized_document": document_json,
                "media_assets": document_json["media_assets"],
            }
        assets = [
            asset
            for asset in document.media_assets
            if asset.mime_type.startswith("image/")
        ]
        semaphore = asyncio.Semaphore(_VISION_MAX_CONCURRENCY)

        async def analyze_one(asset: MediaAsset) -> VisionDescription | Exception:
            async with semaphore:
                try:
                    return await services.vision_analyzer.analyze(asset)
                except Exception as exc:
                    return exc

        outcomes = await asyncio.gather(
            *(analyze_one(asset) for asset in assets)
        )

        descriptions: list[VisionDescription] = []
        issues: list[str] = []
        for asset, outcome in zip(assets, outcomes):
            if isinstance(outcome, VisionDescription):
                descriptions.append(outcome)
                continue
            reason = (
                outcome.detail.message
                if isinstance(outcome, AgentError)
                else "??????"
            )
            issues.append(
                f"?? {asset.asset_id} ???????{reason}"
            )
        return {
            "vision_descriptions": [
                _json_model(description) for description in descriptions
            ],
            "vision_issues": issues,
            "normalized_document": document_json,
            "media_assets": document_json["media_assets"],
        }

    return await _run_node(state, "analyze_images", services, operation)


def _planning_pipeline(services: GraphServices) -> str:
    """规划流水线：`text`（Claude 逐图分析 + 文本规划）或 `multimodal`（ds4.1 一次看）。

    以前这段逻辑内联在 `plan_requirements` 里，`analyze_images` 也要用同一份判断，
    所以抽出来 —— 两处不一致就会出现「多模态规划 + 还在跑 Claude」的浪费。
    """
    pipeline = str(getattr(services.settings, "planning_pipeline", "text") or "text")
    preferences = getattr(services, "provider_preferences", None)
    if preferences is not None:
        pipeline = getattr(preferences, "planning_pipeline", None) or pipeline
    return pipeline


async def _planning_media_parts(
    document: NormalizedDocument,
    services: GraphServices,
) -> list[dict[str, Any]]:
    """多模态规划要带的媒体（按文档顺序，链接走 sha256 缓存）。"""
    host = getattr(services, "public_media_host", None)
    if host is None:
        return []
    cache = MediaUploadCache(
        Path(services.settings.data_dir) / "media-upload-cache.json"
    )
    return await build_planning_media_parts(
        list(document.media_assets),
        public_media_host=host,
        cache=cache,
    )


async def _history_context_for_plan(
    state: AgentState,
    *,
    limit: int = 12,
) -> str | None:
    """把这条需求**历次返工被要求改的地方**整理成规划上下文。

    用户 2026-09-18 选的第 2 条：「一次规划能把更多的信息上传然后效果更好，视频就不会
    再多次生成」。这些要求本来就在我们自己的数据里（计划任务的 `rework_requirements`），
    喂给 planner **不增加任何模型调用**。

    只在重新规划（退回修改）时才有内容 —— 全新需求没有历史，返回 None。
    """
    collected: list[str] = []
    for plan_key in ("draft_plan", "approved_plan"):
        plan = state.get(plan_key)
        if not isinstance(plan, dict):
            continue
        for task in plan.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for item in task.get("rework_requirements") or []:
                text = str(item).strip()
                if text and text not in collected:
                    collected.append(text)
    # 整篇「重新生成计划」（新 run）时上面的两条通道都是空的：历史由 runtime 从
    # **同一条需求的历次运行**里捞出来放进 `carried_rework_requirements`。
    # 2026-10-10「铲」任务：没有这一段，新 run 的规划器对上一轮被挑的问题一无所知。
    for item in state.get("carried_rework_requirements") or []:
        text = str(item).strip()
        if text and text not in collected:
            collected.append(text)
    if not collected:
        return None
    return "\n".join(f"- {item}" for item in collected[-limit:])


async def _plan_with_optional_fallback(
    services: GraphServices,
    document: NormalizedDocument,
    descriptions: list[VisionDescription],
    state: AgentState,
    planning_prompt: str | None,
    mode: str,
    resolved_characters: list[Any],
    knowledge_context: str | None,
    media_parts: list[dict[str, Any]],
    history_context: str | None = None,
) -> Any:
    """多模态规划；失败且允许时自动回退到纯文本（懒补图片视觉描述）。

    回退路径等价于改造前的行为 —— 保证「切到多模态」不会比现在更差。
    """
    planner = services.planner
    common = {
        **_planner_prompt_argument(planner, planning_prompt),
        **_planner_mode_argument(planner, mode),
        **_character_context_argument(planner, resolved_characters),
        **_knowledge_context_argument(planner, knowledge_context),
        **_history_context_argument(planner, history_context),
    }
    try:
        return await planner.plan(
            document,
            descriptions,
            state.get("planner_feedback"),
            **common,
            **_planning_media_argument(planner, media_parts),
        )
    except AgentError:
        fallback = (
            media_parts
            and getattr(services.settings, "planning_fallback_to_text", True)
        )
        if not fallback:
            raise
        _LOGGER.warning("多模态规划失败，回退纯文本流程（懒补图片视觉描述）")
        lazy_descriptions = await _describe_images(document, services)
        return await planner.plan(
            document,
            lazy_descriptions,
            state.get("planner_feedback"),
            **common,
        )


def _history_context_argument(
    planner: Any,
    history_context: str | None,
) -> dict[str, Any]:
    """只在 planner 支持 `history_context` 时才传（与其它可选参数同一套签名探测）。"""
    if not history_context:
        return {}
    if "history_context" not in signature(planner.plan).parameters:
        return {}
    return {"history_context": history_context}


def _planning_media_argument(
    planner: Any,
    media_parts: list[dict[str, Any]],
) -> dict[str, Any]:
    """只在 planner 支持 `media_parts` 时才传（与其它可选参数同一套签名探测）。"""
    if not media_parts:
        return {}
    if "media_parts" not in signature(planner.plan).parameters:
        return {}
    return {"media_parts": media_parts}


async def _describe_images(
    document: NormalizedDocument,
    services: GraphServices,
) -> list[VisionDescription]:
    """逐张图片做视觉描述（回退路径与现有流程共用）。"""
    analyzer = services.vision_analyzer
    if analyzer is None:
        return []
    assets = [
        asset
        for asset in document.media_assets
        if asset.mime_type.startswith("image/")
        and asset.download_error is None
    ]
    semaphore = asyncio.Semaphore(_VISION_MAX_CONCURRENCY)

    async def analyze_one(asset: MediaAsset) -> VisionDescription | Exception:
        async with semaphore:
            try:
                return await analyzer.analyze(asset)
            except Exception as exc:  # noqa: BLE001
                return exc

    outcomes = await asyncio.gather(*(analyze_one(asset) for asset in assets))
    return [
        outcome for outcome in outcomes if isinstance(outcome, VisionDescription)
    ]


async def plan_requirements(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        descriptions = [
            VisionDescription.model_validate(item)
            for item in state.get("vision_descriptions", [])
        ]
        planning_prompt = _planning_prompt(state)
        mode = await _planning_mode_for_run(state["run_id"], services, state)
        # 参考图去水印整条链路已在 2026-10-10 删除（用户：「把这个功能删了，自动第一轮
        # 一定保证原本的参考图」）—— 规划阶段不再对参考图做任何重画/替换。
        resolved_characters = (
            await _resolve_character_assets(document, services)
            if mode == "image"
            else []
        )
        # 多模态规划：文本 + **原始图片/视频**一次交给模型（实测比纯文本流程稳得多）。
        # 开关以「前端高级设置里的偏好」为准，环境变量只作兜底（用户 2026-09-18）。
        pipeline = _planning_pipeline(services)
        media_parts: list[dict[str, Any]] = []
        if pipeline == "multimodal" and mode != "image":
            media_parts = await _planning_media_parts(document, services)
        # 知识库必须在 planner **之前**查：命中的规则当上下文喂给 planner，
        # 让它一次就把经验写进提示词。旧做法是写完再改写提示词（且逐任务调导演台），
        # 既和 planner 打架，又要多花 N 次调用。
        knowledge_context = await _knowledge_context_for_plan(document, services)
        plan = await _plan_with_optional_fallback(
            services,
            document,
            descriptions,
            state,
            planning_prompt,
            mode,
            resolved_characters,
            knowledge_context,
            media_parts,
            # 这条需求历次返工被要求改的地方（重新规划时才有）—— 免费，不增加调用。
            await _history_context_for_plan(state),
        )
        plan_json = _json_model(plan)
        # 整篇重新规划时把**继承来的返工要求**并回每条任务（2026-10-10「铲」）。
        # 上面 `_history_context_for_plan` 只保证规划器**知道**这些要求；这里保证
        # 它们留在任务的 `rework_requirements` 里 —— 否则下一次返工/重做又会清零，
        # 用户看到的就是「上一版修好的东西这一版又变回去了」。
        carried = [
            str(item).strip()
            for item in (state.get("carried_rework_requirements") or [])
            if str(item).strip()
        ]
        if carried:
            stamped = inherit_rework_requirements(plan_json, carried)
            if stamped:
                # 事件只是可见性，写不进去绝不能让这次规划失败（与
                # `_record_visual_context` 同一套兜法）。
                try:
                    await services.repository.append_event(
                        state["run_id"],
                        "plan_requirements",
                        "completed",
                        f"已从这条需求的历史运行继承 {len(carried)} 条返工要求"
                        f"（并入 {stamped} 个任务）：" + "、".join(carried[:5]),
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("记录返工要求继承事件失败", exc_info=True)
        updates: AgentState = {
            "draft_plan": plan_json,
            "task_plan": plan_json,
        }
        # 素材库参考图要进 document.media_assets，否则 _task_assets 解析
        # 不到这些 asset_id，执行阶段会直接判定计划无效。
        character_media = await _character_media_assets(
            resolved_characters,
            getattr(services, "asset_library_store", None),
        )
        if character_media:
            known = {asset.asset_id for asset in document.media_assets}
            merged = list(document.media_assets) + [
                asset
                for asset in character_media
                if asset.asset_id not in known
            ]
            updates["normalized_document"] = _json_model(
                document.model_copy(update={"media_assets": merged})
            )
        # 参考图去水印**已整条删除**（用户 2026-10-10：「现在是不是有根据水印自动
        # 重新生成参考图的功能，把这个功能删了，自动第一轮一定保证原本的参考图」）。
        # 旧实现拿 `reference_plan[].reason` 里的水印/平台标识关键词当判据，调图片模型
        # 重画一版**替换用户给的原图**（只换 local_path、任务名 `image-<N>-nowm`、并把
        # usable 改回 True），用户拿到的不再是自己那张图。删掉后原图原样送生成；
        # 水印可能被成片复现，由提示词里的「不要出现水印/logo」约束兜底。
        # 别再重新引入：`tests/unit/test_watermark.py` 是删除守卫。
        # 分镜关键帧候选图：**在审批之前**就把每个时间段的关键帧生成好，让用户在
        # 审批页挑图（用户 2026-09-22：「先生成几个分镜图供用户选择…效果会更好、
        # 需要修改的次数也会更少」）。Seedance 2.5 的关键帧参考会严格对齐输入图，
        # 只给文字没有画面锚点就只能靠模型自由发挥。
        if mode != "image":
            storyboard_config = _storyboard_config(services)
        if mode != "image" and storyboard_config.enabled:
            video_tasks = _video_tasks_of(updates["draft_plan"])
            frames_by_task, storyboard_assets = (
                await _generate_storyboard_candidates(
                    services=services,
                    run_id=state["run_id"],
                    tasks=video_tasks,
                    assets=list(document.media_assets),
                    config=storyboard_config,
                )
            )
            if storyboard_assets:
                updates["storyboard_assets"] = [
                    _json_model(asset) for asset in storyboard_assets
                ]
            if frames_by_task:
                _attach_storyboard_frames(
                    updates["draft_plan"],
                    frames_by_task,
                    {asset.asset_id for asset in storyboard_assets},
                    {
                        task.task_id: len(build_storyboard_frames(task))
                        for task in video_tasks
                    },
                )
                updates["task_plan"] = updates["draft_plan"]
            elif video_tasks:
                # 一个候选都没出（生图全失败）→ 在任务上挂一条警告。
                # 生成失败是**有意不阻断规划**的，所以必须留下可见痕迹，否则
                # 就成了「悄悄降级成没有分镜图」（真实跑第一次就踩过：参考模式写错
                # 导致每段都抛异常，界面上一片安静）。
                _warn_missing_storyboards(
                    updates["draft_plan"],
                    {task.task_id for task in video_tasks},
                )
                updates["task_plan"] = updates["draft_plan"]
        return updates

    return await _run_node(state, "plan_requirements", services, operation)


def _default_video_model_key(services: GraphServices) -> str:
    preferences = getattr(services, "provider_preferences", None)
    preferred = getattr(preferences, "video_provider", None)
    settings = services.settings
    fallback = resolve_video_model_key(
        getattr(settings, "seedance_model", None),
        fallback=DEFAULT_VIDEO_MODEL_KEY,
    )
    resolved = resolve_video_model_key(preferred, fallback=fallback or "")
    if resolved in VIDEO_MODEL_BY_KEY:
        return resolved
    resolved = resolve_video_model_key(
        getattr(settings, "video_provider", None),
        fallback=fallback or "",
    )
    return resolved if resolved in VIDEO_MODEL_BY_KEY else DEFAULT_VIDEO_MODEL_KEY

async def audit_plan(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = TaskPlan.model_validate(_draft_plan(state))
        report = await services.planner.audit(document, plan)
        return {"audit_report": _json_model(report)}

    return await _run_node(state, "audit_plan", services, operation)


async def validate_planned_tasks(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        raw_plan = _draft_plan(state)
        default_model_key = _default_video_model_key(services)
        normalized_payload = raw_plan
        if isinstance(raw_plan, dict):
            normalized_payload = dict(raw_plan)
            normalized_tasks: list[Any] = []
            for item in raw_plan.get("tasks", []):
                if not isinstance(item, dict):
                    normalized_tasks.append(item)
                    continue
                normalized_task, notes = normalize_video_task_payload(
                    item,
                    default_model_key=default_model_key,
                    max_output_count=services.settings.max_output_count,
                )
                if notes:
                    warnings = list(normalized_task.get("warnings") or [])
                    warnings.extend(notes)
                    normalized_task["warnings"] = list(dict.fromkeys(warnings))
                normalized_tasks.append(normalized_task)
            normalized_payload["tasks"] = normalized_tasks
        plan = TaskPlan.model_validate(normalized_payload)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        audit = AuditReport.model_validate(state.get("audit_report", {}))
        if audit.corrections_required:
            issues.extend(
                f"audit: {issue}"
                for issue in audit.issues
                if issue.startswith("\u6280\u672f\u963b\u65ad")
                or "\u4eba\u5de5\u5904\u7406" in issue
            )
        normalized_json = _json_model(plan)
        return {
            "draft_plan": normalized_json,
            "task_plan": normalized_json,
            "validation_issues": issues,
            "status": "waiting_approval",
        }

    return await _run_node(state, "validate_plan", services, operation)


def _approval_payload(state: AgentState) -> dict[str, Any]:
    plan = _draft_plan(state)
    revision = _document_revision(state)
    payload = {
        "action": "review_plan",
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "status": "waiting_approval",
        "document_revision": revision,
        "source_revision": revision,
        "draft_plan": plan,
        "task_plan": plan,
        "audit_report": state.get("audit_report"),
        "validation_issues": state.get("validation_issues", []),
    }
    json.dumps(payload, ensure_ascii=False)
    return payload


def _storyboard_ids_from_state(state: AgentState) -> set[str]:
    """运行里所有「服务端生成的分镜候选图」的 asset_id。"""
    return {
        item.get("asset_id")
        for item in (state.get("storyboard_assets") or [])
        if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
    }


def _with_server_storyboard_frames(
    original: TaskPlan,
    tasks: list[GenerationTask],
    storyboard_ids: set[str] | None = None,
) -> list[GenerationTask]:
    """审批时用**服务端存的候选图**覆盖前端回传的帧，只采纳用户的选择。

    前端批准 payload 里为了过 schema，只带 `{index, start, end, prompt, selected}`
    （视图里的 candidates 是带 preview_url 的对象，不能原样回传）。这里按 index 把
    候选并回来，否则批准后任务就丢掉候选图列表了。

    **并且按服务端的帧重新拼一遍 `reference_images`** —— 批准不能信前端回传的参考图。
    实测（2026-09-23 运行 b2210150 / 74ce13bf）：审批页的「参考素材」一栏把分镜关键帧
    也当普通参考图列出来，用户在那一栏删过图 / 保存过顺序之后，前端回传的
    `reference_images` 里**就没有每段选中的分镜图**了（`storyboard_frames` 有 3 段、
    `reference_images` 只剩 1 张），服务端照单全收 → 视频拿不到关键帧。
    """
    stored = {task.task_id: task for task in original.tasks}
    merged: list[GenerationTask] = []
    for task in tasks:
        source = stored.get(task.task_id)
        if source is None or not source.storyboard_frames:
            merged.append(task)
            continue
        frames = [
            StoryboardFrame.model_validate(item)
            for item in storyboard_merge_selection(
                source.storyboard_frames,
                [
                    frame.model_dump(mode="json")
                    for frame in (task.storyboard_frames or [])
                ],
            )
        ]
        if not frames:
            merged.append(task)
            continue
        merged.append(
            _task_with_storyboard_references(
                task, frames, storyboard_ids or set()
            )
        )
    return merged


def _task_with_storyboard_references(
    task: GenerationTask,
    frames: list[StoryboardFrame],
    storyboard_ids: set[str],
) -> GenerationTask:
    """把「每段选中的分镜图」重新落进任务的参考图与提示词（服务端权威，就地算）。"""
    payload = task.model_dump(mode="json")
    frame_json = [_json_model(frame) for frame in frames]
    payload["storyboard_frames"] = frame_json
    storyboard_apply_selection(
        payload,
        frame_json,
        reference_ids_for_selection(frame_json, storyboard_ids),
    )
    try:
        return GenerationTask.model_validate(payload)
    except ValidationError:
        # 重新拼出来的参考图不合法（理论上不该发生）→ 退回前端原样，别把审批卡死。
        _LOGGER.warning(
            "按分镜帧重拼参考图失败，沿用前端回传：task=%s", task.task_id
        )
        return task


def _parse_approval(value: Any) -> ApprovalDecision:
    if not isinstance(value, dict):
        raise _validation_error("审批请求格式无效：期望 JSON 对象")
    allowed_keys = {"action", "selected_task_ids", "tasks", "feedback"}
    extra_keys = set(value) - allowed_keys
    if extra_keys:
        raise _validation_error(
            "审批请求包含未知字段："
            + "、".join(sorted(str(key) for key in extra_keys))
        )
    try:
        decision = ApprovalDecision.model_validate(value)
    except ValidationError as exc:
        compact = "; ".join(
            ".".join(str(part) for part in item["loc"]) + ": " + str(item["msg"])
            for item in exc.errors(include_url=False, include_input=False)[:6]
        )
        raise _validation_error(f"审批载荷无效：{compact}") from None
    if decision is None:
        raise _validation_error("审批载荷无法解析")

    if decision.action == "reject":
        if (
            not isinstance(decision.feedback, str)
            or not decision.feedback.strip()
            or decision.selected_task_ids
            or decision.tasks
        ):
            raise _validation_error(
                "退回重新规划时必须填写反馈，且不能携带任务选择或任务修改"
            )
    elif decision.action == "cancel":
        if (
            decision.selected_task_ids
            or decision.tasks
            or decision.feedback is not None
        ):
            raise _validation_error("取消请求不能携带任务选择或任务修改")
    elif not decision.selected_task_ids:
        raise _validation_error("批准时必须选择至少一个任务")
    elif decision.feedback is not None:
        raise _validation_error("批准请求不能携带反馈")
    elif len(decision.selected_task_ids) != len(
        set(decision.selected_task_ids)
    ):
        raise _validation_error("批准的任务选择不能重复")
    return decision


async def human_approval(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> Command:
    _ensure_thread_id(state, config)
    resume_value = interrupt(_approval_payload(state))

    async def operation() -> Command:
        decision = _parse_approval(resume_value)
        decision_json = _json_model(decision)
        if decision.action == "reject":
            return Command(
                update={
                    "approval_decision": decision_json,
                    "planner_feedback": decision.feedback.strip(),
                    "approved_tasks": [],
                    "approved_plan": None,
                    "status": "running",
                },
                goto="plan_requirements",
            )
        if decision.action == "cancel":
            return Command(
                update={
                    "approval_decision": decision_json,
                    "approved_tasks": [],
                    "approved_plan": None,
                    "status": "cancelled",
                },
                goto=END,
            )

        original = TaskPlan.model_validate(_draft_plan(state))
        try:
            candidate = (
                reconcile_task_asset_coverage(
                    original,
                    _with_server_storyboard_frames(
                        original,
                        decision.tasks,
                        _storyboard_ids_from_state(state),
                    ),
                )
                if decision.tasks
                else original
            )
        except Exception as exc:
            raise _validation_error(
                f"审批任务合并失败：{type(exc).__name__}: {exc}"
            ) from None
        original_ids = {task.task_id for task in original.tasks}
        unknown_ids = [
            task.task_id
            for task in candidate.tasks
            if task.task_id not in original_ids
        ]
        if unknown_ids:
            raise _validation_error(
                "审批包含未知任务：" + "、".join(sorted(unknown_ids))
            )
        try:
            approved = candidate.approved_subset(
                decision.selected_task_ids,
                services.settings.max_output_count,
            )
        except Exception as exc:
            raise _validation_error(
                f"所选任务无法批准：{type(exc).__name__}: {exc}"
            ) from None
        if approved is None or not approved.tasks:
            raise _validation_error("所选任务无法批准：批准结果为空")
        return Command(
            update={
                "approval_decision": decision_json,
                "approval_revision": _document_revision(state),
                "approved_tasks": [
                    _json_model(task) for task in approved.tasks
                ],
                "approved_plan": _json_model(approved),
                "status": "approval_pending_validation",
            },
            goto="revalidate_approval",
        )

    return await _run_node(state, "human_approval", services, operation)


async def revalidate_approval(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        approval_revision = state.get("approval_revision")
        if (
            not isinstance(approval_revision, int)
            or isinstance(approval_revision, bool)
            or approval_revision < 0
        ):
            raise _validation_error(
                "审批状态已失效，请刷新页面后重新审批"
            )
        if approval_revision != _document_revision(state):
            # 用户口径（2026-09-18）：「点击开始生成就是生成视频了，不要乱规划哦」。
            # 版本不一致不再拦住生成，只记一条提示 —— 审批就是审批，直接出片。
            await services.repository.append_event(
                state.get("run_id", "unknown-run"),
                "revalidate_approval",
                "source_changed",
                "审批与文档版本不一致，仍按已审批的计划生成",
            )
        draft = TaskPlan.model_validate(_draft_plan(state))
        decision = ApprovalDecision.model_validate(
            state.get("approval_decision")
        )
        if decision.action != "approve":
            raise _validation_error("审批决策已不是批准状态")
        try:
            candidate = (
                reconcile_task_asset_coverage(
                    draft,
                    _with_server_storyboard_frames(
                        draft,
                        decision.tasks,
                        _storyboard_ids_from_state(state),
                    ),
                )
                if decision.tasks
                else draft
            )
        except Exception as exc:
            raise _validation_error(
                f"审批任务合并失败：{type(exc).__name__}: {exc}"
            ) from None
        draft_ids = {task.task_id for task in draft.tasks}
        unknown_ids = [
            task.task_id
            for task in candidate.tasks
            if task.task_id not in draft_ids
        ]
        if unknown_ids:
            raise _validation_error(
                "审批包含未知任务：" + "、".join(sorted(unknown_ids))
            )
        try:
            selected_plan = candidate.approved_subset(
                decision.selected_task_ids,
                services.settings.max_output_count,
            )
        except Exception as exc:
            raise _validation_error(
                f"所选任务无法批准：{type(exc).__name__}: {exc}"
            ) from None
        checkpoint_plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        if checkpoint_plan.model_dump(mode="json") != selected_plan.model_dump(
            mode="json"
        ):
            raise _validation_error(
                "审批内容与系统记录不一致，请刷新页面后重新审批"
            )
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                selected_plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        if issues:
            raise _validation_error(
                language_validation_message(issues)
                or "The approved plan is not valid"
            )
        return {"validation_issues": [], "status": "approved"}

    return await _run_node(
        state, "revalidate_approval", services, operation
    )


async def check_source_revision(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> Command:
    async def operation() -> Command:
        _ensure_thread_id(state, config)
        source_url = state.get("source_url")
        approval_revision = state.get("approval_revision")
        if (
            not isinstance(source_url, str)
            or not source_url
            or not isinstance(approval_revision, int)
            or isinstance(approval_revision, bool)
            or approval_revision < 0
        ):
            raise _validation_error()
        current_revision = await services.document_source.get_revision(source_url)
        if current_revision != approval_revision:
            # 用户口径（2026-09-18）：「点击开始生成就是生成视频了，不要乱规划哦」。
            # 以前这里会**作废审批、清空已选任务、回到 ingest_source 重新规划** ——
            # 用户看到的就是"点了开始生成，结果又变回待审批"。现在只记一条提示，
            # 仍然按**你已经审批过的计划**生成。
            await services.repository.append_event(
                state.get("run_id", "unknown-run"),
                "check_source_revision",
                "source_changed",
                "文档在审批后有改动，仍按已审批的计划生成",
            )
        return Command(
            update={"status": "approved"}, goto="execute_selected_tasks"
        )

    return await _run_node(
        state, "check_source_revision", services, operation
    )


def _execution_error(exc: BaseException) -> dict[str, object]:
    safe = _safe_error(exc).detail
    # 供应商自己给的说明比我们的泛化文案有用得多（用户 2026-09-18 要求记录拒绝原因），
    # 所以有就**追加**在中文泛化文案后面；没有则保持原样 —— 不能直接把 message 换成
    # 适配层的文案，某些包装路径下那是「The workflow node could not be completed」这种
    # 英文内部话术，反而更看不懂。
    reason = _execution_provider_reason(safe.technical_detail)
    base = _safe_execution_message(safe.category)
    result: dict[str, object] = {
        "category": safe.category.value,
        "message": f"{base}：{reason}" if reason else base,
        "retryable": safe.retryable,
    }
    if isinstance(exc, AgentError):
        code = _safe_execution_code(exc.detail.technical_detail)
        if code is not None:
            result["code"] = code
    return result


def _execution_provider_reason(technical_detail: str) -> str | None:
    """从 technical_detail 里取出 `provider_message=`（供应商原话，已脱敏）。"""
    for part in technical_detail.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.strip() == "provider_message":
            cleaned = value.strip()
            if cleaned:
                return cleaned[:200]
    return None


def _safe_execution_message(category: ErrorCategory) -> str:
    return {
        ErrorCategory.CONFIGURATION: "生成服务配置不完整",
        ErrorCategory.PERMISSION: "生成服务凭证无效或权限不足",
        ErrorCategory.DOCUMENT: "参考素材无法用于生成",
        ErrorCategory.VALIDATION: "生成参数不符合供应商要求",
        ErrorCategory.TRANSIENT: "生成服务暂时不可用，请稍后重试",
        ErrorCategory.PROVIDER_TERMINAL: "生成服务拒绝了请求",
        ErrorCategory.DELIVERY: "生成结果交付失败",
    }[category]


def _safe_execution_code(technical_detail: str) -> str | None:
    fields: dict[str, str] = {}
    for part in technical_detail.split(";"):
        key, separator, value = part.strip().partition("=")
        normalized = value.strip().lower().replace("-", "_")
        if (
            separator
            and key.strip() in {
                "operation",
                "status",
                "cause",
                "provider_code",
            }
            and normalized
            and normalized.replace("_", "").replace(".", "").isalnum()
            and len(normalized) <= 48
        ):
            fields[key.strip()] = normalized
    operation = fields.get("operation", "generation")
    if "provider_code" in fields:
        return f"{operation}_{fields['provider_code']}"
    if "status" in fields:
        return f"{operation}_http_{fields['status']}"
    if "cause" in fields:
        return f"{operation}_{fields['cause']}"
    return None


_OUTPUT_SLOT_MARKER = "::output:"


def base_task_id(task_id: str) -> str:
    """执行单元 id -> 所属任务 id（`task-1::output:2` -> `task-1`）。

    成片审核页是按**每条成片**勾选的（artifact 级 id），而提示词/参数挂在任务上，
    两边都要用这个换算，别再各写一份。
    """
    return task_id.split(_OUTPUT_SLOT_MARKER, 1)[0]


def _execution_units(task: GenerationTask) -> list[GenerationTask]:
    # 图片与视频都按 output_count 拆分执行单元；早期只有视频支持多产出，
    # 图片模式接入后这个类型门槛不再成立。
    if task.output_count == 1:
        return [task]
    return [
        task.model_copy(
            update={
                "task_id": f"{task.task_id}{_OUTPUT_SLOT_MARKER}{index}",
                "output_count": 1,
            }
        )
        for index in range(1, task.output_count + 1)
    ]


def _split_partially_selected_outputs(
    reworked: GenerationTask,
    original: GenerationTask,
    target_unit_ids: set[str],
) -> list[GenerationTask]:
    """一个镜头产出多条、却只勾了其中几条时，把任务拆成「一条一个执行单元」。

    `_execution_units` 是从**同一个任务对象**派生全部产出的，提示词与
    `task_fingerprint` 共享。只重跑其中一条就必须让每条拥有独立提示词，否则：
    被保留的那几条会因为 fingerprint 变了被判成 `submission_uncertain`（整轮失败），
    而把整个任务全部重跑又不是用户勾选的意思
    （2026-09-23 复现：勾 1 条视频，submit 了 3 次）。
    没被勾中的单元原样返回（提示词不动），fingerprint 不变 → 复用已有成片。
    """
    if reworked.output_count <= 1:
        return [reworked]
    units = _execution_units(original)
    selected = {unit.task_id for unit in units} & target_unit_ids
    if not selected or len(selected) == len(units):
        return [reworked]
    return [
        (
            reworked.model_copy(update={"task_id": unit.task_id, "output_count": 1})
            if unit.task_id in selected
            else unit
        )
        for unit in units
    ]


def _provider_terminal_error(message: str) -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message=message,
            technical_detail="Provider execution returned an invalid terminal result",
            retryable=False,
        )
    )


def _validate_submission_identity(
    submission: ProviderSubmission,
    *,
    provider: str,
    official_id: str,
) -> None:
    if (
        submission.provider != provider
        or submission.provider_task_id != official_id
    ):
        raise _provider_terminal_error("供应商任务身份不一致")


def _task_assets(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset] | None = None,
) -> list[MediaAsset]:
    assets_by_id = {asset.asset_id: asset for asset in document.media_assets}
    # 审批页选中的分镜关键帧不在 document.media_assets 里（那条通道会触发未覆盖
    # 素材门禁），但它们是任务的正式参考图，执行时必须能解析到。
    for asset in extra_assets or []:
        assets_by_id.setdefault(asset.asset_id, asset)
    ordered = sorted(task.reference_images, key=lambda reference: reference.order)
    if [reference.order for reference in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise _validation_error("The approved plan is not valid")
    try:
        return [assets_by_id[reference.asset_id] for reference in ordered]
    except KeyError:
        raise _validation_error("The approved plan is not valid") from None


def _task_fingerprint(task: GenerationTask) -> str:
    canonical = json.dumps(
        task.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _intent_is_stale(
    operation: dict[str, Any], lease_seconds: float
) -> bool:
    try:
        updated_at = datetime.fromisoformat(operation["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        return True
    return (datetime.now(UTC) - updated_at).total_seconds() >= (
        lease_seconds
    )


async def _keep_submission_intent_alive(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    client_id: str,
    task_fingerprint: str,
) -> None:
    interval = max(
        0.01, min(5.0, services.settings.submission_intent_lease_seconds / 3)
    )
    while True:
        await async_sleep(interval)
        renewed = await services.repository.renew_submission_intent_lease(
            run_id, task.task_id, client_id, provider, task_fingerprint
        )
        if not renewed:
            return


async def _generate_storyboard_candidates(
    *,
    services: GraphServices,
    run_id: str,
    tasks: list[GenerationTask],
    assets: list[MediaAsset],
    config: StoryboardConfig,
) -> tuple[dict[str, list[StoryboardFrame]], list[MediaAsset]]:
    """给每个时间段准备**恰好一张**参考图：能复用就复用，不能用/没有才生成。

    用户 2026-09-22 的方案：规划器在 `reference_plan` 里给每张文档配图打了标
    （分镜图/全局图 + 能不能用）。这里：

    - 某段有**可用的分镜图** → 直接拿它当该段的参考图，**不生成、不花钱**；
    - 该段没有可用分镜图（没配图 / 水印 / 低质）→ **并发**生成 K 张候选让用户挑；
    - `usable=False` 的图一律不进参考图（水印就是这么顺带解决的）。

    返回 `(每个任务的 frames, 新增素材)`。任何一段失败都只记日志、不阻断整个 run。
    """
    per_task: dict[str, list[StoryboardFrame]] = {}
    reuse: dict[str, dict[int, str]] = {}
    for task in tasks:
        frames = build_storyboard_frames(task)
        if not frames:
            continue
        per_task[task.task_id] = frames
        # 可复用的分镜图：role=shot + usable + 标明属于哪一段。
        reuse[task.task_id] = {
            int(item.segment_index): item.asset_id
            for item in task.reference_plan
            if item.role == "shot"
            and item.usable
            and item.segment_index is not None
            and item.asset_id in {asset.asset_id for asset in assets}
        }
    if not per_task or not config.enabled:
        return {}, []

    semaphore = asyncio.Semaphore(max(1, config.concurrency))
    created: list[MediaAsset] = []
    lock = asyncio.Lock()
    by_id = {task.task_id: task for task in tasks}

    #: 能复用的段直接定下来（不占并发、不调模型）。
    for task_id, frames in per_task.items():
        for frame in frames:
            asset_id = reuse.get(task_id, {}).get(frame.index)
            if asset_id:
                frame.candidates = [asset_id]
                frame.selected = asset_id

    async def one(
        task: GenerationTask, frame: StoryboardFrame, candidate: int
    ) -> None:
        # **一张候选 = 一次生图调用**：Ark 的 Seedream 接入点不支持
        # `sequential_image_generation`（实测 400 InvalidParameter），靠 output_count
        # 一次要 K 张只会拿到 1 张。用户 2026-09-22 的口径就是「生图并发就行」——
        # 于是 K 张走 K 个并发调用，provider 无关。
        image_task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-sb{frame.index}-{candidate}",
                "task_type": TaskType.IMAGE_TO_IMAGE,
                "title": (
                    f"{task.title} · 第 {frame.start:g}-{frame.end:g}s "
                    f"关键帧 {candidate}"
                ),
                "prompt": keyframe_prompt(task, frame),
                "prompt_slots": None,
                "image_size": config.image_size,
                "image_provider": None,
                "video_provider": None,
                "duration": None,
                "resolution": None,
                "generate_audio": None,
                "size_variants": [],
                "safe_area": None,
                "output_count": 1,
                "reference_mode": "multi_reference",
                "storyboard_frames": [],
                # **判为不能用的图不许当生图输入**：否则水印/低质会被继承进新生成的
                # 分镜图（用户 2026-09-23 问「生成参考图时有没有参考我给的参考图」时
                # 查出来的漏洞 —— 剔除原本发生在生成之后，太晚了）。
                "reference_images": _usable_image_references(task, assets),
            }
        )
        async with semaphore:
            settled = None
            # 并发生图会撞上瞬时错误（2026-09-22 实测：6 个并发里 4 个报
            # 「连接 Seedance 服务失败，请稍后重试」，结果每段只剩一张、有一整段
            # 因为两张全挂而消失）。可重试的错误必须退避重试，否则用户看到的就是
            # 「为什么只看到一张分镜图」。
            for attempt in range(1, _STORYBOARD_ATTEMPTS + 1):
                started = time.monotonic()
                try:
                    provider, generator = await _generator_for_task(
                        run_id, image_task, services
                    )
                    submission = await generator.submit(image_task, assets)
                    settled = await _poll_submission(
                        generator,
                        submission,
                        services,
                        run_id,
                        image_task.task_id,
                    )
                    break
                except AgentError as exc:
                    detail = getattr(exc, "detail", None)
                    retryable = bool(getattr(detail, "retryable", False))
                    if not retryable or attempt == _STORYBOARD_ATTEMPTS:
                        _LOGGER.warning(
                            "分镜候选生成失败 run=%s task=%s frame=%s "
                            "candidate=%s attempt=%s elapsed=%.1fs message=%s detail=%s",
                            run_id,
                            task.task_id,
                            frame.index,
                            candidate,
                            attempt,
                            time.monotonic() - started,
                            getattr(detail, "message", exc),
                            getattr(detail, "technical_detail", ""),
                        )
                        return
                    _LOGGER.info(
                        "分镜候选瞬时失败，重试 run=%s task=%s frame=%s "
                        "candidate=%s attempt=%s elapsed=%.1fs detail=%s",
                        run_id,
                        task.task_id,
                        frame.index,
                        candidate,
                        attempt,
                        time.monotonic() - started,
                        getattr(detail, "technical_detail", ""),
                    )
                    await asyncio.sleep(_STORYBOARD_RETRY_SECONDS * attempt)
                except Exception:  # noqa: BLE001 —— 单段失败不该拖垮规划
                    _LOGGER.warning(
                        "分镜候选生成失败 run=%s task=%s frame=%s",
                        run_id,
                        task.task_id,
                        frame.index,
                        exc_info=True,
                    )
                    return
            if settled is None:
                return
            if (
                settled is None
                or settled.status.lower() not in _SUCCESS_PROVIDER_STATUSES
            ):
                _LOGGER.warning(
                    "分镜候选未成功 run=%s task=%s frame=%s status=%s",
                    run_id,
                    task.task_id,
                    frame.index,
                    getattr(settled, "status", "unknown"),
                )
                return
            for position, result in enumerate(settled.result_items):
                if not result.mime_type.startswith("image/"):
                    continue
                asset_id = f"{image_task.task_id}-{position + 1}"
                try:
                    materialized = (
                        await services.file_store.materialize_provider_result(
                            run_id,
                            image_task.task_id,
                            settled.provider_task_id,
                            position,
                            result,
                            kind="image",
                        )
                    )
                except Exception:  # noqa: BLE001 —— 单张失败不该拖垮整段
                    _LOGGER.warning(
                        "分镜候选落盘失败 run=%s task=%s frame=%s",
                        run_id,
                        task.task_id,
                        frame.index,
                        exc_info=True,
                    )
                    continue
                stored = materialized.stored
                asset = MediaAsset(
                    asset_id=asset_id,
                    source_block_id="",
                    origin=STORYBOARD_ORIGIN,
                    local_path=stored.local_path,
                    mime_type=stored.mime_type,
                    size=stored.size,
                    sha256=stored.sha256,
                    width=stored.width,
                    height=stored.height,
                )
                async with lock:
                    created.append(asset)
                    frame.candidates.append(asset.asset_id)

    await asyncio.gather(
        *(
            one(by_id[task_id], frame, candidate)
            for task_id, frames in per_task.items()
            for frame in frames
            # 已经复用了可用分镜图的段，不再生成（省时间省钱）。
            if not frame.candidates
            for candidate in range(1, max(1, config.candidates) + 1)
        )
    )
    kept = {
        task_id: [frame for frame in frames if frame.candidates]
        for task_id, frames in per_task.items()
    }
    return {task_id: frames for task_id, frames in kept.items() if frames}, created


async def regenerate_storyboard_frame(
    *,
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    frame: StoryboardFrame,
    assets: list[MediaAsset],
    config: StoryboardConfig,
) -> tuple[list[MediaAsset], StoryboardFrame]:
    """**只重跑某一段**的分镜图（用户 2026-09-22 选的方案 A）。

    同一段文本、同一参考图，**重新采样 K 张**替换该段候选 —— 不重新规划、不改提示词、
    不动其它段、不调 LLM。做法上把这一段单独拼成一个只有它一个时间段的临时任务，
    复用既有的并发生成链路（含重试与落盘），再把结果映射回原段号。

    返回 `(新增素材, 带新候选的 frame)`；生成失败时返回空候选（调用方据此报错）。
    """
    segment = task.model_copy(
        update={
            # 唯一 task_id：否则新图的 asset_id 会和旧候选撞号。
            "task_id": f"{task.task_id}-re{frame.index}-{uuid4().hex[:6]}",
            "prompt": (
                f"镜头 {frame.index}（{frame.start:g}-{frame.end:g}s）："
                f"{frame.prompt or '按原描述生成'}"
            ),
            "storyboard_frames": [],
            # 清掉复用信息：重新生成就是**必须出图**，不能命中「直接复用原图」。
            "reference_plan": [],
        }
    )
    # **不受 K 开关影响**：用户是在已有的分镜图上点「重新生成」，分镜功能本来就是
    # 开着的（这条运行的候选图就是证据）。K=0 只是「新任务默认不出分镜图」，
    # 不该让重生成空转 —— 实测踩到：K 被设回 0 后按钮返回 200 但什么都没发生。
    active_config = StoryboardConfig(
        candidates=max(1, config.candidates),
        concurrency=config.concurrency,
        image_size=config.image_size,
    )
    frames_by_task, created = await _generate_storyboard_candidates(
        services=services,
        run_id=run_id,
        tasks=[segment],
        assets=assets,
        config=active_config,
    )
    generated = (frames_by_task.get(segment.task_id) or [None])[0]
    if generated is None or not generated.candidates:
        return [], frame
    return created, frame.model_copy(
        update={
            "candidates": list(generated.candidates),
            "selected": generated.candidates[0],
            # 重新生成出来就有新关键帧了 → 撤销「用户删掉了这一段的关键帧」的标记，
            # 否则这一段以后再也吃不到「没选就取第一张」的默认规则。
            "selection_cleared": False,
        }
    )


def _usable_references(task: GenerationTask) -> list[ImageReference]:
    """任务里**能用**的参考图（
eference_plan 判为 usable=False 的剔掉）。

    没有 
eference_plan（老计划 / 分镜图关闭）时原样返回，行为不变。
    """
    unusable = {
        item.asset_id for item in task.reference_plan if item.usable is False
    }
    if not unusable:
        return list(task.reference_images)
    return [
        reference
        for reference in task.reference_images
        if reference.asset_id not in unusable
    ]


def _usable_image_references(
    task: GenerationTask, assets: list[MediaAsset]
) -> list[ImageReference]:
    """分镜关键帧是**生图**任务：只带图片素材。

    2026-10-08：参考视频现在默认留在 `reference_images` 里（见 planner 的
    `_retain_video_references`），而关键帧生成走的是生图供应商 —— 视频/音频引用
    在那边会被 `参考素材类型与用途不匹配` 直接拒掉。这里按 MIME 过滤一次。
    """
    image_ids = {
        asset.asset_id
        for asset in assets
        if str(asset.mime_type).startswith("image/")
    }
    return [
        reference
        for reference in _usable_references(task)
        if reference.asset_id in image_ids
    ]


def _storyboard_config(services: GraphServices) -> StoryboardConfig:
    """分镜候选的开关与并发度（`candidates=0` 即整体关闭）。

    K 以**用户在高级设置里的偏好**为准（`provider_preferences`，热更新、无需重启），
    环境变量只作兜底 —— 与 planning_pipeline / video_provider 同一套口径。
    """
    settings = services.settings
    candidates = getattr(settings, "storyboard_candidates", 0)
    preferences = getattr(services, "provider_preferences", None)
    preferred = getattr(preferences, "storyboard_candidates", None)
    if isinstance(preferred, int) and not isinstance(preferred, bool):
        candidates = preferred
    return StoryboardConfig(
        candidates=int(candidates or 0),
        concurrency=int(getattr(settings, "storyboard_concurrency", 4) or 4),
        image_size=str(getattr(settings, "storyboard_image_size", "2K") or "2K"),
    )


def _video_tasks_of(plan: dict[str, Any]) -> list[GenerationTask]:
    """计划里所有 image_to_video 任务（分镜关键帧只给视频任务生成）。"""
    tasks: list[GenerationTask] = []
    for raw in (plan.get("tasks") or []):
        if not isinstance(raw, dict) or raw.get("task_type") != "image_to_video":
            continue
        try:
            tasks.append(GenerationTask.model_validate(raw))
        except Exception:  # noqa: BLE001 —— 单条任务不合法不该挡住其它任务
            continue
    return tasks


def _drop_unusable_references(task: dict[str, Any]) -> None:
    """把规划器判为 `usable=False` 的**分镜图**从参考图里摘掉。

    用户 2026-09-22：「替换原参考图那些有水印的图也不能参考」—— 带水印/低质的原图
    进了参考图，模型就会把水印复现出来（实测成片同一位置检出「即梦AI」）。分镜图的
    画面信息由「该段重新生成的分镜图 + prompt 文本」承载，所以直接摘掉。

    **全局图（角色/场景）不摘**（用户明确「不要给我删掉了」）—— 它们由
    `_regenerate_unusable_globals()` 重绘成干净版本后继续用。
    """
    plan_items = task.get("reference_plan")
    if not isinstance(plan_items, list) or not plan_items:
        return
    unusable = {
        item.get("asset_id")
        for item in plan_items
        if isinstance(item, dict)
        and item.get("usable") is False
        and item.get("role") == "shot"
    }
    if not unusable:
        return
    kept = [
        ref
        for ref in (task.get("reference_images") or [])
        if isinstance(ref, dict) and ref.get("asset_id") not in unusable
    ]
    for order, ref in enumerate(kept, start=1):
        ref["order"] = order
    task["reference_images"] = kept


def _warn_missing_storyboards(
    plan: dict[str, Any], task_ids: set[str]
) -> None:
    """分镜候选一张都没生成出来时，在对应任务上挂一条可见警告。"""
    for raw in (plan.get("tasks") or []):
        if not isinstance(raw, dict) or raw.get("task_id") not in task_ids:
            continue
        warnings = list(raw.get("warnings") or [])
        warnings.append(
            "分镜关键帧没有生成成功（生图服务未返回结果），本次按纯文字提示词生成"
        )
        raw["warnings"] = list(dict.fromkeys(warnings))


def _attach_storyboard_frames(
    plan: dict[str, Any],
    frames_by_task: dict[str, list[StoryboardFrame]],
    storyboard_ids: set[str],
    expected: dict[str, int] | None = None,
) -> None:
    """把生成好的关键帧写回计划里的任务（就地改 draft_plan 的 JSON）。

    同时按「默认第一张」把选择**立刻落进任务的参考图与提示词** —— 用户直接点
    批准（没点过任何候选）时，关键帧也必须随视频一起提交。

    **参考图在这里重排成 v4 的形状**：全局图（角色/场景，`role=global` 且可用）
    在前、每段一张分镜图在后。带水印/低质被判 `usable=False` 的图不进参考图
    （用户 2026-09-22：「替换原参考图那些有水印的图也不能参考」）。

    `expected` 是每个任务**本该有**的段数；有段落一张都没生成出来时挂一条可见警告
    （2026-09-22 实跑：第 2 段两张图都连接失败，整段静默消失，用户只看到「少了」）。
    """
    for raw in (plan.get("tasks") or []):
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("task_id")
        _drop_unusable_references(raw)
        frames = frames_by_task.get(task_id)
        wanted = (expected or {}).get(task_id, 0)
        if not frames:
            if wanted:
                _append_storyboard_warning(
                    raw,
                    f"分镜关键帧有 {wanted} 个时间段全部生成失败，这些段没有候选图可选",
                )
            continue
        if wanted and len(frames) < wanted:
            _append_storyboard_warning(
                raw,
                f"分镜关键帧有 {wanted - len(frames)} 个时间段生成失败"
                f"（共 {wanted} 段，只有 {len(frames)} 段有候选图）",
            )
        payload = storyboard_default_selection(
            [_json_model(frame) for frame in frames]
        )
        raw["storyboard_frames"] = payload
        # 摘掉再追加的 id 集合 = 生成的分镜候选 ∪ 每段选中的图（见
        # `reference_ids_for_selection`）。复用的分镜图必然被它那一段选中，所以
        # 不会再出现「同一张图既是基础参考又是该段画面」的重复（2026-09-23 实测：
        # image-2 出现两遍）；反过来，**没被选中的 shot 图不会被误删**。
        storyboard_apply_selection(
            raw,
            payload,
            reference_ids_for_selection(payload, storyboard_ids),
        )


def _append_storyboard_warning(task: dict[str, Any], message: str) -> None:
    warnings = list(task.get("warnings") or [])
    warnings.append(message)
    task["warnings"] = list(dict.fromkeys(warnings))


async def _generator_for_task(run_id: str, task: GenerationTask, services: GraphServices):
    if task.task_type is TaskType.IMAGE_TO_IMAGE:
        registry = getattr(services, "image_providers", None)
        if not registry:
            if services.image_generator is None:
                raise _validation_error("No image provider is configured")
            return "chiyun", services.image_generator

        preferences = getattr(services, "provider_preferences", None)
        preferred_image = getattr(preferences, "image_provider", None)
        explicit_provider = task.image_provider
        requested = explicit_provider or preferred_image
        if requested is None:
            requested = "aiport" if "aiport" in registry else task.resolved_image_provider

        generator = registry.get(requested)
        if generator is None:
            if explicit_provider is not None:
                available = ", ".join(sorted(registry)) or "none"
                raise _validation_error(
                    f"Selected image provider {requested!r} is unavailable; available: {available}"
                )
            fallback = "aiport" if "aiport" in registry else next(iter(registry), None)
            if fallback is None:
                raise _validation_error("No image provider is configured")
            requested = fallback
            generator = registry[fallback]
        return requested, generator

    settings = getattr(services, "settings", None)
    preferences = getattr(services, "provider_preferences", None)
    preferred_video = getattr(preferences, "video_provider", None)
    settings_provider = getattr(settings, "video_provider", None)
    aiport_generator = getattr(services, "aiport_video_generator", None)
    generator_registry = getattr(services, "seedance_video_generators", None) or {}
    default_generator = getattr(services, "seedance_video_generator", None)
    legacy_video_generator = getattr(services, "video_generator", None)
    explicit_provider = task.video_provider

    if settings_provider == "aiport":
        generator = aiport_generator or legacy_video_generator
        if generator is None:
            raise _validation_error(
                "The selected local video provider is unavailable; check AI Port/ComfyUI"
            )
        return "aiport", generator

    default_key = resolve_video_model_key(
        getattr(settings, "seedance_model", None),
        fallback=DEFAULT_VIDEO_MODEL_KEY,
    ) or DEFAULT_VIDEO_MODEL_KEY
    preferred_key = None
    for candidate in (explicit_provider, preferred_video, settings_provider):
        if candidate == "aiport":
            continue
        resolved = resolve_video_model_key(candidate, fallback="")
        if resolved in VIDEO_MODEL_BY_KEY:
            preferred_key = resolved
            break
    model_key = preferred_key or default_key

    # 显式指定走真人类通道（自动切换用）：Seedance 拒绝真人素材时，任务会被改写成
    # `video_provider="volcengine_portrait"` 再重跑一次。
    if (
        explicit_provider == "volcengine_portrait"
        and services.portrait_video_generator is not None
    ):
        return (
            "volcengine_portrait",
            services.portrait_video_generator.for_run(
                run_id,
                model_key=model_key,
            ),
        )

    if (
        model_key
        and services.portrait_video_generator is not None
        and services.production_task_store is not None
    ):
        binding = await services.production_task_store.get_by_run(run_id)
        if binding is not None and binding.snapshot.task_type == "\u771f\u4eba\u7c7b":
            return (
                "volcengine_portrait",
                services.portrait_video_generator.for_run(
                    run_id,
                    model_key=model_key,
                ),
            )

    generator = generator_registry.get(model_key)
    using_model_registry = bool(generator_registry)
    if generator is None and not using_model_registry:
        cloud_requested = (
            explicit_provider not in (None, "aiport")
            or preferred_video not in (None, "aiport")
        )
        legacy_provider = getattr(legacy_video_generator, "provider", None)
        if legacy_provider is None and settings_provider not in (None, "aiport"):
            legacy_provider = "seedance"
        if default_generator is not None:
            generator = default_generator
        elif legacy_provider not in (None, "aiport"):
            generator = legacy_video_generator
        elif cloud_requested:
            raise _validation_error(
                "The selected Seedance model is unavailable; configure ARK_API_KEY"
            )
        else:
            generator = legacy_video_generator
    if generator is None:
        raise _validation_error(
            "The selected Seedance model is unavailable; configure ARK_API_KEY"
        )
    return (model_key if using_model_registry else "seedance"), generator

async def _transition_operation(
    services: GraphServices,
    run_id: str,
    task_id: str,
    operation: dict[str, Any],
    phase: str,
    official_id: str | None,
) -> bool:
    client_id = operation.get("client_submission_id")
    if not isinstance(client_id, str):
        return False
    return await services.repository.compare_and_set_operation(
        run_id,
        task_id,
        operation["operation"],
        expected_phase=operation["phase"],
        expected_client_submission_id=client_id,
        expected_official_id=operation.get("official_id"),
        expected_provider=operation["provider"],
        expected_task_fingerprint=operation["task_fingerprint"],
        phase=phase,
        official_id=official_id,
    )


async def _existing_valid_artifacts(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
) -> list[Artifact] | None:
    artifacts = await services.repository.list_artifacts(
        run_id, task_id=task.task_id
    )
    if len(artifacts) != task.output_count:
        if artifacts:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    expected_ids = {
        sha256(f"{run_id}\0{task.task_id}\0{index}".encode()).hexdigest()[:32]
        for index in range(task.output_count)
    }
    if {artifact.artifact_id for artifact in artifacts} != expected_ids:
        await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    if not all(
        services.file_store.verify_artifact(run_id, artifact)
        for artifact in artifacts
    ):
        await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    return artifacts


# 真正跑在本机的推理供应商（ComfyUI 网关等）。只有这些才该说「本地模型」——
# 旧文案把这句写死了，云端方舟的任务也显示「本地模型生成中」，用户因此以为
# 重跑被强制走了本地（2026-09-16 误判）。
_LOCAL_PROVIDER_NAMES = {"aiport", "local_gateway", "local_llm", "comfyui"}


def _provider_progress_message(provider: str | None, status: str) -> str | None:
    """把轮询状态翻成给用户看的事件文案；不需要播报的状态返回 None。"""
    name = (provider or "").strip()
    is_local = name in _LOCAL_PROVIDER_NAMES
    if status in {"submitted", "pending", "queued"}:
        if is_local:
            return "本地模型排队中，等待 GPU / ComfyUI 空闲"
        return f"云端模型（{name or '生成服务'}）排队等待中"
    if status in {"running", "processing"}:
        if is_local:
            return "本地模型生成中，请稍候"
        return f"云端模型（{name or '生成服务'}）生成中，请稍候"
    return None


async def _poll_submission(
    generator: Any,
    submission: ProviderSubmission,
    services: GraphServices,
    run_id: str,
    task_id: str,
) -> ProviderSubmission | None:
    current = submission
    last_reported_status = ""
    for attempt in range(services.settings.provider_poll_max_attempts):
        try:
            current = await generator.poll(current)
        except AgentError as exc:
            if not exc.detail.retryable:
                raise
        else:
            _validate_submission_identity(
                current,
                provider=submission.provider,
                official_id=submission.provider_task_id,
            )
            status = current.status.lower()
            progress = (
                _provider_progress_message(submission.provider, status)
                if status != last_reported_status
                else None
            )
            if progress is not None:
                last_reported_status = status
                await services.repository.append_event(
                    run_id, "execute_selected_tasks", "running", progress
                )
            if status not in _PENDING_PROVIDER_STATUSES:
                return current
        if attempt + 1 < services.settings.provider_poll_max_attempts:
            await async_sleep(services.settings.provider_poll_interval_seconds)
    return None


def _resize_image_bytes(content: bytes, variant: str | None) -> bytes | None:
    """把出图字节裁到需求要求的尺寸；无需处理时返回 None。

    必须在落盘前处理：FileStore.verify_artifact 要求产物文件名恰好是
    {sha256}.{extension}，落盘后另写文件会破坏这个契约，导致产物校验
    失败、任务被判 failed。

    裁切失败一律返回 None 走原图——出原图比整个 run 失败好，人工在审核
    界面能看到实际尺寸。
    """
    if not variant:
        return None
    try:
        target = parse_size_variant(variant)
    except ValueError:
        _LOGGER.warning("尺寸变体无法解析，保留原图 variant=%s", variant)
        return None
    try:
        with Image.open(BytesIO(content)) as image:
            if (image.width, image.height) == (target.width, target.height):
                return None
            resized = cover_crop(image.convert("RGB"), target)
            buffer = BytesIO()
            resized.save(buffer, format="PNG")
    except (OSError, ValueError):
        _LOGGER.warning("出图裁切失败，保留原图 variant=%s", variant)
        return None
    return buffer.getvalue()


async def _materialize_submission(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    submission: ProviderSubmission,
) -> list[Artifact]:
    if len(submission.result_items) != task.output_count:
        raise _provider_terminal_error("供应商返回的结果数量不正确")
    kind = "image" if task.task_type is TaskType.IMAGE_TO_IMAGE else "video"
    artifacts: list[Artifact] = []
    for index, result in enumerate(submission.result_items):
        # 模型原生出图尺寸与需求要求的尺寸通常不同，落盘前裁一次才是
        # 可交付产物；落盘后再改会破坏 {sha256}.{ext} 的产物命名契约。
        variant = (
            next(iter(task.resolved_size_variants), None)
            if kind == "image"
            else None
        )
        materialized = await services.file_store.materialize_provider_result(
            run_id,
            task.task_id,
            submission.provider_task_id,
            index,
            result,
            kind=kind,
            transform=(
                (lambda content: _resize_image_bytes(content, variant))
                if variant
                else None
            ),
        )
        stored = materialized.stored
        artifact = Artifact(
            artifact_id=sha256(
                f"{run_id}\0{task.task_id}\0{index}".encode()
            ).hexdigest()[:32],
            task_id=task.task_id,
            kind=kind,
            local_path=stored.local_path,
            mime_type=stored.mime_type,
            size=stored.size,
            sha256=stored.sha256,
            provider_url=materialized.provider_url,
            provider_task_id=submission.provider_task_id,
            status="ready",
        )
        artifacts.append(artifact)
    await services.repository.replace_task_artifacts(
        run_id, task.task_id, artifacts
    )
    return artifacts


async def _repair_succeeded_submission(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    generator: Any,
    submit_operation: dict[str, Any],
) -> tuple[ExecutionRecord, list[Artifact]]:
    official_id = submit_operation.get("official_id")
    if not isinstance(official_id, str):
        return ExecutionRecord(
            task_id=task.task_id, provider=provider, status="submission_uncertain"
        ), []
    created, repair = await services.repository.create_artifact_repair_intent_if_absent(
        run_id, task.task_id, provider, official_id, uuid4().hex,
        submit_operation["task_fingerprint"],
    )
    if not created:
        existing = await _existing_valid_artifacts(services, run_id, task)
        if repair["phase"] == "succeeded" and existing is not None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="succeeded"
            ), existing
        if repair["phase"] != "intent_created":
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id,
                status=repair["phase"],
            ), []
    try:
        submission = await _poll_submission(
            generator,
            ProviderSubmission(provider=provider, provider_task_id=official_id,
                               status="submitted"),
            services,
            run_id,
            task.task_id,
        )
        if submission is None:
            target = "timed_out"
            artifacts: list[Artifact] = []
        elif submission.status.lower() not in _SUCCESS_PROVIDER_STATUSES:
            target = "failed"
            artifacts = []
        else:
            artifacts = await _materialize_submission(
                services, run_id, task, submission
            )
            target = "succeeded"
        changed = await _transition_operation(
            services, run_id, task.task_id, repair, target, official_id
        )
        latest = await services.repository.get_operation(
            run_id, task.task_id, repair["operation"]
        )
        authoritative = target if changed else (
            latest["phase"] if latest is not None else "failed"
        )
        if authoritative == "succeeded":
            valid = await _existing_valid_artifacts(services, run_id, task)
            if valid is not None:
                artifacts = valid
            else:
                authoritative = "failed"
                artifacts = []
        elif artifacts:
            await services.repository.delete_task_artifacts(
                run_id, task.task_id
            )
            artifacts = []
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status=authoritative,
        ), artifacts
    except Exception as exc:
        latest = await services.repository.get_operation(
            run_id, task.task_id, repair["operation"]
        )
        if latest is not None and latest["phase"] == "intent_created":
            await _transition_operation(
                services, run_id, task.task_id, latest, "failed", official_id
            )
            latest = await services.repository.get_operation(
                run_id, task.task_id, repair["operation"]
            )
        if latest is not None and latest["phase"] == "succeeded":
            valid = await _existing_valid_artifacts(services, run_id, task)
            if valid is not None:
                return ExecutionRecord(
                    task_id=task.task_id, provider=provider,
                    provider_task_id=official_id, status="succeeded",
                ), valid
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id,
            status=latest["phase"] if latest is not None else "failed",
            error=_execution_error(exc),
        ), []


async def _finish_submit_phase(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    operation: dict[str, Any],
    target: str,
    official_id: str,
    artifacts: list[Artifact] | None = None,
    error: dict[str, object] | None = None,
) -> tuple[ExecutionRecord, list[Artifact]]:
    if target == "timed_out" and error is None:
        error = {
            "category": "transient_error",
            "message": "生成服务等待超时，请稍后重新运行。",
            "retryable": True,
        }

    changed = await _transition_operation(
        services, run_id, task.task_id, operation, target, official_id
    )
    latest = await services.repository.get_operation(
        run_id, task.task_id, "submit"
    )
    if latest is None or (
        latest.get("provider") != provider
        or latest.get("task_fingerprint") != _task_fingerprint(task)
        or latest.get("official_id") != official_id
    ):
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status="submission_uncertain",
        ), []
    authoritative = target if changed else latest["phase"]
    if authoritative == "succeeded":
        valid = await _existing_valid_artifacts(services, run_id, task)
        if valid is None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="failed",
            ), []
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status="succeeded",
        ), valid
    if artifacts:
        await services.repository.delete_task_artifacts(run_id, task.task_id)
    return ExecutionRecord(
        task_id=task.task_id, provider=provider,
        provider_task_id=official_id, status=authoritative,
        error=error,
    ), []


#: 供应商拒绝里表示「输入图含真人」的措辞（火山原文是英文）。
_REAL_PERSON_REJECTION = re.compile(
    r"may contain real person|real person|contains?\s+real\s+human|真人",
    re.IGNORECASE,
)


def _should_switch_to_portrait(
    exc: BaseException,
    provider: str,
    services: GraphServices,
) -> bool:
    """这次拒绝是不是「输入图疑似真人」，且值得自动改走真人类通道。

    用户 2026-09-18：Seedance 对真人素材是合规红线（换模型没用），真人素材本来就要走
    私域虚拟人像（`asset://`）通道 —— 所以这里自动切一次。
    """
    if provider == "volcengine_portrait":
        return False  # 已经在真人通道上了，别再切
    if getattr(services, "portrait_video_generator", None) is None:
        return False  # 没配真人通道，切不了
    detail = getattr(exc, "detail", None)
    text = " ".join(
        str(part)
        for part in (
            getattr(detail, "message", ""),
            getattr(detail, "technical_detail", ""),
        )
    )
    return bool(_REAL_PERSON_REJECTION.search(text))


#: 火山对参考图的硬限制（素材/生成同源）：每边 300~6000px，宽高比 0.4~2.5。
#:
#: 用户 2026-09-18 实测报错：「expected the height to be at least 300px, but received
#: a 384x254px image」—— 文档里的横幅/小图（高 228~254px）被当参考图提交，整单被拒。
_REFERENCE_MIN_SIDE = 300
_REFERENCE_MAX_SIDE = 6000
_REFERENCE_MIN_ASPECT = 0.4
_REFERENCE_MAX_ASPECT = 2.5


def _reference_size_problem(asset: MediaAsset) -> str | None:
    """参考图尺寸不符合火山要求时返回原因，符合则返回 None。"""
    if not asset.mime_type.startswith("image/"):
        return None
    width = int(getattr(asset, "width", 0) or 0)
    height = int(getattr(asset, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None  # 拿不到尺寸就不拦，交给供应商判断
    if width < _REFERENCE_MIN_SIDE or height < _REFERENCE_MIN_SIDE:
        return f"尺寸 {width}x{height} 小于 {_REFERENCE_MIN_SIDE}px"
    if width > _REFERENCE_MAX_SIDE or height > _REFERENCE_MAX_SIDE:
        return f"尺寸 {width}x{height} 超过 {_REFERENCE_MAX_SIDE}px"
    ratio = width / height
    if ratio < _REFERENCE_MIN_ASPECT or ratio > _REFERENCE_MAX_ASPECT:
        return f"宽高比 {ratio:.2f} 超出 {_REFERENCE_MIN_ASPECT}~{_REFERENCE_MAX_ASPECT}"
    return None


def _upscale_reference(asset: MediaAsset) -> MediaAsset | None:
    """把过小的参考图**等比放大**到合规尺寸（不裁切、不拉伸）。

    用户口径（2026-09-18）：「不要拦截，能不能帮我自动等比例放大」—— 火山要求参考图
    每边 ≥300px，文档里的横幅（384x254）以前会让整单被拒，现在直接放大后再提交。
    放大失败（文件损坏等）返回 None，由调用方决定丢弃。
    """
    width = int(getattr(asset, "width", 0) or 0)
    height = int(getattr(asset, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    scale = max(
        _REFERENCE_MIN_SIDE / width,
        _REFERENCE_MIN_SIDE / height,
    )
    if scale <= 1:
        return None
    target_width = max(_REFERENCE_MIN_SIDE, round(width * scale))
    target_height = max(_REFERENCE_MIN_SIDE, round(height * scale))
    # 放大后也不能超过上限：超过就整体缩回去（仍保持比例）。
    overflow = max(
        target_width / _REFERENCE_MAX_SIDE,
        target_height / _REFERENCE_MAX_SIDE,
        1,
    )
    target_width = max(_REFERENCE_MIN_SIDE, round(target_width / overflow))
    target_height = max(_REFERENCE_MIN_SIDE, round(target_height / overflow))
    source = Path(str(asset.local_path))
    target = source.with_name(f"{source.stem}-upscaled.png")
    try:
        with Image.open(source) as image:
            image.convert("RGB").resize(
                (target_width, target_height), Image.Resampling.LANCZOS
            ).save(target, format="PNG")
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "参考图放大失败 asset=%s path=%s", asset.asset_id, source, exc_info=True
        )
        return None
    content = target.read_bytes()
    return asset.model_copy(
        update={
            "local_path": target,
            "mime_type": "image/png",
            "size": len(content),
            "width": target_width,
            "height": target_height,
            "sha256": sha256(content).hexdigest(),
        }
    )


async def _without_unusable_references(
    assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
) -> list[MediaAsset]:
    """执行前整理参考素材：过小的图**等比放大**，修不了的跳过，都记一条事件。

    以前这两种都会让整单被火山拒（`invalid_media` / `submit_invalidparameter`），
    用户只看到一句「生成服务拒绝了请求」。现在：能修的修（放大 ✓）、修不了的跳过
    并写清原因（用户 2026-09-18：「不要拦截，能不能帮我自动等比例放大」）。

    **2026-10-08：这里不再摘视频。** 以前无条件把 `video/*` 从素材列表里摘掉，
    与上游能力（`video_url + role=reference_video`）直接冲突，而且是 2026-09-24
    「节点全 completed / 零产物 / last_error:null」的根因之一：摘了素材、任务却还
    引用着它 → `generation_asset_mapping_mismatch`。视频现在原样保留，要不要降级
    只由「上游是否明确拒绝」决定（见 `_video_references_to_frames`）。
    """
    undersized = [
        asset
        for asset in assets
        if _reference_size_problem(asset) is not None
    ]
    if not undersized:
        return assets
    notes: list[str] = []
    replacements: dict[str, MediaAsset] = {}
    dropped: set[str] = set()
    for asset in undersized:
        problem = _reference_size_problem(asset) or ""
        if "宽高比" in problem:
            # 比例问题放大也改不了（比例不随缩放变化），照旧提交交给供应商判断。
            notes.append(f"{asset.asset_id} {problem}（放大改不了比例，仍按原样提交）")
            continue
        fixed = _upscale_reference(asset)
        if fixed is None:
            dropped.add(asset.asset_id)
            notes.append(f"{asset.asset_id} {problem}，且放大失败，已跳过")
            continue
        replacements[asset.asset_id] = fixed
        notes.append(
            f"{asset.asset_id} {problem} → 已等比放大到 "
            f"{fixed.width}x{fixed.height}"
        )
    if notes:
        await services.repository.append_event(
            run_id,
            "execute_selected_tasks",
            "running",
            "参考素材已自动整理：" + "；".join(notes),
        )
        _LOGGER.warning("整理参考素材 run=%s notes=%s", run_id, notes)
    result: list[MediaAsset] = []
    for asset in assets:
        if asset.asset_id in dropped:
            continue
        result.append(replacements.get(asset.asset_id, asset))
    return result


async def _without_video_references(
    assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
) -> list[MediaAsset]:
    """兼容旧调用点（历史名字）。

    语义已随 2026-10-08 的 P0 修复反转：**视频保留**，不再在这里摘除 —— 降级只发生
    在上游明确拒绝视频时（`_video_references_to_frames`）。保留这个名字是为了不
    破坏 `repro_video_reference_frame_fix.py` 一类历史脚本的 import。
    """
    return await _without_unusable_references(assets, run_id, services)


#: 参考视频抽帧后的素材 id 后缀。planner 的 `_register_video_frame_aliases` 与前端
#: `review-state.js` 都按这个后缀把抽帧素材认回原视频，**不能改成带序号的写法**。
_VIDEO_FRAME_ASSET_SUFFIX = "-frame"

#: 参考视频被**裁剪**后的素材 id 后缀（超出上游时长上限时的首选修复，
#: 保留运动参考；见 `_trim_reference_videos`）。
_VIDEO_TRIM_ASSET_SUFFIX = "-trim"

#: 参考视频被**压缩/转码**后的素材 id 后缀（体积超限或编码不被上游接受时的首选修复：
#: 保留**完整时长与帧率**，只降码率/必要时降分辨率、并把编码归一成 H.264）。
_VIDEO_COMPRESS_ASSET_SUFFIX = "-small"


@dataclass(frozen=True, slots=True)
class _CompressStep:
    """一轮压缩/转码档位。

    - `kind="crf"`：**画质优先转码**（不追体积，只把编码归一成上游认的 H.264）；
    - `kind="bitrate"`：按字节预算反推码率（必要时同时等比降分辨率）。
    """

    kind: str
    max_long_side: int | None
    budget_ratio: float = 0.92


#: 编码不符（HEVC/H.265 等）时的**第一轮**：只换编码，分辨率/帧率/时长一律不动。
_COMPRESS_CODEC_STEP = _CompressStep(kind="crf", max_long_side=None)

#: 体积超限时的逐轮档位：第一轮**只降码率、保分辨率**，还超才逐级降分辨率。
_COMPRESS_SIZE_STEPS: tuple[_CompressStep, ...] = (
    _CompressStep(kind="bitrate", max_long_side=None, budget_ratio=0.92),
    _CompressStep(kind="bitrate", max_long_side=1280, budget_ratio=0.88),
    _CompressStep(kind="bitrate", max_long_side=854, budget_ratio=0.82),
)

#: 体积问题的**轮数上限**（到顶就交给下一级「裁短时长」）。
_VIDEO_COMPRESS_MAX_ROUNDS = len(_COMPRESS_SIZE_STEPS)


def _video_asset_for_reference(
    asset_id: str,
    assets_by_id: Mapping[str, MediaAsset],
) -> MediaAsset | None:
    """引用指向视频素材时返回那个视频，否则 None。

    两种写法都认：直接引用视频（`upload-xxx` / `video-1`），以及历史契约里的抽帧
    别名（`video-1-frame`，planner 的别名表与前端都是按这个后缀认的）。
    """
    asset = assets_by_id.get(asset_id)
    if asset is not None and asset.mime_type.startswith("video/"):
        return asset
    if asset_id.endswith(_VIDEO_FRAME_ASSET_SUFFIX):
        base = assets_by_id.get(asset_id[: -len(_VIDEO_FRAME_ASSET_SUFFIX)])
        if base is not None and base.mime_type.startswith("video/"):
            return base
    return None


def _videos_of_task(
    task: GenerationTask,
    assets_by_id: Mapping[str, MediaAsset],
) -> list[tuple[ImageReference, MediaAsset]]:
    return [
        (reference, video)
        for reference in sorted(task.reference_images, key=lambda i: i.order)
        if (video := _video_asset_for_reference(reference.asset_id, assets_by_id))
        is not None
    ]


def _normalize_video_references(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset],
) -> GenerationTask:
    """把「指向视频素材」的引用**规范成 `reference_video`**，不再抽帧替换。

    2026-10-08 P0 修复（根因 ②）：以前这里**默认**把 `reference_video` 换成该视频的
    一张抽帧静帧、role 改写为 `reference_image` —— 用户手动挂回参考视频后，运动参考
    被阉割成静帧参考，而静帧携带的**内容**（小红书水印 + 用户明确不要参考的紫海绵块）
    被当成普通参考图吸收进成片（紫色占比 4.0s 从 0.17 跳到 0.38~0.46）。

    上游方舟多参考模式**本来就支持** `video_url + role=reference_video`
    （`integrations/seedance.py` 的 `_validate_submission` 明确放行），所以默认路径
    就是**原样把视频交给上游**。这里只做三件不花钱、不丢信息的规范化：

    1. 历史契约里的抽帧别名（`video-1-frame`）归回原视频 `video-1`；
    2. 视频引用的 role 一律写成 `reference_video`；
    3. 去重（同一个视频被引用两次 → 只留一条，否则供应商会以 `duplicate_reference` 拒单）。

    抽帧只作为**失败兜底**，见 `_video_references_to_frames`。
    """
    assets_by_id: dict[str, MediaAsset] = {
        asset.asset_id: asset for asset in document.media_assets
    }
    for asset in extra_assets:
        assets_by_id.setdefault(asset.asset_id, asset)
    videos = _videos_of_task(task, assets_by_id)
    if not videos:
        return task
    updated: list[ImageReference] = []
    seen: set[str] = set()
    for reference in sorted(task.reference_images, key=lambda item: item.order):
        video = _video_asset_for_reference(reference.asset_id, assets_by_id)
        candidate = (
            reference
            if video is None
            else ImageReference(
                asset_id=video.asset_id,
                role="reference_video",
                order=reference.order,
            )
        )
        if candidate.asset_id in seen:
            continue
        seen.add(candidate.asset_id)
        updated.append(candidate)
    try:
        canonical = canonicalize_references(updated)
    except ValueError:
        raise _validation_error("The approved plan is not valid") from None
    if canonical == list(task.reference_images):
        return task
    return task.model_copy(update={"reference_images": canonical})


async def _extract_video_reference_frame(
    video: MediaAsset,
    document: NormalizedDocument,
    run_id: str,
    services: GraphServices,
) -> tuple[MediaAsset, str] | None:
    """把参考视频抽成**一张**图片素材（`<video_id>-frame`）—— 仅用于失败兜底。

    素材 id 用的是契约里的单帧命名（不带序号）：planner 的 `_asset_base_id` /
    `_register_video_frame_aliases` 只认 `-frame` 后缀，换成 `-frame-1` 会让覆盖统计
    与前端别名同时认不出来。

    选帧不再用 `representative_frame_index`（实测那会取到带小红书水印的第 1 帧、
    主体还是不要参考的紫海绵），改由 `choose_reference_frame` 避开首尾并跳过角部
    带水印的帧。抽帧/落盘失败一律返回 None（不猜内容），由调用方沿用老行为
    「摘掉该参考视频」并记事件。
    """
    try:
        with tempfile.TemporaryDirectory(
            prefix="feishu-video-ref-frame-"
        ) as work_dir:
            paths = await asyncio.to_thread(
                extract_video_frames,
                Path(str(video.local_path)),
                _VIDEO_FRAME_COUNT,
                Path(work_dir),
            )
            choice = choose_reference_frame(paths)
            chosen = paths[choice.index - 1]
            content = await asyncio.to_thread(chosen.read_bytes)
    except Exception:  # noqa: BLE001 —— 抽帧失败不能拖挂整单
        _LOGGER.warning(
            "参考视频抽帧失败，已摘掉该参考视频 video=%s",
            video.asset_id,
            exc_info=True,
        )
        return None
    frame_id = f"{video.asset_id}{_VIDEO_FRAME_ASSET_SUFFIX}"
    try:
        stored = services.file_store.save_input(
            run_id, f"{frame_id}.jpg", content
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "参考视频抽帧落盘失败 video=%s", video.asset_id, exc_info=True
        )
        return None
    return (
        MediaAsset(
            asset_id=frame_id,
            source_block_id=video.source_block_id,
            origin="video_frame",
            file_token=None,
            local_path=stored.local_path,
            mime_type=stored.mime_type,
            size=stored.size,
            sha256=stored.sha256,
            width=stored.width,
            height=stored.height,
        ),
        choice.reason,
    )


#: 压缩缓存里「已经试过、压不出来」的哨兵值。
#:
#: 为什么需要它：主动预检与被动修复会**两次**走到这一级（预检压不动 → 提交被拒 →
#: 梯子再试一次）。没有这个哨兵，同一个视频会被反复压（3 轮 × 2 次 = 6 次 ffmpeg），
#: 而「轮数上限」的语义也变成"每次尝试 3 轮"而不是"每个视频 3 轮"。
_COMPRESS_UNAVAILABLE = object()


def _human_bytes(size: int) -> str:
    """事件里给人看的体积写法（与用户自己的说法一致：「18.4MB」）。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size}B"


def _video_size_of(video: MediaAsset) -> int:
    """参考视频的实际字节数：以磁盘为准（闸门量的就是 `st_size`）。"""
    try:
        return Path(str(video.local_path)).stat().st_size
    except OSError:
        return int(video.size or 0)


def _compress_reason_suffix(reason: str) -> str:
    """压缩事件里要不要再带上拒绝原因。

    体积超限的拒绝原因（「参考图片超过大小限制…cause=input_too_large」）与前半句
    「体积过大（X > 上限 Y）」完全重复，带上只会把事件撑长、让人更难读；别的拒绝
    原因（例如上游的通用拒绝）才值得附上一句，且截断到一行以内。
    """
    if _is_size_rejection(reason):
        return ""
    cleaned = " ".join(str(reason).split())
    if not cleaned:
        return ""
    return f"，{cleaned[:120]}"


async def _compress_reference_videos(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
    cache: dict[str, MediaAsset | None],
    max_bytes: int | None,
    *,
    reason: str,
    accepted_codecs: frozenset[str] | set[str] | None = None,
) -> tuple[GenerationTask, list[MediaAsset]] | None:
    """**首选修复**：参考视频体积超限 / 编码不被上游接受时**重编码后重传**。

    解决 2026-10-10 用户撞到的两个不同错误（同一级修复，两种触发）：

    1. `generation_input_too_large`（用户原话「是视频太大了，能不能帮我自动缩小」）
       —— 实测（零成本复现，`_diag_repro_input_too_large.py`）量的是**单个参考素材
       文件的字节数**，拦截点是本机闸门
       `SeedanceVideoGenerator(max_input_bytes)`（默认 32 MiB = 33,554,432 B），
       **发生在任何上游请求之前**（复现时上游请求数 = 0）；上游方舟文档允许
       「单个视频不超过 200 MB」，34.06 MiB 只越线 2.06 MB。
    2. `invalid_media`（`input media detect failed`，用户原话「怎么又失败了」）
       —— 上游拿到素材后**解不了**：实测两条失败样本都是 **HEVC/H.265**
       （见 `SeedanceVideoGenerator.reference_video_accepted_codecs` 的证据链）。

    梯子顺序（每一级都留可见事件，任何一级不成立就不动）：

    1. **压缩/转码**（本函数）：不牺牲任何时长/帧 —— 只降码率、必要时降分辨率、
       并把编码归一成 H.264；
    2. **裁短**（`_trim_reference_videos`）：丢时间长度；
    3. **抽帧**（`_video_references_to_frames`）：连"运动"一起丢。

    「没超就一个字都不动」：体积与编码都合格时直接跳过、**不做任何重编码**
    （重编码是无谓的画质损失与耗时）。
    """
    codecs = (
        frozenset(codec.lower() for codec in accepted_codecs)
        if accepted_codecs
        else None
    )
    has_size_cap = (
        isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes > 0
    )
    if not has_size_cap and codecs is None:
        return None
    assets_by_id: dict[str, MediaAsset] = {
        asset.asset_id: asset for asset in document.media_assets
    }
    for asset in extra_assets:
        assets_by_id.setdefault(asset.asset_id, asset)
    videos = _videos_of_task(task, assets_by_id)
    if not videos:
        return None
    created: list[MediaAsset] = []
    notes: list[str] = []
    replacements: dict[str, str] = {}
    for reference, video in videos:
        cache_key = f"{video.asset_id}:compress"
        if cache.get(cache_key) is _COMPRESS_UNAVAILABLE:
            # 这个视频在预检阶段已经压过、压不动 → 不再空转（事件也已经写过）。
            continue
        cached = cache.get(cache_key)
        if isinstance(cached, MediaAsset):
            compressed = cached
            trail: list[str] = []
            original_info = None
            compressed_info = None
        else:
            original_bytes = _video_size_of(video)
            original_info = await asyncio.to_thread(
                probe_video_info, Path(str(video.local_path))
            )
            if original_info is not None and original_info.size_bytes > 0:
                original_bytes = original_info.size_bytes
            size_over = has_size_cap and original_bytes > max_bytes  # type: ignore[operator]
            codec = original_info.codec if original_info is not None else None
            codec_mismatch = bool(
                codecs
                and isinstance(codec, str)
                and codec.lower() not in codecs
            )
            if not size_over and not codec_mismatch:
                # 体积与编码都合格：一个字都不动（不无脑重编码）。
                cache[cache_key] = None
                continue
            outcome = await _compress_video_asset(
                video,
                original_info,
                original_bytes,
                max_bytes if has_size_cap else None,
                codec_mismatch,
                run_id,
                services,
            )
            compressed = outcome.asset
            trail = outcome.trail
            compressed_info = outcome.result.info if outcome.result else None
            cache[cache_key] = (
                compressed if compressed is not None else _COMPRESS_UNAVAILABLE
            )
        if compressed is None:
            problems = []
            if has_size_cap and _video_size_of(video) > max_bytes:  # type: ignore[operator]
                problems.append(
                    f"体积过大（{_human_bytes(_video_size_of(video))} > 上限 "
                    f"{_human_bytes(max_bytes)})"  # type: ignore[arg-type]
                )
            if original_info is not None and codecs and (
                isinstance(original_info.codec, str)
                and original_info.codec.lower() not in codecs
            ):
                problems.append(f"编码上游不认（{original_info.codec}）")
            note = (
                f"{video.asset_id} {'、'.join(problems) or '参考视频不合格'}"
                "但重编码失败，未改动该素材"
            )
            if trail:
                note += "；逐轮：" + "，".join(trail)
            notes.append(note)
            continue
        if original_info is None:
            original_info = await asyncio.to_thread(
                probe_video_info, Path(str(video.local_path))
            )
        if compressed_info is None:
            compressed_info = await asyncio.to_thread(
                probe_video_info, Path(str(compressed.local_path))
            )
        created.append(compressed)
        replacements[reference.asset_id] = compressed.asset_id
        before = (
            f"{original_info.width}x{original_info.height}"
            if original_info is not None
            else "?x?"
        )
        after = (
            f"{compressed_info.width}x{compressed_info.height}"
            if compressed_info is not None
            else "?x?"
        )
        kbps = (
            f"{compressed_info.bitrate_bps / 1000:.0f}kbps"
            if compressed_info is not None and compressed_info.bitrate_bps
            else "未知码率"
        )
        size_over_now = has_size_cap and _video_size_of(video) > max_bytes  # type: ignore[operator]
        codec_now = (
            original_info.codec
            if original_info is not None and isinstance(original_info.codec, str)
            else None
        )
        codec_mismatch_now = bool(
            codecs and codec_now and codec_now.lower() not in codecs
        )
        problems = []
        if size_over_now:
            problems.append(
                f"体积过大（{_human_bytes(_video_size_of(video))} > 上限约 "
                f"{_human_bytes(max_bytes)}"  # type: ignore[arg-type]
                f"{_compress_reason_suffix(reason)}）"
            )
        if codec_mismatch_now:
            problems.append(f"编码上游不认（{codec_now} → h264）")
        action = (
            "已压缩并转码后重传"
            if size_over_now and codec_mismatch_now
            else "已压缩后重传"
            if size_over_now
            else "已转码后重传"
        )
        note = (
            f"参考视频{'、'.join(problems)}，{action}："
            f"{reference.asset_id} → {compressed.asset_id}"
            f"（{before} → {after} / {kbps}，"
            f"{_human_bytes(_video_size_of(compressed))}，时长与运动保留）"
        )
        if len(trail) > 1:
            note += "；逐轮：" + "，".join(trail)
        notes.append(note)
    if not replacements:
        if notes:
            await services.repository.append_event(
                run_id,
                "execute_selected_tasks",
                "running",
                "；".join(notes),
            )
        return None
    updated = [
        reference
        if reference.asset_id not in replacements
        else ImageReference(
            asset_id=replacements[reference.asset_id],
            role=reference.role,
            order=reference.order,
        )
        for reference in sorted(task.reference_images, key=lambda item: item.order)
    ]
    try:
        canonical = canonicalize_references(updated)
    except ValueError:
        raise _validation_error("The approved plan is not valid") from None
    await services.repository.append_event(
        run_id,
        "execute_selected_tasks",
        "running",
        "；".join(notes),
    )
    _LOGGER.warning(
        "参考视频已压缩后重传 run=%s task=%s notes=%s",
        run_id,
        task.task_id,
        notes,
    )
    return (
        task.model_copy(update={"reference_images": canonical}),
        created,
    )


@dataclass(frozen=True, slots=True)
class _CompressOutcome:
    """一次「压缩/转码」的产物与逐轮记录（失败时 asset 为 None）。"""

    asset: MediaAsset | None
    trail: list[str]
    result: Any = None


async def _compress_video_asset(
    video: MediaAsset,
    source_info: Any,
    original_bytes: int,
    max_bytes: int | None,
    codec_mismatch: bool,
    run_id: str,
    services: GraphServices,
) -> _CompressOutcome:
    """把单个参考视频重编码到**上游能收**的样子：`{video_id}-small` 素材。

    返回 `(素材 | None, 逐轮记录, 最后一轮结果)` —— 无论成功失败都把每一轮的
    **实际结果**带回去，调用方要把它写进可见事件（「每轮记录实际结果」「不静默」）。

    **迭代收敛**（档位见 `_COMPRESS_CODEC_STEP` / `_COMPRESS_SIZE_STEPS`）：

    - 编码不符（HEVC 等）先来一轮**画质优先转码**（H.264，分辨率/帧率/时长不动）；
    - 体积超限再按字节预算逐轮降码率，第 1 轮保分辨率，之后逐级把长边降到 1280 / 854；
    - 每轮都拿**产物的真实字节数**比对闸门；到 `_VIDEO_COMPRESS_MAX_ROUNDS` 轮
      仍不合格就放弃 —— 由调用方交给下一级（裁短时长），而不是无限重压。
    - 每一轮的产物本身都过了 `verify_reference_video` 自检（能解码 + H.264 + mp4 +
      时长/分辨率/帧率符合预期），**自检不过的产物绝不会被采用**。
    """
    compressed_id = f"{video.asset_id}{_VIDEO_COMPRESS_ASSET_SUFFIX}"
    trail: list[str] = []
    budget = (
        max_bytes
        if isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes > 0
        else original_bytes
    )
    steps: list[_CompressStep] = []
    if codec_mismatch:
        steps.append(_COMPRESS_CODEC_STEP)
    if max_bytes and (codec_mismatch or original_bytes > max_bytes):
        steps.extend(_COMPRESS_SIZE_STEPS)
    if not steps:
        return _CompressOutcome(None, ["无需重编码"])
    try:
        with tempfile.TemporaryDirectory(
            prefix="feishu-video-ref-compress-"
        ) as work_dir:
            source = Path(str(video.local_path))
            # 源视频的**真实解码帧数**只数一次，供每一轮自检比对「没有抽帧」。
            source_frames = await asyncio.to_thread(
                count_decoded_frames, source
            )
            if not source_frames:
                return _CompressOutcome(
                    None, ["源视频无法完整解码（连帧数都数不出来），不重编码"]
                )
            for index, step in enumerate(steps, start=1):
                output = Path(work_dir) / f"compressed-{index}.mp4"
                result = await asyncio.to_thread(
                    compress_video,
                    source,
                    None if step.kind == "crf" else max_bytes,
                    output,
                    max_long_side=step.max_long_side,
                    budget_ratio=step.budget_ratio,
                    crf=20 if step.kind == "crf" else None,
                    expected_frames=source_frames,
                )
                if result is None:
                    trail.append(f"第 {index} 轮（{step.kind}）压缩失败或自检未过")
                    continue
                info = result.info
                rate_kbps = (
                    result.video_kbps
                    if result.video_kbps > 0
                    else (info.bitrate_bps or 0) / 1000
                )
                trail.append(
                    f"第 {index} 轮（{step.kind}）{info.width}x{info.height}/"
                    f"{info.codec or '?'}/{rate_kbps / 1000:.1f}Mbps → "
                    f"{_human_bytes(info.size_bytes)}"
                    + ("（达标）" if info.size_bytes <= budget else "（仍超）")
                )
                if info.size_bytes > budget:
                    continue
                content = await asyncio.to_thread(output.read_bytes)
                saved = services.file_store.save_input(
                    run_id, f"{compressed_id}.mp4", content
                )
                return _CompressOutcome(
                    MediaAsset(
                        asset_id=compressed_id,
                        source_block_id=video.source_block_id,
                        origin="video_compress",
                        file_token=None,
                        local_path=saved.local_path,
                        mime_type=saved.mime_type,
                        size=saved.size,
                        sha256=saved.sha256,
                        width=saved.width,
                        height=saved.height,
                    ),
                    trail,
                    result,
                )
            trail.append(
                f"已用满 {len(steps)} 轮仍未压到 {_human_bytes(budget)} 以内"
                f"（原始 {_human_bytes(original_bytes)}）"
            )
            return _CompressOutcome(None, trail)
    except Exception:  # noqa: BLE001 —— 压缩失败不能拖挂整单
        _LOGGER.warning(
            "参考视频压缩失败 video=%s max_bytes=%s",
            video.asset_id,
            max_bytes,
            exc_info=True,
        )
        trail.append("压缩过程异常（详见日志）")
        return _CompressOutcome(None, trail)


async def _trim_reference_videos(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
    cache: dict[str, MediaAsset | None],
    max_seconds: float,
    *,
    reason: str,
) -> tuple[GenerationTask, list[MediaAsset]] | None:
    """修复梯子的**第二级**：参考视频超出上游时长上限时**裁短后重传**（保留运动参考）。

    第一级是 `_compress_reference_videos`（体积超限时压缩，保住完整时长与帧率）；
    这一级只在**时长**确实超限时动手，丢的是"时间长度"，不是"运动"本身。

    上游实测硬上限（错误原文）：`the parameter video duration (seconds) specified in
    the request must be less than or equal to 30.2 for model doubao-seedance-2-5`，
    而「刮太空沙」的参考视频是 43.7s。抽帧会丢掉用户要的运动信息（P0 要修的正是
    这件事），所以这里先裁短；裁剪不可行时由调用方退到抽帧（第三级）。

    只改被裁的那个视频素材，**role 仍是 `reference_video`、位置不变**，所以提示词里
    的 `@视频N` 不需要重映射。裁剪也一样必须留可见事件。
    """
    assets_by_id: dict[str, MediaAsset] = {
        asset.asset_id: asset for asset in document.media_assets
    }
    for asset in extra_assets:
        assets_by_id.setdefault(asset.asset_id, asset)
    videos = _videos_of_task(task, assets_by_id)
    if not videos:
        return None
    created: list[MediaAsset] = []
    notes: list[str] = []
    replacements: dict[str, str] = {}
    for reference, video in videos:
        cached = cache.get(f"{video.asset_id}:trim")
        if isinstance(cached, MediaAsset):
            trimmed = cached
        else:
            duration = await asyncio.to_thread(
                probe_video_duration, Path(str(video.local_path))
            )
            if duration is None or duration <= max_seconds:
                cache[f"{video.asset_id}:trim"] = None
                continue
            trimmed = await _trim_video_asset(
                video, duration, max_seconds, run_id, services
            )
            cache[f"{video.asset_id}:trim"] = trimmed
        if trimmed is None:
            notes.append(
                f"{video.asset_id} 超出参考视频时长上限（{max_seconds:g}s）"
                "但裁剪失败，已改用抽帧"
            )
            continue
        created.append(trimmed)
        replacements[reference.asset_id] = trimmed.asset_id
        notes.append(
            f"参考视频超出上游时长上限（{reason}），已裁剪到 {max_seconds:g}s 后重传："
            f"{reference.asset_id} → {trimmed.asset_id}（保留运动参考）"
        )
    if not replacements:
        if notes:
            await services.repository.append_event(
                run_id,
                "execute_selected_tasks",
                "running",
                "；".join(notes),
            )
        return None
    updated = [
        reference
        if reference.asset_id not in replacements
        else ImageReference(
            asset_id=replacements[reference.asset_id],
            role=reference.role,
            order=reference.order,
        )
        for reference in sorted(task.reference_images, key=lambda item: item.order)
    ]
    try:
        canonical = canonicalize_references(updated)
    except ValueError:
        raise _validation_error("The approved plan is not valid") from None
    await services.repository.append_event(
        run_id,
        "execute_selected_tasks",
        "running",
        "；".join(notes),
    )
    _LOGGER.warning(
        "参考视频已裁剪后重传 run=%s task=%s notes=%s",
        run_id,
        task.task_id,
        notes,
    )
    return (
        task.model_copy(update={"reference_images": canonical}),
        created,
    )


async def _trim_video_asset(
    video: MediaAsset,
    duration: float,
    max_seconds: float,
    run_id: str,
    services: GraphServices,
) -> MediaAsset | None:
    """把视频裁到 `max_seconds` 内并落盘成 `{video_id}-trim` 素材。"""
    trimmed_id = f"{video.asset_id}{_VIDEO_TRIM_ASSET_SUFFIX}"
    try:
        with tempfile.TemporaryDirectory(
            prefix="feishu-video-ref-trim-"
        ) as work_dir:
            output = Path(work_dir) / "trimmed.mp4"
            result = await asyncio.to_thread(
                trim_video, Path(str(video.local_path)), max_seconds, output
            )
            if result is None:
                return None
            content = await asyncio.to_thread(Path(result).read_bytes)
    except Exception:  # noqa: BLE001 —— 裁剪失败不能拖挂整单
        _LOGGER.warning(
            "参考视频裁剪失败 video=%s duration=%s max=%s",
            video.asset_id,
            duration,
            max_seconds,
            exc_info=True,
        )
        return None
    try:
        stored = services.file_store.save_input(
            run_id, f"{trimmed_id}.mp4", content
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "参考视频裁剪落盘失败 video=%s", video.asset_id, exc_info=True
        )
        return None
    return MediaAsset(
        asset_id=trimmed_id,
        source_block_id=video.source_block_id,
        origin="video_trim",
        file_token=None,
        local_path=stored.local_path,
        mime_type=stored.mime_type,
        size=stored.size,
        sha256=stored.sha256,
        width=stored.width,
        height=stored.height,
    )


async def _video_references_to_frames(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
    cache: dict[str, MediaAsset | None],
    *,
    reason: str,
) -> tuple[GenerationTask, list[MediaAsset]]:
    """**失败兜底**：把「指向视频素材」的引用换成该视频的抽帧画面。

    触发条件只有一个：**上游明确拒绝**这个视频（拿得到可判定的错误），或者当前通道
    确定性地不支持视频引用（真人类通道只收图片素材）。**任何正常路径都不许偷偷降级**
    —— 这正是 2026-10-08 用户抱怨的「挂上参考视频后主角被换成参考视频里不要参考的主
    角」的成因：以前这里是**默认**行为。

    降级必须**留可见痕迹**：写一条 `execute_selected_tasks/running` 事件（含降级原因
    与被拒绝的素材 id），并在日志里 warning。返回 `(改写后的任务, 新增的抽帧素材)`；
    抽帧失败的那个视频沿用老行为**摘掉引用**并记事件（剩下的参考素材照常生成，一张都
    不剩时由调用方明确失败，不许静默空跑）。
    """
    assets_by_id: dict[str, MediaAsset] = {
        asset.asset_id: asset for asset in document.media_assets
    }
    for asset in extra_assets:
        assets_by_id.setdefault(asset.asset_id, asset)
    videos = _videos_of_task(task, assets_by_id)
    if not videos:
        return task, []

    mime_types = {
        asset_id: asset.mime_type for asset_id, asset in assets_by_id.items()
    }
    created: list[MediaAsset] = []
    notes: list[str] = []
    replacements: dict[str, str] = {}
    extracted_frames: dict[str, ImageReference] = {}
    dropped_refs: set[str] = set()
    for reference, video in videos:
        # 抽帧别名要能被 `remap_prompt_references` 认出媒体类型（否则 token 重映射
        # 会整体失败、提示词留在「参考视频N」上）。
        mime_types.setdefault(
            f"{video.asset_id}{_VIDEO_FRAME_ASSET_SUFFIX}", video.mime_type
        )
        if video.asset_id not in cache:
            extracted = await _extract_video_reference_frame(
                video, document, run_id, services
            )
            cache[video.asset_id] = extracted[0] if extracted else None
            if extracted is not None:
                cache[f"{video.asset_id}:reason"] = extracted[1]
        frame = cache.get(video.asset_id)
        if frame is None or not isinstance(frame, MediaAsset):
            # 上游已经拒绝过这个视频、现在又抽不出帧 → 只能摘掉引用（留在任务里
            # 只会让上游再拒一次），并且必须写进事件让人看得见。
            dropped_refs.add(reference.asset_id)
            notes.append(
                f"参考视频抽帧失败，已摘掉该参考视频：{video.asset_id}"
            )
            continue
        if frame not in created:
            created.append(frame)
        replacements[reference.asset_id] = frame.asset_id
        extracted_frames.setdefault(
            reference.asset_id,
            ImageReference(
                asset_id=frame.asset_id,
                role="reference_image",
                order=reference.order,
            ),
        )
        frame_reason = cache.get(f"{video.asset_id}:reason")
        notes.append(
            f"参考视频已改用抽帧画面：{reference.asset_id} → {frame.asset_id}"
            + (f"（{frame_reason}）" if isinstance(frame_reason, str) else "")
        )
    # 非视频引用原样保留，视频引用按**原 order** 落成抽帧引用；同素材只留一条
    # （否则供应商适配层会以 `duplicate_reference` 拒单）。
    merged: list[ImageReference] = []
    seen: set[str] = set()
    for reference in sorted(task.reference_images, key=lambda item: item.order):
        if reference.asset_id in dropped_refs:
            continue
        candidate = extracted_frames.get(reference.asset_id, reference)
        if candidate.asset_id in seen:
            continue
        seen.add(candidate.asset_id)
        merged.append(candidate)
    for asset in created:
        mime_types[asset.asset_id] = asset.mime_type
    try:
        canonical = canonicalize_references(merged)
    except ValueError:
        # 原计划本身 order 重复/断档：保持与 `_task_assets` 一致的报错口径。
        raise _validation_error("The approved plan is not valid") from None
    prompt = task.prompt
    try:
        prompt = remap_prompt_references(
            task.prompt,
            sorted(task.reference_images, key=lambda item: item.order),
            canonical,
            mime_types,
            replacement_asset_ids=replacements,
        )
    except Exception:  # noqa: BLE001 —— 重映射失败不该挡住生成
        _LOGGER.warning(
            "抽帧后重映射提示词素材 token 失败，保留原提示词 task=%s",
            task.task_id,
            exc_info=True,
        )
    head = f"参考视频被上游拒绝（{reason}），已降级为抽帧画面"
    if notes:
        await services.repository.append_event(
            run_id,
            "execute_selected_tasks",
            "running",
            head + "：" + "；".join(notes),
        )
        _LOGGER.warning(
            "参考视频降级为抽帧画面 run=%s task=%s reason=%s notes=%s",
            run_id,
            task.task_id,
            reason,
            notes,
        )
    return (
        task.model_copy(
            update={"reference_images": canonical, "prompt": prompt}
        ),
        created,
    )


async def _replace_video_references_with_frames(
    task: GenerationTask,
    document: NormalizedDocument,
    extra_assets: list[MediaAsset],
    run_id: str,
    services: GraphServices,
    cache: dict[str, MediaAsset | None],
) -> tuple[GenerationTask, list[MediaAsset]]:
    """兼容旧调用点（历史名字）：等价于「强制降级成抽帧」。

    新代码的默认路径是 `_normalize_video_references`（视频原样直传上游），只有上游
    明确拒绝时才调 `_video_references_to_frames`。这个函数保留给历史脚本 / 回归测试，
    它**显式**表示"降级"，不再是隐式默认行为。
    """
    return await _video_references_to_frames(
        task,
        document,
        extra_assets,
        run_id,
        services,
        cache,
        reason="参考视频被上游拒绝",
    )


def _unusable_reference_message(
    task: GenerationTask,
    assets: list[MediaAsset],
    *,
    had_references: bool,
) -> str | None:
    """整理后「一张参考图都不剩」时给出可读原因；正常情况返回 None。

    用户口径（2026-09-24）：「不许静默空跑」—— 以前节点全 completed、零产物、
    `last_error: null`，用户只能看到「生成失败」四个字。纯文生视频（本来就没有
    参考素材）不算异常，照常放行。
    """
    if not had_references:
        return None
    asset_ids = {asset.asset_id for asset in assets}
    missing = [
        reference.asset_id
        for reference in sorted(
            task.reference_images, key=lambda item: item.order
        )
        if reference.asset_id not in asset_ids
    ]
    if assets and not missing:
        return None
    detail = "、".join(missing) if missing else "参考视频抽帧失败或素材不可用"
    return f"参考素材整理后没有可用参考图，已停止生成：{detail}"


async def _unusable_reference_records(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    reason: str,
) -> list[ExecutionRecord]:
    """给「没有可用参考图」的执行单元造失败记录（带可读原因，不是静默 completed）。"""
    records: list[ExecutionRecord] = []
    for unit in _execution_units(task):
        try:
            provider, _generator = await _generator_for_task(
                run_id, unit, services
            )
        except Exception:  # noqa: BLE001 —— 已经在报错路径上，不能再抛
            provider = "unassigned"
        records.append(
            ExecutionRecord(
                task_id=unit.task_id,
                provider=provider,
                status="failed",
                error={
                    "category": ErrorCategory.VALIDATION.value,
                    "message": reason,
                    "retryable": False,
                    "code": "generation_unusable_reference_assets",
                },
            )
        )
    return records


def _zero_artifact_message(records: list[ExecutionRecord]) -> str:
    """本次执行零产物时写进 `last_error` 的人话原因。"""
    for record in records:
        message = (record.error or {}).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return "本次执行没有生成任何产物，请查看运行事件了解原因"


def _record_failure_event(record: ExecutionRecord) -> str | None:
    """把一次**非成功**的尝试落成一条可读事件（含上游原文）；成功返回 None。

    2026-10-10：用户连续两条 `invalid_media` 失败，运行事件里**没有任何字样** ——
    错误只写在 `execution_records`/`last_error` 里，事件流只剩旧的"超出时长上限"。
    排查时只能靠翻 checkpoint 二进制才找得到原文。任何失败都必须有一条事件，
    否则用户看到的就只是"又失败了"。
    """
    if record.status == "succeeded":
        return None
    error = record.error if isinstance(record.error, dict) else None
    message = str((error or {}).get("message") or "").strip()
    code = str((error or {}).get("code") or "").strip()
    if not message and not code:
        return None
    head = f"任务 {record.task_id} 生成失败"
    if code:
        head += f"（{code}）"
    if message:
        head += f"：{message}"
    return head[:600]


#: 供应商「拒绝这个视频引用」时错误文案里会出现的判据词。
#:
#: 只有拿得到**可判定的**错误才允许降级成抽帧（用户 2026-10-08：「不允许在任何正常
#: 路径上偷偷降级」）。方舟把上游 body 的 code/message 带回来了（见
#: `SeedanceVideoGenerator._http_error`），所以 `video_url` / `reference_video` 这类
#: 标识符能直接匹配到；本地校验的「参考素材类型与用途不匹配」也一并算。
_VIDEO_REJECTION_MARKERS = (
    "video_url",
    "reference_video",
    "video role",
    "role=video",
    "unsupported video",
    "invalid video",
    "video duration",
    "视频参考",
    "参考视频",
    "视频素材",
    "参考素材类型与用途不匹配",
    # 本机闸门的体积超限（`cause=input_too_large` / 「参考图片超过大小限制」）。
    # 2026-10-10：用户报 `参考素材无法用于生成（generation_input_too_large）` 就是
    # 它 —— 以前这里不认，于是修复梯子根本不启动、任务直接判死（参考视频 34.06MiB
    # 撞上本机 32MiB 闸门）。认了之后由梯子第一级（压缩）自动解决。
    "input_too_large",
    "超过大小限制",
    # 上游拿到素材后**解码/校验失败**（2026-10-10 用户第二条报错
    # `input media detect failed: invalid_media`，`code=poll_invalidparameter`）。
    # 实测根因是参考视频编码为 HEVC/H.265：上游只认 H.264
    # （见 `SeedanceVideoGenerator.reference_video_accepted_codecs` 的证据链）。
    # 认它之后梯子第一级会转码成 H.264 重传；不认它就等于原样再拒一次。
    "invalid_media",
    "input media detect failed",
    "media detect failed",
)

#: 「这次拒绝是**体积/编码**问题」的判据（只有压缩/转码能解决，裁短与抽帧都不行）。
_SIZE_REJECTION_MARKERS = (
    "input_too_large",
    "超过大小限制",
    "invalid_media",
    "media detect failed",
)


def _is_size_rejection(reason: str | None) -> bool:
    """这次拒绝是不是**单个素材**的体积/编码问题（只能靠重编码解决）。

    注意**总量**超限（`total_input_too_large` / 「总量超过大小限制」）不算：
    那个是"所有素材加起来太大"，裁短某个视频**确实**能减小总量，所以不在这里挡死，
    让它照旧走梯子下面的档位。（两者的判据词是包含关系，必须先把总量挑出来。）
    """
    if not isinstance(reason, str):
        return False
    lowered = reason.lower()
    if "total_input_too_large" in lowered or "总量超过大小限制" in reason:
        return False
    return any(marker in lowered for marker in _SIZE_REJECTION_MARKERS)


#: 降级/修复钩子：`(任务, 原因, 参考视频时长上限秒数, 参考视频体积上限字节数)
#: -> (改写后的任务, 新素材) | None`。
_DegradeHook = "Callable[[GenerationTask, str, float | None, int | None], Awaitable[tuple[GenerationTask, list[MediaAsset]] | None]]"


def _generator_accepts_video_references(generator: Any) -> bool:
    """当前供应商通道是否接受 `video_url + role=reference_video`。

    判据**只有一个来源**：生成器自己的 `supports_video_references`（方舟多参考为
    True，真人类通道声明 False）。拿不到就按 True 处理 —— 上游能力是既成事实
    （`integrations/seedance.py` 会把视频发成 `{"type":"video_url","role":"reference_video"}`），
    不该由应用侧再猜一次。
    """
    flag = getattr(generator, "supports_video_references", None)
    return flag if isinstance(flag, bool) else True


def _video_rejection_reason(
    exc: BaseException,
    task: GenerationTask,
    assets: list[MediaAsset],
) -> str | None:
    """上游是不是**明确拒绝了这个参考视频**（是则返回可读原因，否则 None）。"""
    if not any(
        str(asset.mime_type).startswith("video/") for asset in assets
    ):
        return None
    detail = getattr(exc, "detail", None)
    category = getattr(detail, "category", None)
    if category not in {
        ErrorCategory.VALIDATION,
        ErrorCategory.DOCUMENT,
        ErrorCategory.PROVIDER_TERMINAL,
    }:
        return None
    text = " ".join(
        str(part)
        for part in (
            getattr(detail, "message", ""),
            getattr(detail, "technical_detail", ""),
        )
    )
    lowered = text.lower()
    if not any(marker.lower() in lowered for marker in _VIDEO_REJECTION_MARKERS):
        return None
    first_line = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    return first_line[:200]


async def _execute_one_task(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    assets: list[MediaAsset],
    *,
    degrade: Any = None,
    preflight: Any = None,
) -> tuple[ExecutionRecord, list[Artifact]]:
    task_fingerprint = _task_fingerprint(task)
    provider, generator = await _generator_for_task(run_id, task, services)
    if preflight is not None and _generator_accepts_video_references(generator):
        # **主动预检**（2026-10-10 新增）：参考视频的**体积 / 编码**超过这条通道
        # 实际能收的范围时，**在提交之前**修好。
        # 为什么不能只靠"被拒后修复"：体积闸门是我们自己的（本地就能判定），而编码
        # 问题（HEVC）上游会先**接受**提交、再在 poll 阶段失败（白等 40 秒 + 用户
        # 看到一条失败尝试）。两种都能在本地确定，就不该浪费一次注定失败的提交。
        # 这里只做压缩/转码 —— **绝不动时长、绝不抽帧**（那是后面两级的职责，只有
        # 上游明确拒绝时才会走到）。
        repaired = await preflight(
            task,
            getattr(generator, "reference_video_max_bytes", None),
            getattr(generator, "reference_video_accepted_codecs", None),
        )
        if repaired is not None:
            task, assets = repaired
            task_fingerprint = _task_fingerprint(task)
    if degrade is not None and not _generator_accepts_video_references(generator):
        # 通道**确定性地**不支持视频引用（真人类通道只收 asset:// 图片）→ 这是可判定
        # 的「上游不接受」，走同一条修复路径（写事件 + 换抽帧），而不是让适配层
        # 偷偷把引用摘掉（旧 `_strip_non_image_references` 就是这么干的，与上游能力
        # 冲突且不留痕迹）。
        degraded = await degrade(
            task,
            "当前生成通道不支持参考视频（只接受图片素材）",
            getattr(generator, "reference_video_max_seconds", None),
            getattr(generator, "reference_video_max_bytes", None),
        )
        if degraded is not None:
            task, assets = degraded
            task_fingerprint = _task_fingerprint(task)
            degrade = None
    existing_artifacts = await _existing_valid_artifacts(
        services, run_id, task
    )
    operation = await services.repository.get_operation(
        run_id, task.task_id, "submit"
    )
    if operation is not None and (
        operation.get("provider") != provider
        or operation.get("task_fingerprint") != task_fingerprint
    ):
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=operation.get("official_id"),
                status="submission_uncertain",
                error={"message": "已保存任务身份与当前审批任务不一致"},
            ),
            [],
        )
    if existing_artifacts is not None:
        if operation is None:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
            existing_artifacts = None
        elif operation["phase"] not in {"submitted", "succeeded"}:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=operation.get("official_id"),
                status=operation["phase"],
            ), []
    if existing_artifacts is not None:
        official_id = operation.get("official_id") if operation else None
        if operation is not None and operation["phase"] != "succeeded":
            if not isinstance(official_id, str):
                return ExecutionRecord(
                    task_id=task.task_id, provider=provider,
                    status="submission_uncertain",
                ), []
            return await _finish_submit_phase(
                services, run_id, task, provider, operation, "succeeded",
                official_id, existing_artifacts,
            )
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=official_id,
                status="succeeded",
            ),
            existing_artifacts,
        )

    owns_submit = False
    immediate: ProviderSubmission | None = None
    if operation is None:
        created, operation = (
            await services.repository.create_submission_intent_if_absent(
                run_id, task.task_id, provider, uuid4().hex, task_fingerprint
            )
        )
        owns_submit = created

    phase = operation["phase"]
    if phase == "intent_created" and not owns_submit:
        if _intent_is_stale(
            operation, services.settings.submission_intent_lease_seconds
        ):
            client_id = operation.get("client_submission_id")
            if isinstance(client_id, str):
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=services.settings.submission_intent_lease_seconds
                )
                await services.repository.expire_submission_intent_lease(
                    run_id, task.task_id, client_id, provider,
                    task_fingerprint, cutoff.isoformat(),
                )
            latest = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            phase = (latest or {}).get("phase", "submission_uncertain")
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                status=phase,
            ),
            [],
        )
    if phase in _TERMINAL_PROVIDER_PHASES:
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=operation.get("official_id"),
                status=phase,
            ),
            [],
        )

    if phase == "succeeded":
        return await _repair_succeeded_submission(
            services, run_id, task, provider, generator, operation
        )

    if owns_submit:
        client_id = operation["client_submission_id"]
        await services.repository.renew_submission_intent_lease(
            run_id, task.task_id, client_id, provider, task_fingerprint
        )
        heartbeat = asyncio.create_task(
            _keep_submission_intent_alive(
                services, run_id, task, provider, client_id, task_fingerprint
            )
        )
        try:
            try:
                immediate = await generator.submit(
                    task, assets, submission_id=client_id
                )
            except AgentError as exc:
                rejection_reason = (
                    _video_rejection_reason(exc, task, assets)
                    if degrade is not None
                    else None
                )
                if rejection_reason is not None:
                    # 上游**明确拒绝**参考视频 → 这是唯一允许修复的时机（先压缩、
                    # 再裁短、最后抽帧）。必须在事件里留下痕迹（用户在任务详情里能看到
                    # 「已压缩后重传」/「已裁剪到 Ns 后重传」/「已降级为抽帧画面」），
                    # 否则就是从「效果差」变成「没头没尾地换了一种做法」。
                    degraded = await degrade(
                        task,
                        rejection_reason,
                        getattr(generator, "reference_video_max_seconds", None),
                        getattr(generator, "reference_video_max_bytes", None),
                    )
                    if degraded is None:
                        raise
                    degraded_task, degraded_assets = degraded
                    await services.repository.delete_task_operations(
                        run_id, task.task_id
                    )
                    # 把钩子继续传下去：修复梯子要能升级（压缩 → 裁短 → 抽帧）；
                    # `repair_stages` 保证每一级最多走一次，不会无限重试。
                    return await _execute_one_task(
                        services,
                        run_id,
                        degraded_task,
                        degraded_assets,
                        degrade=degrade,
                        preflight=preflight,
                    )
                if not _should_switch_to_portrait(exc, provider, services):
                    raise
                # 用户 2026-09-18：Seedance 因「输入图疑似真人」拒绝时**自动改走
                # 真人类通道**（真人素材在 Seedance 侧是合规红线，换模型没用）。
                # 清掉按旧 provider 建的提交意图，再把任务改写成真人类重跑一次。
                await services.repository.delete_task_operations(
                    run_id, task.task_id
                )
                await services.repository.append_event(
                    run_id,
                    "execute_selected_tasks",
                    "running",
                    "Seedance 拒绝真人素材，已自动改走真人类通道",
                )
                _LOGGER.warning(
                    "Seedance 拒绝真人素材，自动改走真人类通道 run=%s task=%s",
                    run_id,
                    task.task_id,
                )
                return await _execute_one_task(
                    services,
                    run_id,
                    task.model_copy(
                        update={"video_provider": "volcengine_portrait"}
                    ),
                    assets,
                    degrade=degrade,
                )
            official_id = immediate.provider_task_id
            if immediate.provider != provider:
                raise _provider_terminal_error("供应商任务身份不一致")
            if provider in _SYNC_IMAGE_PROVIDERS and official_id != client_id:
                raise _provider_terminal_error("供应商任务标识不一致")
            transitioned = await _transition_operation(
                services,
                run_id,
                task.task_id,
                operation,
                "submitted",
                official_id,
            )
            if not transitioned:
                latest = await services.repository.get_operation(
                    run_id, task.task_id, "submit"
                )
                if (
                    latest is None
                    or latest["phase"] != "submitted"
                    or latest.get("client_submission_id") != client_id
                    or latest.get("official_id") != official_id
                    or latest.get("provider") != provider
                    or latest.get("task_fingerprint") != task_fingerprint
                ):
                    raise _provider_terminal_error("提交状态无法安全落库")
            operation = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            if operation is None or operation["phase"] != "submitted":
                raise _provider_terminal_error("提交状态无法安全落库")
        except Exception as exc:
            latest = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            local_preflight_failure = (
                isinstance(exc, AgentError)
                and exc.detail.category
                in {ErrorCategory.VALIDATION, ErrorCategory.DOCUMENT}
            )
            if latest is not None and latest["phase"] == "intent_created":
                await _transition_operation(
                    services,
                    run_id,
                    task.task_id,
                    latest,
                    "failed" if local_preflight_failure else "submission_uncertain",
                    None,
                )
                latest = await services.repository.get_operation(
                    run_id, task.task_id, "submit"
                )
            if latest is not None and latest["phase"] == "succeeded":
                return await _repair_succeeded_submission(
                    services, run_id, task, provider, generator, latest
                )
            return (
                ExecutionRecord(
                    task_id=task.task_id,
                    provider=provider,
                    provider_task_id=(latest or {}).get("official_id"),
                    status=(latest or {}).get("phase", "submission_uncertain"),
                    error=_execution_error(exc),
                ),
                [],
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    official_id = operation.get("official_id")
    if not isinstance(official_id, str) or not official_id:
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                status="submission_uncertain",
            ),
            [],
        )
    submission = immediate
    try:
        if submission is None or submission.status.lower() in _PENDING_PROVIDER_STATUSES:
            submission = await _poll_submission(
                generator,
                ProviderSubmission(
                    provider=provider,
                    provider_task_id=official_id,
                    status="submitted",
                ),
                services,
                run_id,
                task.task_id,
            )
        if submission is None:
            return await _finish_submit_phase(
                services, run_id, task, provider, operation,
                "timed_out", official_id,
            )
        status = submission.status.lower()
        if status not in _SUCCESS_PROVIDER_STATUSES:
            phase = status if status in {"cancelled", "expired"} else "failed"
            return await _finish_submit_phase(
                services, run_id, task, provider, operation, phase,
                official_id,
                error=_execution_error(
                    _provider_terminal_error("供应商生成任务失败")
                ),
            )
        artifacts = await _materialize_submission(
            services, run_id, task, submission
        )
        latest = await services.repository.get_operation(
            run_id, task.task_id, "submit"
        )
        if latest is None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="submission_uncertain",
            ), []
        return await _finish_submit_phase(
            services, run_id, task, provider, latest, "succeeded",
            official_id, artifacts,
        )
    except Exception as exc:
        latest = await services.repository.get_operation(
            run_id, task.task_id, "submit"
        )
        chiyun_staging_invalid = (
            provider in _SYNC_IMAGE_PROVIDERS
            and isinstance(exc, AgentError)
            and exc.detail.category is ErrorCategory.PROVIDER_TERMINAL
            and (
                "operation=materialize" in exc.detail.technical_detail
                or "operation=poll; cause=staging_invalid"
                in exc.detail.technical_detail
            )
        )
        failure_phase = (
            "submission_uncertain" if chiyun_staging_invalid else "failed"
        )
        if latest is not None and latest["phase"] == "submitted":
            return await _finish_submit_phase(
                services, run_id, task, provider, latest, failure_phase,
                official_id, error=_execution_error(exc),
            )
        if latest is not None and latest["phase"] == "succeeded":
            return await _repair_succeeded_submission(
                services, run_id, task, provider, generator, latest
            )
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=official_id,
                status=(latest or {}).get("phase", failure_phase),
                error=_execution_error(exc),
            ),
            [],
        )


async def execute_selected_tasks(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise _validation_error()
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        if issues:
            raise _validation_error(
                language_validation_message(issues)
                or "The approved plan is not valid"
            )
        records: list[ExecutionRecord] = []
        artifacts: list[Artifact] = []
        storyboard_assets: list[MediaAsset] = []
        for item in state.get("storyboard_assets", []):
            try:
                storyboard_assets.append(MediaAsset.model_validate(item))
            except Exception:  # noqa: BLE001 —— 坏素材不该挡住执行
                continue
        #: 参考视频抽帧结果按视频缓存：多条任务引用同一个视频时只抽一次。
        frame_cache: dict[str, Any] = {}
        #: 参考视频裁剪结果按视频缓存（同一条视频只裁一次）。
        trim_cache: dict[str, Any] = {}
        #: 参考视频压缩结果按视频缓存（同一条视频只压一次）。
        compress_cache: dict[str, Any] = {}
        #: 每条任务已经走到修复梯子的第几级（防止无限重试）。
        repair_stages: dict[str, int] = {}
        #: 修复梯子各级**新造出来的素材**（压缩 `-small` / 裁剪 `-trim` 结果），
        #: 按任务累积。**必须累积**：下一级要靠它才找得到上一级的产物 —— 否则
        #: 「压缩 → 裁短 → 抽帧」的升级会在第二级看不到 `-small`、误判"没有视频引用"
        #: 而直接放弃（这是本函数一处老缺口，压缩级上线时才暴露出来）。
        repaired_assets: dict[str, list[MediaAsset]] = {}

        def _ladder_pool(task_id: str) -> list[MediaAsset]:
            return storyboard_assets + repaired_assets.get(task_id, [])

        for task in plan.tasks:
            had_references = bool(task.reference_images)
            # 2026-10-08 P0：**默认把参考视频原样直传上游**（role=reference_video）。
            # 这里只做「抽帧别名归回原视频 + role 规范 + 去重」，不抽帧、不摘引用。
            # 抽帧降级只在上游明确拒绝时发生（下面的 `degrade` 钩子）。
            task = _normalize_video_references(
                task, document, storyboard_assets
            )
            assets = await _without_unusable_references(
                _task_assets(task, document, storyboard_assets),
                run_id,
                services,
            )

            async def _compress_step(
                rejected: GenerationTask,
                reason: str,
                max_bytes: int | None,
                accepted_codecs: Any,
            ) -> tuple[GenerationTask, list[MediaAsset]] | None:
                """梯子第一级的共用实现（主动预检与被动修复走同一条路）。"""
                compressed = await _compress_reference_videos(
                    rejected,
                    document,
                    _ladder_pool(rejected.task_id),
                    run_id,
                    services,
                    compress_cache,
                    max_bytes,
                    reason=reason,
                    accepted_codecs=accepted_codecs,
                )
                if compressed is None:
                    return None
                repaired_task, created = compressed
                repaired_assets.setdefault(rejected.task_id, []).extend(created)
                return repaired_task, _task_assets(
                    repaired_task, document, _ladder_pool(rejected.task_id)
                )

            async def preflight(
                current: GenerationTask,
                max_bytes: int | None = None,
                accepted_codecs: Any = None,
            ) -> tuple[GenerationTask, list[MediaAsset]] | None:
                """**提交前**把体积/编码不合格的参考视频修好（只压缩/转码）。"""
                return await _compress_step(
                    current, "提交前预检", max_bytes, accepted_codecs
                )

            async def degrade(
                rejected: GenerationTask,
                reason: str,
                max_seconds: float | None = None,
                max_bytes: int | None = None,
                accepted_codecs: Any = None,
                *,
                _storyboard: list[MediaAsset] = storyboard_assets,
            ) -> tuple[GenerationTask, list[MediaAsset]] | None:
                """上游/本机闸门拒绝参考视频时的**修复梯子**（唯一入口，每一步都留事件）。

                顺序（每一级都是「条件不成立就一个字都不动」）：

                1. **压缩/转码**（`_compress_reference_videos`）：只降码率/必要时降分辨率、
                   把编码归一成 H.264，**完整保留时长与帧率** —— 运动参考一点不丢；
                2. **裁短**时长（`_trim_reference_videos`）：丢时间长度；
                3. **抽帧**（`_video_references_to_frames`）：最后兜底。

                每一级都失败 → 返回 None，让原始错误如实浮出来（不许静默空跑）。
                """
                stage = repair_stages.get(rejected.task_id, 0)
                pool = _ladder_pool(rejected.task_id)
                if stage == 0 and (max_bytes or accepted_codecs):
                    repaired = await _compress_step(
                        rejected, reason, max_bytes, accepted_codecs
                    )
                    if repaired is not None:
                        repair_stages[rejected.task_id] = 1
                        return repaired
                if _is_size_rejection(reason):
                    # 体积/编码问题只能靠压缩/转码解决：裁短只会让运动参考更短、
                    # 抽帧直接丢掉运动，两者都不是"把素材变成上游能收的样子"的正确
                    # 手段。压缩不动就如实失败。
                    # 2026-10-10 活体验证时这里排在了裁短**后面**，结果是：一个素材
                    # 体积超限，梯子却去把**另一个** 65.5s 的视频裁到 30s（白丢 35 秒
                    # 时长），再提交一次注定失败的请求。顺序必须是这样。
                    return None
                if stage <= 1 and max_seconds:
                    trimmed = await _trim_reference_videos(
                        rejected,
                        document,
                        pool,
                        run_id,
                        services,
                        trim_cache,
                        max_seconds,
                        reason=reason,
                    )
                    if trimmed is not None:
                        repair_stages[rejected.task_id] = 2
                        repaired_task, created = trimmed
                        repaired_assets.setdefault(
                            rejected.task_id, []
                        ).extend(created)
                        return repaired_task, _task_assets(
                            repaired_task, document, _ladder_pool(rejected.task_id)
                        )
                if _is_size_rejection(reason):
                    # 体积问题只能靠压缩解决：裁短只会让运动参考更短、抽帧直接丢掉运动，
                    # 两者都不是"把文件压小"的正确手段。压缩不动就如实失败。
                    return None
                rewritten, created = await _video_references_to_frames(
                    rejected,
                    document,
                    pool,
                    run_id,
                    services,
                    frame_cache,
                    reason=reason,
                )
                repair_stages[rejected.task_id] = max(stage, 2) + 1
                if rewritten.reference_images == rejected.reference_images:
                    return None
                if not rewritten.reference_images:
                    # 全摘光了：不许静默变成纯文生视频，让原始错误如实浮出来。
                    return None
                repaired_assets.setdefault(rejected.task_id, []).extend(created)
                return rewritten, _task_assets(
                    rewritten, document, _ladder_pool(rejected.task_id)
                )

            unusable_reason = _unusable_reference_message(
                task, assets, had_references=had_references
            )
            if unusable_reason is not None:
                # 不许静默空跑：节点全 completed + 零产物 + last_error: null 是
                # 用户最看不懂的状态（2026-09-24 实测）。这里明确失败并留下原因。
                await services.repository.append_event(
                    run_id,
                    "execute_selected_tasks",
                    "running",
                    unusable_reason,
                )
                _LOGGER.warning(
                    "参考素材整理后没有可用参考图，停止生成 run=%s task=%s reason=%s",
                    run_id,
                    task.task_id,
                    unusable_reason,
                )
                records.extend(
                    await _unusable_reference_records(
                        services, run_id, task, unusable_reason
                    )
                )
                continue
            unit_results = await asyncio.gather(
                *(
                    _execute_one_task(
                        services,
                        run_id,
                        unit,
                        assets,
                        degrade=degrade,
                        preflight=preflight,
                    )
                    for unit in _execution_units(task)
                )
            )
            for record, task_artifacts in unit_results:
                records.append(record)
                artifacts.extend(task_artifacts)
                failure = _record_failure_event(record)
                if failure is not None:
                    # 2026-10-10：用户连撞两条 `invalid_media`，运行**事件里一个字都没有**
                    # （错误只躺在 execution_records / last_error 里）—— 用户和我都只能猜。
                    # 任何非成功的尝试都必须有一条带原文的可见事件。
                    await services.repository.append_event(
                        run_id,
                        "execute_selected_tasks",
                        "running",
                        failure,
                    )
        all_succeeded = all(record.status == "succeeded" for record in records)
        status = (
            "verification_pending"
            if all_succeeded
            else "completed_with_errors"
            if artifacts
            else "failed"
        )
        update: dict[str, Any] = {
            "execution_records": [_json_model(record) for record in records],
            "artifacts": [_json_model(artifact) for artifact in artifacts],
            "status": status,
        }
        if status == "failed":
            # 零产物必须留下可读原因，否则用户只能看到「生成失败」四个字。
            update["last_error"] = _json_model(
                _validation_error(_zero_artifact_message(records)).detail
            )
        return update

    return await _run_node(
        state, "execute_selected_tasks", services, operation
    )


async def verify_and_download_artifacts(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise _validation_error()
        artifacts = [
            Artifact.model_validate(item) for item in state.get("artifacts", [])
        ]
        records = [
            ExecutionRecord.model_validate(item)
            for item in state.get("execution_records", [])
        ]
        plan_tasks = TaskPlan(
            tasks=state.get("approved_tasks", []), document_summary=""
        ).tasks
        tasks = {
            unit.task_id: unit
            for task in plan_tasks
            for unit in _execution_units(task)
        }
        record_ids = [record.task_id for record in records]
        if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(tasks):
            raise _provider_terminal_error("生成产物记录不一致")
        successful = {record.task_id: record for record in records
                      if record.status == "succeeded"}
        if any(artifact.task_id not in successful for artifact in artifacts):
            raise _provider_terminal_error("生成产物记录不一致")
        verified: list[Artifact] = []
        for task_id, record in successful.items():
            task = tasks.get(task_id)
            if task is None:
                raise _provider_terminal_error("生成产物记录不一致")
            state_items = sorted(
                (item for item in artifacts if item.task_id == task_id),
                key=lambda item: item.artifact_id,
            )
            repository_items = sorted(
                await services.repository.list_artifacts(run_id, task_id=task_id),
                key=lambda item: item.artifact_id,
            )
            if (
                len(state_items) != task.output_count
                or [item.model_dump(mode="json") for item in state_items]
                != [item.model_dump(mode="json") for item in repository_items]
                or any(item.provider_task_id != record.provider_task_id
                       for item in state_items)
                or not all(services.file_store.verify_artifact(run_id, item)
                           for item in state_items)
            ):
                raise _provider_terminal_error("生成产物记录或文件校验失败")
            verified.extend(state_items)
        return {
            "artifacts": [_json_model(artifact) for artifact in verified],
            # Verified artifacts mean generation succeeded. Feishu result-table
            # export is optional and must not gate the success status.
            "status": "succeeded" if verified else "failed",
        }

    return await _run_node(
        state, "verify_and_download_artifacts", services, operation
    )

async def _rework_prompt_for_task(
    services: GraphServices,
    task: GenerationTask,
    feedback: str,
    visual_context: str = "",
    media_parts: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], str, bool, list[str]]:
    """算出这条任务重跑后的提示词、累积要求与负向约束。

    走 `integrations.rework_prompt` 的共享实现，与多维表格重跑
    （`GraphRuntime.clone_run_for_approval`）保持完全一致的语义：
    要求只累积不覆盖、优先 AI 融合、永不因超长失败。

    `media_parts` 是上一版成片的视频 part（一次多模态调用，用户 2026-09-28）。

    **负向约束不再往里加东西**（用户口径 2026-09-17：「不要在约束里加东西，直接描述
    在正文就行」）：融合已经把这次的要求写进正文了，再把 `must_avoid` 并进
    `negative_constraints` 只会让它一轮一轮堆下去 —— 而否定句堆太多会把"不要出现的
    东西"反复喂给模型、反而加深印象。这里只对**存量**做一次合并 + 限量清理。
    """
    base_prompt, requirements = rework_inputs(task, feedback)
    prompt, truncated, _must_avoid = await build_rework_prompt(
        base_prompt,
        # 只喂**本轮**要求：历史不再重喂（`requirements` 仍回给调用方做累积记录）。
        # 见 `rework_prompt.fusion_requirements` 的实测说明（互斥的旧要求会被一起
        # 写进正文、还会把上一轮写对的约束删掉）。
        fusion_requirements(feedback),
        fuse=getattr(services.planner, "fuse_rework_prompt", None),
        visual_context=visual_context,
        media_parts=media_parts,
    )
    constraints = merge_negative_constraints(task.negative_constraints, [])
    return base_prompt, requirements, prompt, truncated, constraints


async def _record_visual_context(
    services: GraphServices,
    run_id: str,
    visual_context: str,
    *,
    note: str = "",
) -> None:
    """把「模型看过的上一版画面」记进运行事件。

    用户 2026-09-18：「融合返工要求没有上传前一次的生成视频吗，这样看不出问题啊」——
    看片其实一直在做，但过程完全不可见。现在把"看了什么"写进事件，用户能直接核对。

    `note` 覆盖默认文案（一次多模态调用时没有文字画面可写）。
    """
    try:
        text = " ".join((visual_context or "").split())
        message = note or (
            f"已让模型看过上一版成片，融合时带上画面：{text[:600]}"
            if text
            else "本次返工没带上一版画面（没有成片文件，或看片失败）"
        )
        await services.repository.append_event(
            run_id,
            "review_artifacts",
            "running",
            message,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("记录画面上下文事件失败 run=%s", run_id, exc_info=True)


def _artifact_review_payload(state: AgentState) -> dict[str, Any]:
    payload = {
        "action": "review_artifacts",
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "status": "waiting_review",
        "artifacts": state.get("artifacts", []),
    }
    json.dumps(payload, ensure_ascii=False)
    return payload


def _parse_artifact_review(value: Any) -> ArtifactReviewDecision:
    if not isinstance(value, dict):
        raise _validation_error("成片确认请求格式无效：需要 JSON 对象")
    allowed_keys = {"action", "feedback", "task_ids"}
    extra_keys = set(value) - allowed_keys
    if extra_keys:
        raise _validation_error(
            "成片确认请求包含未知字段："
            + "、".join(sorted(str(key) for key in extra_keys))
        )
    try:
        decision = ArtifactReviewDecision.model_validate(value)
    except ValidationError as exc:
        compact = "; ".join(
            ".".join(str(part) for part in item["loc"]) + ": " + str(item["msg"])
            for item in exc.errors(include_url=False, include_input=False)[:6]
        )
        raise _validation_error(f"成片确认载荷无效：{compact}") from None

    if len(decision.task_ids) != len(set(decision.task_ids)):
        raise _validation_error("重跑任务不能重复选择")
    if decision.action == "adjust":
        if not isinstance(decision.feedback, str) or not decision.feedback.strip():
            raise _validation_error("退回调整时必须填写调整意见")
    elif decision.feedback is not None or decision.task_ids:
        raise _validation_error("确认或取消时不能携带调整意见或任务")
    return decision


async def review_artifacts(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> Command:
    """Review generated artifacts and optionally rerun selected tasks."""
    _ensure_thread_id(state, config)
    if not services.settings.artifact_review_enabled:
        return Command(update={}, goto=END)

    resume_value = interrupt(_artifact_review_payload(state))

    async def operation() -> Command:
        decision = _parse_artifact_review(resume_value)
        decision_json = _json_model(decision)
        if decision.action == "confirm":
            return Command(
                update={
                    "artifact_review_decision": decision_json,
                    "artifact_review_feedback": None,
                    "status": "succeeded",
                },
                goto="deliver_to_feishu",
            )
        if decision.action == "adjust":
            run_id = state.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise _validation_error()
            plan = approved_plan_from_state(
                state,
                max_output_count=services.settings.max_output_count,
            )
            artifacts = [
                Artifact.model_validate(item)
                for item in state.get("artifacts", [])
            ]
            records = [
                ExecutionRecord.model_validate(item)
                for item in state.get("execution_records", [])
            ]
            available_task_ids = {artifact.task_id for artifact in artifacts}
            requested_ids = set(decision.task_ids) or set(available_task_ids)

            target_task_ids = {base_task_id(task_id) for task_id in requested_ids}
            known_task_ids = {task.task_id for task in plan.tasks}
            unknown_ids = target_task_ids - known_task_ids
            if unknown_ids:
                raise _validation_error(
                    "重跑任务不存在："
                    + "、".join(sorted(unknown_ids))
                )
            if not target_task_ids:
                raise _validation_error("请至少选择一条需要重跑的任务")

            # 用户是**按每条成片勾选**的（artifact 级 id，形如 task-1::output:2）。
            # 以前这里按 base task id 展开成「整个任务的全部产出」，于是勾 1 条会
            # 把同任务另外 2 条一起重跑（2026-09-23 复现：只勾 1 条，submit 3 次）。
            known_unit_ids = {
                unit.task_id
                for task in plan.tasks
                for unit in _execution_units(task)
            }
            target_unit_ids = {
                unit_id for unit_id in requested_ids if unit_id in known_unit_ids
            }
            if not target_unit_ids:
                # 兼容只传 base task id 的旧前端 / 旧决策记录。
                target_unit_ids = {
                    unit.task_id
                    for task in plan.tasks
                    if task.task_id in target_task_ids
                    for unit in _execution_units(task)
                }

            feedback = decision.feedback.strip()
            updated_tasks: list[GenerationTask] = []
            # 上一版成片**直接送能看视频的模型**（不抽帧）：让融合模型"看到"实际
            # 画成了什么，而不是只靠用户打的字（用户 2026-09-17 要求）。
            # 注意：本函数后半段还有一个 `artifacts`（**模型对象**列表，见
            # `remaining_artifacts`），这里是 state 里的**字典**产物 —— 名字必须分开，
            # 否则会把后面那个覆盖掉（2026-09-28 实测：AttributeError 'dict' object
            # has no attribute 'task_id'）。
            take_artifacts = list(state.get("artifacts") or [])
            # 一次多模态调用优先（用户 2026-09-28）：成片视频直接附进融合请求，
            # 模型自己看片 —— 不再先跑一遍文本看片再让融合去读转述。
            media_parts = await build_take_media_parts(
                take_artifacts,
                public_media_host=getattr(services, "public_media_host", None),
                cache=MediaUploadCache(
                    Path(services.settings.data_dir) / "take-media-links.json"
                ),
            )
            if media_parts:
                visual_context = ""
                await _record_visual_context(
                    services,
                    state.get("run_id", "unknown-run"),
                    "",
                    note="已把上一版成片作为视频附进融合请求（一次多模态调用）",
                )
            else:
                # 回落：成片传不上去时仍走文字看片（并带上上一版正文）。
                visual_context = await describe_output_videos(
                    getattr(services, "video_analyzer", None),
                    take_artifacts,
                    # 2026-09-28：看片要拿上一版正文才能逐条核对「要求 X，实际 Y」。
                    prompts={task.task_id: task.prompt for task in plan.tasks},
                )
                await _record_visual_context(
                    services, state.get("run_id", "unknown-run"), visual_context
                )
            for task in plan.tasks:
                if task.task_id not in target_task_ids:
                    updated_tasks.append(task)
                    continue
                # 老 run 的返工历史写在提示词正文里，先接住再并入累积清单，
                # 否则升级后历史要求会凭空丢失。
                (
                    base_prompt,
                    requirements,
                    prompt,
                    truncated,
                    constraints,
                ) = await _rework_prompt_for_task(
                    services,
                    task,
                    feedback,
                    visual_context,
                    media_parts=media_parts,
                )
                if requirements and prompt.strip() == (base_prompt or "").strip():
                    # 融合没生效（被限流 / 上游报错 / 模型判定无需改动）—— 以前静默，
                    # 用户只看到"提示词一个字没变"却不知道为什么（2026-09-18：
                    # 「为什么我要求他给出动作指导，他还是没有动我的提示词」）。
                    try:
                        await services.repository.append_event(
                            state.get("run_id", "unknown-run"),
                            "review_artifacts",
                            "running",
                            "本次返工没有改动提示词正文（融合未生效：多半是模型限流或"
                            "上游报错）—— 要求已记录，稍后重跑即可带上；也可以直接在"
                            "审批页手动改。",
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning("记录返工未生效事件失败", exc_info=True)
                task_updates: dict[str, Any] = {
                    "prompt": prompt,
                    "rework_requirements": requirements,
                    # 冻结锚点：只写第一次，往后每轮都不能覆盖。注意它与
                    # rework_previous_prompt 分工不同——后者是融合基准/改前。
                    "rework_base_prompt": task.rework_base_prompt or base_prompt,
                    # 展示用的「改前」是这一版被返工前的样子（不是冻结的第一版）。
                    "rework_previous_prompt": task.prompt,
                    "negative_constraints": constraints,
                }
                if truncated:
                    task_updates["warnings"] = [
                        *task.warnings,
                        "返工要求过多，提示词已截断；历史要求仍完整保留",
                    ]
                updated_tasks.extend(
                    _split_partially_selected_outputs(
                        task.model_copy(update=task_updates),
                        task,
                        target_unit_ids,
                    )
                )

            for unit_id in sorted(target_unit_ids):
                await services.repository.delete_task_operations(run_id, unit_id)
                await services.repository.delete_task_artifacts(run_id, unit_id)

            updated_plan = plan.model_copy(update={"tasks": updated_tasks})
            remaining_artifacts = [
                artifact
                for artifact in artifacts
                if artifact.task_id not in target_unit_ids
            ]
            remaining_records = [
                record for record in records if record.task_id not in target_unit_ids
            ]
            await services.repository.append_event(
                run_id,
                "review_artifacts",
                "running",
                "Selected task feedback queued for regeneration",
            )
            return Command(
                update={
                    "artifact_review_decision": decision_json,
                    "artifact_review_feedback": feedback,
                    "approved_tasks": [
                        _json_model(task) for task in updated_plan.tasks
                    ],
                    "approved_plan": _json_model(updated_plan),
                    "execution_records": [
                        _json_model(record) for record in remaining_records
                    ],
                    "artifacts": [
                        _json_model(artifact) for artifact in remaining_artifacts
                    ],
                    "delivery_record": None,
                    "last_error": None,
                    "status": "running",
                },
                goto="execute_selected_tasks",
            )
        return Command(
            update={
                "artifact_review_decision": decision_json,
                "artifact_review_feedback": None,
                "status": "cancelled",
            },
            goto=END,
        )

    return await _run_node(state, "review_artifacts", services, operation)


async def deliver_to_feishu(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    """Export only when this node is reached by an explicit user action.

    This node is deliberately disconnected from the automatic generation
    path. Verified artifacts already mean the task succeeded; export failure
    is diagnostic and never downgrades that success.
    """
    _ensure_thread_id(state, config)
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _validation_error()
    summary = _NODE_SUMMARIES["deliver_to_feishu"]
    artifacts = [
        Artifact.model_validate(item) for item in state.get("artifacts", [])
    ]
    try:
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        if services.delivery_writer is None:
            raise RuntimeError("飞书结果表交付服务未配置")
        await services.repository.append_event(
            run_id, "deliver_to_feishu", "started", f"{summary} started"
        )
        record = await services.delivery_writer.deliver(
            run_id, document, plan, artifacts
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure = _safe_error(exc)
        await services.repository.append_event(
            run_id,
            "deliver_to_feishu",
            "failed",
            f"{summary} failed ({failure.detail.category.value})",
        )
        return {
            "status": "succeeded" if artifacts else "failed",
            "delivery_record": None,
            "last_error": _json_model(failure.detail),
        }
    await services.repository.append_event(
        run_id, "deliver_to_feishu", "completed", f"{summary} completed"
    )
    return {
        "delivery_record": _json_model(record),
        "status": "succeeded" if artifacts else "failed",
        "last_error": None,
    }
