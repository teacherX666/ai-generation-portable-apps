"""v4 参考图组装规则：复用 / 剔除 / 保留 / 每段一张 / 只换一段 / 并发不互相覆盖。

用户 2026-09-23 的口径：
- 「我有些分镜图原本效果就可以，不需要重新生成，只需要取消水印就行了」→
  **可用的 shot 图直接复用**，不调生图、不花钱；
- 「现在的分镜图生成出来没一个能用的」→ 只有「没配图 / 模糊低质」的段才重新生成；
- 角色/场景这类**全局图全部保留**（不删）；
- 每段**恰好一张**参考图（全局图在前、每段一张在后）；
- 「重新生成这一张」只换该段，且两段并发时互不覆盖。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.plan import GenerationTask, StoryboardFrame
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.integrations import storyboard

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


def _reference(asset_id: str, order: int) -> dict[str, object]:
    return {"asset_id": asset_id, "role": "reference_image", "order": order}


def _plan_item(
    asset_id: str,
    *,
    role: str,
    usable: bool,
    segment_index: int | None = None,
    reason: str = "",
) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "role": role,
        "segment_index": segment_index,
        "usable": usable,
        "reason": reason,
    }


def _video_task(
    *,
    task_id: str = "task-1",
    reference_images: list[dict[str, object]],
    reference_plan: list[dict[str, object]],
    frames: list[dict[str, object]] | None = None,
) -> GenerationTask:
    payload: dict[str, object] = {
        "task_id": task_id,
        "task_type": "image_to_video",
        "title": "巨型南瓜",
        "source_block_ids": ["block-1"],
        "user_intent": "生成片头",
        "prompt": _PROMPT,
        "reference_images": reference_images,
        "reference_plan": reference_plan,
        "aspect_ratio": "9:16",
        "duration": 12,
        "resolution": "720p",
    }
    if frames is not None:
        payload["storyboard_frames"] = frames
    return GenerationTask.model_validate(payload)


class _FakeFileStore:
    """只够用的落盘替身：返回带 sha256/size 的假 StoredFile。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def materialize_provider_result(
        self, run_id, task_id, official_id, index, result, *, kind
    ):
        content = (result.base64_data or "x").encode("utf-8")
        path = self.tmp_path / f"{task_id}-{index + 1}.png"
        path.write_bytes(content)
        return SimpleNamespace(
            stored=SimpleNamespace(
                local_path=path,
                mime_type=result.mime_type,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                width=64,
                height=64,
            )
        )


class _FakeImageGenerator:
    """每次 submit 返回 1 张图（一张候选 = 一次调用）；记录收到过哪些任务。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        #: 每次生图时任务带上的参考图 asset_id（用来验证「不可用的图不许当输入」）。
        self.inputs: dict[str, list[str]] = {}

    async def submit(self, task, assets, *, submission_id=None):
        from feishu_generation_agent.domain.artifact import (
            ProviderResult,
            ProviderSubmission,
        )

        self.submitted.append(task.task_id)
        self.inputs[task.task_id] = [
            reference.asset_id for reference in task.reference_images
        ]
        return ProviderSubmission(
            provider="seedream",
            provider_task_id=f"fake-{task.task_id}",
            status="succeeded",
            result_items=[
                ProviderResult(base64_data="img", mime_type="image/png")
            ],
        )

    async def poll(self, submission):
        return submission


def _services(tmp_path: Path, generator: _FakeImageGenerator, candidates: int):
    return SimpleNamespace(
        settings=SimpleNamespace(
            provider_poll_max_attempts=1,
            storyboard_candidates=candidates,
            storyboard_concurrency=4,
            storyboard_image_size="2K",
        ),
        provider_preferences=SimpleNamespace(storyboard_candidates=candidates),
        file_store=_FakeFileStore(tmp_path),
        image_providers={"seedream": generator},
        image_generator=None,
        repository=None,
    )


async def _assemble(
    tmp_path: Path,
    task: GenerationTask,
    assets: list[MediaAsset],
    *,
    candidates: int = 1,
):
    """跑一遍真实的组装链路：生成候选（能复用就复用）→ 写回计划。"""
    from feishu_generation_agent.graph import nodes as nodes_module

    generator = _FakeImageGenerator()
    services = _services(tmp_path, generator, candidates)

    async def fake_poll_submission(g, submission, services, run_id, task_id):
        return submission

    original = nodes_module._poll_submission
    nodes_module._poll_submission = fake_poll_submission
    try:
        frames_by_task, storyboard_assets = (
            await nodes_module._generate_storyboard_candidates(
                services=services,
                run_id="run-1",
                tasks=[task],
                assets=assets,
                config=storyboard.StoryboardConfig(candidates=candidates),
            )
        )
    finally:
        nodes_module._poll_submission = original

    plan = {"tasks": [task.model_dump(mode="json")]}
    if frames_by_task:
        nodes_module._attach_storyboard_frames(
            plan,
            frames_by_task,
            {asset.asset_id for asset in storyboard_assets},
            {task.task_id: len(storyboard.build_storyboard_frames(task))},
        )
    return plan, frames_by_task, storyboard_assets, generator


def _refs(plan: dict) -> list[str]:
    return [
        reference["asset_id"]
        for reference in plan["tasks"][0]["reference_images"]
    ]


def _frames(plan: dict) -> list[dict]:
    return plan["tasks"][0]["storyboard_frames"]


async def test_usable_shot_image_is_reused_without_calling_the_model(
    tmp_path: Path,
) -> None:
    """① 可用的分镜图直接复用：那一段**不调生图**（用户要的就是「别重画我的图」）。"""
    task = _video_task(
        reference_images=[
            _reference("image-1", 1),
            _reference("image-2", 2),
            _reference("image-3", 3),
        ],
        reference_plan=[
            _plan_item(
                "image-1", role="shot", usable=True, segment_index=1,
                reason="画面清晰，与第 1 段一致",
            ),
            _plan_item("image-2", role="global", usable=True, reason="角色参考图"),
            _plan_item("image-3", role="global", usable=True, reason="场景参考图"),
        ],
    )
    assets = [_asset("image-1"), _asset("image-2"), _asset("image-3")]

    plan, frames_by_task, _created, generator = await _assemble(
        tmp_path, task, assets
    )

    frames = frames_by_task["task-1"]
    # 第 1 段复用 image-1；第 2、3 段没有可用分镜图 → 各生成 1 张（共 2 次调用）。
    assert frames[0].candidates == ["image-1"]
    assert frames[0].selected == "image-1"
    assert sorted(generator.submitted) == ["task-1-sb2-1", "task-1-sb3-1"]
    assert "task-1-sb1-1" not in generator.submitted


async def test_unusable_shot_image_is_not_used_as_reference(
    tmp_path: Path,
) -> None:
    """② `usable=false` 的分镜图既不进参考图，也不许当生图输入。"""
    task = _video_task(
        reference_images=[
            _reference("image-1", 1),
            _reference("image-2", 2),
            _reference("image-3", 3),
        ],
        reference_plan=[
            _plan_item(
                "image-1", role="shot", usable=False, segment_index=1,
                reason="画面模糊、分辨率低，看不清主体",
            ),
            _plan_item("image-2", role="global", usable=True, reason="角色参考图"),
            _plan_item("image-3", role="global", usable=True, reason="场景参考图"),
        ],
    )
    assets = [_asset("image-1"), _asset("image-2"), _asset("image-3")]

    plan, _frames_by_task, _created, generator = await _assemble(
        tmp_path, task, assets
    )

    # 不可用的 shot 图被摘掉，且没有以别的身份回来。
    assert "image-1" not in _refs(plan)
    # 生图时带的参考图里也没有它（否则水印/低质会被继承进新图）。
    assert all(
        "image-1" not in inputs for inputs in generator.inputs.values()
    )


async def test_global_images_are_all_kept(tmp_path: Path) -> None:
    """③ 全局图（角色/场景）全部保留 —— 包括判为不可用的那张。"""
    task = _video_task(
        reference_images=[
            _reference("image-1", 1),
            _reference("image-2", 2),
            _reference("image-3", 3),
        ],
        reference_plan=[
            _plan_item(
                "image-1", role="shot", usable=True, segment_index=1,
                reason="画面清晰",
            ),
            _plan_item(
                "image-2", role="global", usable=False,
                reason="角色图带平台水印",
            ),
            _plan_item("image-3", role="global", usable=True, reason="场景参考图"),
        ],
    )
    assets = [_asset("image-1"), _asset("image-2"), _asset("image-3")]

    plan, _frames_by_task, _created, _generator = await _assemble(
        tmp_path, task, assets
    )

    refs = _refs(plan)
    assert "image-2" in refs  # 角色图不删（水印由去水印那一步就地解决）
    assert "image-3" in refs
    assert "image-1" in refs


async def test_exactly_one_reference_per_segment_after_the_globals(
    tmp_path: Path,
) -> None:
    """④ 每段恰好 1 张：全局图在前，每段一张分镜图在后，不重不漏。"""
    task = _video_task(
        reference_images=[
            _reference("image-1", 1),
            _reference("image-2", 2),
            _reference("image-3", 3),
        ],
        reference_plan=[
            _plan_item(
                "image-1", role="shot", usable=True, segment_index=1,
                reason="画面清晰",
            ),
            _plan_item("image-2", role="global", usable=True, reason="角色参考图"),
            _plan_item("image-3", role="global", usable=True, reason="场景参考图"),
        ],
    )
    assets = [_asset("image-1"), _asset("image-2"), _asset("image-3")]

    plan, frames_by_task, _created, _generator = await _assemble(
        tmp_path, task, assets, candidates=2
    )

    frames = frames_by_task["task-1"]
    assert [len(frame.candidates) for frame in frames] == [1, 2, 2]
    # 用户没点过就按「第一张候选」算 —— 默认选择在写回计划时补齐。
    selected = [frame["selected"] for frame in _frames(plan)]
    assert all(selected)
    refs = _refs(plan)
    # 全局图（image-2 / image-3）在前，每段选中的图在后，各出现一次。
    assert refs[:2] == ["image-2", "image-3"]
    assert refs[2:] == selected
    assert len(refs) == len(set(refs))
    assert [ref["order"] for ref in plan["tasks"][0]["reference_images"]] == [
        1, 2, 3, 4, 5,
    ]


def _state_with_frames(frames: list[dict[str, object]]) -> dict:
    task = _video_task(
        reference_images=[
            _reference("image-1", 1),
            _reference("image-2", 2),
        ],
        reference_plan=[
            _plan_item("image-1", role="global", usable=True, reason="角色参考图"),
            _plan_item("image-2", role="global", usable=True, reason="场景参考图"),
        ],
        frames=frames,
    )
    # 初始的三张候选图是**服务端生成的**，必须登记在 storyboard_assets 里 ——
    # 否则重生成时它们不在「摘掉再追加」的集合里，会作为基础参考残留下来。
    generated = [
        asset_id
        for frame in frames
        for asset_id in (frame.get("candidates") or [])
    ]
    return {
        "draft_plan": {
            "tasks": [task.model_dump(mode="json")],
            "document_summary": "",
            "excluded_assets": [],
        },
        "media_assets": [
            _asset("image-1").model_dump(mode="json"),
            _asset("image-2").model_dump(mode="json"),
        ],
        "storyboard_assets": [
            _asset(asset_id).model_dump(mode="json") for asset_id in generated
        ],
    }


def _frames_for_segments() -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "start": float(index - 1),
            "end": float(index),
            "prompt": f"第 {index} 段画面",
            "candidates": [f"task-1-sb{index}-1"],
            "selected": f"task-1-sb{index}-1",
        }
        for index in (1, 2, 3)
    ]


class _FakeRegenerator:
    """每段返回一张**新**候选（asset_id 唯一），并让出事件循环以制造并发交错。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.calls: list[int] = []
        self.delay = delay

    async def __call__(self, *, run_id, task, frame, assets):
        self.calls.append(frame.index)
        await asyncio.sleep(self.delay)
        asset_id = f"{task.task_id}-re{frame.index}-{uuid4().hex[:6]}-1"
        return [_asset(asset_id)], frame.model_copy(
            update={"candidates": [asset_id], "selected": asset_id}
        )


def _runtime_harness(state: dict, regenerator: _FakeRegenerator):
    """绕开 __init__ 造一个 GraphRuntime，只把「读写状态」换成内存实现。"""
    runtime = object.__new__(GraphRuntime)
    runtime._storyboard_regenerator = regenerator
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
            **(extra_updates or {}),
        }
        box["persisted"].append(box["state"])

    runtime._waiting_state = waiting_state
    runtime._persist_draft = persist_draft
    return runtime, box


async def test_regenerate_replaces_only_that_segment() -> None:
    """⑤ 「重新生成这一张」只换该段，其它段的候选与参考图原样不动。"""
    regenerator = _FakeRegenerator()
    runtime, box = _runtime_harness(
        _state_with_frames(_frames_for_segments()), regenerator
    )

    await runtime.regenerate_storyboard_frame(
        "run-1", task_id="task-1", index=2
    )

    frames = box["state"]["draft_plan"]["tasks"][0]["storyboard_frames"]
    assert regenerator.calls == [2]
    assert frames[0]["candidates"] == ["task-1-sb1-1"]
    assert frames[1]["candidates"] != ["task-1-sb2-1"]
    assert frames[1]["selected"] == frames[1]["candidates"][0]
    assert frames[2]["candidates"] == ["task-1-sb3-1"]
    refs = [
        reference["asset_id"]
        for reference in box["state"]["draft_plan"]["tasks"][0][
            "reference_images"
        ]
    ]
    # 全局图在前、每段选中的图在后，各一次。
    assert refs[:2] == ["image-1", "image-2"]
    assert refs[2:] == [frame["selected"] for frame in frames]
    assert len(refs) == len(set(refs))


async def test_concurrent_regeneration_does_not_overwrite_each_other() -> None:
    """⑥ 两段并发重生成：都拿到自己的新图，谁也没覆盖谁（用户实测要求）。"""
    regenerator = _FakeRegenerator()
    runtime, box = _runtime_harness(
        _state_with_frames(_frames_for_segments()), regenerator
    )

    await asyncio.gather(
        runtime.regenerate_storyboard_frame(
            "run-1", task_id="task-1", index=1
        ),
        runtime.regenerate_storyboard_frame(
            "run-1", task_id="task-1", index=3
        ),
    )

    frames = box["state"]["draft_plan"]["tasks"][0]["storyboard_frames"]
    assert sorted(regenerator.calls) == [1, 3]
    assert frames[0]["candidates"] != ["task-1-sb1-1"]  # 第 1 段换了新图
    assert frames[1]["candidates"] == ["task-1-sb2-1"]  # 第 2 段没被动过
    assert frames[2]["candidates"] != ["task-1-sb3-1"]  # 第 3 段也换了新图
    refs = [
        reference["asset_id"]
        for reference in box["state"]["draft_plan"]["tasks"][0][
            "reference_images"
        ]
    ]
    assert refs[2:] == [frame["selected"] for frame in frames]
    assert len(refs) == len(set(refs))
    # 两次落盘都发生了（第二次基于第一次的最新状态合并，而不是覆盖）。
    assert len(box["persisted"]) == 2


async def test_approval_keeps_every_selected_keyframe_in_references() -> None:
    """批准后参考图必须包含**每段选中的分镜图**（运行 b2210150 实测丢过）。

    实测：审批页「参考素材」一栏把分镜关键帧也当普通参考图列出，用户删过图 /
    保存过顺序后，前端回传的 `reference_images` 里就没有每段选中的分镜图了
    （`storyboard_frames` 有 3 段、`reference_images` 只剩 1 张）。服务端必须按
    自己的帧重新拼一遍，而不是照单全收。
    """
    from feishu_generation_agent.domain.plan import TaskPlan
    from feishu_generation_agent.graph.nodes import (
        _with_server_storyboard_frames,
    )

    frames = _frames_for_segments()
    original = TaskPlan.model_validate(
        {
            "tasks": [
                _video_task(
                    reference_images=[_reference("image-1", 1)],
                    reference_plan=[
                        _plan_item(
                            "image-1", role="global", usable=True,
                            reason="角色参考图",
                        )
                    ],
                    frames=frames,
                ).model_dump(mode="json")
            ],
            "document_summary": "",
            "excluded_assets": [],
        }
    )
    # 前端只回传标量字段，而且参考图被删到只剩一张（正是实测的那个 bug）。
    incoming = [
        GenerationTask.model_validate(
            {
                **original.tasks[0].model_dump(mode="json"),
                "reference_images": [_reference("image-1", 1)],
                "storyboard_frames": [
                    {
                        "index": frame["index"],
                        "start": frame["start"],
                        "end": frame["end"],
                        "prompt": frame["prompt"],
                        "selected": frame["selected"],
                    }
                    for frame in frames
                ],
            }
        )
    ]

    merged = _with_server_storyboard_frames(
        original, incoming, {"task-1-sb1-1", "task-1-sb2-1", "task-1-sb3-1"}
    )

    task = merged[0]
    assert [frame.candidates for frame in task.storyboard_frames] == [
        ["task-1-sb1-1"],
        ["task-1-sb2-1"],
        ["task-1-sb3-1"],
    ]
    refs = [reference.asset_id for reference in task.reference_images]
    assert refs == [
        "image-1",
        "task-1-sb1-1",
        "task-1-sb2-1",
        "task-1-sb3-1",
    ]
    assert "关键帧参考：" in task.prompt


async def test_approval_keeps_server_frames_when_frontend_sends_none() -> None:
    """前端一帧都没回传时，用服务端存的帧兜住（默认第一张），不能丢候选图。"""
    from feishu_generation_agent.domain.plan import TaskPlan
    from feishu_generation_agent.graph.nodes import (
        _with_server_storyboard_frames,
    )

    frames = _frames_for_segments()
    task = _video_task(
        reference_images=[_reference("image-1", 1)],
        reference_plan=[
            _plan_item("image-1", role="global", usable=True, reason="角色参考图")
        ],
        frames=frames,
    )
    original = TaskPlan.model_validate(
        {
            "tasks": [task.model_dump(mode="json")],
            "document_summary": "",
            "excluded_assets": [],
        }
    )
    incoming = [
        GenerationTask.model_validate(
            {
                **task.model_dump(mode="json"),
                "storyboard_frames": [],
            }
        )
    ]

    merged = _with_server_storyboard_frames(
        original, incoming, {"task-1-sb1-1", "task-1-sb2-1", "task-1-sb3-1"}
    )

    assert len(merged[0].storyboard_frames) == 3
    assert [ref.asset_id for ref in merged[0].reference_images] == [
        "image-1",
        "task-1-sb1-1",
        "task-1-sb2-1",
        "task-1-sb3-1",
    ]


def test_unselected_shot_image_is_not_dropped() -> None:
    """没被任何一段选中的 shot 图**不能**被摘掉（摘了没人加回来 = 参考图少一张）。"""
    from feishu_generation_agent.integrations.storyboard import (
        apply_selection,
        reference_ids_for_selection,
    )

    frames = [
        {
            "index": 1,
            "start": 0,
            "end": 5,
            "selected": "sb-1",
        }
    ]
    task = {
        "prompt": "镜头 1（0-5s）：卡车行驶。",
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1},
            {"asset_id": "image-2", "role": "reference_image", "order": 2},
        ],
    }

    # image-2 是 reference_plan 里的 shot 图，但这一段没选它（候选全失败）。
    apply_selection(
        task, frames, reference_ids_for_selection(frames, {"sb-1", "sb-2"})
    )

    assert [ref["asset_id"] for ref in task["reference_images"]] == [
        "image-1",
        "image-2",
        "sb-1",
    ]


def test_reused_shot_image_appears_only_once() -> None:
    """复用的分镜图不能同时以「基础参考」和「该段画面」出现两次（实测 image-2 两遍）。"""
    from feishu_generation_agent.integrations.storyboard import (
        apply_selection,
        reference_ids_for_selection,
    )

    frames = [
        StoryboardFrame(
            index=1, start=0, end=5, candidates=["image-1"], selected="image-1"
        ),
        StoryboardFrame(
            index=2, start=5, end=8, candidates=["sb-2"], selected="sb-2"
        ),
    ]
    payload = [frame.model_dump(mode="json") for frame in frames]
    task = {
        "prompt": "镜头 1（0-5s）：卡车行驶。\n镜头 2（5-8s）：南瓜滚落。",
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1}
        ],
    }

    apply_selection(
        task,
        payload,
        reference_ids_for_selection(payload, {"sb-2"}),
    )

    assert [ref["asset_id"] for ref in task["reference_images"]] == [
        "image-1",
        "sb-2",
    ]
    assert [ref["order"] for ref in task["reference_images"]] == [1, 2]


# ---------------------------------------------------------------------------
# 用户 2026-09-23：「分镜图我明明已经删掉他了不需要了还是能影响我」
#
# 删参考图只摘掉了 `reference_images`，**那一帧的 `selected` 还指着它**；
# 批准时服务端按帧重算参考图（`_with_server_storyboard_frames` → `apply_selection`）
# 又把它加了回来 —— 删了等于没删。下面的用例先复现、再锁死修复后的语义。
# ---------------------------------------------------------------------------


def _storyboard_approval_state(
    *,
    candidates: list[str],
    selected: str | None,
    extra_globals: list[str] | None = None,
) -> dict:
    """待审批任务：第 1 段候选 `candidates`、选中 `selected`。

    参考图 = 全局图（`image-1` + `extra_globals`）+ 该段选中的分镜图，与真实运行时
    `_attach_storyboard_frames` 落出来的形状一致。候选图必须登记进
    `storyboard_assets`（服务端生成的那条通道），否则引用校验认不出它们。
    """
    globals_ = ["image-1", *(extra_globals or [])]
    references = [
        _reference(asset_id, index)
        for index, asset_id in enumerate(globals_, start=1)
    ]
    if selected:
        references.append(_reference(selected, len(references) + 1))
    task = _video_task(
        reference_images=references,
        reference_plan=[
            _plan_item(asset_id, role="global", usable=True, reason="角色参考图")
            for asset_id in globals_
        ],
        frames=[
            {
                "index": 1,
                "start": 0.0,
                "end": 5.0,
                "prompt": "卡车运载巨型南瓜在弯道行驶",
                "candidates": list(candidates),
                "selected": selected,
            }
        ],
    )
    return {
        "draft_plan": {
            "tasks": [task.model_dump(mode="json")],
            "document_summary": "",
            "excluded_assets": [],
        },
        "media_assets": [
            _asset(asset_id).model_dump(mode="json") for asset_id in globals_
        ],
        "storyboard_assets": [
            _asset(asset_id).model_dump(mode="json") for asset_id in candidates
        ],
    }


def _task_json(state: dict) -> dict:
    return state["draft_plan"]["tasks"][0]


def _reference_ids_of(state: dict) -> list[str]:
    return [
        reference["asset_id"]
        for reference in _task_json(state)["reference_images"]
    ]


def _approve(state: dict, *, frontend_selected: str | None) -> GenerationTask:
    """走一遍「批准前按服务端帧重算参考图」这条路。

    `frontend_selected` 模拟前端批准 payload 里回传的 `selected`（审批页把分镜关键帧
    也当普通参考图列出来，删过图之后它可能还回传旧值 —— 正是实测的那个 bug）。
    """
    from feishu_generation_agent.domain.plan import TaskPlan
    from feishu_generation_agent.graph.nodes import (
        _with_server_storyboard_frames,
    )

    original = TaskPlan.model_validate(state["draft_plan"])
    stored = original.tasks[0]
    storyboard_ids = {
        item["asset_id"] for item in state.get("storyboard_assets", [])
    }
    incoming = [
        GenerationTask.model_validate(
            {
                **stored.model_dump(mode="json"),
                "storyboard_frames": [
                    {
                        "index": frame.index,
                        "start": frame.start,
                        "end": frame.end,
                        "prompt": frame.prompt,
                        "selected": frontend_selected,
                    }
                    for frame in stored.storyboard_frames
                ],
            }
        )
    ]
    return _with_server_storyboard_frames(original, incoming, storyboard_ids)[0]


async def test_unlinking_the_selected_keyframe_clears_that_segment() -> None:
    """删掉被选中的分镜图 → 该帧 `selected` 清空、该候选被摘掉（bug 复现点）。"""
    runtime, box = _runtime_harness(
        _storyboard_approval_state(candidates=["sb-a", "sb-b"], selected="sb-a"),
        _FakeRegenerator(),
    )

    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    frame = _task_json(box["state"])["storyboard_frames"][0]
    assert "sb-a" not in _reference_ids_of(box["state"])
    # 修复前这里是 "sb-a"（帧还指着已删的图）→ 批准时被原样加回来。
    assert frame["selected"] is None
    # 修复前是 ["sb-a", "sb-b"] → 「默认第一张」又会选中已删的 sb-a。
    assert frame["candidates"] == ["sb-b"]
    # 「用户显式删过」要和「还没选过」区分开，否则默认规则会把剩下的候选补上来。
    assert frame["selection_cleared"] is True


async def test_approval_does_not_resurrect_the_deleted_keyframe() -> None:
    """删掉分镜图后批准：参考图里不能再出现它（用户报的就是这一条）。"""
    runtime, box = _runtime_harness(
        _storyboard_approval_state(candidates=["sb-a", "sb-b"], selected="sb-a"),
        _FakeRegenerator(),
    )
    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    # 前端可能还把删之前的选中值回传回来（视图没刷新）——服务端必须拒掉。
    stale = _approve(box["state"], frontend_selected="sb-a")
    assert [ref.asset_id for ref in stale.reference_images] == ["image-1"]
    assert stale.storyboard_frames[0].selected is None
    assert "sb-a" not in stale.prompt

    # 视图刷新后前端回传 null，同样不能凭空补一张。
    cleared = _approve(box["state"], frontend_selected=None)
    assert [ref.asset_id for ref in cleared.reference_images] == ["image-1"]
    assert cleared.storyboard_frames[0].selected is None


async def test_cleared_segment_is_not_auto_selected_again_in_the_view() -> None:
    """删过关键帧的段在审批页视图里**不高亮任何候选**。

    视图里高亮 = 前端批准时把它回传回来。删掉 sb-a 之后如果这里默认高亮 sb-b，
    用户直接点批准，这一段就会带着一张他没挑过的关键帧进视频 —— 还是「删了等于没删」。
    """
    runtime, box = _runtime_harness(
        _storyboard_approval_state(candidates=["sb-a", "sb-b"], selected="sb-a"),
        _FakeRegenerator(),
    )
    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    view = GraphRuntime._storyboard_view(
        "run-1", box["state"]["draft_plan"]["tasks"]
    )
    frame = view[0]["storyboard_frames"][0]
    assert frame["selected"] is None
    assert [item["selected"] for item in frame["candidates"]] == [False]
    # 候选还在（用户想改主意可以点），只是不再默认选中。
    assert [item["asset_id"] for item in frame["candidates"]] == ["sb-b"]

    # 用户**重新点**了剩下那张 → 尊重他的显式选择。
    picked = _approve(box["state"], frontend_selected="sb-b")
    assert [ref.asset_id for ref in picked.reference_images] == [
        "image-1",
        "sb-b",
    ]


async def test_segment_without_candidates_has_no_keyframe() -> None:
    """删掉某段**唯一**的分镜图 → 该段没有关键帧（不会 fallback 回同一张）。"""
    runtime, box = _runtime_harness(
        _storyboard_approval_state(candidates=["sb-a"], selected="sb-a"),
        _FakeRegenerator(),
    )

    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    frame = _task_json(box["state"])["storyboard_frames"][0]
    assert frame["candidates"] == []
    assert frame["selected"] is None
    for frontend_selected in ("sb-a", None):
        task = _approve(box["state"], frontend_selected=frontend_selected)
        assert [ref.asset_id for ref in task.reference_images] == ["image-1"]
        assert task.storyboard_frames[0].selected is None
        assert "关键帧参考" not in task.prompt


async def test_unlinking_a_global_image_keeps_it_deleted() -> None:
    """删全局图（不在任何帧的候选里）→ 保持删除，且不影响该段的分镜选择。"""
    runtime, box = _runtime_harness(
        _storyboard_approval_state(
            candidates=["sb-a"], selected="sb-a", extra_globals=["image-2"]
        ),
        _FakeRegenerator(),
    )

    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="image-2")

    frame = _task_json(box["state"])["storyboard_frames"][0]
    assert frame["selected"] == "sb-a"  # 删全局图不该动分镜选择
    assert frame["candidates"] == ["sb-a"]
    assert _reference_ids_of(box["state"]) == ["image-1", "sb-a"]

    task = _approve(box["state"], frontend_selected="sb-a")
    assert [ref.asset_id for ref in task.reference_images] == [
        "image-1",
        "sb-a",
    ]


async def test_unlinking_a_candidate_that_is_not_selected_keeps_the_selection() -> None:
    """删掉的候选不是该段选中的那张时，只摘候选、不动 `selected`（别误清别人的选择）。"""
    state = _storyboard_approval_state(candidates=["sb-a", "sb-b"], selected="sb-b")
    # 把 sb-a 也放进参考图，模拟「它同时被当普通参考图引用」时用户点了删除。
    state["draft_plan"]["tasks"][0]["reference_images"].append(
        _reference("sb-a", 3)
    )
    runtime, box = _runtime_harness(state, _FakeRegenerator())

    await runtime.unlink_reference("run-1", task_id="task-1", asset_id="sb-a")

    frame = _task_json(box["state"])["storyboard_frames"][0]
    assert frame["candidates"] == ["sb-b"]
    assert frame["selected"] == "sb-b"
    assert frame["selection_cleared"] is False
    task = _approve(box["state"], frontend_selected="sb-b")
    assert [ref.asset_id for ref in task.reference_images] == [
        "image-1",
        "sb-b",
    ]


def test_merge_selection_keeps_or_revokes_the_cleared_mark() -> None:
    """`selection_cleared` 的撤销规则：回传 null 保持清空，显式选图才撤销。"""
    from feishu_generation_agent.integrations.storyboard import merge_selection

    def cleared_frames() -> list[StoryboardFrame]:
        # 每次都要新对象：`merge_selection` 是**就地改**传入的帧。
        return [
            StoryboardFrame(
                index=1,
                start=0,
                end=5,
                candidates=["sb-b"],
                selected=None,
                selection_cleared=True,
            )
        ]

    # 前端回传 null（这一段的候选一张都没高亮）→ 保持「这一段不要关键帧」。
    kept = merge_selection(cleared_frames(), [{"index": 1, "selected": None}])
    assert kept[0]["selected"] is None
    assert kept[0]["selection_cleared"] is True

    # 用户重新点了剩下那张候选 → 撤销标记、按他选的来。
    revoked = merge_selection(cleared_frames(), [{"index": 1, "selected": "sb-b"}])
    assert revoked[0]["selected"] == "sb-b"
    assert revoked[0]["selection_cleared"] is False

    # 前端塞一个不认识的 id → 忽略（原样保留）。
    ignored = merge_selection(cleared_frames(), [{"index": 1, "selected": "sb-x"}])
    assert ignored[0]["selected"] is None
    assert ignored[0]["selection_cleared"] is True


def test_never_selected_segment_still_defaults_to_the_first_candidate() -> None:
    """没删过的段仍然按「默认第一张」算（`selection_cleared` 不能把默认行为改掉）。"""
    tasks = [
        {
            "task_id": "task-1",
            "storyboard_frames": [
                {
                    "index": 1,
                    "start": 0,
                    "end": 5,
                    "prompt": "第 1 段",
                    "candidates": ["sb-a", "sb-b"],
                    "selected": None,
                }
            ],
        }
    ]

    view = GraphRuntime._storyboard_view("run-1", tasks)
    assert [
        item["selected"] for item in view[0]["storyboard_frames"][0]["candidates"]
    ] == [True, False]

    filled = storyboard.default_selection(
        [dict(tasks[0]["storyboard_frames"][0])]
    )
    assert filled[0]["selected"] == "sb-a"

    # 显式删过的段：默认规则不再补。
    cleared = storyboard.default_selection(
        [{**tasks[0]["storyboard_frames"][0], "selection_cleared": True}]
    )
    assert cleared[0]["selected"] is None
