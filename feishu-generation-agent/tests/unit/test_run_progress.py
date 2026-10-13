"""运行视图里的「已完成 / 总数」进度。

用户 2026-09-23：「3 个任务完成 2 个，界面还是只显示『生成中』，看不出在动」。
执行阶段的进度**不在 checkpoint 里**（`execution_records` 要等
`execute_selected_tasks` 全部跑完才写回），所以后端按两条边写边更新的通道现算：
`operations`（每个执行单元一条，phase 到终态就是有结果了）与 `artifacts`
（每个执行单元落盘后立刻写库）。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.graph.runtime import GraphRuntime


def _approved(*specs: tuple[str, int]) -> list[dict]:
    return [
        {"task_id": task_id, "output_count": count} for task_id, count in specs
    ]


def _artifact(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id)


def _operation(task_id: str, phase: str) -> dict:
    return {"task_id": task_id, "phase": phase}


def test_no_approved_tasks_means_no_progress() -> None:
    # 还没审批（或老数据）时界面不该显示进度，而不是显示 0/0。
    assert GraphRuntime._generation_progress({}, [], []) is None
    assert (
        GraphRuntime._generation_progress(
            {"approved_tasks": []}, [], []
        )
        is None
    )


def test_progress_counts_finished_tasks() -> None:
    """3 个任务完成 2 个 → 2/3。"""
    state = {"approved_tasks": _approved(("task-1", 1), ("task-2", 1), ("task-3", 1))}
    operations = [
        _operation("task-1", "succeeded"),
        _operation("task-2", "succeeded"),
        _operation("task-3", "submitted"),
    ]

    assert GraphRuntime._generation_progress(state, operations, []) == {
        "completed": 2,
        "total": 3,
    }


def test_artifacts_alone_are_enough() -> None:
    """产物落盘即算完成 —— 不等 phase 更新。"""
    state = {"approved_tasks": _approved(("task-1", 1), ("task-2", 1))}

    assert GraphRuntime._generation_progress(
        state, [], [_artifact("task-1")]
    ) == {"completed": 1, "total": 2}


def test_failed_task_still_counts_as_finished() -> None:
    """失败也要算完成，否则进度永远卡在 N-1/N。"""
    state = {"approved_tasks": _approved(("task-1", 1), ("task-2", 1))}
    operations = [
        _operation("task-1", "failed"),
        _operation("task-2", "running"),
    ]

    assert GraphRuntime._generation_progress(state, operations, []) == {
        "completed": 1,
        "total": 2,
    }


def test_multi_output_tasks_count_every_unit() -> None:
    """一条任务出 3 条候选 = 3 个执行单元（`::output:N`）。"""
    state = {"approved_tasks": _approved(("task-1", 3), ("task-2", 1))}
    operations = [
        _operation("task-1::output:1", "succeeded"),
        _operation("task-1::output:2", "succeeded"),
        _operation("task-1::output:3", "running"),
        _operation("task-2", "submitted"),
    ]

    assert GraphRuntime._generation_progress(state, operations, []) == {
        "completed": 2,
        "total": 4,
    }


def test_progress_never_exceeds_total() -> None:
    """重复计数不会让界面显示 4/3。"""
    state = {"approved_tasks": _approved(("task-1", 1))}
    operations = [_operation("task-1", "succeeded")]
    artifacts = [_artifact("task-1")]

    assert GraphRuntime._generation_progress(state, operations, artifacts) == {
        "completed": 1,
        "total": 1,
    }


def test_unknown_task_ids_are_ignored() -> None:
    """别的运行 / 分镜候选留下的 operation 不该被算进来。"""
    state = {"approved_tasks": _approved(("task-1", 1))}
    operations = [
        _operation("task-1-sb1-1", "succeeded"),
        _operation("image-1-nowm", "succeeded"),
    ]

    assert GraphRuntime._generation_progress(state, operations, []) == {
        "completed": 0,
        "total": 1,
    }
