import httpx
import pytest

from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.domain.video_models import VIDEO_MODEL_BY_KEY
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _task(*, duration: int, resolution: str) -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-video",
            "task_type": "image_to_video",
            "title": "模型能力测试",
            "source_block_ids": ["block-1"],
            "user_intent": "生成视频",
            "prompt": "镜头一：主体向前移动",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "16:9",
            "duration": duration,
            "resolution": resolution,
            "output_count": 1,
        }
    )


def _generator(model_key: str) -> SeedanceVideoGenerator:
    capability = VIDEO_MODEL_BY_KEY[model_key]
    return SeedanceVideoGenerator(
        httpx.AsyncClient(trust_env=False),
        base_url="https://ark.fictional.test/api/v3",
        api_key="fictional-key",
        model=capability.model,
        capability=capability,
        provider_name=model_key,
    )


def test_seedance_20_accepts_15_seconds_and_4k() -> None:
    _generator("seedance2.0")._validate_video_parameters(
        _task(duration=15, resolution="4k")
    )


def test_seedance_25_rejects_4k() -> None:
    with pytest.raises(AgentError, match="视频分辨率无效"):
        _generator("seedance2.5")._validate_video_parameters(
            _task(duration=30, resolution="4k")
        )


def test_seedance_25_accepts_30_seconds_and_720p() -> None:
    _generator("seedance2.5")._validate_video_parameters(
        _task(duration=30, resolution="720p")
    )
