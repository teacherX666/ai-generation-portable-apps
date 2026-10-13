"""参考图去水印：整条自动链路已按用户口径**删除**（2026-10-10）。

用户原话：「现在是不是有根据水印自动重新生成参考图的功能，把这个功能删了，
自动第一轮一定保证原本的参考图」。

**历史（别再重新引入）**：这条功能是 2026-09-22 用户要求加的（「即梦水印还是没去掉」）。
它的行为是**静默替换用户提供的原图**：拿规划器 `reference_plan[].reason` 里的
水印 / 平台标识关键词当判据（`水印 / 即梦 / 角标 / logo / 标识 / 图标 / 播放按钮 /
缩略图 / 封面图` 命中任一个），调图片模型重画一版，只换素材的 `local_path`，
任务名变成 `image-<N>-nowm`，并把 `usable` 改回 `True`。生产实锤：run `0d486f16`
（小猫拍照）的 reason 里就留着「（已用图片模型做最小编辑去水印，可直接作为参考）」。
用户 2026-10-10 改口径：**不要自动重画，自动第一轮一定用文档里那张原图。**

**保留的口径边界**（用户 2026-10-10 明确选「最小范围」）：`usable=False` 的原图
**仍然不进参考图**（`_drop_unusable_references` / `_usable_references` 一行不动）。
删掉重画之后，参考图上的水印可能被成片复现 —— 由提示词里的
「不要出现水印 / logo」约束兜底。

本文件从「功能测试」改成**删除守卫**：任何人把这条链路重新接回来都在这里红。
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from feishu_generation_agent.domain.plan import ReferencePlanItem
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices

SOURCE_URL = "https://fiction.feishu.cn/docx/doc-graph"

#: 规划器判「有水印、不可用」时写下的 reason（真实生产文案，逐字取自 run 0d486f16）。
WATERMARKED_REASON = "右下角带「即梦AI」水印与参数文字，不能直接作为成片参考"


def test_watermark_module_is_gone() -> None:
    """判据模块也一起删 —— 留着它等于半个功能还在。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("feishu_generation_agent.integrations.watermark")


@pytest.mark.parametrize(
    "name",
    [
        "_remove_reference_watermarks",
        "_remove_watermark_with_image_model",
        "_mark_references_usable",
    ],
)
def test_watermark_helpers_are_gone(name: str) -> None:
    from feishu_generation_agent.graph import nodes

    assert not hasattr(nodes, name), f"{name} 必须删除（用户 2026-10-10 口径）"


class _WatermarkPlanner:
    """让规划器把文档里那张图判成「有水印、不可用」。

    签名与 conftest 的 `FakeGraphPlanner` 对齐（`_planner_prompt_argument` 要求
    planner 显式接受 `system_prompt` / `exact_system_prompt`，只写 `**kwargs` 会直接抛错）。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def plan(
        self,
        document: Any,
        descriptions: Any,
        feedback: Any,
        system_prompt: str | None = None,
        exact_system_prompt: str | None = None,
    ) -> Any:
        plan = await self._inner.plan(
            document,
            descriptions,
            feedback,
            system_prompt=system_prompt,
            exact_system_prompt=exact_system_prompt,
        )
        task = plan.tasks[0].model_copy(
            update={
                "reference_plan": [
                    ReferencePlanItem(
                        asset_id="asset-1",
                        role="shot",
                        segment_index=1,
                        usable=False,
                        reason=WATERMARKED_REASON,
                    )
                ]
            }
        )
        return plan.model_copy(update={"tasks": [task]})


class _TolerantGenerator:
    """conftest 的假图片生成器要求显式 `submission_id`，而自动去水印链路不传。

    包一层把 `submission_id` 补上，**好让「去水印真的把原图换掉」这个行为在删除前
    可复现** —— 否则它只是静默失败，测出来的「没换」是假象、测试就没有判别力。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.submit_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def submit(
        self, task: Any, assets: Any, *, submission_id: str | None = None
    ) -> Any:
        self.submit_calls += 1
        return await self._inner.submit(
            task, assets, submission_id=submission_id or uuid4().hex
        )


async def test_first_round_keeps_the_original_reference_image(
    fake_services: GraphServices,
) -> None:
    """自动第一轮必须保证原本的参考图：不重画、不换文件、不偷偷改判可用。"""
    generator = _TolerantGenerator(fake_services.image_generator)
    services = replace(
        fake_services,
        planner=_WatermarkPlanner(fake_services.planner),
        image_generator=generator,
    )
    graph = build_graph(services, InMemorySaver())
    run_id, thread_id = "run-nowm", "thread-nowm"
    await services.repository.create_run(run_id, thread_id, SOURCE_URL)

    await graph.ainvoke(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "source_url": SOURCE_URL,
            "source_revision": 7,
        },
        config={"configurable": {"thread_id": thread_id}},
    )

    # ① 「自动重画」这件事本身必须消失：一次生图调用都不该发生。
    assert generator.submit_calls == 0, "规划阶段仍然调了图片模型（自动去水印还在）"

    snapshot = await graph.aget_state(
        {"configurable": {"thread_id": thread_id}}
    )
    state = dict(snapshot.values or {})

    # ② 素材还是文档里那张原图（sha256 未被替换）。
    assets = state.get("media_assets") or []
    assert assets, "文档素材不该丢"
    assert assets[0]["sha256"] == "graph-sha-asset-1"

    # ③ 规划器判的「不可用」不许被偷偷改回可用、reason 也不许被追加去水印说明。
    plan_task = (state.get("draft_plan") or {}).get("tasks")[0]
    item = plan_task["reference_plan"][0]
    assert item["usable"] is False
    assert item["reason"] == WATERMARKED_REASON
