from dataclasses import replace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices


async def test_probe_reopening_completed_review(
    fake_services: GraphServices,
) -> None:
    services = replace(
        fake_services,
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )
    graph = build_graph(services, InMemorySaver())
    config = {"configurable": {"thread_id": "thread-reopen-review"}}
    first = await graph.ainvoke(
        {
            "run_id": "run-reopen-review",
            "thread_id": "thread-reopen-review",
            "source_url": "https://fiction.feishu.cn/docx/doc-graph",
            "source_revision": 7,
        },
        config=config,
    )
    plan = first["__interrupt__"][0].value["task_plan"]
    review = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )
    assert review["__interrupt__"][0].value["status"] == "waiting_review"
    completed = await graph.ainvoke(
        Command(resume={"action": "confirm"}),
        config=config,
    )
    assert completed["status"] == "succeeded"

    await graph.aupdate_state(
        config,
        {"status": "waiting_review"},
        as_node="verify_and_download_artifacts",
    )
    reopened = await graph.ainvoke(None, config=config)
    assert reopened["__interrupt__"][0].value["status"] == "waiting_review"
