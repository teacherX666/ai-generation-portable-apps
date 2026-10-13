from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    run_id: str
    thread_id: str
    source_url: str
    planning_prompt: dict[str, Any]
    planning_mode: str
    source_type: str
    source_token: str
    document_id: str
    document_title: str
    document_revision: int
    status: str
    requester_open_id: str | None
    trigger_type: str
    reply_context: dict[str, str]
    requirement_request: dict[str, Any]
    source_document: dict[str, Any]
    normalized_document: dict[str, Any]
    media_assets: list[dict[str, Any]]
    # 规划阶段生成的分镜关键帧候选图（每段 K 张）。**故意不放进 media_assets** ——
    # 那会触发「未覆盖素材」门禁，把审批页卡死；这里单独一条通道给预览与选用。
    storyboard_assets: list[dict[str, Any]]
    source_revision: int
    vision_descriptions: list[dict[str, Any]]
    vision_issues: list[str]
    # 整篇「重新生成计划」（新 run）时，从**同一条需求的历次运行**里继承下来的返工要求。
    # 2026-10-10「铲」任务：新 run 的 state 里没有 draft_plan/approved_plan，
    # `_history_context_for_plan` 因此什么也拿不到，上一轮攒下的要求被清零。
    # 这条通道由 `GraphRuntime._run_to_approval` 在起步时填。
    carried_rework_requirements: list[str]
    draft_revision: int
    draft_plan: dict[str, Any]
    task_plan: dict[str, Any]
    audit_report: dict[str, Any]
    validation_issues: list[str]
    planner_feedback: str | None
    approval_decision: dict[str, Any] | None
    approval_revision: int | None
    approved_tasks: list[dict[str, Any]]
    approved_plan: dict[str, Any] | None
    execution_records: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    artifact_review_decision: dict[str, Any] | None
    artifact_review_feedback: str | None
    delivery_record: dict[str, Any] | None
    last_error: dict[str, Any] | None
    error: dict[str, Any]
