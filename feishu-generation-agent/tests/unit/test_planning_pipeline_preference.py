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
