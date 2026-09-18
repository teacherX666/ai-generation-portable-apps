from dataclasses import replace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices


class _FusingPlanner:
    """把现有假 planner 包一层，只额外提供 fuse_rework_prompt。"""

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
        assert isinstance(self._fused, str)
        return self._fused


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def test_artifact_review_reruns_only_selected_task(
    fake_services: GraphServices,
) -> None:
    services = replace(
        fake_services,
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-artifact-rerun")
    first = await graph.ainvoke(
        {
            "run_id": "run-artifact-rerun",
            "thread_id": "thread-artifact-rerun",
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
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )
    review = generated["__interrupt__"][0].value
    assert review["status"] == "waiting_review"
    artifact_task_id = review["artifacts"][0]["task_id"]
    submits_before = services.video_generator.submit_calls

    rerun = await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "动作再慢一点，结尾保持稳定",
                "task_ids": [artifact_task_id],
            }
        ),
        config=config,
    )

    rerun_review = rerun["__interrupt__"][0].value
    assert rerun_review["status"] == "waiting_review"
    assert services.video_generator.submit_calls == submits_before + 1
    assert "动作再慢一点" in rerun["approved_tasks"][0]["prompt"]


async def test_second_rework_keeps_first_requirement(
    fake_services: GraphServices,
) -> None:
    """第二次返工不能把第一次修好的要求丢掉——这是返工「净化」的核心。"""
    services = replace(
        fake_services,
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-artifact-rerun-twice")
    first = await graph.ainvoke(
        {
            "run_id": "run-artifact-rerun-twice",
            "thread_id": "thread-artifact-rerun-twice",
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
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )
    review = generated["__interrupt__"][0].value
    first_task_id = review["artifacts"][0]["task_id"]

    await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "手不要僵",
                "task_ids": [first_task_id],
            }
        ),
        config=config,
    )
    after_first_rework = graph.get_state(config).values["approved_plan"]["tasks"][0]
    second_review = (
        await graph.ainvoke(
            Command(
                resume={
                    "action": "adjust",
                    "feedback": "背景太暗",
                    "task_ids": [first_task_id],
                }
            ),
            config=config,
        )
    )["__interrupt__"][0].value
    assert second_review["status"] == "waiting_review"

    state = graph.get_state(config).values
    task = state["approved_plan"]["tasks"][0]
    assert "手不要僵" in task["prompt"]
    assert "背景太暗" in task["prompt"]
    assert task["rework_requirements"] == ["手不要僵", "背景太暗"]
    assert task["rework_base_prompt"] == plan["tasks"][0]["prompt"]
    # 「改前原文」是**上一版**（第一次返工后的提示词），不是最初那一版：
    # 审批页要看到的是「这次改了什么」。
    assert task["rework_previous_prompt"] == after_first_rework["prompt"]


async def test_second_rework_keeps_the_original_frozen_base(
    fake_services: GraphServices,
) -> None:
    """rework_base_prompt 必须一直是「首次返工前」那一版。

    2026-09-17 把融合基准改成「上一版」时，调用方顺手写成
    `"rework_base_prompt": base_prompt` —— 于是它每轮都被覆盖成上一版，
    历史锚点（最初那一版）就没了。生产数据里能看到这个指纹：同一条记录的
    前几版 base_len=1064，到某一版突然变成 1213（＝上一版长度）。
    """
    services = _fusing_services(
        fake_services,
        lambda base, requirements: f"{base}（已融合）",
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-rework-frozen-base")
    task_id = await _drive_to_review(graph, config, "run-rework-frozen-base")
    original = graph.get_state(config).values["approved_plan"]["tasks"][0]["prompt"]

    after_first = await _rework_once(graph, config, task_id)
    await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "背景太暗",
                "task_ids": [task_id],
            }
        ),
        config=config,
    )
    after_second = graph.get_state(config).values["approved_plan"]["tasks"][0]

    # 冻结锚点：两轮之后仍然是最初那一版。
    assert after_first["rework_base_prompt"] == original
    assert after_second["rework_base_prompt"] == original
    # 但「融合基准 / 改前」是上一版，本轮只在它之上叠加。
    assert after_second["rework_previous_prompt"] == after_first["prompt"]
    assert after_second["prompt"] == f"{after_first['prompt']}（已融合）"


async def _drive_to_review(graph, config, run_id: str) -> str:
    """把 run 推进到成片审核暂停点，返回第一条产物的 task_id。"""
    first = await graph.ainvoke(
        {
            "run_id": run_id,
            "thread_id": config["configurable"]["thread_id"],
            "source_url": "https://fiction.feishu.cn/docx/doc-graph",
            "source_revision": 7,
        },
        config=config,
    )
    approved_plan = first["__interrupt__"][0].value["task_plan"]
    generated = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": approved_plan["tasks"],
            }
        ),
        config=config,
    )
    return generated["__interrupt__"][0].value["artifacts"][0]["task_id"]


def _fusing_services(fake_services: GraphServices, fused: object) -> GraphServices:
    return replace(
        fake_services,
        planner=_FusingPlanner(fake_services.planner, fused),
        settings=fake_services.settings.model_copy(
            update={"artifact_review_enabled": True}
        ),
    )


async def _rework_once(graph, config, task_id: str) -> dict:
    await graph.ainvoke(
        Command(
            resume={
                "action": "adjust",
                "feedback": "手不要僵",
                "task_ids": [task_id],
            }
        ),
        config=config,
    )
    return graph.get_state(config).values["approved_plan"]["tasks"][0]


async def test_rework_adopts_ai_fused_prompt_without_marker(
    fake_services: GraphServices,
) -> None:
    services = _fusing_services(
        fake_services,
        lambda base, requirements: f"{base}（已按返工要求融合）",
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-rework-fusion-ok")
    task_id = await _drive_to_review(graph, config, "run-rework-fusion-ok")

    task = await _rework_once(graph, config, task_id)

    assert "【返工要求】" not in task["prompt"]
    assert task["prompt"].endswith("（已按返工要求融合）")
    assert task["rework_requirements"] == ["手不要僵"]


async def test_rework_falls_back_when_fusion_raises(
    fake_services: GraphServices,
) -> None:
    services = _fusing_services(fake_services, RuntimeError("上游 500"))
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-rework-fusion-fail")
    task_id = await _drive_to_review(graph, config, "run-rework-fusion-fail")

    task = await _rework_once(graph, config, task_id)

    assert "【返工要求】手不要僵" in task["prompt"]
    assert task["rework_requirements"] == ["手不要僵"]


async def test_rework_falls_back_when_fusion_adds_reference_token(
    fake_services: GraphServices,
) -> None:
    """融合结果擅自新增素材令牌 → 判为不合约，回退安全拼接。"""
    services = _fusing_services(
        fake_services,
        lambda base, requirements: f"{base} @图片9",
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-rework-fusion-token")
    task_id = await _drive_to_review(graph, config, "run-rework-fusion-token")

    task = await _rework_once(graph, config, task_id)

    assert "【返工要求】手不要僵" in task["prompt"]


async def test_rework_keeps_requirements_in_body_not_in_constraints(
    fake_services: GraphServices,
) -> None:
    """返工**不再往负向约束里加东西**（用户口径 2026-09-17：「不要在约束里加东西，
    直接描述在正文就行」）。

    以前融合顺带产出的 must_avoid 会并进 negative_constraints，一轮一轮堆下去 ——
    否定句堆太多会把"不要出现的东西"反复喂给模型、反而加深印象。现在要求由融合写进
    正文，约束只做存量合并+限量清理。
    """
    services = _fusing_services(
        fake_services,
        lambda base, requirements: {
            "prompt": f"{base}（已按返工要求融合）",
            "must_avoid": ["老头d 不得跑出起跑线", "眼睛不得发光"],
        },
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-rework-must-avoid")
    task_id = await _drive_to_review(graph, config, "run-rework-must-avoid")

    task = await _rework_once(graph, config, task_id)

    # 要求进正文
    assert "已按返工要求融合" in task["prompt"]
    # 约束里不再出现融合产出的必避项
    assert "老头d 不得跑出起跑线" not in task["negative_constraints"]
    assert "眼睛不得发光" not in task["negative_constraints"]
