from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from feishu_generation_agent.domain.asset_library import CharacterAsset
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    ArtifactReviewDecision,
    GenerationTask,
    ImageReference,
    ReferenceMode,
)
from feishu_generation_agent.integrations.feishu_source import parse_feishu_url


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1)
    # 直连文档时由调用方声明产出类型；多维表格来的 run 走 binding 判定。
    planning_mode: Literal["video", "image"] = "video"

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        normalized = value.strip()
        source_type, token = parse_feishu_url(normalized)
        hostname = urlsplit(normalized).hostname
        if hostname is None:
            raise ValueError("飞书文档链接缺少域名")
        return f"https://{hostname.lower()}/{source_type.value}/{quote(token, safe='')}"

    def to_domain(self) -> RequirementRequest:
        return RequirementRequest(
            source_url=self.source_url.strip(),
            planning_mode=self.planning_mode,
        )


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "cancel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    tasks: list[GenerationTask] = Field(default_factory=list)
    feedback: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DecisionRequest":
        if self.action == "approve":
            if not self.selected_task_ids:
                raise ValueError("批准时必须选择至少一个任务")
            if len(self.selected_task_ids) != len(set(self.selected_task_ids)):
                raise ValueError("不能重复选择同一任务")
            if self.feedback is not None:
                raise ValueError("批准时不能提交退回意见")
        elif self.action == "reject":
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("退回重新规划时必须填写意见")
            if self.selected_task_ids or self.tasks:
                raise ValueError("退回时不能提交已选任务")
        elif self.selected_task_ids or self.tasks or self.feedback is not None:
            raise ValueError("取消时不能提交任务或意见")
        return self

    def to_domain(self) -> ApprovalDecision:
        return ApprovalDecision.model_validate(self.model_dump(mode="json"))


class BitableRerunRequest(BaseModel):
    """重跑请求（「重新运行」/「重跑选中任务」共用一套）。

    与 `ArtifactReviewRequest` 的关键差别：**返工要求可以为空**。用户口径
    （2026-09-24）：「如果返工要求里没内容则是直接不改提示词原封不动让我重新审核」
    —— 空要求是一次合法的「原样重跑」，不是错误，所以这里不能像成片审核那样
    强制 `feedback` 非空。

    `action` 保留可选：老前端/老脚本发的是成片审核那套 body（带
    `action="adjust"`），不能因为多了一个字段就 422。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["adjust"] = "adjust"
    feedback: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    #: 2026-10-10（②-b）：本次重跑要用的时长。
    #:
    #: 给了它且与被复用计划的时长不同时，`clone_run_for_approval` 会**拒绝**把上一版
    #: 提示词原样保留（置 blocking_issues + 人话提示），因为提示词里的时间轴与
    #: 「预计 N 秒」全是按旧时长写的（实测后果：成片前 4.08 秒画面完全冻结）。
    #: 审批页改时长走的是任务 PATCH + 提交前的 `duration_consistency_issues`（②-a），
    #: 这个字段是给 API / 脚本调用方的对称入口；不传 = 沿用上一版时长。
    duration: int | None = Field(default=None, ge=4, le=60)

    @model_validator(mode="after")
    def validate_task_ids(self) -> "BitableRerunRequest":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("不能重复选择同一任务")
        return self


class ArtifactReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm", "adjust", "cancel"]
    feedback: str | None = None
    task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ArtifactReviewRequest":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("不能重复选择同一任务")
        if self.action == "adjust":
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("退回调整时必须填写调整意见")
        elif self.feedback is not None or self.task_ids:
            raise ValueError("确认或取消时不能携带调整意见或任务")
        return self

    def to_domain(self) -> ArtifactReviewDecision:
        return ArtifactReviewDecision.model_validate(
            self.model_dump(mode="json")
        )


class ReferenceListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: list[ImageReference] = Field(min_length=1)
    reference_mode: ReferenceMode | None = None


class ExcludedAssetRequest(BaseModel):
    """把一个用不到的素材排除掉。

    覆盖门要求「每个素材要么被任务引用、要么被排除」，而审批页过去只有引用的增删、
    没有排除入口（2026-09-17 超级大床：image-4..image-8 未覆盖，批准按钮一直灰着）。
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)


class TaskPatchRequest(BaseModel):
    """审批页任务字段热修改。字段白名单在 runtime.patch_task 里再校验一遍。"""

    model_config = ConfigDict(extra="forbid")

    patch: dict[str, Any] = Field(min_length=1)


class OptimizePromptsRequest(BaseModel):
    """审批页原地用 AI 重写选中任务的提示词（不重新规划、不新建运行）。

    勾选复用审批页已有的任务勾选框 —— 勾哪几条就优化哪几条。
    """

    model_config = ConfigDict(extra="forbid")

    task_ids: list[str] = Field(min_length=1)
    feedback: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_selection(self) -> "OptimizePromptsRequest":
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("不能重复选择同一任务")
        if not self.feedback.strip():
            raise ValueError("请填写优化要求")
        return self


class PlannerPromptUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_text: str = Field(min_length=1, max_length=20_000)

    @field_validator("prompt_text")
    @classmethod
    def validate_prompt_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("提示词不能为空白")
        return value


class PlannerPromptResponse(BaseModel):
    mode: Literal["prime", "personal"]
    editable: bool
    prompt_text: str
    version: int = Field(ge=0)
    source: Literal["prime", "personal"]


class ProviderPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_provider: str = Field(min_length=1)
    image_provider: str = Field(min_length=1)
    #: 规划流水线：text（现状）或 multimodal（一次调用把原图/视频交给 ds4.1）。
    planning_pipeline: Literal["text", "multimodal"] = "text"
    #: 每个时间段生成几张分镜关键帧候选（0 = 关闭）。用户 2026-09-22：
    #: 「把 K 的选择放在高级设置里供用户选择」。
    storyboard_candidates: int = Field(default=0, ge=0, le=4)


class ProviderPreferencesResponse(BaseModel):
    video_provider: str
    image_provider: str
    planning_pipeline: Literal["text", "multimodal"] = "text"
    storyboard_candidates: int = 0


class BitableClaimResponse(BaseModel):
    run_id: str


class BitableRetryResponse(BitableClaimResponse):
    status: Literal["accepted"] = "accepted"


class AssetLibraryItem(BaseModel):
    asset_id: str
    name: str
    variant: str
    kind: str
    description: str
    aliases: list[str]
    tags: list[str]
    model_prefs: list[str]
    prompt_fragment: str
    url: str
    mime_type: str
    byte_size: int
    created_at: str
    updated_at: str

    @classmethod
    def from_domain(cls, asset: CharacterAsset) -> "AssetLibraryItem":
        return cls(
            asset_id=asset.asset_id,
            name=asset.name,
            variant=asset.variant,
            kind=asset.kind.value,
            description=asset.description,
            aliases=list(asset.aliases),
            tags=list(asset.tags),
            model_prefs=list(asset.model_prefs),
            prompt_fragment=asset.prompt_fragment,
            url=asset.storage_url,
            mime_type=asset.mime_type,
            byte_size=asset.byte_size,
            created_at=asset.created_at.isoformat(),
            updated_at=asset.updated_at.isoformat(),
        )


class AssetLibraryListResponse(BaseModel):
    items: list[AssetLibraryItem]
    total: int

    @classmethod
    def from_domain(
        cls, assets: list[CharacterAsset]
    ) -> "AssetLibraryListResponse":
        items = [AssetLibraryItem.from_domain(asset) for asset in assets]
        return cls(items=items, total=len(items))


class AssetLibraryUpdateRequest(BaseModel):
    name: str | None = None
    variant: str | None = None
    kind: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None
    model_prefs: list[str] | None = None
    prompt_fragment: str | None = None
