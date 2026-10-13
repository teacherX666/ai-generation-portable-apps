"""Regression: 「重跑选中任务」必须只重跑勾选的成片，其余成片留在同一版本里。

背景（2026-09-23 用户反馈「多条视频重跑时只选择部分视频…没能准确选择」）：
界面是按**每条成片**勾选的，后端却按 base task id 展开成「整个任务的全部产出」，
于是勾 1 条会把同任务另外几条一起重跑。
"""
from dataclasses import replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.domain.plan import AuditReport, TaskPlan
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices


class _MultiTaskPlanner:
    """在 fake planner 外面套一层，返回多个任务（每条一个镜头）。"""

    def __init__(self, inner: Any, task_count: int) -> None:
        self._inner = inner
        self._task_count = task_count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def plan(
        self,
        document,
        descriptions,
        feedback,
        system_prompt=None,
        exact_system_prompt=None,
    ) -> TaskPlan:
        del document, descriptions, feedback, system_prompt, exact_system_prompt
        base = self._inner.task
        return TaskPlan(
            tasks=[
                base.model_copy(
                    update={
                        "task_id": f"task-{index}",
                        "title": f"镜头 {index}",
                        "prompt": f"蓝色纸船第 {index} 段漂向远处",
                    }
                )
                for index in range(1, self._task_count + 1)
            ],
            document_summary="纸船连续漂流视频",
        )

    async def audit(self, document, plan) -> AuditReport:
        del document, plan
        return AuditReport()


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def _drive(services, thread_id: str, run_id: str, task_count: int):
    graph = build_graph(services, InMemorySaver())
    config = _config(thread_id)
    first = await graph.ainvoke(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "source_url": "https://fiction.feishu.cn/docx/doc-graph",
            "source_revision": 7,
        },
        config=config,
    )
    plan = first["__interrupt__"][0].value["task_plan"]
    generated = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": [t["task_id"] for t in plan["tasks"]],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )
    return graph, config, generated["__interrupt__"][0].value


def _review_services(fake_services: GraphServices) -> GraphServices:
    return replace(
        fake_services,
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )


async def test_rerun_keeps_the_other_shots_in_the_same_version(
    fake_services: GraphServices,
) -> None:
    services = _review_services(fake_services)
    services = replace(
        services,
        planner=_MultiTaskPlanner(services.planner, 3),
    )
    graph, config, review = await _drive(
        services, "thread-keep-shots", "run-keep-shots", 3
    )
    ids = sorted(a["task_id"] for a in review["artifacts"])
    assert ids == ["task-1", "task-2", "task-3"], ids

    before = services.video_generator.submit_calls
    rerun = await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "第 2 个镜头里纸船穿帮了，重做",
                "task_ids": ["task-2"],
            }
        ),
        config=config,
    )
    after = services.video_generator.submit_calls

    # 只重跑勾中的那一条。
    assert after - before == 1, f"勾了 1 条，却 submit 了 {after - before} 次"

    # 其余成片留在**同一个 run** 里，最终交付仍是完整的三镜头。
    final = rerun["__interrupt__"][0].value
    final_ids = sorted(a["task_id"] for a in final["artifacts"])
    assert final_ids == ["task-1", "task-2", "task-3"], final_ids
    assert final["status"] == "waiting_review"
    assert final["run_id"] == "run-keep-shots"

    # 没勾的两条成片文件没被动过（artifact_id 不变 = 还是原来那条）。
    old = {a["task_id"]: a["artifact_id"] for a in review["artifacts"]}
    new = {a["task_id"]: a["artifact_id"] for a in final["artifacts"]}
    assert new["task-1"] == old["task-1"]
    assert new["task-3"] == old["task-3"]


async def test_partial_output_selection_keeps_the_untouched_samples(
    fake_services: GraphServices,
) -> None:
    """一个镜头产出 4 条，只勾第 2 条：其余 3 条必须原样保留。"""
    services = _review_services(fake_services)
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-partial-output")
    first = await graph.ainvoke(
        {
            "run_id": "run-partial-output",
            "thread_id": "thread-partial-output",
            "source_url": "https://fiction.feishu.cn/docx/doc-graph",
            "source_revision": 7,
        },
        config=config,
    )
    plan = first["__interrupt__"][0].value["task_plan"]
    tasks = [{**plan["tasks"][0], "output_count": 4}]
    generated = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": tasks,
            }
        ),
        config=config,
    )
    review = generated["__interrupt__"][0].value
    ids = [a["task_id"] for a in review["artifacts"]]
    assert ids == [f"task-video::output:{i}" for i in range(1, 5)], ids

    before = services.video_generator.submit_calls
    rerun = await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "第 2 条里纸船穿帮了",
                "task_ids": [ids[1]],
            }
        ),
        config=config,
    )
    after = services.video_generator.submit_calls
    assert after - before == 1, f"勾了 1 条，却 submit 了 {after - before} 次"

    final = rerun["__interrupt__"][0].value
    final_ids = sorted(a["task_id"] for a in final["artifacts"])
    assert final_ids == sorted(ids), final_ids
    assert final["status"] == "waiting_review"
