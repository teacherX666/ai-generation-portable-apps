from enum import StrEnum

from pydantic import BaseModel, Field


class TableTaskStatus(StrEnum):
    PENDING = "待处理"
    PROCESSING = "处理中"
    WAITING_APPROVAL = "待审批"
    REVIEWING = "待确认成片"
    GENERATING = "生成中"
    WRITING_BACK = "回写中"
    COMPLETED = "已完成"
    FAILED = "失败"
    WRITEBACK_FAILED = "回写失败"


class BitableLocation(BaseModel):
    wiki_token: str
    app_token: str | None = None
    table_id: str
    view_id: str
    source_url: str


class BitableTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    status: TableTaskStatus = TableTaskStatus.PENDING
    executor_open_ids: list[str] = Field(default_factory=list)
    executor_names: list[str] = Field(default_factory=list)
    has_result: bool = False
    # 这条记录当前是否已经被领取（以及领到哪一次运行）。#1 之前扫描会把
    # 已领取的记录整个滤掉，用户一点「开始分析」它就消失了；现在改为带着
    # 这两个字段回到列表，前端据此渲染状态徽章并跳回那条运行。
    claim_status: TableTaskStatus | None = None
    claimed_run_id: str | None = None


class BitableBinding(BaseModel):
    app_token: str
    table_id: str
    view_id: str
    record_id: str
    source_url: str
    display_text: str
    run_id: str
    thread_id: str
    claimant_open_id: str
    status: TableTaskStatus
    approval_version: int = Field(default=0, ge=0)
    plan_fingerprint: str | None = None
    reply_context: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None
