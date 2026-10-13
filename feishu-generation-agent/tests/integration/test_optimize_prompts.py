"""审批页「原地用 AI 优化所选任务的提示词」。

2026-09-23 用户要求：「在等待你审核的阶段，也可以使用退回重跑功能优化提示词」。
审批页过去只有两条路 —— 手工逐条改，或「退回修改计划」把整份计划重新规划。
这里补中间那条：勾选任务 + 一句要求 → 只重写这几条的提示词 → 仍停在审批页。
"""
from dataclasses import replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import pytest

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunValidationError,
)


class _FusingPlanner:
    """把 fake planner 包一层，只额外提供 fuse_rework_prompt。"""

    def __init__(self, inner: Any, fused: object) -> None:
        self._inner = inner
        self._fused = fused
        self.fuse_calls: list[tuple[str, list[str]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def fuse_rework_prompt(
        self,
        original_prompt: str,
        requirements: list[str],
    ) -> str | None:
        self.fuse_calls.append((original_prompt, list(requirements)))
        if isinstance(self._fused, Exception):
            raise self._fused
        if callable(self._fused):
            return self._fused(original_prompt, requirements)
        return self._fused


async def _runtime_at_approval(
    services: GraphServices,
    *,
    fused: object,
    run_id: str,
    thread_id: str,
) -> tuple[GraphRuntime, Any]:
    planner = _FusingPlanner(services.planner, fused)
    services = replace(services, planner=planner)
    graph = build_graph(services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=services.repository,
        file_store=services.file_store,
        settings=services.settings,
        delivery_writer=services.delivery_writer,
        rework_fuser=planner.fuse_rework_prompt,
    )
    await services.repository.create_run(
        run_id, thread_id, "https://fiction.feishu.cn/docx/doc-graph"
    )
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "source_url": "https://fiction.feishu.cn/docx/doc-graph",
            "source_revision": 7,
        },
        config=config,
    )
    # 生产里这一步由运行时写库；直接驱动图时要自己补，否则 _waiting_state 会拒。
    await services.repository.update_run_status(run_id, "waiting_approval")
    return runtime, graph


async def test_optimize_prompts_rewrites_in_place_and_stays_at_approval(
    fake_services: GraphServices,
) -> None:
    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: f"{base}（已按「{reqs[-1]}」改写）",
        run_id="run-optimize",
        thread_id="thread-optimize",
    )
    try:
        view = await runtime.get_run_view("run-optimize")
        assert view["status"] == "waiting_approval"
        before = view["approval"]["tasks"][0]["prompt"]

        result = await runtime.optimize_prompts(
            "run-optimize",
            task_ids=["task-video"],
            feedback="动作再慢一点，结尾保持稳定",
        )

        assert result["optimized_task_ids"] == ["task-video"]
        assert result["unchanged_task_ids"] == []

        after = await runtime.get_run_view("run-optimize")
        task = after["approval"]["tasks"][0]
        # 提示词被 AI 改写了，而且**没有**重新规划、没有新 run。
        assert task["prompt"] != before
        assert task["prompt"].endswith("（已按「动作再慢一点，结尾保持稳定」改写）")
        assert task["rework_requirements"] == ["动作再慢一点，结尾保持稳定"]
        assert task["rework_base_prompt"] == before
        assert task["rework_previous_prompt"] == before
        # 仍停在审批页，可以继续改 / 批准。
        assert after["status"] == "waiting_approval"
        assert after["run_id"] == "run-optimize"
        # 其它字段没被动过。
        assert task["task_id"] == "task-video"
        assert task["duration"] == view["approval"]["tasks"][0]["duration"]
    finally:
        await runtime.close()


async def test_optimize_prompts_accumulates_requirements_across_rounds(
    fake_services: GraphServices,
) -> None:
    """连续优化两次：要求只累积不覆盖，融合基准是上一版。"""
    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused=lambda base, reqs: f"{base}+{reqs[-1]}",
        run_id="run-optimize-twice",
        thread_id="thread-optimize-twice",
    )
    try:
        await runtime.optimize_prompts(
            "run-optimize-twice", task_ids=["task-video"], feedback="手不要僵"
        )
        first = (await runtime.get_run_view("run-optimize-twice"))["approval"]["tasks"][0]
        await runtime.optimize_prompts(
            "run-optimize-twice", task_ids=["task-video"], feedback="背景太暗"
        )
        second = (await runtime.get_run_view("run-optimize-twice"))["approval"]["tasks"][0]

        assert second["rework_requirements"] == ["手不要僵", "背景太暗"]
        # 冻结锚点仍是第一次优化前那一版。
        assert second["rework_base_prompt"] == first["rework_base_prompt"]
        # 融合基准 / 展示用的「改前」是上一版。
        assert second["rework_previous_prompt"] == first["prompt"]
        assert second["prompt"] == f"{first['prompt']}+背景太暗"
    finally:
        await runtime.close()


async def test_optimize_prompts_feeds_only_the_new_requirement_to_the_fuser(
    fake_services: GraphServices,
) -> None:
    """融合只吃**本轮**要求，历史不再重喂（用户 2026-09-29：「把累积的规则删了」）。

    生产实测（爱心气球 `1c84009e`）：清单攒到 8 条时 #6「女孩手保持握紧，但气球绳仍
    从手中滑脱」与 #8「抬手的同时松开气球绳子」**互斥**，而融合规则是「每条要求都
    落实、一条都不能遗漏」→ 它把两种说法都写进正文（同一段里既有「同时手松开，
    气球绳子脱手」又有「绳子从她握紧的手掌上方滑脱」），用户看到的仍是「松手太刻意」；
    同一轮它还删掉了上一轮已经写对的 4 条约束。

    历史要求仍累积在 `rework_requirements` 里 —— 重新规划要靠它把历史带过去，记录也
    不能丢；旧修复本身留在**上一版正文**里（融合基准就是上一版）。
    """
    seen: list[list[str]] = []

    def fused(base: str, reqs: list[str]) -> str:
        seen.append(list(reqs))
        return f"{base}+{reqs[-1]}"

    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused=fused,
        run_id="run-optimize-scoped",
        thread_id="thread-optimize-scoped",
    )
    try:
        await runtime.optimize_prompts(
            "run-optimize-scoped", task_ids=["task-video"], feedback="手不要僵"
        )
        await runtime.optimize_prompts(
            "run-optimize-scoped", task_ids=["task-video"], feedback="背景太暗"
        )

        task = (await runtime.get_run_view("run-optimize-scoped"))["approval"]["tasks"][0]

        # 融合只看到本轮那一条（不再把「手不要僵」重喂第二遍）
        assert seen == [["手不要僵"], ["背景太暗"]]
        # 但记录仍然累积（重新规划要用）
        assert task["rework_requirements"] == ["手不要僵", "背景太暗"]
    finally:
        await runtime.close()


async def test_optimize_prompts_falls_back_when_fusion_fails(
    fake_services: GraphServices,
) -> None:
    """融合不可用不能让优化本身失败：正文保持原样，要求仍被记下来。"""
    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused=RuntimeError("上游 500"),
        run_id="run-optimize-fusion-fail",
        thread_id="thread-optimize-fusion-fail",
    )
    try:
        before = (await runtime.get_run_view("run-optimize-fusion-fail"))["approval"]["tasks"][0]
        result = await runtime.optimize_prompts(
            "run-optimize-fusion-fail",
            task_ids=["task-video"],
            feedback="动作再慢一点",
        )
        after = (await runtime.get_run_view("run-optimize-fusion-fail"))["approval"]["tasks"][0]

        assert after["prompt"] == before["prompt"]
        assert after["rework_requirements"] == ["动作再慢一点"]
        # 融合没生效要如实告诉调用方，别让用户以为改好了。
        assert result["optimized_task_ids"] == []
        assert result["unchanged_task_ids"] == ["task-video"]
    finally:
        await runtime.close()


async def test_optimize_prompts_rejects_bad_input(
    fake_services: GraphServices,
) -> None:
    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused="unused",
        run_id="run-optimize-invalid",
        thread_id="thread-optimize-invalid",
    )
    try:
        with pytest.raises(RunValidationError):
            await runtime.optimize_prompts(
                "run-optimize-invalid", task_ids=["task-video"], feedback="   "
            )
        with pytest.raises(RunValidationError):
            await runtime.optimize_prompts(
                "run-optimize-invalid", task_ids=[], feedback="动作慢一点"
            )
        with pytest.raises(RunValidationError):
            await runtime.optimize_prompts(
                "run-optimize-invalid", task_ids=["task-nope"], feedback="动作慢一点"
            )
    finally:
        await runtime.close()


async def test_optimize_prompts_is_rejected_outside_approval(
    fake_services: GraphServices,
) -> None:
    """只有等待审批的运行能原地优化 —— 成片阶段走「重跑选中任务」。"""
    runtime, graph = await _runtime_at_approval(
        fake_services,
        fused="unused",
        run_id="run-optimize-wrong-stage",
        thread_id="thread-optimize-wrong-stage",
    )
    try:
        await fake_services.repository.update_run_status(
            "run-optimize-wrong-stage", "succeeded"
        )
        with pytest.raises(RunConflict):
            await runtime.optimize_prompts(
                "run-optimize-wrong-stage",
                task_ids=["task-video"],
                feedback="动作慢一点",
            )
    finally:
        await runtime.close()
