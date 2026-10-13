"""分镜关键帧：切段、静帧提示词、选图落地。

用户 2026-09-22：「先生成几个分镜图供用户选择，然后生成出来的视频效果会更好、
需要修改的次数也会更少」；「按时间段生成，每段 K 张候选，生图并发就行」。
"""

from __future__ import annotations

import hashlib

from feishu_generation_agent.domain.plan import GenerationTask, StoryboardFrame
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.integrations import storyboard


def _task(prompt: str, **updates: object) -> GenerationTask:
    payload: dict[str, object] = {
        "task_id": "task-1",
        "task_type": "image_to_video",
        "title": "巨型南瓜",
        "source_block_ids": ["block-1"],
        "user_intent": "生成片头",
        "prompt": prompt,
        "reference_images": [],
        "aspect_ratio": "9:16",
        "duration": 5,
        "resolution": "720p",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


_PROMPT = (
    "吉卜力动画风格，9:16 竖屏。\n"
    "镜头 1（0-2s）：卡车运载巨型南瓜在弯道行驶。\n"
    "镜头 2（2-5s）：南瓜滚落砸在马路中央，后方车辆急刹。\n"
    "高清，不要水印，不要 logo。"
)


def test_frames_are_cut_from_the_timeline_one_per_line() -> None:
    frames = storyboard.build_storyboard_frames(_task(_PROMPT))

    assert [(frame.index, frame.start, frame.end) for frame in frames] == [
        (1, 0.0, 2.0),
        (2, 2.0, 5.0),
    ]
    assert frames[0].prompt == "卡车运载巨型南瓜在弯道行驶"
    # 最后一个时间段不能把后面的「风格与约束」整段吞进去（实测踩过）。
    assert frames[1].prompt == "南瓜滚落砸在马路中央，后方车辆急刹"


def test_frames_require_a_timeline() -> None:
    # 旧格式（没有时间轴）切不出帧 —— 上层据此跳过生成，不报错。
    assert storyboard.build_storyboard_frames(_task("镜头 1：展示纸船。")) == []


def test_keyframe_prompt_carries_style_but_not_the_other_segments() -> None:
    task = _task(_PROMPT)
    frame = storyboard.build_storyboard_frames(task)[0]

    prompt = storyboard.keyframe_prompt(task, frame)

    assert "第 0-2 秒的关键帧静帧" in prompt
    assert "卡车运载巨型南瓜在弯道行驶" in prompt
    assert "吉卜力动画风格" in prompt  # 风格必须带上，否则关键帧会把风格带偏
    assert "南瓜滚落" not in prompt  # 别的段落不能混进来
    assert "9:16" in prompt


def test_reference_line_numbers_the_selected_frames_after_document_refs() -> None:
    frames = [
        StoryboardFrame(index=1, start=0, end=2, selected="task-1-sb1-1"),
        StoryboardFrame(index=2, start=2, end=5, selected="task-1-sb2-2"),
    ]

    line = storyboard.keyframe_reference_line(frames, 3)

    assert "@图片3 是第 0-2 秒的关键帧" in line
    assert "@图片4 是第 2-5 秒的关键帧" in line
    # 一张都没选就不加这一句
    assert storyboard.keyframe_reference_line(
        [StoryboardFrame(index=1, start=0, end=2)], 1
    ) == ""


def test_selection_is_materialized_into_references_and_is_idempotent() -> None:
    task = {
        "prompt": "镜头 1（0-2s）：卡车行驶。",
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1}
        ],
    }
    frames = [
        {
            "index": 1,
            "start": 0,
            "end": 2,
            "selected": "task-1-sb1-2",
        }
    ]

    GraphRuntime._apply_storyboard_selection(
        task, frames, {"task-1-sb1-1", "task-1-sb1-2"}
    )
    GraphRuntime._apply_storyboard_selection(
        task, frames, {"task-1-sb1-1", "task-1-sb1-2"}
    )

    # 文档参考图在前、关键帧在后，order 连续；重复调用不会越堆越多。
    assert [ref["asset_id"] for ref in task["reference_images"]] == [
        "image-1",
        "task-1-sb1-2",
    ]
    assert [ref["order"] for ref in task["reference_images"]] == [1, 2]
    assert task["prompt"].count("关键帧参考：") == 1
    assert "@图片2 是第 0-2 秒的关键帧" in task["prompt"]


def test_default_selection_falls_back_to_the_first_candidate() -> None:
    """用户没点过就按第一张算 —— 否则直接点批准会一张关键帧都不带。"""
    frames = [
        {"index": 1, "start": 0, "end": 3, "candidates": ["sb-1", "sb-2"]},
        {"index": 2, "start": 3, "end": 6, "candidates": ["sb-3"]},
    ]

    filled = storyboard.default_selection(frames)

    assert [frame["selected"] for frame in filled] == ["sb-1", "sb-3"]
    # 用户已经选过的不被覆盖
    again = storyboard.default_selection(
        [{**filled[0], "selected": "sb-2"}]
    )
    assert again[0]["selected"] == "sb-2"


def test_selection_ignores_asset_ids_we_did_not_generate() -> None:
    current = [
        StoryboardFrame(
            index=1, start=0, end=2, candidates=["task-1-sb1-1"]
        )
    ]

    merged = GraphRuntime._merge_storyboard_selection(
        current,
        [{"index": 1, "selected": "someone-elses-asset"}],
    )

    assert merged[0]["selected"] is None
    assert merged[0]["candidates"] == ["task-1-sb1-1"]


def test_reference_validation_knows_about_storyboard_assets() -> None:
    """引用校验必须认得服务端生成的分镜关键帧，否则报「图片引用无效」。

    2026-09-22 实跑：选完分镜图后再动参考图 / 批准，直接报这个错、视频生成不了 ——
    因为几处校验都只从 `media_assets` 取素材，而分镜候选故意放在单独的
    `storyboard_assets` 通道里。
    """
    storyboard_asset = {
        "asset_id": "task-1-sb1-1-1",
        "source_block_id": "",
        "origin": storyboard.STORYBOARD_ORIGIN,
        "local_path": "outputs/runs/run-1/tasks/task-1-sb1-1/abc.png",
        "mime_type": "image/png",
        "size": 10,
        "sha256": "a" * 64,
    }
    state = {
        "media_assets": [
            {
                "asset_id": "image-1",
                "source_block_id": "block-1",
                "origin": "feishu",
                "local_path": "data/runs/run-1/inputs/a.png",
                "mime_type": "image/png",
                "size": 10,
                "sha256": "b" * 64,
            }
        ],
        "storyboard_assets": [storyboard_asset],
    }

    ids = {asset.asset_id for asset in GraphRuntime._state_assets(state)}

    assert ids == {"image-1", "task-1-sb1-1-1"}


def test_approval_keeps_server_side_candidates() -> None:
    """批准时用服务端存的候选图，只采纳用户的选择。

    前端为了过 schema 只回传标量字段；不并回来的话，批准后任务就丢掉候选图列表。
    """
    from feishu_generation_agent.domain.plan import TaskPlan
    from feishu_generation_agent.graph.nodes import (
        _with_server_storyboard_frames,
    )

    original = TaskPlan.model_validate(
        {
            "tasks": [
                {
                    "task_id": "task-1",
                    "task_type": "image_to_video",
                    "title": "巨型南瓜",
                    "source_block_ids": ["b1"],
                    "user_intent": "片头",
                    "prompt": "镜头 1（0-2s）：卡车行驶。",
                    "reference_images": [],
                    "aspect_ratio": "9:16",
                    "duration": 2,
                    "resolution": "720p",
                    "storyboard_frames": [
                        {
                            "index": 1,
                            "start": 0,
                            "end": 2,
                            "prompt": "卡车行驶",
                            "candidates": ["sb-1", "sb-2"],
                            "selected": "sb-1",
                        }
                    ],
                }
            ]
        }
    )
    incoming = [
        GenerationTask.model_validate(
            {
                **original.tasks[0].model_dump(mode="json"),
                "storyboard_frames": [
                    {
                        "index": 1,
                        "start": 0,
                        "end": 2,
                        "prompt": "卡车行驶",
                        "selected": "sb-2",
                    }
                ],
            }
        )
    ]

    merged = _with_server_storyboard_frames(original, incoming)

    frame = merged[0].storyboard_frames[0]
    assert frame.candidates == ["sb-1", "sb-2"]  # 候选图没丢
    assert frame.selected == "sb-2"  # 用户的选择被采纳


class _FakeFileStore:
    """只够用的落盘替身：返回带 sha256/size 的假 StoredFile。"""

    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.saved: list[str] = []

    async def materialize_provider_result(
        self, run_id, task_id, official_id, index, result, *, kind
    ):
        from types import SimpleNamespace

        content = result.base64_data or "x"
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.tmp_path / f"{task_id}-{index + 1}.png"
        path.write_bytes(raw)
        self.saved.append(f"{task_id}-{index + 1}")
        return SimpleNamespace(
            stored=SimpleNamespace(
                local_path=path,
                mime_type=result.mime_type,
                size=len(raw),
                sha256=digest,
                width=64,
                height=64,
            )
        )


class _FakeImageGenerator:
    """每次 submit 返回 1 张图（一张候选 = 一次调用）；记录收到过哪些任务。"""

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.submitted: list[str] = []

    async def submit(self, task, assets, *, submission_id=None):
        from feishu_generation_agent.domain.artifact import (
            ProviderResult,
            ProviderSubmission,
        )

        self.submitted.append(task.task_id)
        return ProviderSubmission(
            provider="seedream",
            provider_task_id=f"fake-{task.task_id}",
            status="succeeded",
            result_items=[
                ProviderResult(
                    base64_data=f"img-{index}", mime_type="image/png"
                )
                for index in range(self.count)
            ],
        )

    async def poll(self, submission):
        return submission


async def test_candidates_are_generated_per_segment_and_attached(
    tmp_path,
) -> None:
    """每个时间段并发生成 K 张候选（K 张 = K 次调用），挂回对应的 frame。

    这条用例是补课：真实跑第一次时炸在 `ReferenceMode.MULTI_REFERENCE`
    （`ReferenceMode` 是 Literal 不是 Enum），单元测试没覆盖到生成路径；
    第二次又发现 Ark 接入点不支持 `sequential_image_generation`，只能并发多调。
    """
    from types import SimpleNamespace

    from feishu_generation_agent.graph import nodes as nodes_module

    generator = _FakeImageGenerator()
    services = SimpleNamespace(
        settings=SimpleNamespace(
            provider_poll_max_attempts=1,
            storyboard_candidates=2,
            storyboard_concurrency=4,
            storyboard_image_size="2K",
        ),
        provider_preferences=SimpleNamespace(storyboard_candidates=2),
        file_store=_FakeFileStore(tmp_path),
        image_providers={"seedream": generator},
        image_generator=None,
        repository=None,
    )

    async def fake_poll_submission(g, submission, services, run_id, task_id):
        return submission

    original = nodes_module._poll_submission
    nodes_module._poll_submission = fake_poll_submission
    try:
        frames_by_task, assets = await nodes_module._generate_storyboard_candidates(
            services=services,
            run_id="run-1",
            tasks=[_task(_PROMPT)],
            assets=[],
            config=storyboard.StoryboardConfig(candidates=2),
        )
    finally:
        nodes_module._poll_submission = original

    frames = frames_by_task["task-1"]
    assert [frame.index for frame in frames] == [1, 2]
    assert all(len(frame.candidates) == 2 for frame in frames)
    assert len(assets) == 4
    assert sorted(generator.submitted) == [
        "task-1-sb1-1",
        "task-1-sb1-2",
        "task-1-sb2-1",
        "task-1-sb2-2",
    ]
    assert all(asset.origin == storyboard.STORYBOARD_ORIGIN for asset in assets)


async def test_transient_connection_failure_is_retried(tmp_path) -> None:
    """瞬时连接失败必须重试。

    真实踩坑（2026-09-22）：3 段 × 2 张 = 6 个并发调用里 4 个报
    「连接 Seedance 服务失败，请稍后重试」，用户看到「每段只剩一张」、还有一整段
    因为两张全挂而消失。
    """
    from types import SimpleNamespace

    from feishu_generation_agent.domain.artifact import (
        ProviderResult,
        ProviderSubmission,
    )
    from feishu_generation_agent.domain.errors import (
        AgentError,
        ErrorCategory,
        ErrorDetail,
    )
    from feishu_generation_agent.graph import nodes as nodes_module

    class _FlakyGenerator:
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, task, assets, *, submission_id=None):
            self.calls += 1
            if self.calls == 1:
                raise AgentError(
                    ErrorDetail(
                        category=ErrorCategory.TRANSIENT,
                        message="连接 Seedance 服务失败，请稍后重试",
                        technical_detail="cause=connect_error",
                        retryable=True,
                    )
                )
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

    generator = _FlakyGenerator()
    services = SimpleNamespace(
        settings=SimpleNamespace(
            provider_poll_max_attempts=1,
            storyboard_candidates=1,
            storyboard_concurrency=4,
            storyboard_image_size="2K",
        ),
        provider_preferences=SimpleNamespace(storyboard_candidates=1),
        file_store=_FakeFileStore(tmp_path),
        image_providers={"seedream": generator},
        image_generator=None,
        repository=None,
    )

    async def fake_poll_submission(g, submission, services, run_id, task_id):
        return submission

    async def no_sleep(_seconds):
        return None

    original_poll = nodes_module._poll_submission
    original_sleep = nodes_module.asyncio.sleep
    nodes_module._poll_submission = fake_poll_submission
    nodes_module.asyncio.sleep = no_sleep
    try:
        frames, assets = await nodes_module._generate_storyboard_candidates(
            services=services,
            run_id="run-1",
            tasks=[_task(_PROMPT)],
            assets=[],
            config=storyboard.StoryboardConfig(candidates=1),
        )
    finally:
        nodes_module._poll_submission = original_poll
        nodes_module.asyncio.sleep = original_sleep

    # 第一段第一次失败、重试后成功；第二段一次成功 —— 两段都有候选。
    assert generator.calls == 3
    assert [frame.index for frame in frames["task-1"]] == [1, 2]
    assert len(assets) == 2


async def test_dropped_segment_leaves_a_visible_warning() -> None:
    """整段生成失败时必须在任务上留警告，不能静默少一段。"""
    from feishu_generation_agent.graph import nodes as nodes_module

    plan = {"tasks": [{"task_id": "task-1", "warnings": []}]}
    nodes_module._attach_storyboard_frames(
        plan,
        {"task-1": []},
        set(),
        {"task-1": 3},
    )

    warnings = plan["tasks"][0]["warnings"]
    assert any("3 个时间段全部生成失败" in text for text in warnings)


async def test_generation_is_skipped_when_candidates_is_zero() -> None:
    from types import SimpleNamespace

    from feishu_generation_agent.graph import nodes as nodes_module

    generator = _FakeImageGenerator()
    services = SimpleNamespace(
        settings=SimpleNamespace(provider_poll_max_attempts=1),
        provider_preferences=SimpleNamespace(storyboard_candidates=0),
        file_store=None,
        image_providers={"seedream": generator},
        image_generator=None,
        repository=None,
    )

    frames, assets = await nodes_module._generate_storyboard_candidates(
        services=services,
        run_id="run-1",
        tasks=[_task(_PROMPT)],
        assets=[],
        config=nodes_module._storyboard_config(services),
    )

    assert frames == {}
    assert assets == []
    assert generator.submitted == []
