from dataclasses import replace

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import _generator_for_task


def _video_task(model_key: str) -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-video",
            "task_type": "image_to_video",
            "title": "测试视频任务",
            "source_block_ids": ["block-1"],
            "user_intent": "生成一个动作镜头",
            "prompt": "镜头一：主体从左向右移动",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "16:9",
            "duration": 10,
            "resolution": "720p",
            "output_count": 1,
            "video_provider": model_key,
        }
    )


async def test_registry_routes_each_model_to_its_own_generator(fake_services):
    seedance_20 = "seedance-2.0-generator"
    seedance_25 = "seedance-2.5-generator"
    services = replace(
        fake_services,
        seedance_video_generators={
            "seedance2.0": seedance_20,
            "seedance2.5": seedance_25,
        },
        seedance_video_generator=seedance_25,
        video_generator=seedance_25,
    )

    provider_20, generator_20 = await _generator_for_task(
        "run-1", _video_task("seedance2.0"), services
    )
    provider_25, generator_25 = await _generator_for_task(
        "run-1", _video_task("seedance2.5"), services
    )

    assert (provider_20, generator_20) == ("seedance2.0", seedance_20)
    assert (provider_25, generator_25) == ("seedance2.5", seedance_25)
