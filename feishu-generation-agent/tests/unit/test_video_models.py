from feishu_generation_agent.domain.video_models import (
    VIDEO_MODEL_BY_KEY,
    normalize_video_task_payload,
    resolve_video_model_key,
)


def _video_task(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "task-1",
        "task_type": "image_to_video",
        "title": "测试镜头",
        "source_block_ids": ["block-1"],
        "user_intent": "生成一个连续动作镜头",
        "prompt": "镜头一：主体从左向右移动",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "16:9",
        "duration": 10,
        "resolution": "720p",
        "output_count": 1,
    }
    payload.update(updates)
    return payload


def test_video_model_catalog_exposes_expected_hard_boundaries() -> None:
    seedance_20 = VIDEO_MODEL_BY_KEY["seedance2.0"]
    seedance_25 = VIDEO_MODEL_BY_KEY["seedance2.5"]

    assert seedance_20.duration_max == 15
    assert "4k" in seedance_20.resolutions
    assert seedance_25.duration_max == 30
    assert "4k" not in seedance_25.resolutions
    assert "1080p" not in seedance_25.resolutions


def test_model_key_accepts_aliases_and_model_ids() -> None:
    assert resolve_video_model_key("seedance") == "seedance2.5"
    assert resolve_video_model_key("doubao-seedance-2-0-260128") == "seedance2.0"
    assert resolve_video_model_key("doubao-seedance-2-5-260628") == "seedance2.5"


def test_normalization_clamps_parameters_for_seedance_25() -> None:
    normalized, warnings = normalize_video_task_payload(
        _video_task(
            video_provider="seedance2.5",
            duration=30,
            resolution="4k",
            aspect_ratio="9:21",
            output_count=9,
        ),
        default_model_key="seedance2.5",
        max_output_count=4,
    )

    assert normalized["duration"] == 30
    assert normalized["resolution"] == "720p"
    assert normalized["aspect_ratio"] == "9:16"
    assert normalized["output_count"] == 4
    assert warnings


def test_normalization_preserves_seedance_20_4k_and_longer_options() -> None:
    normalized, warnings = normalize_video_task_payload(
        _video_task(
            video_provider="seedance2.0",
            duration=15,
            resolution="4k",
            output_count=2,
        ),
        default_model_key="seedance2.5",
        max_output_count=4,
    )

    assert normalized["video_provider"] == "seedance2.0"
    assert normalized["duration"] == 15
    assert normalized["resolution"] == "4k"
    assert normalized["output_count"] == 2
    assert warnings == []
