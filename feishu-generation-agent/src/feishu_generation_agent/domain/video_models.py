from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VideoModelCapability:
    key: str
    model: str
    label: str
    duration_min: int
    duration_max: int
    default_duration: int
    resolutions: tuple[str, ...]
    default_resolution: str
    aspect_ratios: tuple[str, ...]
    default_aspect_ratio: str
    max_output_count: int
    supports_audio: bool = True

    def public_payload(self, *, configured: bool) -> dict[str, Any]:
        return {
            "name": self.key,
            "label": self.label,
            "mode": "cloud",
            "configured": configured,
            "model": self.model,
            "capabilities": {
                "duration_min": self.duration_min,
                "duration_max": self.duration_max,
                "default_duration": self.default_duration,
                "resolutions": list(self.resolutions),
                "default_resolution": self.default_resolution,
                "aspect_ratios": list(self.aspect_ratios),
                "default_aspect_ratio": self.default_aspect_ratio,
                "max_output_count": self.max_output_count,
                "supports_audio": self.supports_audio,
            },
        }


VIDEO_MODELS: tuple[VideoModelCapability, ...] = (
    VideoModelCapability(
        key="seedance2.0",
        model="ep-20260912121738-vtd78",
        label="Seedance 2.0 Mini",
        duration_min=4,
        duration_max=15,
        default_duration=10,
        resolutions=("480p", "720p", "1080p", "4k"),
        default_resolution="720p",
        aspect_ratios=(
            "21:9",
            "16:9",
            "4:3",
            "1:1",
            "3:4",
            "9:16",
            "adaptive",
        ),
        default_aspect_ratio="16:9",
        max_output_count=4,
    ),
    VideoModelCapability(
        key="seedance2.5",
        model="doubao-seedance-2-5-260628",
        label="Seedance 2.5（最长 30 秒）",
        duration_min=4,
        duration_max=30,
        default_duration=10,
        resolutions=("480p", "720p"),
        default_resolution="720p",
        aspect_ratios=(
            "21:9",
            "16:9",
            "4:3",
            "1:1",
            "3:4",
            "9:16",
            "adaptive",
        ),
        default_aspect_ratio="16:9",
        max_output_count=4,
    ),
)

VIDEO_MODEL_BY_KEY = {item.key: item for item in VIDEO_MODELS}
VIDEO_MODEL_BY_ID = {item.model: item for item in VIDEO_MODELS}
DEFAULT_VIDEO_MODEL_KEY = "seedance2.5"

# 同一个能力档位会被多种写法指代：火山方舟的模型名（doubao-…）与 endpoint id
# （ep-…）都会出现在 .env、计划文件和前端里。漏认一个，下面就会静默落到 fallback，
# 于是 2.0 的模型拿到 2.5 的时长上限——正是 2026-09-15 那起
# generation_invalid_duration 事故（Seedance 2.5 任务配了 2.0 端点与 15s 上限）的成因模式。
# 只登记能确认同档位的别名；拿不准的（例如 2.0 fast 的实际上限未核实）宁可让它认不出来。
VIDEO_MODEL_ALIASES: dict[str, str] = {
    "doubao-seedance-2-0-260128": "seedance2.0",
}


def resolve_video_model_key(
    value: str | None,
    *,
    fallback: str = DEFAULT_VIDEO_MODEL_KEY,
) -> str | None:
    if not isinstance(value, str):
        return fallback if fallback in VIDEO_MODEL_BY_KEY else None
    normalized = value.strip()
    if not normalized:
        return fallback if fallback in VIDEO_MODEL_BY_KEY else None
    if normalized in VIDEO_MODEL_BY_KEY:
        return normalized
    if normalized in VIDEO_MODEL_BY_ID:
        return VIDEO_MODEL_BY_ID[normalized].key
    if normalized in VIDEO_MODEL_ALIASES:
        return VIDEO_MODEL_ALIASES[normalized]
    if normalized == "seedance":
        return fallback if fallback in VIDEO_MODEL_BY_KEY else DEFAULT_VIDEO_MODEL_KEY
    # 认不出的标识必须 fail closed：绝不能静默套用 fallback 的能力上限。
    return None


def _ratio_value(value: str) -> float | None:
    normalized = value.strip().lower().replace("×", "x").replace("*", "x")
    if normalized == "adaptive":
        return None
    width, separator, height = normalized.partition(":")
    if not separator:
        width, separator, height = normalized.partition("x")
    if not separator:
        return None
    try:
        width_value = float(width)
        height_value = float(height)
    except ValueError:
        return None
    if width_value <= 0 or height_value <= 0:
        return None
    return width_value / height_value


def _nearest_aspect_ratio(value: str | None, capability: VideoModelCapability) -> str:
    if value in capability.aspect_ratios:
        return str(value)
    target = _ratio_value(value or "")
    if target is None:
        return capability.default_aspect_ratio
    opaque = [
        item
        for item in capability.aspect_ratios
        if item != "adaptive" and _ratio_value(item) is not None
    ]
    if not opaque:
        return capability.default_aspect_ratio
    return min(
        opaque,
        key=lambda item: abs(float(_ratio_value(item)) - target),
    )


def _supported_resolution(
    value: str | None,
    capability: VideoModelCapability,
) -> str:
    normalized = (value or "").strip().lower().replace("×", "x")
    if normalized in capability.resolutions:
        return normalized
    rank = {"480p": 1, "720p": 2, "1080p": 3, "4k": 4}
    requested_rank = rank.get(normalized)
    if requested_rank is None:
        return capability.default_resolution
    lower = [
        item
        for item in capability.resolutions
        if rank.get(item, 0) <= requested_rank
    ]
    if lower:
        return max(lower, key=lambda item: rank.get(item, 0))
    return capability.default_resolution


def normalize_video_task_payload(
    payload: dict[str, Any],
    *,
    default_model_key: str,
    max_output_count: int,
) -> tuple[dict[str, Any], list[str]]:
    if payload.get("task_type") != "image_to_video":
        return payload, []
    model_key = resolve_video_model_key(
        payload.get("video_provider") or default_model_key,
        fallback=default_model_key,
    )
    if model_key is None:
        return payload, ["视频任务缺少可用模型"]
    capability = VIDEO_MODEL_BY_KEY[model_key]
    normalized = dict(payload)
    warnings: list[str] = []

    normalized["video_provider"] = model_key
    duration = normalized.get("duration")
    if not isinstance(duration, int) or isinstance(duration, bool):
        normalized["duration"] = capability.default_duration
    else:
        clamped = min(
            capability.duration_max,
            max(capability.duration_min, duration),
        )
        if clamped != duration:
            warnings.append(
                f"视频时长已按 {capability.label} 调整为 {clamped} 秒"
            )
        normalized["duration"] = clamped

    resolution = normalized.get("resolution")
    normalized_resolution = _supported_resolution(
        resolution if isinstance(resolution, str) else None,
        capability,
    )
    if (
        isinstance(resolution, str)
        and resolution.strip().lower() != normalized_resolution
    ):
        warnings.append(
            f"分辨率已按 {capability.label} 调整为 {normalized_resolution}"
        )
    normalized["resolution"] = normalized_resolution

    aspect_ratio = normalized.get("aspect_ratio")
    normalized_ratio = _nearest_aspect_ratio(
        aspect_ratio if isinstance(aspect_ratio, str) else None,
        capability,
    )
    if isinstance(aspect_ratio, str) and aspect_ratio != normalized_ratio:
        warnings.append(
            f"画面比例已按 {capability.label} 调整为 {normalized_ratio}"
        )
    normalized["aspect_ratio"] = normalized_ratio

    output_count = normalized.get("output_count", 1)
    if not isinstance(output_count, int) or isinstance(output_count, bool):
        output_count = 1
    normalized["output_count"] = min(
        max(1, output_count),
        capability.max_output_count,
        max_output_count,
    )
    if normalized["output_count"] != output_count:
        warnings.append(
            f"生成数量已按 {capability.label} 调整为 {normalized['output_count']}"
        )
    return normalized, warnings
