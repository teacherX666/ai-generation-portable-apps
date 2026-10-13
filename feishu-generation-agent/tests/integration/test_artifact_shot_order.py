"""Regression: 成片卡片必须按**分镜顺序**返回，并带上镜头/序号标注。

背景（2026-09-23 用户反馈「卡片顺序对不上分镜顺序，不知道该勾哪一条」）：
`list_artifacts` 是按 artifact_id（sha256 哈希）排的，等于随机序。
"""
from dataclasses import replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.plan import AuditReport, TaskPlan
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import GraphRuntime, _artifact_shot_meta


class _MultiTaskPlanner:
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
                        "title": f"镜头 {index} 标题",
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


def test_shot_meta_maps_every_execution_unit(
    fake_services: GraphServices,
) -> None:
    base = fake_services.planner.task
    state = {
        "approved_plan": TaskPlan(
            document_summary="示例",
            tasks=[
                base.model_copy(
                    update={"task_id": "task-1", "title": "镜头一", "output_count": 2}
                ),
                base.model_copy(
                    update={"task_id": "task-2", "title": "镜头二", "output_count": 1}
                ),
            ],
        ).model_dump(mode="json")
    }
    order, meta = _artifact_shot_meta(state)

    assert meta["task-1::output:1"] == {
        "shot_index": 1,
        "shot_total": 2,
        "slot_index": 1,
        "slot_total": 2,
        "task_title": "镜头一",
    }
    assert meta["task-1::output:2"]["slot_index"] == 2
    assert meta["task-2"]["shot_index"] == 2
    assert meta["task-2"]["slot_total"] == 1
    # 位次必须是「镜头顺序 + 产出序号」。
    assert order["task-1::output:1"] < order["task-1::output:2"] < order["task-2"]


def test_shot_meta_survives_a_broken_plan() -> None:
    assert _artifact_shot_meta({}) == ({}, {})
    assert _artifact_shot_meta({"approved_plan": {"tasks": "nope"}}) == ({}, {})


async def test_run_view_lists_artifacts_in_shot_order(
    fake_services: GraphServices,
) -> None:
    services = replace(
        fake_services,
        planner=_MultiTaskPlanner(fake_services.planner, 3),
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-shot-order"
    run_id = "run-shot-order"
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
    await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": [t["task_id"] for t in plan["tasks"]],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )
    await services.repository.create_run(
        run_id, thread_id, "https://fiction.feishu.cn/docx/doc-graph"
    )
    runtime = GraphRuntime(
        graph=graph,
        repository=services.repository,
        file_store=services.file_store,
        settings=services.settings,
        delivery_writer=services.delivery_writer,
    )
    try:
        view = await runtime.get_run_view(run_id)
    finally:
        await runtime.close()

    task_ids = [a["task_id"] for a in view["artifacts"]]
    assert task_ids == ["task-1", "task-2", "task-3"], task_ids
    assert [a["shot_index"] for a in view["artifacts"]] == [1, 2, 3]
    assert all(a["shot_total"] == 3 for a in view["artifacts"])
    assert [a["slot_index"] for a in view["artifacts"]] == [1, 1, 1]
    assert view["artifacts"][0]["task_title"] == "镜头 1 标题"
