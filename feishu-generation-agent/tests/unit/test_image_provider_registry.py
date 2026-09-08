from dataclasses import replace

import pytest

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import _generator_for_task
from feishu_generation_agent.storage.provider_preferences import ProviderPreferences


def _image_task(**updates: object) -> GenerationTask:
    payload = {
        "task_id": "task-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["block-1"],
        "user_intent": "出一张 CG 图",
        "prompt": "@图片1 中的男性中景，戏剧化顶光 + 侧逆光",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def _video_task() -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-2",
            "task_type": "image_to_video",
            "title": "熊猫拉抽屉",
            "source_block_ids": ["block-1"],
            "user_intent": "完成动作",
            "prompt": "熊猫拉开抽屉",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )


async def test_default_image_task_routes_to_banana(fake_services):
    services = replace(
        fake_services,
        image_providers={
            "banana": "banana-generator",
            "seedream": "seedream-generator",
            "gpt-image2": "gpt-generator",
        },
    )

    provider, generator = await _generator_for_task(
        "run-1", _image_task(), services
    )

    assert provider == "banana"
    assert generator == "banana-generator"


@pytest.mark.parametrize(
    "requested", ["seedream", "banana", "gpt-image2"]
)
async def test_explicit_provider_routes_to_that_generator(
    fake_services, requested: str
):
    services = replace(
        fake_services,
        image_providers={
            "banana": "banana-generator",
            "seedream": "seedream-generator",
            "gpt-image2": "gpt-generator",
        },
    )

    provider, generator = await _generator_for_task(
        "run-1", _image_task(image_provider=requested), services
    )

    assert provider == requested
    assert generator == f"{requested.split('-')[0]}-generator"


async def test_explicit_missing_image_provider_is_reported(fake_services):
    """An explicitly unavailable provider must fail instead of silently switching models."""
    services = replace(
        fake_services,
        image_providers={"banana": "banana-generator"},
    )

    with pytest.raises(Exception, match="Selected image provider"):
        await _generator_for_task(
            "run-1", _image_task(image_provider="seedream"), services
        )


async def test_explicit_missing_cloud_image_provider_is_reported(fake_services):
    """An explicitly unavailable cloud provider must fail clearly."""
    services = replace(
        fake_services,
        image_providers={"seedream": "seedream-generator"},
    )

    with pytest.raises(Exception, match="Selected image provider"):
        await _generator_for_task(
            "run-1", _image_task(image_provider="banana"), services
        )


async def test_falls_back_to_legacy_image_generator_when_registry_absent(
    fake_services,
):
    """未配置 registry 时沿用旧的单实例 image_generator + 旧 provider 名。

    存量 run 的 provider 名已持久化为 chiyun（见 test_storage.py:467），
    回落路径不能改名，否则历史 submission 对不上。
    """
    provider, generator = await _generator_for_task(
        "run-1", _image_task(), fake_services
    )

    assert provider == "chiyun"
    assert generator is fake_services.image_generator


async def test_video_task_still_routes_to_seedance(fake_services):
    services = replace(
        fake_services,
        image_providers={"banana": "banana-generator"},
    )

    provider, generator = await _generator_for_task(
        "run-1", _video_task(), services
    )

    assert provider == "seedance"
    assert generator is fake_services.video_generator


async def test_video_task_explicit_seedance_requires_seedance_generator(fake_services):
    """An explicit Seedance choice must not silently fall back to local video."""
    aiport = "aiport-generator"
    services = replace(
        fake_services,
        settings=fake_services.settings.model_copy(update={"video_provider": "aiport"}),
        aiport_video_generator=aiport,
        video_generator=aiport,
        provider_preferences=ProviderPreferences(
            video_provider="aiport",
            image_provider="seedream",
        ),
    )

    task = _video_task().model_copy(update={"video_provider": "seedance"})

    with pytest.raises(Exception, match="Seedance provider is unavailable"):
        await _generator_for_task("run-1", task, services)


async def test_video_task_explicit_seedance_uses_seedance_generator(fake_services):
    seedance = "seedance-generator"
    services = replace(
        fake_services,
        settings=fake_services.settings.model_copy(update={"video_provider": "aiport"}),
        aiport_video_generator="aiport-generator",
        seedance_video_generator=seedance,
        video_generator="aiport-generator",
        provider_preferences=ProviderPreferences(
            video_provider="aiport",
            image_provider="seedream",
        ),
    )

    task = _video_task().model_copy(update={"video_provider": "seedance"})

    provider, generator = await _generator_for_task("run-1", task, services)

    assert (provider, generator) == ("seedance", seedance)


async def test_video_task_prefers_local_aiport_when_preference_says_so(fake_services):
    """云部署（settings.video_provider=seedance）时，用户偏好 aiport 则走本地。"""
    aiport = "aiport-generator"
    services = replace(
        fake_services,
        aiport_video_generator=aiport,
        provider_preferences=ProviderPreferences(
            video_provider="aiport",
            image_provider="seedream",
        ),
    )

    provider, generator = await _generator_for_task("run-1", _video_task(), services)

    assert provider == "aiport"
    assert generator == "aiport-generator"
