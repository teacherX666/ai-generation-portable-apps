import asyncio
from contextlib import asynccontextmanager, nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
from inspect import Parameter, signature
import json
import logging
import mimetypes
import os
from pathlib import Path
from time import monotonic
import time
from typing import Annotated, Any, AsyncIterator, Literal
from urllib.parse import unquote_to_bytes, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.bootstrap import (
    open_application_services,
    runtime_is_configured,
)
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.asset_library import AssetKind
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.document import (
    PlanningPromptSnapshot,
    RequirementRequest,
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.domain.video_models import (
    VIDEO_MODELS,
    resolve_video_model_key,
)
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.integrations.bitable_delivery import (
    BitableResultConflict,
)
from feishu_generation_agent.integrations.feishu_bitable import BitableSchemaError
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.integrations.watermark_removal import (
    output_path_for,
    remove_video_watermark,
)
from feishu_generation_agent.storage.bitable_tasks import TaskAlreadyClaimed
from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    DuplicateAssetError,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskAlreadyClaimed
from feishu_generation_agent.storage.checkpoints import open_checkpointer
from feishu_generation_agent.storage.planner_prompts import PlannerPromptStore
from feishu_generation_agent.storage.provider_preferences import (
    ProviderPreferences,
    ProviderPreferenceStore,
)
from feishu_generation_agent.web.schemas import (
    AssetLibraryItem,
    AssetLibraryListResponse,
    AssetLibraryUpdateRequest,
    ArtifactReviewRequest,
    BitableClaimResponse,
    BitableRerunRequest,
    BitableRetryResponse,
    CreateRunRequest,
    DecisionRequest,
    PlannerPromptResponse,
    ExcludedAssetRequest,
    OptimizePromptsRequest,
    PlannerPromptUpdate,
    ProviderPreferencesResponse,
    ProviderPreferencesUpdate,
    ReferenceListRequest,
    TaskPatchRequest,
    WatermarkDecisionRequest,
)
from feishu_generation_agent.integrations.video_insight import (
    analyze_artifacts,
)

ProductionCategory = Literal["animation", "portrait", "image"]
_MAX_IDENTITY_LENGTH = 255
_LOGGER = logging.getLogger(__name__)
#: 审片结果缓存：`(run_id, 产物签名) -> findings`。同一条成片只让模型看一次
#: （每次审片都是一次真实模型调用）。超量直接清空，不做 LRU。
_take_findings_cache: dict[tuple[str, str], dict] = {}

#: 「任务记录」**所有任务共用**的条数额度：最新的 10 条留在列表里，更老的整条进回收站。
#:
#: 2026-10-30 用户口径：「两者应该共用十条的次数限制，或者说以后所有任务都共用十条
#: 次数限制」。以前是各算各的（多维表格 10 条记录 + 直连 10 条任务 = 最多 20 条），
#: 用户看到的"10 条"其实有 20 条。
#:
#: 执行者是 `_enforce_shared_task_window`（写路径触发）；两类任务的归档各自落在自己的
#: 存储上：多维表格 → 整条记录（绑定表 `deleted`），直连 → 整份文档的全部版本
#: （`runs.archived_at`）。回收站是同一个列表。
_SHARED_TASK_LIMIT = 10

#: 共用窗口里「这条任务正被占着、绝不能归档」的运行时状态。
_SHARED_WINDOW_BUSY_STATUSES = frozenset({
    "running",
    "resuming",
    "planning",
    "waiting_provider",
    "delivering",
})

#: 刚被恢复的任务在这段时间内**不参与**窗口收敛。
#:
#: 没有它会发生什么：用户从回收站恢复一条老任务 → 它的 `updated_at` 很老 →
#: 下一次收敛又把它当"最老的那条"收回去 —— 用户看到的就是「我刚恢复的又被删了」。
#: 只记在内存里、重启即失效：重启后的第一次收敛本来就该按当时的真实顺序来。
_RECENT_RESTORE_PIN_SECONDS = 120.0
_recent_restores: dict[str, float] = {}


def _pin_recent_restore(run_ids: Any) -> None:
    if not run_ids:
        return
    now = monotonic()
    for run_id in run_ids:
        text = str(run_id or "").strip()
        if text:
            _recent_restores[text] = now
    # 顺手清掉过期的，别让这张表无限长
    for key, stamp in list(_recent_restores.items()):
        if now - stamp > _RECENT_RESTORE_PIN_SECONDS:
            _recent_restores.pop(key, None)


def _is_recently_restored(run_id: str) -> bool:
    stamp = _recent_restores.get(str(run_id or ""))
    if stamp is None:
        return False
    if monotonic() - stamp > _RECENT_RESTORE_PIN_SECONDS:
        _recent_restores.pop(str(run_id), None)
        return False
    return True
#: 运行时状态 → 多维表格任务状态（前端 `RUN_STATUS_UI` 认后者）。
#:
#: 「任务记录」列表里两类任务混排，状态文案必须是同一套词汇，否则同一个位置会
#: 出现「waiting_review」和「待确认成片」两种写法（用户一眼看上去就是乱的）。
_DIRECT_STATUS_LABELS = {
    "succeeded": TableTaskStatus.COMPLETED.value,
    "completed_with_errors": TableTaskStatus.COMPLETED.value,
    "waiting_review": TableTaskStatus.REVIEWING.value,
    "waiting_approval": TableTaskStatus.WAITING_APPROVAL.value,
    "failed": TableTaskStatus.FAILED.value,
    "cancelled": TableTaskStatus.FAILED.value,
    "canceled": TableTaskStatus.FAILED.value,
    "error": TableTaskStatus.FAILED.value,
    "timed_out": TableTaskStatus.FAILED.value,
    "delivery_failed": TableTaskStatus.FAILED.value,
}

#: 直连任务里「还在自己推进」的运行时状态 —— 自动回收与删除都要避开它们。
#:
#: 注意**不含** `waiting_approval` / `waiting_review`：直连任务不占任何多维表格记录，
#: 「停在等人操作」的那条归档掉不会把别人的任务带走（多维表格那边必须拦，是因为
#: 整条记录是被那条运行占着的）。用户 2026-10-30 报「直接分析的任务没有删除按钮」，
#: 根因就是前端把多维表格那套「待审批/待确认成片也不许删」的判据套到了直连任务上。
_DIRECT_ACTIVE_STATUSES = frozenset({
    "created",
    "planning",
    "running",
    "resuming",
    "waiting_provider",
    "delivering",
})


def _direct_status_label(status: str) -> str:
    """没有绑定的运行用运行时状态，这里翻成前端认的那套中文状态。

    认不出来的状态（created / planning / running / resuming / waiting_provider /
    delivering…）一律「处理中」—— 这些都是「还在自己往前跑」，与列表徽章的语义一致。
    """
    return _DIRECT_STATUS_LABELS.get(
        str(status or "").strip().lower(), TableTaskStatus.PROCESSING.value
    )


#: 直连任务（「生成计划」）的合成记录 id 前缀。
#:
#: 多维表格任务用飞书记录 id（`rec…`）当分组键：同一条记录的**历次重跑**在界面上
#: 叠成一行，成片滑条里能看到每一版。直连任务没有记录，于是每条 run 各占一行 ——
#: 用户 2026-10-30：「我重跑任务应该和其它正常任务一样的逻辑，记录都和到一起」。
#: 这里用**文档**造一个稳定的合成 id（同一份文档的重跑 = 同一条任务），
#: 前端照同一套 `record_id` 分组逻辑就能把它叠成一行。
_DIRECT_RECORD_PREFIX = "direct:"


def _direct_record_id(source_url: str) -> str:
    """直连任务的分组键：`direct:<文档token>`；取不到 token 就返回空串（各自成行）。"""
    token = str(source_url or "").rstrip("/").rsplit("/", 1)[-1].strip()
    return f"{_DIRECT_RECORD_PREFIX}{token}" if token else ""


_WORKSPACE_STYLESHEET_LINK = (
    '<link rel="stylesheet" href="static/styles.css">'
)


def _render_workspace_html(static_dir: Path) -> str:
    html = (static_dir / "index.html").read_text("utf-8")
    if html.count(_WORKSPACE_STYLESHEET_LINK) != 1:
        raise RuntimeError(
            "workspace stylesheet link must appear exactly once"
        )
    styles = (static_dir / "styles.css").read_text("utf-8")
    inline = f"<style data-agent-inline-styles>\n{styles}\n</style>"
    return html.replace(_WORKSPACE_STYLESHEET_LINK, inline)


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    owner_user_id: str
    portal_user_id: str | None
    username: str
    is_portal: bool


def current_identity(request: Request) -> RequestIdentity:
    portal_user_id = request.headers.get("X-Portal-User-Id")
    if portal_user_id is None:
        return RequestIdentity(
            owner_user_id="prime-local",
            portal_user_id=None,
            username="",
            is_portal=False,
        )

    _validate_portal_user_id(portal_user_id)
    username = _decode_username(request.headers.get("X-Username", ""))
    return RequestIdentity(
        owner_user_id=portal_user_id,
        portal_user_id=portal_user_id,
        username=username,
        is_portal=True,
    )


def require_portal_identity(request: Request) -> RequestIdentity:
    identity = current_identity(request)
    if not identity.is_portal:
        raise HTTPException(status_code=403, detail="本地 Prime 提示词不可修改")
    return identity


_PORTAL_SIG_WINDOW = int(os.environ.get("PORTAL_SIG_WINDOW", "60"))


def _is_portal_admin(request: Request) -> bool:
    """True iff the request carries a Portal-issued, HMAC-verified admin flag.

    Portal 代理时才会设置 X-Is-Admin 并用共享的 PORTAL_INTERNAL_TOKEN 对
    (ts, is_admin, username) 签名；子应用直接收到的伪造 X-Is-Admin 会因签名
    校验失败被忽略，普通用户仍然受 owner 隔离保护。
    """
    if request.headers.get("X-Is-Admin") != "1":
        return False
    secret = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
    sig = request.headers.get("X-Portal-Sig") or ""
    ts_raw = request.headers.get("X-Portal-Ts") or ""
    if not secret or not sig or not ts_raw:
        return False
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > _PORTAL_SIG_WINDOW:
        return False
    username = request.headers.get("X-Username", "") or ""
    msg = f"{ts}:1:{username}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _validate_portal_user_id(value: str) -> None:
    if (
        not value
        or value == "prime-local"
        or value != value.strip()
        or len(value) > _MAX_IDENTITY_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise HTTPException(status_code=400, detail="Portal 用户身份无效")


def _decode_username(value: str) -> str:
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in "0123456789abcdefABCDEF"
            or value[index + 2] not in "0123456789abcdefABCDEF"
        ):
            raise HTTPException(status_code=400, detail="Portal 用户名编码无效")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Portal 用户名编码无效") from exc
    if (
        len(decoded) > _MAX_IDENTITY_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise HTTPException(status_code=400, detail="Portal 用户名无效")
    return decoded


def _iso_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _iso_utc_string(value: str) -> str:
    """把多维表格的裸 UTC 时间串转成**带时区**的 ISO 串再返回。

    以前直接返回 `2026-09-18 06:07:37`（UTC 但没有时区标记），前端 `new Date(...)`
    会当**本地时间**解析 —— 界面显示的时间比真实时间早 8 小时，而且跟事件时间对不上，
    用户看到的就是「时间是乱的」（用户 2026-09-18）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


_PORTAL_PROMPT_TTL_SECONDS = 600.0
# run_id -> (取到的时间, {"prompt": 正文, "has_reference_video": bool})。正文与「有没有挂
# 参考视频」都在 LangGraph 的 checkpoint 库里，一次历史列表可能列 200 条 —— 不能每条都去读
# 一遍 1.1GB 的库，所以带 TTL 缓存。
_PORTAL_PROMPT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PORTAL_EMPTY_VIEW: dict[str, Any] = {"prompt": "", "has_reference_video": False}


async def _portal_plan_view(active: Any, run_id: str, thread_id: str) -> dict[str, Any]:
    """给 Portal 的「创作记录」取这条运行的**原始需求正文**与**有没有挂参考视频**（带缓存）。

    参考视频这一位是给**单价分档**用的：方舟对「有/无输入视频」是两档单价（Seedance 2.5
    差 1.6 倍），而方舟的任务接口只返回输出内容，看不出输入侧；只有飞书自己知道这次提交
    有没有带参考视频（2026-10-29 用户报「计算费用应该按挂没挂参考视频判断单价」）。
    取不到就返回空视图，由 Portal 按「无输入视频」档结算，绝不因此让历史列表报错。
    """
    now = time.time()
    cached = _PORTAL_PROMPT_CACHE.get(run_id)
    if cached is not None and now - cached[0] < _PORTAL_PROMPT_TTL_SECONDS:
        return cached[1]
    try:
        view = await active.plan_portal_view(thread_id)
    except Exception:  # noqa: BLE001 - 历史列表不该因读 checkpoint 失败而报错
        view = dict(_PORTAL_EMPTY_VIEW)
    if not isinstance(view, dict):
        view = dict(_PORTAL_EMPTY_VIEW)
    view = {
        "prompt": str(view.get("prompt") or ""),
        "has_reference_video": bool(view.get("has_reference_video")),
        # 文档标题：给「直接分析需求文档」的运行当列表标题用（它们没有多维表格
        # 记录的 display_text），见 `_direct_run_payloads`。
        "title": str(view.get("title") or ""),
        # 产出类型（video / image）：直连任务重跑时要用，见 `rerun_direct_run`。
        "planning_mode": str(view.get("planning_mode") or ""),
    }
    _PORTAL_PROMPT_CACHE[run_id] = (now, view)
    return view


def _feishu_run_model_label(settings: Settings, kind: str, providers: list[str]) -> str:
    image_models = {
        "banana": settings.banana_model,
        "gpt-image2": settings.gpt_image_model,
        "seedream": settings.seedream_model,
        "chiyun": settings.chiyun_model or settings.banana_model,
        "aiport": settings.aiport_image_model,
        "aiport_klein": "flux2_klein_allinone",
        "aiport_klein_v3": "klein_true_v3_assets",
        "aiport_anime2real": "anime2real_auto",
        "aiport_zimage": "zimage_multifunction",
        "aiport_style": "krea2_style_transfer",
    }
    video_models = {
        capability.key: capability.model for capability in VIDEO_MODELS
    }
    video_models.update(
        {
            "seedance": settings.seedance_model,
            "aiport": settings.aiport_video_model,
            "volcengine_portrait": settings.seedance_model,
        }
    )
    labels: list[str] = []
    for provider in providers:
        provider = str(provider or "").strip()
        if not provider:
            continue
        if kind == "video":
            label = video_models.get(provider)
        elif kind == "image":
            label = image_models.get(provider)
        else:
            label = video_models.get(provider) or image_models.get(provider)
        if not label:
            label = provider
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels)


def _storyboard_frame_regenerator(services: Any) -> Any:
    """把「只重生成某一段分镜图」包成运行时能调的回调（方案 A）。

    运行时（GraphRuntime）手里只有 graph/repository/file_store/settings，没有图片
    生成器；生成逻辑在 graph/nodes.py 里，所以在这里闭包注入。
    """

    async def regenerate(*, run_id: str, task: Any, frame: Any, assets: Any):
        from feishu_generation_agent.graph.nodes import (
            _storyboard_config,
            regenerate_storyboard_frame,
        )

        return await regenerate_storyboard_frame(
            services=services,
            run_id=run_id,
            task=task,
            frame=frame,
            assets=list(assets),
            config=_storyboard_config(services),
        )

    return regenerate


def _effective_provider_preferences(
    settings: Settings,
    preferences: ProviderPreferences,
) -> ProviderPreferences:
    image_provider = preferences.image_provider
    if image_provider == "aiport" and not settings.aiport_image_enabled:
        image_provider = "seedream"
    if settings.video_provider == "aiport":
        video_provider = "aiport"
    else:
        fallback = resolve_video_model_key(
            settings.seedance_model,
            fallback="seedance2.5",
        ) or "seedance2.5"
        preferred = resolve_video_model_key(
            preferences.video_provider,
            fallback="",
        )
        video_provider = preferred or fallback
    return ProviderPreferences(
        video_provider=video_provider,
        image_provider=image_provider,
        planning_pipeline=preferences.planning_pipeline,
        # 必须原样带出来：漏了这个字段，响应就会回落到 dataclass 默认值，界面永远
        # 显示 2 —— 用户把 K 设成 0（关闭）后界面还显示 2，而规划阶段按 0 跳过
        # 分镜图，看起来就是「分镜图莫名其妙没了」（2026-09-22 实跑踩到）。
        storyboard_candidates=preferences.storyboard_candidates,
    )


async def _probe_aiport(base_url: str) -> bool:
    """Probe the AI Port module API instead of trusting a TCP connection."""

    def _probe_sync() -> bool:
        url = base_url.rstrip("/") + "/api/modules"
        try:
            request = Request(url, method="GET")
            opener = build_opener(ProxyHandler({}))
            with opener.open(request, timeout=1.5) as response:
                if not 200 <= int(response.status) < 300:
                    return False
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                modules = payload.get("modules") if isinstance(payload, dict) else None
                return isinstance(modules, list) and bool(modules)
        except Exception:
            return False

    return await asyncio.to_thread(_probe_sync)



def create_app(
    *,
    runtime: GraphRuntime | None = None,
    services: GraphServices | None = None,
    settings: Settings | None = None,
    bitable_service: BitableMvpService | Any | None = None,
    planner_prompt_store: PlannerPromptStore | None = None,
    provider_preference_store: ProviderPreferenceStore | None = None,
) -> FastAPI:
    if sum(value is not None for value in (runtime, services, settings)) > 1:
        raise ValueError("runtime, services and settings are mutually exclusive")
    static_dir = Path(__file__).with_name("static")
    asset_library_settings = settings or Settings()
    asset_library_settings.ensure_paths()

    @asynccontextmanager
    async def tracing_environment(settings):
        names = (
            "LANGSMITH_TRACING",
            "LANGCHAIN_TRACING_V2",
            "LANGSMITH_API_KEY",
            "LANGSMITH_PROJECT",
        )
        previous = {name: os.environ.get(name) for name in names}
        if settings.langsmith_tracing:
            settings.require("langsmith_api_key")
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGSMITH_API_KEY"] = (
                settings.langsmith_api_key.get_secret_value()
            )
            os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        else:
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ.pop("LANGSMITH_API_KEY", None)
            os.environ.pop("LANGSMITH_PROJECT", None)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    @asynccontextmanager
    async def activated_services(
        active_services: GraphServices,
        *,
        resume: bool = True,
    ) -> AsyncIterator[GraphRuntime]:
        async with open_checkpointer(active_services.settings) as checkpointer:
            active = GraphRuntime(
                graph=build_graph(active_services, checkpointer),
                repository=active_services.repository,
                file_store=active_services.file_store,
                settings=active_services.settings,
                delivery_writer=active_services.delivery_writer,
                document_source=active_services.document_source,
                vision_analyzer=active_services.vision_analyzer,
                rework_fuser=getattr(
                    active_services.planner, "fuse_rework_prompt", None
                ),
                # 返工时把上一版成片直接送能看视频的模型（不抽帧）。
                video_analyzer=getattr(
                    active_services, "video_analyzer", None
                ),
                # 2026-09-28：成片要作为 video_url 附进**同一次**融合请求
                # （一次多模态调用），需要公开图床把视频传成 https 链接。
                public_media_host=getattr(
                    active_services, "public_media_host", None
                ),
                # 只重生成某一段分镜图（方案 A：同一提示词重新采样）——运行时没有
                # 图片生成器，所以在这里把回调注入进去。
                storyboard_regenerator=_storyboard_frame_regenerator(
                    active_services
                ),
                # 清洗范围探针（2026-10-28）：重洗前也要先确认"洗哪儿、哪儿不许动"，
                # 否则重洗会再把用户的品牌字洗掉一次（用户就是这么连着中招的）。
                cleanup_scope_probe=getattr(
                    active_services, "cleanup_scope_probe", None
                ),
            )
            try:
                if resume:
                    await active.resume_pending_runs()
                yield active
            finally:
                await active.close()

    @asynccontextmanager
    async def _asset_library_lifespan() -> AsyncIterator[None]:
        store = await AssetLibraryStore.open(
            db_path=asset_library_settings.asset_library_db_path,
            assets_dir=asset_library_settings.asset_library_dir,
            base_url=asset_library_settings.asset_base_url,
        )
        asset_library_holder["store"] = store
        try:
            yield
        finally:
            asset_library_holder.pop("store", None)
            await store.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _asset_library_lifespan():
            async with _lifespan_inner(app):
                yield

    @asynccontextmanager
    async def _lifespan_inner(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            async with tracing_environment(services.settings):
                async with activated_services(services) as active:
                    app.state.runtime = active
                    app.state.bitable_service = bitable_service
                    app.state.planner_prompt_store = planner_prompt_store
                    app.state.provider_preference_store = provider_preference_store
                    app.state.provider_preferences = getattr(
                        services, "provider_preferences", None
                    )
                    try:
                        yield
                    finally:
                        if bitable_service is not None:
                            await bitable_service.close()
                        app.state.bitable_service = None
                        app.state.planner_prompt_store = None
                        app.state.provider_preference_store = None
                        app.state.provider_preferences = None
                        app.state.runtime = None
            return

        if runtime is not None:
            app.state.runtime = runtime
            app.state.bitable_service = bitable_service
            app.state.planner_prompt_store = planner_prompt_store
            app.state.provider_preference_store = provider_preference_store
            app.state.provider_preferences = None
            try:
                yield
            finally:
                if bitable_service is not None:
                    await bitable_service.close()
                app.state.bitable_service = None
                app.state.planner_prompt_store = None
                app.state.provider_preference_store = None
                app.state.provider_preferences = None
                await runtime.close()
                app.state.runtime = None
            return

        local_settings = settings or Settings()
        if runtime_is_configured(local_settings):
            async with tracing_environment(local_settings):
                async with open_application_services(local_settings) as application:
                    async with activated_services(
                        application.graph, resume=False
                    ) as active:
                        app.state.runtime = active
                        active_bitable = (
                            application.bitable_factory.create(active)
                            if application.bitable_factory is not None
                            else None
                        )
                        app.state.bitable_service = active_bitable
                        app.state.planner_prompt_store = (
                            planner_prompt_store
                            or getattr(application, "planner_prompt_store", None)
                        )
                        app.state.provider_preference_store = (
                            provider_preference_store
                            or getattr(application, "provider_preference_store", None)
                        )
                        app.state.provider_preferences = getattr(
                            application.graph, "provider_preferences", None
                        )
                        try:
                            if active_bitable is not None:
                                try:
                                    await active_bitable.resume_incomplete()
                                except Exception:
                                    # Keep the local UI available so a later scan can
                                    # report a safe, actionable readiness error.
                                    pass
                            else:
                                await active.resume_pending_runs()
                            yield
                        finally:
                            if active_bitable is not None:
                                await active_bitable.close()
                            app.state.bitable_service = None
                            app.state.planner_prompt_store = None
                            app.state.provider_preference_store = None
                            app.state.provider_preferences = None
                            app.state.runtime = None
            return

        async with tracing_environment(local_settings):
            app.state.runtime = None
            app.state.bitable_service = None
            app.state.planner_prompt_store = planner_prompt_store
            app.state.provider_preference_store = provider_preference_store
            app.state.provider_preferences = None
            try:
                yield
            finally:
                app.state.bitable_service = None
                app.state.planner_prompt_store = None
                app.state.provider_preference_store = None
                app.state.provider_preferences = None
                app.state.runtime = None

    app = FastAPI(title="本地飞书生成任务 Agent", lifespan=lifespan)

    @app.middleware("http")
    async def prevent_static_asset_caching(request: Request, call_next):
        response = await call_next(request)
        # Portal forwards static assets after stripping its Agent mount prefix.
        if request.scope["path"].startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        active_settings = (
            services.settings
            if services is not None
            else runtime.settings
            if runtime is not None
            else settings or Settings()
        )

        def configured(*names: str) -> bool:
            for name in names:
                value = getattr(active_settings, name)
                if hasattr(value, "get_secret_value"):
                    value = value.get_secret_value()
                if value is None or (isinstance(value, str) and not value.strip()):
                    return False
            return True

        checks = {
            "local_storage": True,
            "feishu_read": configured("lark_app_id", "lark_app_secret"),
            "feishu_write": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_output_owner_open_id",
                "lark_output_folder_token",
            ),
            "bitable_read": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_bitable_url",
                "lark_bitable_table_id",
                "lark_bitable_view_id",
            ),
            "bitable_write": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_bitable_url",
                "lark_bitable_table_id",
                "lark_bitable_view_id",
            ),
            "planning": configured("deepseek_api_key", "deepseek_model"),
            "vision": configured("claude_api_key", "claude_model"),
            "image_generation": configured("chiyun_api_key", "chiyun_model")
            or configured("ark_api_key", "seedream_model"),
            "video_generation": configured("ark_api_key", "seedance_model"),
        }
        capabilities = {
            name: {
                "configured": value,
                "reachable": None,
                "permission_ok": None,
                "message": "已配置" if value else "缺少配置",
            }
            for name, value in checks.items()
        }
        local_image = bool(getattr(active_settings, "aiport_image_enabled", False))
        video_provider = getattr(active_settings, "video_provider", "seedance")
        local_reachable = (
            await _probe_aiport(
                getattr(active_settings, "aiport_base_url", "http://127.0.0.1:8801")
            )
            if local_image or video_provider == "aiport"
            else False
        )
        providers: dict[str, list[dict[str, Any]]] = {"image": [], "video": []}
        if local_image:
            # Expose only the two supported image models in the Feishu UI.
            # Other local workflows remain available to their dedicated tools,
            # but are not selectable for this task editor.
            local_labels = {
                "aiport": "\u672c\u5730 Qwen \u56fe\u751f\u56fe",
            }
            for name, label in local_labels.items():
                providers["image"].append(
                    {
                        "name": name,
                        "label": label,
                        "mode": "local",
                        "configured": True,
                        "reachable": local_reachable,
                    }
                )
        if configured("ark_api_key", "seedream_model"):
            providers["image"].append(
                {"name": "seedream", "label": "Seedream 国风", "mode": "cloud", "configured": True}
            )
        if video_provider == "aiport":
            providers["video"].append(
                {
                    "name": "aiport",
                    "label": "\u672c\u5730 MiniMax H3",
                    "mode": "local",
                    "configured": True,
                    "reachable": local_reachable,
                }
            )
        else:
            for capability in VIDEO_MODELS:
                providers["video"].append(
                    capability.public_payload(
                        configured=configured("ark_api_key", "seedance_model")
                    )
                )
        preferences = _effective_provider_preferences(
            active_settings,
            await provider_preferences_from_request(request),
        )
        defaults = {
            "video_provider": preferences.video_provider,
            "image_provider": preferences.image_provider,
        }
        return {
            "ready": (
                checks["local_storage"]
                and checks["feishu_read"]
                and checks["planning"]
                and checks["image_generation"]
                and checks["video_generation"]
                and (checks["bitable_write"] or checks["feishu_write"])
            ),
            "modes": {
                "bitable": checks["bitable_read"],
                "legacy_delivery": checks["feishu_write"],
            },
            "capabilities": capabilities,
            "providers": providers,
            "defaults": defaults,
        }

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", "输入无效"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    def get_runtime(request: Request) -> GraphRuntime:
        active = getattr(request.app.state, "runtime", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="运行时尚未配置",
            )
        return active

    async def artifact_counts(request: Request, bindings: Any) -> dict[str, int]:
        """各条运行已有多少成片 —— 任务记录在外面显示「已成片 N 条」。

        取不到就返回空（任务列表不该因为计数失败而打不开）。
        """
        counter = getattr(
            getattr(get_runtime(request), "repository", None),
            "count_artifacts_by_run",
            None,
        )
        if counter is None:
            return {}
        try:
            return await counter([binding.run_id for binding in bindings])
        except Exception:
            return {}

    def get_bitable_service(request: Request) -> Any:
        active = getattr(request.app.state, "bitable_service", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="多维表格服务尚未配置",
            )
        return active

    def runtime_owner_scope(active: Any, owner_user_id: str):
        repository = getattr(active, "repository", None)
        owner_scope = getattr(repository, "owner_scope", None)
        if callable(owner_scope):
            return owner_scope(owner_user_id)
        return nullcontext()

    async def ensure_owned_run(
        active: Any, run_id: str, owner_user_id: str
    ) -> None:
        repository = getattr(active, "repository", None)
        get_run = getattr(repository, "get_run", None)
        if callable(get_run) and await get_run(
            run_id, owner_user_id=owner_user_id
        ) is None:
            raise RunNotFound("运行不存在")

    def owner_argument(callable_object: Any, owner_user_id: str) -> dict[str, str]:
        try:
            parameter = signature(callable_object).parameters.get(
                "owner_user_id"
            )
        except (TypeError, ValueError):
            parameter = None
        if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
            return {}
        return {"owner_user_id": owner_user_id}

    def planning_prompt_argument(
        callable_object: Any,
        planning_prompt: PlanningPromptSnapshot,
    ) -> dict[str, PlanningPromptSnapshot]:
        try:
            parameter = signature(callable_object).parameters.get(
                "planning_prompt"
            )
        except (TypeError, ValueError):
            parameter = None
        if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
            return {}
        return {"planning_prompt": planning_prompt}

    async def filter_bindings_by_runtime_owner(
        active_runtime: Any,
        bindings: list[Any],
        owner_user_id: str,
    ) -> list[Any]:
        repository = getattr(active_runtime, "repository", None)
        get_run = getattr(repository, "get_run", None)
        if not callable(get_run):
            return []
        visible: list[Any] = []
        for binding in bindings:
            if await get_run(
                binding.run_id, owner_user_id=owner_user_id
            ) is not None:
                visible.append(binding)
        return visible

    async def production_run_kind(
        active_bitable: Any,
        run_id: str,
        owner_user_id: str,
    ) -> bool | None:
        classifier = getattr(active_bitable, "is_production_run", None)
        if not callable(classifier):
            return None
        return await classifier(
            run_id,
            **owner_argument(classifier, owner_user_id),
        )

    def get_planner_prompt_store(request: Request) -> PlannerPromptStore:
        active = getattr(request.app.state, "planner_prompt_store", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="提示词存储尚未配置",
            )
        return active

    def get_provider_preference_store(request: Request) -> ProviderPreferenceStore:
        active = getattr(request.app.state, "provider_preference_store", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="模型偏好存储尚未配置",
            )
        return active

    async def provider_preferences_from_request(request: Request) -> ProviderPreferences:
        active = getattr(request.app.state, "provider_preferences", None)
        if active is not None:
            return active
        store = getattr(request.app.state, "provider_preference_store", None)
        if store is not None:
            return await store.get()
        return ProviderPreferences()

    def active_settings_for_request(request: Request) -> Settings:
        if services is not None:
            return services.settings
        if runtime is not None:
            return runtime.settings
        return settings or Settings()

    def planner_prompt_response(
        identity: RequestIdentity,
        profile: Any | None = None,
    ) -> PlannerPromptResponse:
        if profile is not None:
            return PlannerPromptResponse(
                mode="personal",
                editable=True,
                prompt_text=profile.prompt_text,
                version=profile.version,
                source="personal",
            )
        return PlannerPromptResponse(
            mode="prime",
            editable=identity.is_portal,
            prompt_text=planner_system_prompt(),
            version=0,
            source="prime",
        )

    async def effective_planning_prompt(
        request: Request,
        identity: RequestIdentity,
    ) -> PlanningPromptSnapshot:
        profile = None
        if identity.is_portal:
            profile = await get_planner_prompt_store(request).get(
                identity.owner_user_id
            )
        snapshot = build_planning_prompt_snapshot(
            owner_user_id=identity.owner_user_id,
            source="personal" if profile is not None else "prime",
            version=profile.version if profile is not None else 0,
            prompt_text=(
                profile.prompt_text
                if profile is not None
                else planner_system_prompt()
            ),
        )
        _LOGGER.info(
            "Planning prompt snapshot owner_user_id=%s source=%s "
            "version=%d sha256=%s",
            snapshot.owner_user_id,
            snapshot.source,
            snapshot.version,
            snapshot.prompt_sha256,
        )
        return snapshot

    @app.get("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def get_planner_prompt(request: Request) -> PlannerPromptResponse:
        identity = current_identity(request)
        if not identity.is_portal:
            return planner_prompt_response(identity)
        profile = await get_planner_prompt_store(request).get(
            identity.owner_user_id
        )
        return planner_prompt_response(identity, profile)

    @app.put("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def update_planner_prompt(
        request: Request,
        identity: Annotated[RequestIdentity, Depends(require_portal_identity)],
        payload: PlannerPromptUpdate,
    ) -> PlannerPromptResponse:
        try:
            profile = await get_planner_prompt_store(request).save(
                portal_user_id=identity.owner_user_id,
                username=identity.username,
                prompt_text=payload.prompt_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="提示词无效") from exc
        return planner_prompt_response(identity, profile)

    @app.delete("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def delete_planner_prompt(request: Request) -> PlannerPromptResponse:
        identity = current_identity(request)
        if not identity.is_portal:
            raise HTTPException(status_code=403, detail="本地 Prime 提示词不可删除")
        await get_planner_prompt_store(request).delete(identity.owner_user_id)
        return planner_prompt_response(identity)

    @app.get("/api/provider-preferences", response_model=ProviderPreferencesResponse)
    async def get_provider_preferences(request: Request) -> ProviderPreferencesResponse:
        active_settings = active_settings_for_request(request)
        preferences = _effective_provider_preferences(
            active_settings,
            await provider_preferences_from_request(request),
        )
        return ProviderPreferencesResponse(
            video_provider=preferences.video_provider,
            image_provider=preferences.image_provider,
            planning_pipeline=preferences.planning_pipeline,
            storyboard_candidates=preferences.storyboard_candidates,
        )

    @app.put("/api/provider-preferences", response_model=ProviderPreferencesResponse)
    async def update_provider_preferences(
        request: Request,
        payload: ProviderPreferencesUpdate,
    ) -> ProviderPreferencesResponse:
        active_settings = active_settings_for_request(request)
        try:
            video_provider = payload.video_provider
            if active_settings.video_provider == "aiport":
                video_provider = "aiport"
            else:
                video_provider = resolve_video_model_key(
                    video_provider,
                    fallback="",
                )
                if video_provider is None:
                    raise ValueError("不支持的视频模型")
            image_provider = payload.image_provider
            if image_provider == "aiport" and not active_settings.aiport_image_enabled:
                image_provider = "seedream"
            preferences = await get_provider_preference_store(request).save(
                video_provider=video_provider,
                image_provider=image_provider,
                planning_pipeline=payload.planning_pipeline,
                storyboard_candidates=payload.storyboard_candidates,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app_state = request.app.state
        current = getattr(app_state, "provider_preferences", None)
        if current is not None:
            # 就地更新共享对象：GraphServices.provider_preferences 与 app.state
            # 持有同一引用，改它即可让图执行层的路由立即生效，无需重启服务。
            current.video_provider = preferences.video_provider
            current.image_provider = preferences.image_provider
            current.planning_pipeline = preferences.planning_pipeline
            current.storyboard_candidates = preferences.storyboard_candidates
        return ProviderPreferencesResponse(
            video_provider=preferences.video_provider,
            image_provider=preferences.image_provider,
            planning_pipeline=preferences.planning_pipeline,
            storyboard_candidates=preferences.storyboard_candidates,
        )

    def raise_bitable_error(exc: Exception, *, expose_conflict: bool = False) -> None:
        if isinstance(exc, HTTPException):
            # 已经在子入口里翻译好的 HTTPException 必须**原样透出**。
            # 反例（2026-10-30 实测）：多维表格重跑入口发现这是直连任务后转调
            # 直连重跑，那边抛的 409「只有已经结束…的任务可以重跑」被这里的
            # `except Exception` 兜住，压成了 502「读取多维表格失败」——
            # 用户看到的原因跟真实原因完全无关。
            raise exc
        if (
            isinstance(exc, AgentError)
            and exc.detail.category is ErrorCategory.TRANSIENT
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="飞书服务暂时不可用，请稍后重试",
            ) from None
        if isinstance(exc, BitableSchemaError):
            raise HTTPException(
                status_code=422,
                detail="多维表格字段配置不兼容，请检查文本、需求来源、执行人和结果列",
            ) from None
        if isinstance(exc, RunValidationError):
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if isinstance(exc, BitableResultConflict):
            raise HTTPException(
                status_code=409,
                detail="结果列已有附件，已停止回写",
            ) from None
        if isinstance(exc, RunConflict) and (
            expose_conflict or str(exc).endswith("任务暂未启用")
        ):
            # RunConflict 的文案多数是业务层写给用户看的（「只有已经结束的多维表格
            # 任务可以重跑」「该记录上还有一条「生成中」的任务，请先处理…」），但
            # 领取类入口的冲突文案可能带内部标识，所以只有显式声明 expose_conflict
            # 的重跑 / 重试入口才原样透出。
            # 背景（用户 2026-09-21）：「重新运行」返回 409 时被压成「该任务已被领取
            # 或当前不可处理」，用户连点 6 次都看不出发生了什么、也不知道该做什么。
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if isinstance(exc, (TaskAlreadyClaimed, ProductionTaskAlreadyClaimed, RunConflict)):
            raise HTTPException(
                status_code=409,
                detail="该任务已被领取或当前不可处理",
            ) from None
        if isinstance(exc, RunNotFound):
            raise HTTPException(status_code=404, detail="多维表格运行不存在") from None
        raise HTTPException(
            status_code=502,
            detail="读取多维表格失败，请检查链接、权限和字段配置",
        ) from None

    #: 「任务列表」扫描结果缓存（2026-10-15 用户报「飞书模块各个按钮反应都这么慢」）。
    #:
    #: 实测：`/api/bitable/tasks` 单次 **3.9~5.6 秒**（其它接口都是毫秒级）—— 因为它每次
    #: 都**实时扫飞书多维表格**，而分页大小是 10（`BOT_SCAN_PAGE_SIZE`）→ 一次点击要 3 次以上
    #: 跨网往返。前端每次刷新/切按钮都会打这个接口，于是"每个按钮都卡几秒"。
    #:
    #: 修法：**短 TTL 缓存 + 后台刷新（stale-while-revalidate）**——
    #: 有旧值时立刻返回旧值、同时在后台刷新，用户永远不用等那 4 秒；首次访问才等一次。
    _TASKS_TTL_SECONDS = 15.0

    #: ⚠️ 缓存必须**挂在 bitable 服务实例上**：第一版写成模块级字典，跨测试/跨服务串了
    #: （`test_scan_claim_duplicate_and_run_detail_sync` 立刻红 —— 换了服务却拿到旧结果）。
    def _tasks_cache_slot(active: Any) -> dict[str, Any]:
        """每个服务实例一个缓存槽；挂不上就不缓存（退化成原行为，不报错）。"""
        slot = getattr(active, "_feishu_tasks_cache", None)
        if isinstance(slot, dict):
            return slot
        slot = {}
        try:
            setattr(active, "_feishu_tasks_cache", slot)
        except Exception:  # noqa: BLE001
            return {}
        return slot

    async def _refresh_bitable_tasks(
        active: Any, category: str, slot: dict[str, Any]
    ) -> list[dict]:
        tasks = await active.scan(category)
        payload = [_task_payload(task) for task in tasks]
        slot[category] = (time.monotonic(), payload)
        return payload

    async def _bitable_tasks_cached(active: Any, category: str) -> list[dict]:
        slot = _tasks_cache_slot(active)
        cached = slot.get(category)
        if cached is not None:
            if time.monotonic() - cached[0] < _TASKS_TTL_SECONDS:
                return cached[1]
            refresh = slot.get(f"{category}:refresh")
            if refresh is None or refresh.done():
                slot[f"{category}:refresh"] = asyncio.create_task(
                    _refresh_bitable_tasks(active, category, slot)
                )
            return cached[1]          # 立刻给旧值，别让用户等
        return await _refresh_bitable_tasks(active, category, slot)

    def _invalidate_tasks_cache(active: Any, category: str | None = None) -> None:
        """写操作（领取 / 归档 / 还原）之后必须让列表缓存失效。

        否则用户点了「开始分析」之后，列表最长 15 秒还是旧的（徽章不变、记录状态不刷新）
        —— 这是缓存引入的**产品回退**，`test_scan_claim_duplicate_and_run_detail_sync`
        就是这么红起来的。读缓存只用在「只读刷新」这条路上。
        """
        slot = getattr(active, "_feishu_tasks_cache", None)
        if not isinstance(slot, dict):
            return
        if category is None:
            slot.clear()
        else:
            slot.pop(category, None)

    @app.get("/api/bitable/tasks")
    async def scan_bitable_tasks(
        request: Request,
        category: ProductionCategory = "animation",
    ) -> list[dict]:
        active = get_bitable_service(request)
        try:
            return await _bitable_tasks_cached(active, category)
        except Exception as exc:
            raise_bitable_error(exc)

    def _task_payload(task: Any) -> dict:
        if hasattr(task, "progress") and hasattr(task, "deliverable"):
            return {
                "record_id": task.record_id,
                "display_text": task.display_text,
                "source_url": task.source_url,
                "progress": task.progress,
                "task_type": task.task_type,
                "maker_name": task.maker_name,
                "deliverable": task.deliverable,
                "delivery_block_reason": task.delivery_block_reason,
                # 已领取的记录现在会留在列表里，前端据此渲染状态徽章并跳回
                # 对应运行（#1：以前这些记录直接消失，用户找不回来）。
                "claim_status": (
                    task.claim_status.value
                    if task.claim_status is not None
                    else None
                ),
                "claimed_run_id": task.claimed_run_id,
            }
        return task.model_dump(mode="json")

    @app.get("/api/bitable/active-runs")
    async def list_active_bitable_runs(request: Request) -> list[dict]:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            ownership = owner_argument(
                active.active_runs, identity.owner_user_id
            )
            bindings = await active.active_runs(
                **ownership
            )
            if not ownership:
                bindings = await filter_bindings_by_runtime_owner(
                    get_runtime(request),
                    bindings,
                    identity.owner_user_id,
                )
        except Exception as exc:
            raise_bitable_error(exc)
        counts = await artifact_counts(request, bindings)
        return [
            {
                "run_id": binding.run_id,
                "record_id": binding.record_id,
                "display_text": binding.display_text,
                "status": binding.status.value,
                # 带上时间（带时区）：前端按时间倒序排历史记录，缺了这个字段就只能
                # 保持接口顺序，看起来就是乱序的（用户 2026-09-18）。
                "updated_at": _iso_utc_string(getattr(binding, "updated_at", "")),
                "artifact_count": counts.get(binding.run_id, 0),
            }
            for binding in bindings
        ]

    @app.get("/api/bitable/recent-runs")
    async def list_recent_bitable_runs(request: Request) -> list[dict]:
        active = get_bitable_service(request)
        identity = current_identity(request)
        # MVP 模式的服务没有「最近完成任务」能力，返回空列表即可，
        # 避免前端把缺失方法误报成「读取多维表格失败」。
        recent_runs = getattr(active, "recent_runs", None)
        if recent_runs is None:
            return []
        try:
            bindings = await recent_runs(
                **owner_argument(
                    recent_runs, identity.owner_user_id
                )
            )
        except Exception as exc:
            raise_bitable_error(exc)
        payload = await _run_payloads(request, active, identity, bindings)
        # 「直接分析需求文档」建出来的运行没有绑定，单靠上面那份列表永远看不到
        #（用户 2026-10-30 报「生成计划的任务在下面历史里找不到」）——补进来。
        direct_payloads = await _direct_run_payloads(request, active, identity)
        payload.extend(direct_payloads)
        # 按时间倒序（最新的在最上面）—— 以前直接透传多维表格返回的顺序，
        # 界面上的历史记录看起来就是乱序的。
        payload.sort(
            key=lambda item: _iso_timestamp(item.get("updated_at") or ""),
            reverse=True,
        )
        return payload

    async def _run_payload(
        request: Request,
        active: Any,
        identity: RequestIdentity,
        binding: Any,
        counts: dict[str, int],
    ) -> dict:
        """把**一条**绑定转成前端用的运行 payload。"""
        try:
            result_table_url = await active.result_table_url(
                binding.run_id,
                **owner_argument(
                    active.result_table_url,
                    identity.owner_user_id,
                ),
            )
        except AttributeError:
            result_table_url = None
        return {
            "run_id": binding.run_id,
            # 同一条多维表格记录的历次尝试共用一个 record_id：
            # 前端据此把它们叠在同一条任务记录下（重跑不新开一条）。
            "record_id": binding.record_id,
            "display_text": binding.display_text,
            "status": binding.status.value,
            "updated_at": _iso_utc_string(getattr(binding, "updated_at", "")),
            "artifact_count": counts.get(binding.run_id, 0),
            "result_table_url": result_table_url,
            # 失败尝试的落库原因（可能为 None —— 同步失败路径只写状态不写
            # 原因）。前端优先用它，拿不到再去那次运行里找失败事件/执行记录。
            "last_error": getattr(binding, "last_error", None),
            # **运行时**状态，和上面的 `status` 不是一回事：`status` 是多维
            # 表格任务状态（已完成 / 失败 / 生成中…），运行时的 cancelled 与
            # failed 在那边都压成「失败」，前端分不出「这次是被取消的」。
            # 所以单独给一个字段，而不是往 `status` 里塞第二种语义。
            "runtime_status": getattr(binding, "runtime_status", None),
            "rerunnable": binding.status in {
                TableTaskStatus.COMPLETED,
                TableTaskStatus.FAILED,
            },
        }

    async def _run_payloads(
        request: Request,
        active: Any,
        identity: RequestIdentity,
        bindings: Any,
    ) -> list[dict]:
        """把绑定列表转成前端用的运行 payload（recent-runs 与按记录取历史共用）。

        每条**历次尝试**各占一条 payload —— 前端按 `record_id` 把它们叠成同一条
        任务记录（`BitableState.latestRunsByRecord`），点开某条记录时看到的历次
        成片预览滑条就靠这个结构。
        """
        counts = await artifact_counts(request, bindings)
        return [
            await _run_payload(request, active, identity, binding, counts)
            for binding in bindings
        ]

    async def _direct_payload(
        runtime_for_owner: Any,
        identity: RequestIdentity,
        row: dict,
        counts: dict[str, int],
    ) -> dict:
        """一条**没有绑定**的运行 → 前端 payload（列表与按记录取历史共用）。"""
        run_id = str(row.get("run_id") or "")
        status_value = str(row.get("status") or "")
        plan_view = await _portal_plan_view(
            runtime_for_owner, run_id, str(row.get("thread_id") or "")
        )
        # 状态翻成前端认的那套中文词汇（与多维表格任务同一套文案）。
        label = _direct_status_label(status_value)
        return {
            "run_id": run_id,
            # 合成分组键：同一份文档的历次重跑叠成同一条任务记录（见 _direct_record_id）。
            # 取不到 token 时是空串，前端会退回 `run:<run_id>` 各自成行。
            "record_id": _direct_record_id(str(row.get("source_url") or "")) or None,
            "display_text": str(plan_view.get("title") or "")
            or str(row.get("source_url") or "")
            or run_id,
            "status": label,
            "updated_at": _iso_utc_string(str(row.get("updated_at") or "")),
            "artifact_count": int(counts.get(run_id) or 0),
            # 直连任务不写结果表：前端据此不画「结果表」链接。
            "result_table_url": None,
            "last_error": None,
            # 运行时状态原样带出（前端按钮用它判断「重跑/取消」能不能点）。
            "runtime_status": status_value,
            "mode": "direct",
            "source_url": str(row.get("source_url") or ""),
            # 只有已经结束、或成片已生成在等人确认的运行才给重跑 ——
            # 与多维表格那条口径一致（`production_service.rerun` 允许
            # 「已完成 / 失败 / 待确认成片」）。
            "rerunnable": label in {
                TableTaskStatus.COMPLETED.value,
                TableTaskStatus.FAILED.value,
                TableTaskStatus.REVIEWING.value,
            },
        }

    async def _direct_payloads_for_rows(
        runtime_for_owner: Any,
        identity: RequestIdentity,
        rows: list[dict],
    ) -> list[dict]:
        """一批直连运行 → payload（产物计数一次查完）。"""
        counter = getattr(
            getattr(runtime_for_owner, "repository", None),
            "count_artifacts_by_run",
            None,
        )
        counts: dict[str, int] = {}
        if callable(counter):
            try:
                counts = await counter(
                    [str(row.get("run_id") or "") for row in rows]
                )
            except Exception:  # noqa: BLE001
                counts = {}
        return [
            await _direct_payload(runtime_for_owner, identity, row, counts)
            for row in rows
        ]

    async def _direct_rows(
        runtime_for_owner: Any,
        owner_user_id: str,
    ) -> list[dict]:
        """这个 owner 的**未归档**运行（含多维表格的，调用方自己做完集）。

        `exclude_archived` 由仓储在 SQL 里过滤；MVP / 测试替身没有这个方法时退回
        不过滤（宁可多看到几条，也不要因为增量能力缺失把整个列表读空）。
        """
        lister = getattr(
            getattr(runtime_for_owner, "repository", None), "list_runs", None
        )
        if not callable(lister):
            return []
        try:
            return await lister(
                owner_user_id=owner_user_id, exclude_archived=True
            )
        except TypeError:
            return await lister(owner_user_id=owner_user_id)

    def _direct_group_key(row: dict) -> str:
        """一条直连运行属于哪条任务：同一份文档 = 同一条任务（与前端分组键一致）。"""
        return _direct_record_id(str(row.get("source_url") or "")) or (
            f"run:{row.get('run_id')}"
        )

    def _direct_groups(rows: list[dict]) -> list[list[dict]]:
        """按任务分组，任务之间按"最近更新"倒序，任务内部也倒序（最新版本在最前）。"""
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(_direct_group_key(row), []).append(row)
        ordered = list(groups.values())
        for group in ordered:
            group.sort(
                key=lambda item: str(item.get("updated_at") or ""), reverse=True
            )
        ordered.sort(
            key=lambda group: str(group[0].get("updated_at") or ""), reverse=True
        )
        return ordered

    async def _direct_group_rows(
        runtime_for_owner: Any,
        owner_user_id: str,
        run_id: str,
    ) -> list[dict]:
        """给定任一版本，取它**整条任务**（同一份文档）的全部版本。"""
        rows = await _direct_rows(runtime_for_owner, owner_user_id)
        target = next(
            (row for row in rows if str(row.get("run_id") or "") == run_id),
            None,
        )
        if target is None:
            # 已经归档的版本不在"未归档"集合里：从回收站里再找一次，
            # 否则「删除已归档任务」与「恢复」都会找不到自己那条。
            archived = await _direct_archived_rows(runtime_for_owner, owner_user_id)
            target = next(
                (row for row in archived if str(row.get("run_id") or "") == run_id),
                None,
            )
            if target is None:
                raise RunNotFound("运行不存在")
            rows = archived
        key = _direct_group_key(target)
        return [row for row in rows if _direct_group_key(row) == key]

    async def _direct_archived_rows(
        runtime_for_owner: Any,
        owner_user_id: str,
    ) -> list[dict]:
        lister = getattr(
            getattr(runtime_for_owner, "repository", None),
            "list_archived_runs",
            None,
        )
        if not callable(lister):
            return []
        try:
            return await lister(owner_user_id=owner_user_id)
        except TypeError:
            return await lister()

    async def _set_direct_group_archived(
        runtime_for_owner: Any,
        owner_user_id: str,
        run_id: str,
        *,
        archived: bool,
        auto: bool = False,
    ) -> int:
        """整条直连任务进/出回收站（同一份文档的全部版本一起）。"""
        group = await _direct_group_rows(runtime_for_owner, owner_user_id, run_id)
        run_ids = [str(row.get("run_id") or "") for row in group]
        repository = getattr(runtime_for_owner, "repository", None)
        method = getattr(
            repository,
            "archive_runs" if archived else "restore_runs",
            None,
        )
        if not callable(method):
            raise RunNotFound("运行仓储不支持归档")
        if archived:
            return await method(run_ids, auto=auto)
        return await method(run_ids)

    async def _shared_window_tasks(
        request: Request,
        active_code: Any,
        runtime_for_owner: Any,
        owner_user_id: str,
    ) -> list[dict]:
        """把两类任务归一成同一份「任务」清单（供共用窗口排序用）。

        一条任务 = 一条多维表格记录 / 一份直连文档（同一份文档的历次重跑算同一条），
        `updated_at` 取该任务**最新一版**的时间。任何一步读不到都跳过那一类 ——
        这是增量能力，绝不能让它把写路径带崩。
        """
        tasks: list[dict] = []
        # ── 直连任务：同一份文档 = 一条任务 ──
        # 必须排掉**有多维表格绑定**的运行：它们在下面按记录各算一条，不排就会把同一条
        # 任务数两遍（额度凭空少一格）。
        known: set[str] = set()
        known_run_ids = getattr(active_code, "known_run_ids", None)
        if callable(known_run_ids):
            try:
                result = await known_run_ids(
                    **owner_argument(known_run_ids, owner_user_id)
                )
                if isinstance(result, (set, frozenset)):
                    known = {str(item) for item in result}
            except Exception:  # noqa: BLE001
                known = set()
        try:
            for group in _direct_groups(
                await _direct_rows(runtime_for_owner, owner_user_id)
            ):
                if known and all(
                    str(row.get("run_id") or "") in known for row in group
                ):
                    continue
                newest = group[0]
                run_ids = [str(row.get("run_id") or "") for row in group]
                tasks.append({
                    "kind": "direct",
                    "key": _direct_group_key(newest),
                    "run_id": run_ids[0],
                    "run_ids": run_ids,
                    "updated_at": _iso_timestamp(
                        str(newest.get("updated_at") or "")
                    ),
                    "busy": any(
                        str(row.get("status") or "") in _DIRECT_ACTIVE_STATUSES
                        for row in group
                    ),
                })
        except Exception:  # noqa: BLE001
            pass
        # ── 多维表格任务：一条记录 = 一条任务 ──
        recent_runs = getattr(active_code, "recent_runs", None)
        if callable(recent_runs):
            try:
                bindings = await recent_runs(
                    **owner_argument(recent_runs, owner_user_id)
                )
            except Exception:  # noqa: BLE001
                bindings = []
            by_record: dict[str, list[Any]] = {}
            for binding in bindings or []:
                record_id = str(getattr(binding, "record_id", "") or "")
                if not record_id:
                    continue
                by_record.setdefault(record_id, []).append(binding)
            for record_id, items in by_record.items():
                items.sort(
                    key=lambda item: _iso_timestamp(
                        str(getattr(item, "updated_at", "") or "")
                    ),
                    reverse=True,
                )
                newest = items[0]
                runtime_status = str(
                    getattr(newest, "runtime_status", "") or ""
                ).lower()
                tasks.append({
                    "kind": "bitable",
                    "key": record_id,
                    "run_id": str(getattr(newest, "run_id", "") or ""),
                    "run_ids": [
                        str(getattr(item, "run_id", "") or "") for item in items
                    ],
                    "updated_at": _iso_timestamp(
                        str(getattr(newest, "updated_at", "") or "")
                    ),
                    # 「生成中 / 正在恢复」绝不归档：那条记录是被占着的
                    "busy": runtime_status in _SHARED_WINDOW_BUSY_STATUSES,
                })
        return tasks

    async def _enforce_shared_task_window(
        request: Request,
        active_code: Any,
        runtime_for_owner: Any,
        owner_user_id: str,
        *,
        pinned_run_id: str | None = None,
    ) -> list[str]:
        """**所有任务共用 `_SHARED_TASK_LIMIT` 条额度**：最新的 N 条留在列表里，
        更老的**整条**移进回收站（可恢复）。

        用户 2026-10-30：「两者应该共用十条的次数限制，或者说以后所有任务都共用十条
        次数限制」。以前是各算各的（多维表格 10 条记录 + 直连 10 条任务 = 最多 20 条），
        用户看到的就是"说好的 10 条其实有 20 条"。

        触发点在**写路径**（新建 / 领取 / 重跑，两类都算产生新任务），读接口绝不写库 ——
        与 `production_service.auto_archive_overflow` 同一时机、同一语义（整条进回收站、
        带 `auto_archived` 标记）。

        跳过规则（宁可暂时多于 10 条，也不误伤）：
        * 任务里只要有在跑的版本（`running` / `resuming` / 生成中…）整条跳过；
        * 刚产生的那条（`pinned_run_id`：新建/领取/重跑刚拿到的 run_id）必须保住 ——
          不能被自己这次触发吃掉（按 run_id 判，两类任务通用）；
        * 单条失败不影响其它，也不影响调用方。
        """
        if not _SHARED_TASK_LIMIT:
            return []
        try:
            tasks = await _shared_window_tasks(
                request, active_code, runtime_for_owner, owner_user_id
            )
        except Exception:  # noqa: BLE001
            return []
        tasks.sort(key=lambda item: item["updated_at"], reverse=True)
        archived: list[str] = []
        for task in tasks[_SHARED_TASK_LIMIT:]:
            if task["busy"] or (
                pinned_run_id and str(pinned_run_id) in task["run_ids"]
            ):
                continue
            # 刚恢复的那条不参与这一轮收敛（否则"我刚恢复的又被收回去"）
            if any(
                _is_recently_restored(run_id) for run_id in task["run_ids"]
            ):
                continue
            try:
                if task["kind"] == "direct":
                    archiver = getattr(
                        getattr(runtime_for_owner, "repository", None),
                        "archive_runs",
                        None,
                    )
                    if not callable(archiver):
                        continue
                    await archiver(task["run_ids"], auto=True)
                else:
                    archive_run = getattr(active_code, "archive_run", None)
                    if not callable(archive_run):
                        continue
                    await archive_run(
                        task["run_id"],
                        **owner_argument(archive_run, owner_user_id),
                    )
            except Exception:  # noqa: BLE001
                continue
            archived.append(task["key"])
        return archived

    async def _safe_enforce_shared_task_window(
        request: Request,
        runtime_for_owner: Any,
        owner_user_id: str,
        *,
        pinned_run_id: str | None = None,
        active_bitable: Any = None,
    ) -> list[str]:
        """共用窗口收敛的**安全入口**：任何异常都不许冒到调用方。

        自动回收是增量能力：多维表格没配置（MVP / 测试环境）、仓储不支持归档、
        时间读不出来……都只是"这次不收敛"，绝不能让「生成计划 / 领取任务 / 重跑」
        这些主链路失败（2026-10-30 全量回归里 56 条就是这么红的）。
        """
        if active_bitable is None:
            try:
                active_bitable = get_bitable_service(request)
            except Exception:  # noqa: BLE001
                active_bitable = None
        try:
            with runtime_owner_scope(runtime_for_owner, owner_user_id):
                return await _enforce_shared_task_window(
                    request,
                    active_bitable,
                    runtime_for_owner,
                    owner_user_id,
                    pinned_run_id=pinned_run_id,
                )
        except Exception:  # noqa: BLE001
            return []

    async def _converge_shared_window(
        request: Request,
        runtime_for_owner: Any,
        owner_user_id: str,
    ) -> list[str]:
        """显式收敛（`POST /api/runs/task-window/converge` 用它）。"""
        return await _safe_enforce_shared_task_window(
            request, runtime_for_owner, owner_user_id
        )

    async def _direct_run_payloads(
        request: Request,
        active: Any,
        identity: RequestIdentity,
    ) -> list[dict]:
        """**没有多维表格绑定**的运行 —— 左上角「生成计划」（直接分析需求文档）建的。

        用户 2026-10-30：「我飞书左上角的生成计划的任务怎么在下面历史里找不到」。
        根因：那个列表（`/api/bitable/recent-runs`）只列多维表格绑定，而「生成计划」
        走 `POST /api/runs`、不建绑定，于是这类运行在界面上**完全不可见**（全库 1302
        个运行里只有 6 个是这种，其中一条就卡在「待审批」等人操作）。

        payload 形状与 `_run_payload` 对齐（前端同一套渲染），差别只有：
        `record_id` 是 `direct:<文档token>` 合成键（同一份文档的重跑叠成一行）、
        `mode="direct"`（前端据此给标签、把「重跑/删除」接到直连那套接口）、
        `display_text` 取文档标题。

        任何一步取不到都返回空列表 —— 这是**增量**信息，绝不能因为它读失败让整个
        任务记录列表报错。
        """
        known_run_ids = getattr(active, "known_run_ids", None)
        if known_run_ids is None:  # MVP 模式没有这套存储：不猜，直接不补
            return []
        try:
            known = await known_run_ids(
                **owner_argument(known_run_ids, identity.owner_user_id)
            )
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(known, (set, frozenset)):
            return []
        runtime_for_owner = get_runtime(request)
        try:
            rows = await _direct_rows(
                runtime_for_owner, identity.owner_user_id
            )
        except Exception:  # noqa: BLE001
            return []
        direct_rows = [
            row
            for row in rows
            if str(row.get("run_id") or "") and str(row.get("run_id")) not in known
        ]
        direct_rows.sort(
            key=lambda row: str(row.get("created_at") or ""), reverse=True
        )
        return await _direct_payloads_for_rows(
            runtime_for_owner, identity, direct_rows
        )

    async def _direct_record_runs(
        request: Request,
        identity: RequestIdentity,
        *,
        record_id: str,
        run_id: str,
    ) -> dict:
        """直连任务按「合成记录 id」取**全部历次尝试**（前端成片滑条 + 分组用）。

        多维表格那条走 `production_service.record_runs`（按绑定查）；直连任务没有
        绑定，用**同一份文档**当分组：先定位目标运行，再取该 owner 下 source_url
        相同的全部运行。分组键与列表里的 `record_id` 完全一致，前端一套逻辑通吃。
        """
        runtime_for_owner = get_runtime(request)
        rows = await runtime_for_owner.repository.list_runs(
            owner_user_id=identity.owner_user_id
        )
        target: dict | None = None
        if run_id:
            target = next(
                (row for row in rows if str(row.get("run_id") or "") == run_id),
                None,
            )
        if target is None and record_id:
            target = next(
                (
                    row
                    for row in rows
                    if _direct_record_id(str(row.get("source_url") or "")) == record_id
                ),
                None,
            )
        if target is None:
            raise RunNotFound("运行不存在")
        group_id = _direct_record_id(str(target.get("source_url") or ""))
        group = [
            row
            for row in rows
            if _direct_record_id(str(row.get("source_url") or "")) == group_id
        ]
        group.sort(
            key=lambda row: str(row.get("updated_at") or ""), reverse=True
        )
        return {
            "record_id": group_id,
            "runs": await _direct_payloads_for_rows(
                runtime_for_owner, identity, group
            ),
        }

    @app.get("/api/bitable/record-runs")
    async def list_record_bitable_runs(
        request: Request,
        record_id: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        """**某一条记录**的全部历次尝试（不受「最近任务」窗口限制）。

        `recent-runs` 是给全局列表用的：最多 10 条**任务**、扫描窗口 800 条 run。
        更老记录的历次尝试因此永远进不来（2026-09-24 实测：89 条失败尝试里 45 条
        属于窗口外的老记录）。用户点开某条记录时改用它，按 record_id（或 run_id）
        直接取那条记录的**完整**历史；全局列表的窗口与 payload 一个字都不动。
        """
        active = get_bitable_service(request)
        identity = current_identity(request)
        # 「直接分析需求文档」（左上角「生成计划」）的运行不在生产表里，按 record_id
        # 走 `production_service.record_runs` 必然查不到。用 run_id/合成 record_id
        # 认出这一类，改走直连分支（按同一份文档取全部历次尝试），前端一套逻辑通吃。
        direct_record = str(record_id or "").startswith(_DIRECT_RECORD_PREFIX)
        if not direct_record and run_id:
            try:
                direct_record = (
                    await production_run_kind(
                        active, run_id, identity.owner_user_id
                    )
                    is False
                )
            except Exception:  # noqa: BLE001 - 认不出来就当生产运行，保持旧行为
                direct_record = False
        if direct_record:
            try:
                return await _direct_record_runs(
                    request, identity, record_id=record_id or "", run_id=run_id or ""
                )
            except RunNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        record_runs = getattr(active, "record_runs", None)
        if record_runs is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="当前模式不支持按记录读取历史",
            )
        if not record_id and not run_id:
            raise HTTPException(
                status_code=422,
                detail="需要 record_id 或 run_id",
            )
        try:
            resolved_record_id, bindings = await record_runs(
                record_id=record_id,
                run_id=run_id,
                **owner_argument(record_runs, identity.owner_user_id),
            )
        except Exception as exc:
            raise_bitable_error(exc)
        payload = await _run_payloads(request, active, identity, bindings)
        payload.sort(
            key=lambda item: _iso_timestamp(item.get("updated_at") or ""),
            reverse=True,
        )
        return {"record_id": resolved_record_id, "runs": payload}

    @app.get("/api/runs/{run_id}/take-findings")
    async def get_take_findings(run_id: str, request: Request) -> dict:
        """让能看视频的模型审一遍这一版的成片，返回疑似穿帮清单。

        用户要求（2026-09-18）：「现在重做他会不会自动看视频分析穿帮镜头啊，还是必须
        要我自己找问题」—— 审批/成片确认页点一下就能拿到清单，再一键采纳成返工反馈。
        按 (run_id, 产物签名) 缓存，同一条成片只审一次（每次审片都是一次真实模型调用）。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        with runtime_owner_scope(active, identity.owner_user_id):
            view = await active.get_run_view(run_id)
        artifacts = view.get("artifacts") if isinstance(view, dict) else []
        signature = "|".join(
            str(item.get("artifact_id") or item.get("sha256") or "")
            for item in (artifacts or [])
            if isinstance(item, dict)
        )
        cache_key = (run_id, signature)
        cached = _take_findings_cache.get(cache_key)
        if cached is not None:
            return cached
        # 视图里的产物**不含 local_path**（接口刻意不暴露本地路径）——
        # 用内容接口同一套解析拿到真实文件。
        videos = [
            item
            for item in (artifacts or [])
            if isinstance(item, dict) and item.get("kind") == "video"
        ]
        # 把当初的生成提示词一起给模型，让它**逐条核对**要求做到了没有
        #（用户 2026-09-18：「没有抓住关键点，比如提示词明确了第 1 根枝桠三只绿色小鸟」）。
        tasks = (view.get("approval") or {}).get("tasks") or []
        task_prompt = ""
        for task in tasks:
            if isinstance(task, dict) and str(task.get("prompt") or "").strip():
                task_prompt = str(task["prompt"])
                break
        findings: dict | None = None
        for item in videos[:1]:
            try:
                with runtime_owner_scope(active, identity.owner_user_id):
                    path, mime_type = await active.get_artifact_file(
                        run_id, str(item.get("artifact_id"))
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "审片：解析成片文件失败 run=%s artifact=%s",
                    run_id,
                    item.get("artifact_id"),
                    exc_info=True,
                )
                continue
            findings = await analyze_artifacts(
                getattr(active, "video_analyzer", None),
                [{**item, "local_path": str(path), "mime_type": mime_type}],
                prompt=task_prompt,
            )
            break
        if findings is None:
            findings = {
                "available": False,
                "reason": "这一版没有可分析的成片文件",
                "summary": "",
                "problems": [],
                "uncertainties": [],
            }
        if len(_take_findings_cache) > 40:
            _take_findings_cache.clear()
        _take_findings_cache[cache_key] = findings
        return findings

    @app.get("/api/bitable/archived-runs")
    async def list_archived_bitable_runs(request: Request) -> list[dict]:
        """回收站：**两类任务同一个列表**（多维表格记录 + 直连任务）。

        用户 2026-10-30：「两者改成一套接口」——直连任务以前既没有删除按钮、也不受
        「超过 10 条自动回收」限制，回收站里更是完全看不到它们。现在直连任务与多维
        表格任务走同一套归档语义，于是也在这个列表里，`mode="direct"` 区分。
        """
        active = get_bitable_service(request)
        identity = current_identity(request)
        archived_runs = getattr(active, "archived_runs", None)
        if archived_runs is None:
            return await _archived_direct_payloads(request, identity)
        try:
            bindings = await archived_runs(
                **owner_argument(
                    archived_runs, identity.owner_user_id
                )
            )
        except Exception as exc:
            raise_bitable_error(exc)
        payload: list[dict] = []
        for binding in bindings:
            payload.append(
                {
                    "run_id": binding.run_id,
                    # 回收站是按**记录**去重列出的（一条记录一行）；带上 record_id
                    # 让前端/排障能对上「这是哪条记录的哪一次尝试」。
                    "record_id": getattr(binding, "record_id", None),
                    "display_text": binding.display_text,
                    "status": binding.status.value,
                    "updated_at": _iso_utc_string(getattr(binding, "updated_at", "")),
                    # 自动归档（超出「最近 10 条任务」窗口）vs 用户自己删的。回收站
                    # 靠它换文案：自动归档的行显示「最后更新」，不能写成「删除时间」
                    # —— 用户没删过，写「删除」会让他以为是自己误删的。
                    "auto_archived": bool(
                        getattr(binding, "auto_archived", False)
                    ),
                }
            )
        payload.extend(await _archived_direct_payloads(request, identity))
        payload.sort(
            key=lambda item: _iso_timestamp(item.get("updated_at") or ""),
            reverse=True,
        )
        return payload

    async def _archived_direct_payloads(
        request: Request, identity: RequestIdentity
    ) -> list[dict]:
        """回收站里的**直连任务**：按「同一份文档 = 一条任务」去重列出。

        `run_id` 给**最新那一版**——前端的「恢复」按钮拿它调
        `POST /api/runs/{id}/restore`，后端按同一份文档把整条任务一起放回来。
        """
        runtime_for_owner = get_runtime(request)
        try:
            rows = await _direct_archived_rows(
                runtime_for_owner, identity.owner_user_id
            )
        except Exception:  # noqa: BLE001 - 增量信息，读不到就当没有
            return []
        if not rows:
            return []
        payload: list[dict] = []
        for group in _direct_groups(rows):
            newest = group[0]
            run_id = str(newest.get("run_id") or "")
            if not run_id:
                continue
            display = await _portal_plan_view(
                runtime_for_owner, run_id, str(newest.get("thread_id") or "")
            )
            title = str(display.get("title") or "") or str(
                newest.get("source_url") or ""
            ) or run_id
            payload.append(
                {
                    "run_id": run_id,
                    "record_id": _direct_record_id(
                        str(newest.get("source_url") or "")
                    )
                    or None,
                    "display_text": title,
                    "status": _direct_status_label(
                        str(newest.get("status") or "")
                    ),
                    "updated_at": _iso_utc_string(
                        str(newest.get("updated_at") or "")
                    ),
                    # 自动回收（超出「最近 10 条任务」窗口）vs 用户自己删的：
                    # 与多维表格那边同一字段、同一文案口径。
                    "auto_archived": bool(newest.get("archived_auto")),
                    "mode": "direct",
                    "source_url": str(newest.get("source_url") or ""),
                }
            )
        return payload

    @app.post("/api/runs/task-window/converge")
    async def converge_task_window(request: Request) -> dict[str, object]:
        """**立刻**把共用的 10 条窗口收敛到位（超出的整条进回收站，可恢复）。

        为什么需要这个口子（2026-10-30 用户实测反馈）：「还是 14 条任务啊，没变」——
        收敛本来只在**写路径**触发（新建 / 领取 / 重跑），于是"改成共用 10 条"这个改动
        对**存量**毫无即时效果：用户没做写操作，14 条就一直躺着。这里给一个显式、
        幂等、可反复点的收敛入口（前端在发现超出窗口时也会调一次），让
        "任务记录最多 10 条"这条口径**立刻可见**。

        与写路径同一套逻辑（`_enforce_shared_task_window`）：跳过正在跑的任务、
        也不碰刚恢复的那条（`_RECENT_RESTORE_PIN_SECONDS` 内的 pin）。
        """
        identity = current_identity(request)
        runtime_for_owner = get_runtime(request)
        archived = await _converge_shared_window(
            request, runtime_for_owner, identity.owner_user_id
        )
        return {"archived": archived, "limit": _SHARED_TASK_LIMIT}

    @app.post("/api/runs/{run_id}/archive")
    async def archive_any_run(
        run_id: str, request: Request
    ) -> dict[str, str]:
        """**删除 = 进回收站**（两类任务同一套接口）。

        用户 2026-10-30：「直接分析的任务没有删除按钮…两者改成一套接口」。

        以前是两套：多维表格任务走 `/api/bitable/runs/{id}/archive`（整条记录），
        直连任务**只有硬删**（`DELETE /api/runs/{id}`，逐版本删、删了不可恢复），
        而且直连那行在界面上根本不显示删除按钮。现在前端只调这一对口子，后端按
        「这条运行是不是生产运行」分发：

        * 生产运行 → 复用 `production_service.archive_run`（整条记录）；
        * 直连运行 → 整条**任务**（同一份文档的全部版本）一起进回收站。

        语义统一为「可恢复的归档」，因此不再有硬删路径 —— 硬删会把用户的成片一起
        弄丢，而回收站本来就能满足"从列表里弄走它"。
        """
        active = get_bitable_service(request)
        identity = current_identity(request)
        archive_run = getattr(active, "archive_run", None)
        kind = await production_run_kind(active, run_id, identity.owner_user_id)
        # 分类器说"是生产运行"、或**认不出来**（MVP 模式没有这个能力，返回 None）时
        # 都走绑定表 —— 与 `record-runs` 那条入口同一口径：认不出来时保守当成生产
        # 运行，绝不因为判据缺失去动运行时的归档状态。
        if kind is not False:
            if archive_run is None:
                raise HTTPException(
                    status_code=404, detail="多维表格服务不支持归档"
                )
            try:
                await archive_run(
                    run_id,
                    **owner_argument(archive_run, identity.owner_user_id),
                )
            except (RunNotFound, RunConflict, RunValidationError) as exc:
                raise_runtime_error(exc)
            return {"run_id": run_id, "status": "archived", "mode": "bitable"}
        runtime_for_owner = get_runtime(request)
        try:
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            await _set_direct_group_archived(
                runtime_for_owner,
                identity.owner_user_id,
                run_id,
                archived=True,
            )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "archived", "mode": "direct"}

    @app.post("/api/runs/{run_id}/restore")
    async def restore_any_run(
        run_id: str, request: Request
    ) -> dict[str, str]:
        """**从回收站恢复**（两类任务同一套接口，与上面成对）。"""
        active = get_bitable_service(request)
        identity = current_identity(request)
        restore_run = getattr(active, "restore_run", None)
        kind = await production_run_kind(active, run_id, identity.owner_user_id)
        if kind is not False:
            if restore_run is None:
                raise HTTPException(
                    status_code=404, detail="多维表格服务不支持恢复"
                )
            try:
                await restore_run(
                    run_id,
                    **owner_argument(restore_run, identity.owner_user_id),
                )
            except (RunNotFound, RunConflict, RunValidationError) as exc:
                raise_runtime_error(exc)
            # 同上：刚恢复的多维表格记录也钉住，别被紧接着的收敛立刻收回去。
            _pin_recent_restore([run_id])
            return {"run_id": run_id, "status": "restored", "mode": "bitable"}
        runtime_for_owner = get_runtime(request)
        try:
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            group = await _direct_group_rows(
                runtime_for_owner, identity.owner_user_id, run_id
            )
            await _set_direct_group_archived(
                runtime_for_owner,
                identity.owner_user_id,
                run_id,
                archived=False,
            )
            # 恢复完记住它，别让下一次收敛立刻又把它当"最老的那条"收回去。
            _pin_recent_restore(
                [str(row.get("run_id") or "") for row in group]
            )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "restored", "mode": "direct"}

    @app.post("/api/bitable/runs/{run_id}/archive")
    async def archive_bitable_run(
        run_id: str, request: Request
    ) -> dict[str, str]:
        """保留的旧口子（等价于 `/api/runs/{id}/archive`）—— 外部脚本还在用它。"""
        return await archive_any_run(run_id, request)

    @app.post("/api/bitable/runs/{run_id}/restore")
    async def restore_bitable_run(
        run_id: str, request: Request
    ) -> dict[str, str]:
        """保留的旧口子（等价于 `/api/runs/{id}/restore`）。"""
        return await restore_any_run(run_id, request)

    @app.post(
        "/api/bitable/tasks/{record_id}/claim",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def claim_bitable_task(
        record_id: str,
        request: Request,
        category: ProductionCategory = "animation",
    ) -> BitableClaimResponse:
        active = get_bitable_service(request)
        identity = current_identity(request)
        planning_prompt = await effective_planning_prompt(
            request, identity
        )
        try:
            runtime_for_owner = get_runtime(request)
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                run_id = await active.claim(
                    record_id,
                    category,
                    **owner_argument(active.claim, identity.owner_user_id),
                    **planning_prompt_argument(
                        active.claim, planning_prompt
                    ),
                )
        except Exception as exc:
            raise_bitable_error(exc)
        _invalidate_tasks_cache(active, category)
        # 领取 = 产生新任务 → 收敛**共用**的 10 条窗口（两类任务一起排队，
        # 见 `_enforce_shared_task_window`）。
        await _safe_enforce_shared_task_window(
            request,
            runtime_for_owner,
            identity.owner_user_id,
            pinned_run_id=run_id,
            active_bitable=active,
        )
        return BitableClaimResponse(run_id=run_id)

    @app.post(
        "/api/bitable/runs/{run_id}/retry-delivery",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_bitable_delivery(
        run_id: str, request: Request
    ) -> BitableRetryResponse:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                await active.retry_delivery(
                    run_id,
                    **owner_argument(
                        active.retry_delivery, identity.owner_user_id
                    ),
                )
        except Exception as exc:
            raise_bitable_error(exc, expose_conflict=True)
        return BitableRetryResponse(run_id=run_id)

    @app.post(
        "/api/bitable/runs/{run_id}/rerun",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rerun_bitable_run(
        run_id: str,
        request: Request,
        payload: BitableRerunRequest | None = None,
    ) -> BitableClaimResponse:
        """「重新运行」：复用上一版**已批准的计划**，有返工要求就融合改写提示词，
        然后**停在待审批**等用户确认（用户口径 2026-09-24）。

        body 可选：不带 body = 原样重跑（一个字都不改，也不调模型、不产生费用）；
        带 `feedback` = 先按返工要求融合提示词再进审批页。带 `task_ids` 时只改这几条
        （不传 = 整份计划）。
        """
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            # 直连任务（左上角「生成计划」）没有多维表格绑定：往下走必然
            # 「多维表格运行不存在」。同一套重跑口径，改走运行自己那条入口 ——
            # 前端万一判错通道（2026-10-30 实测：同一个直连任务时对时错），
            # 用户也不该撞上一个看不懂的 404。
            if await production_run_kind(
                active, run_id, identity.owner_user_id
            ) is False:
                return await rerun_direct_run(run_id, request, payload)
            rerun_kwargs: dict[str, Any] = owner_argument(
                active.rerun, identity.owner_user_id
            )
            if payload is not None:
                if payload.task_ids:
                    rerun_kwargs["task_ids"] = list(payload.task_ids)
                feedback = (payload.feedback or "").strip()
                if feedback:
                    rerun_kwargs["feedback"] = feedback
                if payload.duration is not None:
                    # ②-b：本次时长与上一版不同时，不允许提示词原样保留。
                    rerun_kwargs["duration"] = payload.duration
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                new_run_id = await active.rerun(run_id, **rerun_kwargs)
        except Exception as exc:
            raise_bitable_error(exc, expose_conflict=True)
        # 重跑 = 产生新任务 → 收敛共用的 10 条窗口（两类任务一起排队）。
        await _safe_enforce_shared_task_window(
            request,
            runtime_for_owner,
            identity.owner_user_id,
            pinned_run_id=new_run_id,
            active_bitable=active,
        )
        return BitableClaimResponse(run_id=new_run_id)

    @app.post(
        "/api/bitable/runs/{run_id}/rerun-selected",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rerun_selected_bitable_run(
        run_id: str,
        payload: BitableRerunRequest,
        request: Request,
    ) -> BitableClaimResponse:
        """「重跑选中任务」：与「重新运行」**同一条逻辑**，只是带上勾选的任务 id。

        返工要求为空是合法的（原样保留提示词进审批页复核）；有内容才走融合改写。
        两种情形都必须停在待审批 —— 批准之后才真正提交生成。
        """
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            # 同上：直连任务也允许从这条入口进来（「重跑选中任务」与「重新运行」
            # 共用一套口径），别因为通道判错给用户一个 404。
            if await production_run_kind(
                active, run_id, identity.owner_user_id
            ) is False:
                return await rerun_direct_run(run_id, request, payload)
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                new_run_id = await active.rerun(
                    run_id,
                    task_ids=list(payload.task_ids),
                    feedback=(payload.feedback or "").strip(),
                    # ②-b：带上了新时长就必须在校验时对齐（提示词原样保留会被拦）。
                    duration=payload.duration,
                    # 刻意不传 auto_approve：重跑只负责把提示词重写好，
                    # 必须停在审批页让用户过目、改参数/换模型后再放行。
                    **owner_argument(active.rerun, identity.owner_user_id),
                )
        except Exception as exc:
            raise_bitable_error(exc, expose_conflict=True)
        # 重跑选中：同上，收敛共用的 10 条窗口。
        await _safe_enforce_shared_task_window(
            request,
            runtime_for_owner,
            identity.owner_user_id,
            pinned_run_id=new_run_id,
            active_bitable=active,
        )
        return BitableClaimResponse(run_id=new_run_id)
    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(payload: CreateRunRequest, request: Request) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        planning_prompt = await effective_planning_prompt(request, identity)
        domain_request = payload.to_domain().model_copy(
            update={"planning_prompt": planning_prompt}
        )
        with runtime_owner_scope(active, identity.owner_user_id):
            run_id = await active.start_run(domain_request)
        # 「所有任务共用 10 条额度」的触发点在**写路径**（新建/领取/重跑）：产生新任务时
        # 才收敛窗口，读接口绝不写库。刚建出来的这条必须保住，所以 pin 住它。
        await _safe_enforce_shared_task_window(
            request, active, identity.owner_user_id, pinned_run_id=run_id
        )
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
    async def rerun_direct_run(
        run_id: str,
        request: Request,
        payload: BitableRerunRequest | None = None,
    ) -> BitableClaimResponse:
        """「直接分析需求文档」那条通道的重跑（左上角「生成计划」建出来的任务）。

        与多维表格那条重跑**同一套口径**（用户 2026-09-24 定的）：复用上一版
        **已批准的计划** → 返工要求有内容就融合改写提示词、没有就一个字不改 →
        新运行停在**待审批**等人确认。区别只有一个：这类运行没有绑定记录，
        不能走 `production_service.rerun`（它第一步 `get_by_run` 必然找不到），
        所以直接调用运行时那一层。

        为什么以前没有这个入口：直连任务在「任务记录」列表里根本不显示
        （用户 2026-10-30 报「生成计划的任务怎么在下面历史里找不到」）。
        现在列表里能看到它们了，重跑也必须能用 —— 否则点开一条历史任务却没有
        「重新运行」，用户只能重新贴一次链接（重新规划、多花一次模型调用）。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            source = await active.repository.get_run(run_id)
            if source is None:
                raise RunNotFound("运行不存在")
            status_value = str(source.get("status") or "").strip().lower()
            # 与多维表格那条口径一致：允许「已完成 / 部分完成 / 失败 / 已取消 /
            # 待确认成片」（后者是成片审核页的主操作就是重跑）。
            if status_value not in {
                "succeeded",
                "completed_with_errors",
                "failed",
                "cancelled",
                "waiting_review",
            }:
                raise RunConflict("只有已经结束、或成片已生成等待确认的任务可以重跑")
            feedback = (payload.feedback or "").strip() if payload is not None else ""
            task_ids = list(payload.task_ids) if payload is not None else []
            duration = payload.duration if payload is not None else None
            plan_view = await _portal_plan_view(
                active, run_id, str(source.get("thread_id") or "")
            )
            rerun_request = RequirementRequest(
                source_url=str(source.get("source_url") or ""),
                trigger_type="rerun",
                planning_mode=(
                    "image"
                    if str(plan_view.get("planning_mode") or "") == "image"
                    else "video"
                ),
            )
            new_run_id = str(uuid4())
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.clone_run_for_approval(
                    run_id,
                    rerun_request,
                    run_id=new_run_id,
                    thread_id=str(uuid4()),
                    task_ids=task_ids or None,
                    feedback=feedback or None,
                    duration=duration,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        # 同 `create_run`：重跑也算"产生新任务"，在这一步收敛共用窗口。
        await _safe_enforce_shared_task_window(
            request, active, identity.owner_user_id, pinned_run_id=new_run_id
        )
        return BitableClaimResponse(run_id=new_run_id)

    @app.get("/api/portal/history")
    async def portal_history(request: Request, limit: int = 60) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_portal:
            raise HTTPException(status_code=403, detail="仅供 Portal 历史记录使用")
        active = get_runtime(request)
        rows = await active.repository.list_runs(owner_user_id=identity.owner_user_id)
        rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        items: list[dict[str, Any]] = []
        for row in rows[: max(1, min(limit, 200))]:
            run_id = str(row.get("run_id") or "")
            created_at = str(row.get("created_at") or "")
            updated_at = str(row.get("updated_at") or "")
            status_value = str(row.get("status") or "")
            # Portal uses this to decide the "运行中" nav badge and the history
            # status chip.  waiting_approval / waiting_review mean the run is
            # paused on a human decision, not actively generating — they used
            # to collapse into "running", so stale approvals made the Portal
            # sidebar show "6 个运行中" while nothing was being generated.
            if status_value in {"succeeded", "completed_with_errors"}:
                portal_status = "done"
            elif status_value in {"failed", "cancelled", "delivery_failed", "timed_out"}:
                portal_status = "failed"
            elif status_value in {"waiting_approval", "waiting_review"}:
                portal_status = status_value
            else:
                portal_status = "running"
            # 读取成片，供 Portal 历史记录展示产出预览（缩略图 + 下载清单）。
            # 刻意不按 portal_status 设闸门：这套任务有成片时的正常终态是
            # waiting_review（成片已生成、等用户确认），只认 "done" 会让 96% 的
            # 成片永远读不到。成片有没有与状态标签无关，直接读 artifacts，
            # 没有就是空数组（list_artifacts 是只读查询，不写库）。
            artifacts: list[Any] = []
            try:
                artifacts = await active.repository.list_artifacts(run_id)
            except Exception:
                artifacts = []
            ready_artifacts = [
                artifact
                for artifact in artifacts
                if getattr(artifact, "status", None) == "ready"
                and getattr(artifact, "kind", None) in {"image", "video"}
            ]
            results = [
                {
                    "url": f"/api/runs/{run_id}/artifacts/{artifact.artifact_id}/content",
                    "kind": artifact.kind,
                }
                for artifact in ready_artifacts
            ]
            artifact_kinds = {artifact.kind for artifact in ready_artifacts}
            if "video" in artifact_kinds:
                item_kind = "video"
            elif "image" in artifact_kinds:
                item_kind = "image"
            else:
                item_kind = "agent"
            providers = []
            ark_task_ids: list[str] = []
            try:
                operations = await active.repository.list_operations(run_id)
                providers = [
                    str(operation.get("provider") or "").strip()
                    for operation in operations
                    if operation.get("provider")
                ]
                # 方舟任务号（cgt-…）：Portal 拿它去方舟换这次生成的**真实 usage**
                # （token 数 + 分辨率 + 有无参考视频 → 金额）。飞书自己不上报 usage，
                # 这是把飞书任务的 token 补进「使用统计」的唯一可靠途径。
                for operation in operations:
                    official_id = str(operation.get("official_id") or "").strip()
                    if official_id.startswith("cgt-") and official_id not in ark_task_ids:
                        ark_task_ids.append(official_id)
            except Exception:
                providers = []
            model_label = _feishu_run_model_label(active.settings, item_kind, providers)
            # 正文：过去这里硬编码占位串，Portal 上所有飞书任务都显示同一句话。
            # 真实需求正文只在 LangGraph 的 checkpoint 里，这里取出来（带 TTL 缓存）；
            # 取不到才回落占位串 —— 绝不假装有内容。
            # 同一个视图还带出「这次有没有挂参考视频」：方舟那两档单价差 1.6 倍，
            # 而方舟接口看不到输入侧，只有提交方（这里）知道。
            plan_view = await _portal_plan_view(
                active, run_id, str(row.get("thread_id") or "")
            )
            prompt_text = str(plan_view.get("prompt") or "")
            items.append({
                "app": "feishu-generation-agent",
                "job_id": run_id,
                "username": identity.username,
                "kind": item_kind,
                "prompt": prompt_text or "飞书任务 Agent 生成任务",
                "model": model_label,
                "params": {"source_url": str(row.get("source_url") or "")},
                "provider_task_ids": ark_task_ids,
                # 有参考视频 → Portal 按「有输入视频」档结算；取不到就是 False（无档）。
                "has_reference_video": bool(plan_view.get("has_reference_video")),
                "status": portal_status,
                "submitted_at": _iso_timestamp(created_at),                "completed_at": (
                    _iso_timestamp(updated_at)
                    if status_value in GraphRuntime._TERMINAL_STATUSES else None
                ),
                "duration": 0,
                "thumb_url": results[0]["url"] if results else "",
                "results": results,
                "error": "",
            })
        return {"ok": True, "items": items}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        active_bitable = getattr(request.app.state, "bitable_service", None)
        if active_bitable is not None:
            try:
                with runtime_owner_scope(
                    active, identity.owner_user_id
                ):
                    await active_bitable.sync_once(
                        run_id,
                        **owner_argument(
                            active_bitable.sync_once,
                            identity.owner_user_id,
                        ),
                    )
            except RunNotFound:
                pass
            except Exception as exc:
                raise_bitable_error(exc)
        try:
            with runtime_owner_scope(active, identity.owner_user_id):
                view = await active.get_run_view(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        if isinstance(view, dict) and active_bitable is not None:
            if not view.get("result_table_url"):
                try:
                    with runtime_owner_scope(
                        active, identity.owner_user_id
                    ):
                        result_table_url = (
                            await active_bitable.result_table_url(
                                run_id,
                                **owner_argument(
                                    active_bitable.result_table_url,
                                    identity.owner_user_id,
                                ),
                            )
                        )
                except AttributeError:
                    result_table_url = None
                except Exception:
                    _LOGGER.warning(
                        "读取运行结果表链接失败: %s",
                        run_id,
                        exc_info=True,
                    )
                    result_table_url = None
                if result_table_url:
                    view["result_table_url"] = result_table_url
        return view

    @app.post(
        "/api/runs/{run_id}/decision",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def decide_run(
        run_id: str,
        payload: DecisionRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            active_bitable = getattr(request.app.state, "bitable_service", None)
            validate_approval = getattr(active_bitable, "validate_approval", None)
            production_kind = await production_run_kind(
                active_bitable, run_id, identity.owner_user_id
            )
            if (
                payload.action == "approve"
                and callable(validate_approval)
                and production_kind is not False
            ):
                await validate_approval(
                    run_id,
                    **owner_argument(
                        validate_approval, identity.owner_user_id
                    ),
                )
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.resume_run(run_id, payload.to_domain())
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RunValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"run_id": run_id, "status": "accepted"}

    @app.post(
        "/api/runs/{run_id}/artifact-review",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def decide_artifact_review(
        run_id: str,
        payload: ArtifactReviewRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.resume_artifact_review(
                    run_id, payload.to_domain()
                )
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RunValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"run_id": run_id, "status": "accepted"}

    @app.post(
        "/api/runs/{run_id}/retry-failed-assets",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_failed_assets(
        run_id: str, request: Request
    ) -> dict[str, str | int]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                result = await active.retry_failed_assets(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "updated", **result}

    @app.post(
        "/api/runs/{run_id}/retry-delivery",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_delivery(
        run_id: str, request: Request
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.retry_delivery(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "accepted"}

    @app.post(
        "/api/runs/{run_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_run(run_id: str, request: Request) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.cancel_run(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "cancelled"}
    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str, request: Request) -> dict[str, str]:
        active_bitable = getattr(request.app.state, "bitable_service", None)
        active = active_bitable or get_runtime(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            production_kind = await production_run_kind(
                active_bitable, run_id, identity.owner_user_id
            )
            if active_bitable is not None and production_kind is not False:
                with runtime_owner_scope(
                    runtime_for_owner, identity.owner_user_id
                ):
                    await active.delete_run(
                        run_id,
                        **owner_argument(
                            active.delete_run, identity.owner_user_id
                        ),
                    )
            else:
                with runtime_owner_scope(
                    runtime_for_owner, identity.owner_user_id
                ):
                    await runtime_for_owner.delete_run(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "deleted"}

    def raise_runtime_error(exc: Exception) -> None:
        if isinstance(exc, RunNotFound):
            raise HTTPException(status_code=404, detail=str(exc)) from None
        if isinstance(exc, RunConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if isinstance(exc, RunValidationError):
            raise HTTPException(status_code=422, detail=str(exc)) from None
        raise exc

    @app.post(
        "/api/runs/{run_id}/references",
        status_code=status.HTTP_201_CREATED,
    )
    async def add_reference(
        run_id: str,
        request: Request,
        file: UploadFile = File(...),
        task_id: str = Form(...),
        role: str = Form(...),
        order: int = Form(..., ge=1),
        replaces_asset_id: str | None = Form(default=None),
    ) -> dict:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            content = await file.read(active.settings.max_download_bytes + 1)
            if len(content) > active.settings.max_download_bytes:
                raise HTTPException(
                    status_code=422, detail="图片超过大小限制"
                )
            with runtime_owner_scope(active, identity.owner_user_id):
                return await active.add_reference(
                    run_id,
                    task_id=task_id,
                    role=role,
                    order=order,
                    filename=file.filename or "upload",
                    content=content,
                    replaces_asset_id=replaces_asset_id,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @app.patch("/api/runs/{run_id}/tasks/{task_id}/references")
    async def update_references(
        run_id: str,
        task_id: str,
        payload: ReferenceListRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.set_references(
                    run_id,
                    task_id=task_id,
                    references=payload.references,
                    reference_mode=payload.reference_mode,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "updated"}

    @app.patch("/api/runs/{run_id}/tasks/{task_id}")
    async def patch_task(
        run_id: str,
        task_id: str,
        payload: TaskPatchRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.patch_task(
                    run_id,
                    task_id=task_id,
                    patch=payload.patch,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "updated"}

    @app.post("/api/runs/{run_id}/optimize-prompts")
    async def optimize_prompts(
        run_id: str,
        payload: OptimizePromptsRequest,
        request: Request,
    ) -> dict[str, Any]:
        """审批页原地用 AI 重写选中任务的提示词。

        以前审批页只有两条路：手工逐条改，或「退回修改计划」把整份计划重新规划
        （分镜结构可能变、手改内容可能被覆盖）。这里补上中间那条：勾选任务 +
        一句话要求 → 只重写这几条的提示词 → 仍停在审批页给你过目。

        2026-10-22 用户要求：这个按钮**也要看得到改动差异**，且与「重做」**同一个接口口径**
        —— 返回值带上 `comparisons`（与退回重跑同源：改写前版 / 冻结锚点 / 改写后 /
        本次要求）。前端拿到立刻渲染 diff，不必干等轮询那一拍。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                result = await active.optimize_prompts(
                    run_id,
                    task_ids=payload.task_ids,
                    feedback=payload.feedback.strip(),
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {
            "run_id": run_id,
            **result,
            # 没有改写发生时给空表：前端据此不渲染「改动对比」面板（「没改」不能显示成「改了」）。
            "comparisons": result.get("comparisons") or {},
        }

    @app.post("/api/runs/{run_id}/tasks/{task_id}/storyboard/{index}/regenerate")
    async def regenerate_storyboard(
        run_id: str,
        task_id: str,
        index: int,
        request: Request,
    ) -> dict[str, Any]:
        """只重跑某一段的分镜图（方案 A：同一提示词重新采样 K 张，不重新规划）。"""
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                result = await active.regenerate_storyboard_frame(
                    run_id, task_id=task_id, index=index
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return result

    @app.delete("/api/runs/{run_id}/tasks/{task_id}/references/{asset_id}")
    async def unlink_reference(
        run_id: str,
        task_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.unlink_reference(
                    run_id,
                    task_id=task_id,
                    asset_id=asset_id,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "unlinked"}

    @app.post(
        "/api/runs/{run_id}/excluded-assets",
        status_code=status.HTTP_201_CREATED,
    )
    async def exclude_asset(
        run_id: str,
        payload: ExcludedAssetRequest,
        request: Request,
    ) -> dict[str, str]:
        """把用不到的素材排除掉 —— 覆盖门要求人做这个决定，界面得给人入口。"""
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.exclude_asset(run_id, asset_id=payload.asset_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "excluded", "asset_id": payload.asset_id}

    @app.delete("/api/runs/{run_id}/assets/{asset_id}")
    async def remove_asset(
        run_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, str]:
        """把素材从**这次运行**里移除（服务器上的文件保留）。

        用户 2026-11-04：审批页「已排除的素材」那一行要能「删除」。语义是他选的 ——
        从本次运行移除、文件保留；**不是**删文件，也**不是**「从排除里拿回来」。
        「重新挂素材」不需要新接口：把素材挂回某个任务走已有的
        `PATCH /api/runs/{run_id}/tasks/{task_id}/references`，覆盖守恒会自动把它
        从 `excluded_assets` 里剔除。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.remove_asset(run_id, asset_id=asset_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "removed", "asset_id": asset_id}

    @app.get("/api/runs/{run_id}/references/{asset_id}/content")
    async def reference_content(
        run_id: str,
        asset_id: str,
        request: Request,
        original: bool = False,
        version: int | None = None,
    ) -> FileResponse:
        """参考图取图。

        默认给**当前生效的那版**（用户选了清洗版就用清洗版；否则按护栏判定 ——
        采用过才给清洗版），`?original=1` 强制给**原图** —— 前端参考图卡片上的
        「看原图 / 看已去水印版」切换走这个参数（不新增端点）。
        `?version=N` 取台账里**第 N 版清洗版**（重洗会留历史版本，供来回对比）。

        原图永远取得到，是硬要求：用户 2026-10-10 撤掉旧版去水印功能的理由就是
        「它把我的原图改掉了，跟原图不一样」。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                path, mime_type = await active.get_reference_file(
                    run_id,
                    asset_id,
                    prefer_cleaned=not original,
                    version=version,
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return FileResponse(path, media_type=mime_type)

    @app.post("/api/runs/{run_id}/references/{asset_id}/watermark-decision")
    async def set_reference_watermark_decision(
        run_id: str,
        asset_id: str,
        payload: WatermarkDecisionRequest,
        request: Request,
    ):
        """记住用户对这张参考图去水印结果的选择（2026-10-23）。

        用户口径：「可以让用户看到清洗后的图片自由选择原图或者继续重新清洗」。护栏因此
        从**硬拦截**降级为**建议**：`cleaned` = 用过图、决定用清洗版（**即使护栏判了
        `kept` 也照用**），`original` = 用原图，`auto` = 交回护栏自动判定。
        存进台账后，**预览与提交都按它生效**（`effective_cleanup_path` 是唯一收口点）。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                entry = await active.set_reference_cleanup_decision(
                    run_id, asset_id, payload.decision, payload.selected
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"ok": True, "cleanup": entry}

    @app.post("/api/runs/{run_id}/references/{asset_id}/reclean")
    async def reclean_reference(run_id: str, asset_id: str, request: Request):
        """**重新清洗**这张参考图。

        重绘有随机性，重洗是拿到更干净一版的最实际手段 —— **刻意不做局部重绘**：
        用户 2026-10-23「局部重绘拼接的时候会有明显漏洞」。产出新版本（`-nowm-2`…），
        旧版本保留、可在界面上来回切。

        ⚠️ **同步**跑（实测每张 73~90 秒），与产物去水印
        （`.../artifacts/{id}/remove-watermark`）同款。台账会先落一条 `running`，
        所以即使客户端等超时了，轮询运行视图也能拿到最终结果。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                entry = await active.reclean_reference(run_id, asset_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"ok": True, "cleanup": entry}

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}/content")
    async def artifact_content(
        run_id: str,
        artifact_id: str,
        request: Request,
    ) -> FileResponse:
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                path, mime_type = await active.get_artifact_file(
                    run_id, artifact_id
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        suffix = mimetypes.guess_extension(mime_type) or ""
        filename = f"feishu-{artifact_id[:12]}{suffix}"
        return FileResponse(path, media_type=mime_type, filename=filename)

    @app.post("/api/runs/{run_id}/artifacts/{artifact_id}/remove-watermark")
    async def remove_artifact_watermark(run_id: str, artifact_id: str, request: Request):
        """**按按钮才跑**的去水印（2026-10-14 用户口径：「成品加一个按钮，按下才去水印」）。

        水印是每帧同一位置的静态叠加层，提示词里写「不参考水印」对它完全无效（实测三遍无效），
        只能在像素上去。产出另存为 `<原名>-nowatermark.mp4`，**绝不覆盖原产物**
        （原产物文件名是它的 sha256，改了会破坏产物校验）。
        """
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                path, mime_type = await active.get_artifact_file(run_id, artifact_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        if "video" not in str(mime_type or ""):
            raise HTTPException(status_code=400, detail="只有视频产物支持去水印")
        try:
            removal = await asyncio.to_thread(
                remove_video_watermark, Path(path)
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        payload = removal.as_dict()
        payload["url"] = (
            f"/api/runs/{run_id}/artifacts/{artifact_id}/nowatermark-content"
        )
        return payload

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}/nowatermark-content")
    async def artifact_nowatermark_content(
        run_id: str, artifact_id: str, request: Request
    ) -> FileResponse:
        """取「已去过水印」的那份（没按过按钮就 404）。"""
        active = get_runtime(request)
        identity = current_identity(request)
        is_admin = _is_portal_admin(request)
        owner_scope = (
            nullcontext()
            if is_admin
            else runtime_owner_scope(active, identity.owner_user_id)
        )
        try:
            if not is_admin:
                await ensure_owned_run(active, run_id, identity.owner_user_id)
            with owner_scope:
                path, mime_type = await active.get_artifact_file(run_id, artifact_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        target = output_path_for(Path(path))
        if not target.is_file():
            raise HTTPException(status_code=404, detail="还没有生成去水印版本")
        return FileResponse(
            target, media_type="video/mp4",
            filename=f"feishu-{artifact_id[:12]}-nowatermark.mp4",
        )

    asset_library_holder: dict[str, AssetLibraryStore] = {}

    def _get_asset_library_store() -> AssetLibraryStore:
        store = asset_library_holder.get("store")
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="素材库尚未初始化",
            )
        return store

    register_asset_library_routes(app, _get_asset_library_store)
    asset_library_settings.asset_library_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        f"/{asset_library_settings.asset_library_dir.name}",
        StaticFiles(directory=asset_library_settings.asset_library_dir),
        name="asset-library",
    )

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def workspace() -> HTMLResponse:
        return HTMLResponse(
            _render_workspace_html(static_dir),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    return app


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def register_asset_library_routes(
    app: FastAPI,
    store_provider: Callable[[], AssetLibraryStore],
) -> None:
    @app.post(
        "/api/asset-library/assets",
        response_model=AssetLibraryItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_asset_library_item(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        variant: Annotated[str, Form()] = "默认",
        kind: Annotated[str, Form()] = "character",
        description: Annotated[str, Form()] = "",
        aliases: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
        model_prefs: Annotated[str | None, Form()] = None,
        prompt_fragment: Annotated[str, Form()] = "",
    ) -> AssetLibraryItem:
        content = await file.read()
        try:
            asset = await store_provider().create(
                name=name,
                variant=variant,
                kind=AssetKind(kind),
                description=description,
                aliases=_split_csv(aliases),
                tags=_split_csv(tags),
                model_prefs=_split_csv(model_prefs),
                prompt_fragment=prompt_fragment,
                content=content,
                mime_type=file.content_type or "",
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return AssetLibraryItem.from_domain(asset)

    @app.get(
        "/api/asset-library/assets",
        response_model=AssetLibraryListResponse,
    )
    async def list_asset_library_items(
        name: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AssetLibraryListResponse:
        assets = await store_provider().list_all(
            kind=AssetKind(kind) if kind else None,
            name=name,
            limit=limit,
            offset=offset,
        )
        return AssetLibraryListResponse.from_domain(assets)

    @app.patch(
        "/api/asset-library/assets/{asset_id}",
        response_model=AssetLibraryItem,
    )
    async def update_asset_library_item(
        asset_id: str,
        payload: AssetLibraryUpdateRequest,
    ) -> AssetLibraryItem:
        try:
            asset = await store_provider().update(
                asset_id,
                name=payload.name,
                variant=payload.variant,
                kind=AssetKind(payload.kind) if payload.kind else None,
                description=payload.description,
                aliases=payload.aliases,
                tags=payload.tags,
                model_prefs=payload.model_prefs,
                prompt_fragment=payload.prompt_fragment,
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
        return AssetLibraryItem.from_domain(asset)

    @app.delete(
        "/api/asset-library/assets/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_asset_library_item(asset_id: str) -> None:
        if not await store_provider().delete(asset_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
