"""为「一次多模态规划」准备媒体 content parts（含公开链接缓存）。

用户 2026-09-17 批准：把**原始图片/视频**直接交给 ds4.1 规划，而不是只给它视觉描述 ——
实测同一份文档，纯文本流程三次重试全废（参考图顺序/镜头编号契约），多模态一次成功。

两个工程细节：
1. **按文档顺序**产出 parts —— 契约要求 `reference_images` 按文档素材顺序排列，把媒体
   按同一顺序摆给模型，它才能一次排对；
2. **按 sha256 缓存公开链接** —— 媒体体积大（一段 7.7MB 视频），每次重跑都重传会让
   规划多花 20 秒以上（实测 11 个媒体 21.5s）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)

_LOGGER = logging.getLogger(__name__)

#: 一次规划最多塞几个媒体（超了只带前面的，后面的仍以文字描述/素材清单形式在提示词里）。
PLANNING_MEDIA_MAX_ITEMS = 12


class MediaUploadCache:
    """`sha256 → 公开 URL` 的本地缓存（一个 JSON 文件，坏了就当空）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, str] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._entries = {
                    str(key): str(value)
                    for key, value in raw.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        except FileNotFoundError:
            pass
        except Exception:
            _LOGGER.warning("媒体链接缓存读取失败，按空处理 path=%s", path, exc_info=True)

    def get(self, sha256: str) -> str | None:
        return self._entries.get(sha256) if sha256 else None

    def put(self, sha256: str, url: str) -> None:
        if not sha256 or not url:
            return
        self._entries[sha256] = url
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            _LOGGER.warning("媒体链接缓存写入失败 path=%s", self._path, exc_info=True)


def _part(asset: MediaAsset, url: str) -> dict[str, Any] | None:
    if asset.mime_type.startswith("image/"):
        return {"type": "image_url", "image_url": {"url": url}}
    if asset.mime_type.startswith("video/"):
        return {"type": "video_url", "video_url": {"url": url}}
    return None


async def build_planning_media_parts(
    assets: list[MediaAsset],
    *,
    public_media_host: PublicMediaHost,
    cache: MediaUploadCache,
    max_items: int = PLANNING_MEDIA_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """按文档顺序把可用的图片/视频变成 content parts。

    单个素材上传失败只跳过它（规划照常进行），绝不让规划因为媒体准备失败而失败。
    """
    usable = [
        asset
        for asset in assets
        if asset.download_error is None
        and asset.mime_type.startswith(("image/", "video/"))
        and Path(asset.local_path).is_file()
    ]
    if len(usable) > max_items:
        _LOGGER.warning(
            "素材 %d 个超过一次规划上限 %d，只带前 %d 个（其余仍以文字形式在提示词里）",
            len(usable),
            max_items,
            max_items,
        )
        usable = usable[:max_items]

    parts: list[dict[str, Any]] = []
    for asset in usable:
        url = cache.get(asset.sha256)
        if url is None:
            try:
                url = await public_media_host.upload(
                    Path(asset.local_path).read_bytes(),
                    f"{asset.asset_id}{Path(str(asset.local_path)).suffix or ''}",
                    asset.mime_type,
                )
            except (PublicMediaUploadError, OSError) as exc:
                _LOGGER.warning(
                    "素材上传失败，本次规划不带它 asset=%s err=%s",
                    asset.asset_id,
                    repr(exc)[:120],
                )
                continue
            cache.put(asset.sha256, url)
        part = _part(asset, url)
        if part is not None:
            parts.append(part)
    return parts
