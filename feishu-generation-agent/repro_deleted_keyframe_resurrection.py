"""复现记录：删掉分镜图后，它还会不会被批准路径加回来。

用法（在 feishu-generation-agent 目录）：
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    .\\.venv\\Scripts\\python.exe _repro_deleted_keyframe.py

这个脚本只读地跑一遍运行时链路（内存 harness，不碰 sqlite、不调上游）：
建任务 → unlink_reference 删掉被选中的分镜图 → 走批准前的参考图重算。
跑完自行删除，不进仓库。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests" / "unit"))

from test_reference_assembly import (  # noqa: E402
    _FakeRegenerator,
    _approve,
    _reference_ids_of,
    _runtime_harness,
    _storyboard_approval_state,
    _task_json,
)

from feishu_generation_agent.graph.runtime import GraphRuntime  # noqa: E402


async def main() -> None:
    if "--legacy" in sys.argv:
        # 模拟修复前的行为：unlink 只摘 reference_images，不动分镜帧。
        GraphRuntime._drop_storyboard_candidate = staticmethod(
            lambda task, asset_id: task
        )
        GraphRuntime._reapply_storyboard_selection = (
            lambda self, task, state: task
        )
        print("=== 修复前（--legacy）===")
    else:
        print("=== 修复后 ===")
    state = _storyboard_approval_state(
        candidates=["sb-a", "sb-b"], selected="sb-a"
    )
    task = _task_json(state)
    print("① 删除前：")
    print("   reference_images =", _reference_ids_of(state))
    print("   frame.selected   =", task["storyboard_frames"][0]["selected"])
    print("   frame.candidates =", task["storyboard_frames"][0]["candidates"])

    runtime, box = _runtime_harness(state, _FakeRegenerator())
    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    task = _task_json(box["state"])
    frame = task["storyboard_frames"][0]
    print("② DELETE 之后（服务端草稿）：")
    print("   reference_images =", _reference_ids_of(box["state"]))
    print("   frame.selected   =", frame["selected"])
    print("   frame.candidates =", frame["candidates"])
    print("   selection_cleared=", frame.get("selection_cleared"))
    print("   prompt 尾部      =", repr(task["prompt"][-60:]))

    view = GraphRuntime._storyboard_view(
        "run-1", box["state"]["draft_plan"]["tasks"]
    )[0]["storyboard_frames"][0]
    print("   视图里高亮的候选 =", [
        item["asset_id"] for item in view["candidates"] if item["selected"]
    ], "（前端只会回传被高亮的那张）")

    stale = _approve(box["state"], frontend_selected="sb-a")
    print("③ 批准时按服务端帧重算（前端还回传旧值 sb-a）：")
    print("   reference_images =", [ref.asset_id for ref in stale.reference_images])
    print("   含 sb-a ?        =", any(r.asset_id == "sb-a" for r in stale.reference_images))

    cleared = _approve(box["state"], frontend_selected=None)
    print("④ 批准时按服务端帧重算（前端回传 null）：")
    print("   reference_images =", [ref.asset_id for ref in cleared.reference_images])


asyncio.run(main())
