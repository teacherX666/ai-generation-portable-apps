import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path


# 注意：这里必须是可变 dataclass（去掉 frozen）。飞书网页端更新偏好后，
# web/app.py 会就地改写共享对象，让图执行层的路由无需重启即可生效。
@dataclass(slots=True)
class ProviderPreferences:
    video_provider: str = "seedance2.5"
    image_provider: str = "aiport"
    #: 规划流水线：`text`（现状）或 `multimodal`（一次调用把原图/视频交给 ds4.1）。
    #: 用户 2026-09-18 要求「前端加个入口放在高级设置里，手动切」—— 走同一套偏好。
    planning_pipeline: str = "text"
    #: 每个时间段生成几张分镜关键帧候选（0 = 关闭）。用户 2026-09-22：
    #: 「把 K 的选择放在高级设置里供用户选择」；当天又要求「默认改成不启用」。
    storyboard_candidates: int = 0


#: K 的合法范围：0（关闭）到 4（再多就只是浪费额度，用户在审批页也挑不过来）。
STORYBOARD_CANDIDATES_MAX = 4


def normalize_storyboard_candidates(value: object, fallback: int = 0) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(0, min(STORYBOARD_CANDIDATES_MAX, number))


class ProviderPreferenceStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> "ProviderPreferenceStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path)
        await store.get()
        return store

    async def get(self) -> ProviderPreferences:
        async with self._lock:
            if not self._path.exists():
                return ProviderPreferences()
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ProviderPreferences()
        return ProviderPreferences(
            video_provider=str(data.get("video_provider") or "seedance2.5"),
            image_provider=str(data.get("image_provider") or "aiport"),
            planning_pipeline=str(data.get("planning_pipeline") or "text"),
            storyboard_candidates=normalize_storyboard_candidates(
                data.get("storyboard_candidates", 0)
            ),
        )

    async def save(
        self,
        *,
        video_provider: str,
        image_provider: str,
        planning_pipeline: str = "text",
        storyboard_candidates: int = 0,
    ) -> ProviderPreferences:
        preferences = ProviderPreferences(
            video_provider=video_provider.strip(),
            image_provider=image_provider.strip(),
            planning_pipeline=planning_pipeline.strip() or "text",
            storyboard_candidates=normalize_storyboard_candidates(
                storyboard_candidates
            ),
        )
        if not preferences.video_provider or not preferences.image_provider:
            raise ValueError("provider must not be blank")
        payload = json.dumps(asdict(preferences), ensure_ascii=False, indent=2)
        async with self._lock:
            self._path.write_text(payload, encoding="utf-8")
        return preferences

    async def close(self) -> None:
        return None