"""用 ds4.1 的多模态能力**直接看视频**（不抽帧）。

用户要求（2026-09-17）：「不要抽帧，直接上传视频」+「视频基本上都没有能作为参考图的，
只能作为分镜参考」。

实测（火山方舟 `deepseek-v4-1-flash-260910`，chat/completions）：
  - `{"type":"image_url"}` 放 PNG → 200，答对颜色 ✓
  - `{"type":"video_url"}` 放 MP4 → 200，答对颜色 ✓（真的看了视频）
  - 视频塞进 `image_url` → 400 InvalidParameter（必须用 `video_url`）

所以文档里的参考视频整段送进模型，产出**分镜参考**用的文字描述，而不是被抽成一帧
冒充参考图。视频本体不内联进请求（成片动辄十几 MB），统一先传到公开图床拿 https
链接 —— 与人像通道上传素材用的是同一套 `PublicMediaHost`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VideoReferenceAnalysis,
    VideoReferenceKind,
)
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)

_LOGGER = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你在看一段**参考视频**（文档里的素材，不是成片）。请判断它在给创作提供什么"
    "参考，只输出 JSON：\n"
    '{"kind":"character|camera_movement|editing_style|scene_style|other",'
    '"summary":"中文描述","uncertainties":["不确定的点"]}\n'
    "summary 要具体到能用来写分镜：画面里有什么主体、动作怎么发生（因果顺序）、"
    "镜头怎么运动、剪辑节奏、场景与光线、画风。不要复述这份说明。"
)

_USER_PROMPT = "这是参考视频，请按要求输出 JSON。"


async def describe_output_videos(
    analyzer: Any,
    artifacts: list[Any] | None,
    *,
    limit: int = 1,
) -> str:
    """把**上一版成片**交给能看视频的模型，返回「实际画面」描述。

    用户要求（2026-09-17）：返工时把上一版成片**直接上传视频**（不抽帧）让模型看到
    实际画成了什么。以前返工只把「你打的字」喂给融合模型，它没见过成片，所以
    「不要参考人物形象」这类要求反复不生效 —— 现在把画面描述一起给它。

    没有分析器 / 没有视频产物 / 分析失败 → 返回空串（返工照常进行，不因为没有画面
    上下文而失败）。
    """
    if analyzer is None or not artifacts:
        return ""
    videos = [
        artifact
        for artifact in artifacts
        if str(_field(artifact, "kind") or "") == "video"
    ]
    lines: list[str] = []
    for artifact in videos[:limit]:
        local_path = _field(artifact, "local_path")
        if not local_path or not Path(str(local_path)).is_file():
            continue
        asset = MediaAsset(
            asset_id=str(_field(artifact, "artifact_id") or "artifact"),
            source_block_id=str(_field(artifact, "task_id") or ""),
            origin="generated",
            local_path=Path(str(local_path)),
            mime_type=str(_field(artifact, "mime_type") or "video/mp4"),
            size=int(_field(artifact, "size") or 0),
            sha256=str(_field(artifact, "sha256") or ""),
        )
        try:
            insight = await analyzer.analyze_video(asset, [])
        except Exception:
            _LOGGER.warning(
                "上一版成片分析失败，本次返工不带画面上下文 artifact=%s",
                asset.asset_id,
                exc_info=True,
            )
            continue
        summary = (insight.summary or "").strip()
        if not summary:
            continue
        suffix = (
            "（不确定：" + "；".join(insight.uncertainties) + "）"
            if insight.uncertainties
            else ""
        )
        lines.append(f"【{asset.asset_id}】{summary}{suffix}")
    return "\n".join(lines)


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


class DeepSeekVideoInsight:
    """把参考视频整段交给 ds4.1 分析，返回分镜参考用的文字描述。"""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: Any,
        model: str,
        public_media_host: PublicMediaHost,
        timeout: float = 300.0,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._api_key = (
            api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        )
        self._model = model
        self._public_media_host = public_media_host
        self._timeout = timeout

    async def analyze_video(
        self,
        asset: MediaAsset,
        frames: list[Any] | None = None,
    ) -> VideoReferenceAnalysis:
        """`frames` 参数仅为兼容旧调用点保留 —— 本实现**不抽帧**。"""
        del frames
        if asset.download_error is not None:
            raise RuntimeError(f"视频素材读取失败：{asset.asset_id}")
        try:
            content = Path(asset.local_path).read_bytes()
        except OSError as exc:
            raise RuntimeError(f"视频素材不可读：{asset.asset_id}") from exc
        if not content:
            raise RuntimeError(f"视频素材为空：{asset.asset_id}")

        try:
            url = await self._public_media_host.upload(
                content,
                f"{asset.asset_id}{Path(str(asset.local_path)).suffix or '.mp4'}",
                asset.mime_type or "video/mp4",
            )
        except PublicMediaUploadError as exc:
            raise RuntimeError(f"参考视频上传失败：{asset.asset_id}") from exc

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _USER_PROMPT},
                        {"type": "video_url", "video_url": {"url": url}},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"参考视频分析请求失败：{asset.asset_id}") from exc
        except ValueError as exc:
            raise RuntimeError(f"参考视频分析返回非 JSON：{asset.asset_id}") from exc

        raw = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        )
        return self._parse(asset.asset_id, raw)

    @staticmethod
    def _parse(asset_id: str, raw: Any) -> VideoReferenceAnalysis:
        if not isinstance(raw, str):
            raise RuntimeError(f"参考视频分析返回为空：{asset_id}")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"参考视频分析返回不是 JSON：{asset_id}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"参考视频分析返回不是对象：{asset_id}")
        try:
            kind = VideoReferenceKind(str(data.get("kind") or "other").strip())
        except ValueError:
            kind = VideoReferenceKind.OTHER
        summary = data.get("summary")
        uncertainties = data.get("uncertainties")
        return VideoReferenceAnalysis(
            asset_id=asset_id,
            kind=kind,
            summary=summary.strip() if isinstance(summary, str) else "",
            uncertainties=[
                item.strip()
                for item in (uncertainties if isinstance(uncertainties, list) else [])
                if isinstance(item, str) and item.strip()
            ],
        )
