from feishu_generation_agent.integrations.native_understanding.schemas import (
    ENGINE_ID_FRAME_V1,
    ENGINE_ID_GEMINI_NATIVE,
    KIND_IMAGE,
    KIND_VIDEO,
    SCHEMA_VERSION_FRAME_V1,
    SCHEMA_VERSION_NATIVE_V1,
    build_cache_key,
)


def test_cache_key_carries_engine_model_schema_and_kind():
    key = build_cache_key(
        engine_id=ENGINE_ID_GEMINI_NATIVE,
        kind=KIND_VIDEO,
        model_name="gemini-2.5-flash",
        schema_version=SCHEMA_VERSION_NATIVE_V1,
        sha256="a" * 64,
    )
    assert key == f"gemini_native:video:gemini-2.5-flash:native_v1:{'a' * 64}"


def test_legacy_and_native_keys_never_collide():
    common = dict(model_name="m", sha256="b" * 64)
    legacy = build_cache_key(
        engine_id=ENGINE_ID_FRAME_V1,
        kind=KIND_IMAGE,
        schema_version=SCHEMA_VERSION_FRAME_V1,
        **common,
    )
    native = build_cache_key(
        engine_id=ENGINE_ID_GEMINI_NATIVE,
        kind=KIND_IMAGE,
        schema_version=SCHEMA_VERSION_NATIVE_V1,
        **common,
    )
    assert legacy != native
    assert legacy.startswith("frame_v1:image:")
    assert native.startswith("gemini_native:image:")


def test_cache_key_rejects_empty_segment():
    import pytest

    with pytest.raises(ValueError, match="model_name"):
        build_cache_key(
            engine_id=ENGINE_ID_GEMINI_NATIVE,
            kind=KIND_VIDEO,
            model_name="",
            schema_version=SCHEMA_VERSION_NATIVE_V1,
            sha256="c" * 64,
        )
