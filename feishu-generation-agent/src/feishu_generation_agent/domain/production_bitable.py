from pydantic import BaseModel, Field, computed_field

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus


class ProductionSchema(BaseModel):
    requirement_name_field_id: str
    task_type_field_id: str
    requirement_attachment_field_id: str
    project_name_field_id: str
    requester_field_id: str
    maker_field_id: str
    progress_field_id: str


class ProductionSourceSnapshot(BaseModel):
    requirement_name: str
    task_type: str = ""
    requirement_attachment: str
    project_names: list[str] = Field(default_factory=list)
    requester_open_ids: list[str] = Field(default_factory=list)
    requester_names: list[str] = Field(default_factory=list)
    maker_open_ids: list[str] = Field(default_factory=list)
    maker_names: list[str] = Field(default_factory=list)


class ProductionTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    progress: str
    task_type: str = ""
    maker_open_id: str | None = None
    maker_name: str | None = None
    snapshot: ProductionSourceSnapshot
    # 已领取的记录不再从扫描结果里剔除，而是把领取状态挂在摘要上，让任务
    # 列表能显示「处理中 / 待审批 / 生成中…」的徽章并跳回对应运行。
    claim_status: TableTaskStatus | None = None
    claimed_run_id: str | None = None

    @computed_field
    @property
    def deliverable(self) -> bool:
        return self.task_type in {"动画类", "真人类", "图片类"}

    @computed_field
    @property
    def delivery_block_reason(self) -> str | None:
        return None if self.deliverable else f"{self.task_type or '未分类'}任务暂未启用"


class ProductionBinding(BaseModel):
    source_location: BitableLocation
    record_id: str
    owner_user_id: str
    source_url: str
    display_text: str
    progress: str
    task_type: str = ""
    maker_open_id: str | None = None
    maker_name: str | None = None
    snapshot: ProductionSourceSnapshot
    run_id: str
    thread_id: str
    status: TableTaskStatus
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    deleted: bool = False
    #: 这一行是被**自动归档**的（超出「最近 10 条任务」窗口后由系统移进回收站），
    #: 而不是用户自己点的「删除」。回收站要靠它把两种情况分开显示：用户看到一条
    #: 自己没印象删过的记录，得能看出是系统按窗口归档的（2026-09-24）。
    auto_archived: bool = False
    #: 这一行是否仍是「当前绑定」（`production_tasks.active`）。读接口靠它判断
    #: 「这次同步有没有真要落库的变化」—— 已经是同一个终态、又已经放掉（active=0）
    #: 的行，再 UPDATE 一次只会刷新 updated_at：用户点开一条失败详情就把它顶到
    #: 「最近任务」最前面（2026-09-24 实测 4 条失败尝试被读接口刷成 04:16:13）。
    active: bool = False
    #: **运行时**状态（succeeded / failed / cancelled / timed_out …）。它与 `status`
    #: 不是一回事：`status` 是多维表格任务状态（已完成 / 失败 / 生成中…），运行时
    #: 的 cancelled 和 failed 在那边都被压成「失败」，前端分不出「这次是被取消的」。
    #: 取不到就留 None，前端照旧显示「未记录失败原因」，绝不猜。
    runtime_status: str | None = None


class ResultTableTarget(BaseModel):
    maker_open_id: str
    maker_name: str
    app_token: str
    table_id: str
    url: str


class ProductionDelivery(BaseModel):
    run_id: str
    result_record_id: str | None = None
    status: str
