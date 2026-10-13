"""规划方式（text / multimodal）走偏好设置，前端高级设置里手动切。"""

from pathlib import Path

from feishu_generation_agent.storage.provider_preferences import (
    ProviderPreferenceStore,
)


async def test_preference_round_trips_planning_pipeline(tmp_path: Path) -> None:
    """默认 text（不改现有工作流）；切到 multimodal 后能读回来。"""
    store = await ProviderPreferenceStore.open(tmp_path / "prefs.json")

    assert (await store.get()).planning_pipeline == "text"

    saved = await store.save(
        video_provider="seedance2.5",
        image_provider="seedream",
        planning_pipeline="multimodal",
    )

    assert saved.planning_pipeline == "multimodal"
    assert (await store.get()).planning_pipeline == "multimodal"


async def test_missing_or_broken_file_falls_back_to_text(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    path.write_text("不是 JSON", encoding="utf-8")

    store = await ProviderPreferenceStore.open(path)

    assert (await store.get()).planning_pipeline == "text"


async def test_preference_round_trips_storyboard_candidates(tmp_path: Path) -> None:
    """分镜候选数 K 也走偏好设置（用户 2026-09-22：放进高级设置让用户选）。

    默认 **0 = 关闭**（用户当天又要求「不用分镜图模式，默认改成不启用」）。
    """
    store = await ProviderPreferenceStore.open(tmp_path / "prefs.json")

    assert (await store.get()).storyboard_candidates == 0  # 默认关闭

    saved = await store.save(
        video_provider="seedance2.5",
        image_provider="seedream",
        storyboard_candidates=4,
    )

    assert saved.storyboard_candidates == 4
    assert (await store.get()).storyboard_candidates == 4

    # 0 = 关闭；越界与垃圾值都被夹回合法区间，不会让规划阶段莫名开始生图。
    await store.save(
        video_provider="seedance2.5",
        image_provider="seedream",
        storyboard_candidates=0,
    )
    assert (await store.get()).storyboard_candidates == 0

    await store.save(
        video_provider="seedance2.5",
        image_provider="seedream",
        storyboard_candidates=9,
    )
    assert (await store.get()).storyboard_candidates == 4


def test_storyboard_candidates_normalization() -> None:
    from feishu_generation_agent.storage.provider_preferences import (
        normalize_storyboard_candidates,
    )

    assert normalize_storyboard_candidates(0) == 0
    assert normalize_storyboard_candidates(3) == 3
    assert normalize_storyboard_candidates(99) == 4
    assert normalize_storyboard_candidates(-3) == 0
    # 垃圾值回落到**关闭**（默认不启用），不是回落到 2。
    assert normalize_storyboard_candidates("x") == 0
    assert normalize_storyboard_candidates(None) == 0
