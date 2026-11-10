"""「已排除的素材」行上的两个新操作 —— 后端这半边（2026-11-04 用户需求）。

用户原话：「已排除的素材可不可以加一个删除和重新挂素材的功能」。两个语义由他自己选定：

* **删除** = 从**这次运行**里移除该素材，**服务器上的文件保留**（不是删文件、
  也不是"从排除里拿回来"）；
* **重新挂** = 把这一条**恢复并挂回某个任务**（挂上之后自动从「已排除」消失）。

本文件钉四件事：

1. `remove_asset` 之后：素材不在 `media_assets`、不在 `excluded_assets`，
   覆盖门的 `uncovered_count` 也不再把它算进去；
2. 还被任务引用的素材 **拒绝删除**（要人先摘掉 —— 不偷偷改计划）；
3. 只有可编辑状态能改：门禁在 `_waiting_state` 上，`RunConflict` 原样冒出来；
4. **「重新挂」不需要新接口**：`set_references` 把已排除的素材挂回任务后，
   `reconcile_task_asset_coverage` 会自动把它从 `excluded_assets` 里剔除
   （这是 2026-10-13「参考图被自动排除」那套覆盖守恒的回归护栏）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.plan import GenerationTask, ImageReference
from feishu_generation_agent.graph.runtime import GraphRuntime, RunValidationError

_PROMPT = (
    "吉卜力动画风格，9:16 竖屏。\n"
    "镜头 1（0-5s）：卡车运载巨型南瓜在弯道行驶。\n"
    "镜头 2（5-8s）：南瓜滚落砸在马路中央。\n"
    "镜头 3（8-12s）：后方车辆急刹，烟尘扬起。\n"
    "高清，不要水印，不要 logo。"
)


def _asset(asset_id: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="feishu",
        local_path=Path(f"data/runs/run-1/inputs/{asset_id}.png"),
        mime_type="image/png",
        size=10,
        sha256=hashlib.sha256(asset_id.encode()).hexdigest(),
        width=144,
        height=256,
    )


def _approval_state(
    *,
    referenced: tuple[str, ...] = ("image-1",),
    excluded: tuple[str, ...] = ("image-3",),
    assets: tuple[str, ...] = ("image-1", "image-2", "image-3"),
) -> dict:
    """一份「待审批」状态：image-1 被任务引用、image-3 被排除、image-2 谁也不管。"""
    references = [
        {"asset_id": asset_id, "role": "reference_image", "order": order}
        for order, asset_id in enumerate(referenced, start=1)
    ]
    plan_items = [
        {
            "asset_id": asset_id,
            "role": "global",
            "segment_index": None,
            "usable": True,
            "reason": "角色参考图",
        }
        for asset_id in referenced
    ]
    task = GenerationTask.model_validate(
        {
            "task_id": "task-1",
            "task_type": "image_to_video",
            "title": "巨型南瓜",
            "source_block_ids": ["block-1"],
            "user_intent": "生成片头",
            "prompt": _PROMPT,
            "reference_images": references,
            "reference_plan": plan_items,
            "aspect_ratio": "9:16",
            "duration": 12,
            "resolution": "720p",
        }
    )
    return {
        "status": "waiting_approval",
        "draft_plan": {
            "tasks": [task.model_dump(mode="json")],
            "document_summary": "",
            "excluded_assets": [
                {"asset_id": asset_id, "reason": "计划没有具体使用这张素材"}
                for asset_id in excluded
            ],
        },
        "media_assets": [
            _asset(asset_id).model_dump(mode="json") for asset_id in assets
        ],
    }


def _harness(state: dict):
    """绕开 __init__ 造 GraphRuntime，只把「读写状态」换成内存实现（同 test_reference_assembly）。"""
    runtime = object.__new__(GraphRuntime)
    runtime._run_locks = {}
    box = {"state": state, "persisted": []}

    async def waiting_state(run_id: str):
        return {"run_id": run_id, "thread_id": "thread-1"}, box["state"]

    async def persist_draft(run, state, plan, assets, extra_updates=None):
        payload = plan.model_dump(mode="json")
        box["state"] = {
            **state,
            "draft_plan": payload,
            "task_plan": payload,
            "media_assets": [asset.model_dump(mode="json") for asset in assets],
            **(extra_updates or {}),
        }
        box["persisted"].append(box["state"])

    runtime._waiting_state = waiting_state
    runtime._persist_draft = persist_draft
    return runtime, box


def _asset_ids(state: dict) -> set[str]:
    return {item["asset_id"] for item in state.get("media_assets") or []}


def _excluded_ids(state: dict) -> set[str]:
    return {
        item["asset_id"] for item in state["draft_plan"].get("excluded_assets") or []
    }


def _reference_ids(state: dict) -> list[str]:
    return [
        reference["asset_id"]
        for reference in state["draft_plan"]["tasks"][0]["reference_images"]
    ]


def _uncovered_count(state: dict) -> int:
    from feishu_generation_agent.domain.plan import TaskPlan

    return GraphRuntime._asset_coverage(
        TaskPlan.model_validate(state["draft_plan"]),
        state.get("media_assets"),
    )["uncovered_count"]


async def test_remove_asset_drops_it_from_media_and_excluded() -> None:
    """删除 = 从这次运行里移除：素材列表和「已排除」里都不再有它，别人不受影响。"""
    runtime, box = _harness(_approval_state())

    await runtime.remove_asset("run-1", asset_id="image-3")

    assert _asset_ids(box["state"]) == {"image-1", "image-2"}
    assert _excluded_ids(box["state"]) == set()
    assert _reference_ids(box["state"]) == ["image-1"], "任务的参考图不许被动"
    assert box["state"]["status"] == "waiting_approval"


async def test_removing_an_uncovered_asset_unblocks_the_coverage_gate() -> None:
    """真正的收益：用不上又不想「排除」的素材，删掉之后覆盖门不再拦它。

    image-2 既没被引用、也没被排除 —— 它正是「批准按钮一直是灰的」那种素材。
    """
    runtime, box = _harness(_approval_state(excluded=()))
    assert _uncovered_count(box["state"]) == 2, "前置条件：image-2/image-3 都是未覆盖"

    await runtime.remove_asset("run-1", asset_id="image-2")
    await runtime.remove_asset("run-1", asset_id="image-3")

    assert _uncovered_count(box["state"]) == 0, "删掉之后不该再算未覆盖"


async def test_remove_asset_refuses_while_a_task_still_references_it() -> None:
    """还被任务引用的素材拒绝删除 —— 要人先摘掉，绝不偷偷改计划。"""
    runtime, box = _harness(_approval_state())

    with pytest.raises(RunValidationError) as excinfo:
        await runtime.remove_asset("run-1", asset_id="image-1")

    assert "image-1" in str(excinfo.value)
    assert "摘" in str(excinfo.value) or "引用" in str(excinfo.value)
    assert _asset_ids(box["state"]) >= {"image-1"}, "拒绝时不许改状态"
    assert box["persisted"] == [], "拒绝时不许落盘"


async def test_remove_asset_rejects_unknown_asset() -> None:
    runtime, box = _harness(_approval_state())

    with pytest.raises(RunValidationError) as excinfo:
        await runtime.remove_asset("run-1", asset_id="image-404")

    assert "image-404" in str(excinfo.value)
    assert box["persisted"] == []


async def test_remove_asset_goes_through_the_waiting_state_gate() -> None:
    """可编辑状态由 `_waiting_state` 把关（与 exclude_asset 同口径）。"""
    runtime, box = _harness(_approval_state())

    async def busy_state(run_id: str):
        raise RuntimeError("运行正在更新，请稍后重试")

    runtime._waiting_state = busy_state

    with pytest.raises(RuntimeError):
        await runtime.remove_asset("run-1", asset_id="image-3")
    assert box["persisted"] == []


async def test_reattaching_an_excluded_asset_drops_it_from_excluded() -> None:
    """「重新挂」不需要新接口：挂回任务后自动从「已排除」消失（回归护栏）。"""
    runtime, box = _harness(
        _approval_state(excluded=("image-3",), assets=("image-1", "image-3"))
    )
    assert _excluded_ids(box["state"]) == {"image-3"}

    await runtime.set_references(
        "run-1",
        task_id="task-1",
        references=[
            ImageReference(asset_id="image-1", role="reference_image", order=1),
            ImageReference(asset_id="image-3", role="reference_image", order=2),
        ],
    )

    assert _reference_ids(box["state"]) == ["image-1", "image-3"]
    assert _excluded_ids(box["state"]) == set(), "挂上了就不该还挂在「已排除」里"
    assert _uncovered_count(box["state"]) == 0
