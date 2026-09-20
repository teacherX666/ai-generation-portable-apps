"""原生理解层的引擎常量与缓存 key 契约。

cache key 故意把 engine_id / kind / model_name / schema_version 全部编进去：
旧 Claude 时代的 key（sha256:model:prompt_version）因此永远命中不到，
等于让存量缓存自然失效，同时一行都不用删。
"""

ENGINE_ID_GEMINI_NATIVE = "gemini_native"
ENGINE_ID_FRAME_V1 = "frame_v1"

SCHEMA_VERSION_NATIVE_V1 = "native_v1"
SCHEMA_VERSION_FRAME_V1 = "frame_v1"

KIND_IMAGE = "image"
KIND_VIDEO = "video"

_SEGMENTS = ("engine_id", "kind", "model_name", "schema_version", "sha256")


def build_cache_key(
    *,
    engine_id: str,
    kind: str,
    model_name: str,
    schema_version: str,
    sha256: str,
) -> str:
    values = {
        "engine_id": engine_id,
        "kind": kind,
        "model_name": model_name,
        "schema_version": schema_version,
        "sha256": sha256,
    }
    for name in _SEGMENTS:
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"cache key segment {name} must be a non-empty string")
        if ":" in value:
            raise ValueError(f"cache key segment {name} must not contain ':'")
    return ":".join(values[name] for name in _SEGMENTS)
