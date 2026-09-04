import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path


# 注意：这里必须是可变 dataclass（去掉 frozen）。飞书网页端更新偏好后，
# web/app.py 会就地改写共享对象，让图执行层的路由无需重启即可生效。
@dataclass(slots=True)
class ProviderPreferences:
    video_provider: str = "aiport"
    image_provider: str = "aiport"


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
            video_provider=str(data.get("video_provider") or "aiport"),
            image_provider=str(data.get("image_provider") or "aiport"),
        )

    async def save(
        self,
        *,
        video_provider: str,
        image_provider: str,
    ) -> ProviderPreferences:
        preferences = ProviderPreferences(
            video_provider=video_provider.strip(),
            image_provider=image_provider.strip(),
        )
        if not preferences.video_provider or not preferences.image_provider:
            raise ValueError("provider must not be blank")
        payload = json.dumps(asdict(preferences), ensure_ascii=False, indent=2)
        async with self._lock:
            self._path.write_text(payload, encoding="utf-8")
        return preferences

    async def close(self) -> None:
        return None