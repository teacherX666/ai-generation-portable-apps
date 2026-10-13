"""整篇「重新生成计划」必须继承同一条需求历次运行的返工要求（2026-10-10「铲」任务）。

真实现象（生产 run `e3ac684b`，多维表格任务「铲」）：用户点「重新生成计划」→ 新 run
从 `ingest_source → normalize_document → analyze_images → plan_requirements` 重跑
（事件流里**没有** `clone_approved_plan`）→ 新任务的 `rework_requirements` 变成**空列表**，
上一轮攒下的「蜡是比较硬化状态的」直接消失。两小时后用户只能重新说一遍
「蜡的质感是明显硬化的，严格遵循参考视频」。

`_history_context_for_plan` 本身没问题，缺的是**跨 run** 这一段：它只能从**本 run** 的
`draft_plan` / `approved_plan` 取历史，而新 run 两条通道都是空的。

这里用真实图 + 真实的 fake planner 把三段链路钉死：
① 老 run 的检查点里留着一条返工要求；
② 同一个 `source_url` 起一个新 run（重新规划）；
③ 新计划的任务里必须带着这条要求，并且事件里看得见「继承」。
"""

import asyncio
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import GraphRuntime

SOURCE_URL = "https://fiction.feishu.cn/docx/doc-graph"

#: 用户在返工里真正说过的那一句（缩写版）。
REQUIREMENT = "蜡是比较硬化状态的，铲的是最右侧的红色"


def _runtime(services: GraphServices) -> tuple[GraphRuntime, Any]:
    graph = build_graph(services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=services.repository,
        file_store=services.file_store,
        settings=services.settings,
        delivery_writer=services.delivery_writer,
    )
    return runtime, graph


async def _run_to_approval(
    runtime: GraphRuntime, *, run_id: str, thread_id: str
) -> None:
    await runtime.start_run(
        RequirementRequest(source_url=SOURCE_URL),
        run_id=run_id,
        thread_id=thread_id,
    )
    for _ in range(200):
        view = await runtime.get_run_view(run_id)
        if view["status"] == "waiting_approval":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{run_id} 没有到达待审批")


async def _plan_tasks(runtime: GraphRuntime, graph: Any, thread_id: str) -> list[dict]:
    snapshot = await graph.aget_state(runtime._config(thread_id))
    plan = (snapshot.values or {}).get("draft_plan") or {}
    return list(plan.get("tasks") or [])


async def _seed_rework_requirement(
    runtime: GraphRuntime, graph: Any, *, thread_id: str
) -> None:
    """把一条返工要求写进某个 run 的检查点（生产里这是返工融合写进去的）。"""
    config = runtime._config(thread_id)
    snapshot = await graph.aget_state(config)
    state = dict(snapshot.values or {})
    plan = state.get("draft_plan") or state.get("task_plan")
    plan["tasks"][0]["rework_requirements"] = [REQUIREMENT]
    state["draft_plan"] = plan
    await runtime._graph_aupdate_state(config, state, as_node="validate_plan")


async def test_fresh_replan_inherits_rework_requirements_from_a_previous_run(
    fake_services: GraphServices,
) -> None:
    runtime, graph = _runtime(fake_services)
    await _run_to_approval(runtime, run_id="run-old", thread_id="thread-old")
    await _seed_rework_requirement(runtime, graph, thread_id="thread-old")

    # 同一条需求、新 run —— 这就是「重新生成计划」。
    await _run_to_approval(runtime, run_id="run-new", thread_id="thread-new")

    tasks = await _plan_tasks(runtime, graph, "thread-new")
    assert tasks, "新 run 没有产出计划"
    assert REQUIREMENT in (tasks[0].get("rework_requirements") or []), (
        "整篇重新规划把历史返工要求清零了 —— 用户会看到「上一版修好的又变回去」"
    )

    events = [
        event
        for event in (await runtime.get_run_view("run-new"))["events"]
        if event.get("node") == "plan_requirements"
    ]
    assert any("继承" in (event.get("summary") or "") for event in events), (
        "继承必须留下可见事件，否则没人看得出历史到底带没带过来"
    )


async def test_brand_new_requirement_gets_no_inherited_history(
    fake_services: GraphServices,
) -> None:
    """全新需求没有历史 —— 不能凭空塞一份进来。"""
    runtime, graph = _runtime(fake_services)

    await _run_to_approval(runtime, run_id="run-first", thread_id="thread-first")

    tasks = await _plan_tasks(runtime, graph, "thread-first")
    assert tasks
    assert not (tasks[0].get("rework_requirements") or [])


async def test_history_from_another_requirement_is_not_inherited(
    fake_services: GraphServices,
) -> None:
    """别的需求文档的返工要求不能被搬过来。"""
    runtime, graph = _runtime(fake_services)
    await _run_to_approval(runtime, run_id="run-other", thread_id="thread-other")
    await _seed_rework_requirement(runtime, graph, thread_id="thread-other")

    # 换一条 source_url（同一张图/同一个文档 id，但需求链接不同）。
    await runtime.start_run(
        RequirementRequest(source_url="https://fiction.feishu.cn/docx/doc-else"),
        run_id="run-new-source",
        thread_id="thread-new-source",
    )
    for _ in range(200):
        view = await runtime.get_run_view("run-new-source")
        if view["status"] == "waiting_approval":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("run-new-source 没有到达待审批")

    tasks = await _plan_tasks(runtime, graph, "thread-new-source")
    assert tasks
    assert not (tasks[0].get("rework_requirements") or [])
