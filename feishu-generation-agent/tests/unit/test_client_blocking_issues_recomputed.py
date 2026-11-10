"""客户端回传的**过期**阻断项不许把用户改对的计划重新判死（2026-10-21 生产事故）。

**生产事故**：飞书任务「刺绣牡丹」（run `c4163b7b`）用户把任务时长改成 10 秒、
服务端 checkpoint 里 `duration=10` 且 `blocking_issues` 已空，可「批准并开始生成」
**连点三次全部秒失败**，日志堆栈落在 `graph/nodes.py:1716` —— 即 `human_approval`
节点里的 `approved_subset()`。

**根因**：审批请求体 `DecisionRequest.tasks` 带着**浏览器那一份任务**，而浏览器是
**乐观更新**（输入框先变、600ms 防抖后才 PATCH），用户改完立刻点批准时，本地那份
任务里仍留着「正文自述时长（10 秒）与任务时长（14 秒）不一致」的旧字符串；
`nodes.human_approval` 用 `reconcile_task_asset_coverage` 把客户端任务**整体替换**
进计划，`blocking_issues` 是**服务端派生字段**却没人重算 → `approved_subset()` 见
非空就抛错。

**修法**：重算下沉到 `domain.reference_contract.refresh_duration_blocking_issues`，
并挂在**所有合并路径共用的** `reconcile_task_asset_coverage` 上。
"""

from __future__ import annotations

from feishu_generation_agent.domain.plan import (
    GenerationTask,
    TaskPlan,
    reconcile_task_asset_coverage,
)

_STALE = (
    "Seedance 提示词正文自述时长（10 秒）与任务时长（14 秒）不一致："
    "请把正文里所有时长表述（如「预计 N 秒」「共 N 秒」）与时间轴一并改成 14 秒，"
    "或把任务时长改回正文写的秒数。"
)


def _task(**updates: object) -> GenerationTask:
    base: dict[str, object] = {
        "task_id": "task-1",
        "task_type": "image_to_video",
        "title": "刺绣牡丹",
        "source_block_ids": ["block-1"],
        "user_intent": "把刺绣牡丹做成动态视频",
        "prompt": "单镜头一镜到底，牡丹刺绣缓缓舒展，共10秒；时间轴 0-10s。",
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "duration": 10,
        "resolution": "720p",
        "output_count": 1,
    }
    base.update(updates)
    return GenerationTask.model_validate(base)


def _plan(**updates: object) -> TaskPlan:
    return TaskPlan(tasks=[_task(**updates)])


def test_client_stale_blocking_issue_is_recomputed_on_merge() -> None:
    """浏览器带着过期阻断项回传 → 合并后必须按当前 prompt/duration 重算成空。

    这条是本次事故的直接回归：不修的话 `approved_subset` 会抛
    「task task-1 has blocking issues and cannot be approved」。
    """
    server_plan = _plan()                     # 服务端：duration=10、正文 10 秒、无阻断
    client_tasks = [
        _task(blocking_issues=[_STALE])       # 浏览器：同一份任务，但带着旧字符串
    ]

    merged = reconcile_task_asset_coverage(server_plan, client_tasks)

    assert merged.tasks[0].blocking_issues == []
    approved = merged.approved_subset(["task-1"], max_output_count=1)
    assert [task.task_id for task in approved.tasks] == ["task-1"]


def test_client_cannot_smuggle_a_real_mismatch_through() -> None:
    """反向守卫：客户端把时长写成 14、正文仍是 10 秒 → 必须**补回**阻断项。

    重算不能变成「客户端说什么就是什么」的后门。
    """
    server_plan = _plan()
    client_tasks = [_task(duration=14, blocking_issues=[])]

    merged = reconcile_task_asset_coverage(server_plan, client_tasks)

    issues = merged.tasks[0].blocking_issues
    assert len(issues) == 1
    assert issues[0].startswith("Seedance 提示词正文自述时长（")
    assert "14 秒" in issues[0]


def test_other_blocking_issues_are_preserved() -> None:
    """别的族原样保留 —— 重算只动「时长」与「格式（悬空引用/空洞）」两族。"""
    other = "结果列已有附件，回写已停止，请先清空结果列再重试"
    server_plan = _plan()
    client_tasks = [_task(blocking_issues=[_STALE, other])]

    merged = reconcile_task_asset_coverage(server_plan, client_tasks)

    assert merged.tasks[0].blocking_issues == [other]


def test_stale_reference_format_blocking_issues_are_dropped() -> None:
    """2026-10-30：悬空引用 / 锚点空洞两族改为**只提示不拦**，存量字符串必须清掉。

    用户原话：「本来就没有图片6，他还是给我阻塞了，把这个阻塞的删了」。实锤 run
    `e92f45bb`：prompt 里只有 `@图片1~@图片4` 与 `@视频1`，快照里的阻塞项却写着
    「提示词里引用了 @图片6」——判据是从**当时**的 prompt 推出来的存量字符串，
    提示词后来修好了也没人重算，`approve()` 见非空即抛错，用户怎么改都批不了。
    """
    dangling = "提示词引用了 @图片6，但本次计划的参考素材里没有对应的图片（上一版可能已被摘掉）"
    hole = "提示词里有被削掉主语的空洞（参考  中…）"
    server_plan = _plan()
    client_tasks = [_task(blocking_issues=[_STALE, dangling, hole])]

    merged = reconcile_task_asset_coverage(server_plan, client_tasks)

    assert merged.tasks[0].blocking_issues == []
    # 清掉之后必须真的能批（这才是用户要的结果）
    approved = merged.approved_subset(["task-1"], max_output_count=1)
    assert [task.task_id for task in approved.tasks] == ["task-1"]


def test_consistent_plan_without_client_tasks_is_untouched() -> None:
    """没有客户端任务回传（`decision.tasks` 为空）时走原计划，不该凭空造阻断项。"""
    server_plan = _plan()
    merged = reconcile_task_asset_coverage(server_plan, server_plan.tasks)
    assert merged.tasks[0].blocking_issues == []


def test_discriminating_power_naive_merge_would_raise() -> None:
    """**判别力自检**：把客户端任务原样并进计划（修复前的行为）必须复现出事故。

    没有这一条，上面那条 `blocking_issues == []` 可能只是「没走到该分支」的假绿。
    """
    import pytest

    client_tasks = [_task(blocking_issues=[_STALE])]
    naive = TaskPlan(
        tasks=client_tasks,
        document_summary="",
        excluded_assets=[],
    )

    with pytest.raises(ValueError) as excinfo:
        naive.approved_subset(["task-1"], max_output_count=1)

    assert "blocking issues" in str(excinfo.value)
