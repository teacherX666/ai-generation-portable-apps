"""清洗范围探针（2026-10-28，用户方案）：**清洗之前先让多模态模型确认"洗哪儿、哪儿不许动"**。

用户原话：「有水印，我重新清洗还是会把字给洗掉，不要洗字我只要洗水印」
        →「还是用原本的清洗，但在清洗之前先丢给模型确认清洗范围，这个可行吗」。

**为什么需要它（实测证据）**：run `82133315` 的参考图 image-2，13:18 那次清洗
把「即梦AI」角标去掉了，**同时把爱心中心的品牌字 "Jewel Loop" 也一起洗掉了** ——
而护栏判它"已去水印并采用"（最差局部差异 31 ≤ 35、受影响 0 块）。
纯灰度判据对"低对比度的浮雕字消失"是瞎的，所以只能**先知道字在哪**，再针对性地守。

产出两个用途：
1. **写进清洗指令**：把"只清理这些区域 / 这些必须原样保留"喂给图片模型；
2. **保护区守卫**：清洗后在 keep 区比边缘能量，掉了就是字被洗没了 → 拒绝、保留原图。

失败一律返回 `None`（= 没有范围信息，按老逻辑照常洗）。用户 2026-10-28 明确：
「失败也可以洗，因为我给用户选择的空间，可以用原图」。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from feishu_generation_agent.integrations.planning_media import (
    MediaUploadCache,
    build_planning_media_parts,
)
from feishu_generation_agent.integrations.reference_watermark_clean import (
    CleanupScope,
    bbox_tuple,
    scope_cache_key,
)

_LOGGER = logging.getLogger(__name__)

#: 探针结果按**图片内容 sha256** 缓存：同一张图跨运行只问一次模型。
SCOPE_CACHE_FILENAME = "cleanup-scope-cache.json"

_SCOPE_SYSTEM_PROMPT = """你是"叠加水印 vs 画面内容"的判别工具。只描述图中直接可见的东西。

铁律：
1. 只输出一个 JSON 对象，不要任何解释、Markdown 围栏或多余文字。
2. `watermarks` 只收**第三方平台 / AI 工具叠加在画面上的角标水印**
   （例如「即梦AI」「豆包AI生成」、账号名、平台 logo、半透明水印字）。
3. `keep` 收**画面本身的内容文字与标记**：品牌字样（如 Jewel Loop）、标题、
   包装与道具上的字、游戏或界面文字（计数、按钮、提示）、手绘标注与箭头。
4. bbox 用**归一化坐标 0~1**（左上角为原点；x1 > x0，y1 > y0），框要贴紧目标、宁可略大。
5. **不确定某处是不是叠加水印时，放进 `keep`** —— 宁可留着水印，也绝不许删掉画面内容。
6. 没有对应项就返回空数组，不要编造。"""

_SCOPE_USER_PROMPT = """看这张图，回答两个问题，输出 JSON：

{"watermarks": [{"label": "右下角即梦AI角标", "bbox": [0.76, 0.95, 0.99, 1.0]}],
 "keep": [{"label": "爱心中心的品牌字 Jewel Loop", "bbox": [0.3, 0.4, 0.7, 0.6]}]}

`watermarks` = 需要被清洗掉的第三方平台水印/角标；
`keep` = 洗的时候**一个像素都不许动**的画面内容文字与标记。
只输出这一个 JSON 对象。"""


def _extract_json(raw: object) -> dict[str, Any] | None:
    """从模型回复里取出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _boxes(raw: object) -> tuple[tuple[float, float, float, float], ...]:
    if not isinstance(raw, list):
        return ()
    out: list[tuple[float, float, float, float]] = []
    for item in raw:
        box = bbox_tuple(item.get("bbox") if isinstance(item, dict) else item)
        if box is not None:
            out.append(box)
    return tuple(out)


def _keeps(
    raw: object,
) -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        box = bbox_tuple(item.get("bbox"))
        if box is None:
            continue
        label = str(item.get("label") or "画面内容").strip() or "画面内容"
        out.append((label, box))
    return tuple(out)


def _probe_model(model: Any) -> Any:
    """与 `image_facts._facts_model` 同一套：关思考 + 要 JSON 输出。

    方舟上的 DeepSeek v4 必须显式下发 `thinking={"type": "disabled"}`，否则
    `content` 会返回空串（全烧在 reasoning_content 上）。
    """
    disabled = {"thinking": {"type": "disabled"}}
    if hasattr(model, "model_copy"):
        base = model.model_copy(update={"extra_body": disabled, "request_timeout": 120})
    else:
        base = model
    if hasattr(base, "bind"):
        try:
            return base.bind(response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001
            return base
    return base


def _inline_image_part(path: Path, max_side: int = 1024) -> dict[str, Any] | None:
    """把图缩到长边 `max_side` 做成 base64 data URL —— 公开图床不可用时的兜底通道。

    为什么需要：日志里实测过公开图床的 presigned 链接被上游拒（
    `Error while downloading: https://…tos-cn-beijing.volces.com/refmedia/…`），
    探针一旦拿不到链接就整条失效 → 守卫形同不存在 → 用户的字又会被洗掉。
    探针只需要"看清框在哪"，缩到 1024 足够；不缩的话 5.7MB 原图请求体太大容易被拒。
    """
    try:
        import base64
        import io

        from PIL import Image

        with Image.open(path) as opened:
            image = opened.convert("RGB")
            image.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
        }
    except Exception:  # noqa: BLE001
        _LOGGER.warning("清洗范围探针：内联缩图失败 path=%s", path, exc_info=True)
        return None


class DeepSeekCleanupScopeProbe:
    """用**同一个** DeepSeek 多模态模型（复用规划那条通道）确认清洗范围。"""

    def __init__(
        self,
        model: Any,
        *,
        public_media_host: Any,
        data_dir: Path,
    ) -> None:
        self._model = _probe_model(model)
        self._public_media_host = public_media_host
        self._scope_path = Path(data_dir) / SCOPE_CACHE_FILENAME
        # 公开链接缓存单独一份（避免和规划那份互相覆盖）。
        self._links = MediaUploadCache(Path(data_dir) / "cleanup-scope-links.json")
        self._memory: dict[str, CleanupScope] = {}

    def _cached(self, key: str) -> CleanupScope | None:
        if key in self._memory:
            return self._memory[key]
        try:
            raw = json.loads(self._scope_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        entry = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(entry, dict):
            return None
        scope = CleanupScope(
            watermarks=_boxes(entry.get("watermarks")),
            keep=_keeps(entry.get("keep")),
        )
        self._memory[key] = scope
        return scope

    def _remember(self, key: str, scope: CleanupScope) -> None:
        self._memory[key] = scope
        try:
            data: dict[str, Any] = {}
            if self._scope_path.is_file():
                loaded = json.loads(self._scope_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            data[key] = {
                "watermarks": [
                    {"label": "平台水印", "bbox": list(box)} for box in scope.watermarks
                ],
                "keep": [
                    {"label": label, "bbox": list(box)} for label, box in scope.keep
                ],
            }
            self._scope_path.parent.mkdir(parents=True, exist_ok=True)
            self._scope_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("清洗范围探针结果写缓存失败", exc_info=True)

    async def probe(self, asset: Any) -> CleanupScope | None:
        """问一次模型；**任何失败都返回 None**（照常走老的清洗逻辑）。"""
        mime = str(getattr(asset, "mime_type", "") or "")
        local = Path(str(getattr(asset, "local_path", "")))
        if not mime.startswith("image/") or not local.is_file():
            return None
        key = scope_cache_key(asset, local)
        if not key:
            return None
        cached = self._cached(key)
        if cached is not None:
            return cached

        try:
            parts = await build_planning_media_parts(
                [asset],
                public_media_host=self._public_media_host,
                cache=self._links,
                max_items=1,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("清洗范围探针：素材准备失败 %r", exc)
            parts = []
        if not parts:
            # 公开图床不可用/链接被上游拒 → 走内联 base64（缩图），别让探针整条失效。
            inline = await asyncio.to_thread(_inline_image_part, local)
            if inline is None:
                _LOGGER.warning("清洗范围探针：拿不到可用的图片数据，跳过探针")
                return None
            parts = [inline]

        messages = [
            {"role": "system", "content": _SCOPE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _SCOPE_USER_PROMPT},
                    *parts,
                ],
            },
        ]
        try:
            response = await self._model.ainvoke(messages, config={"callbacks": []})
        except Exception as exc:  # noqa: BLE001 —— 探针失败照常洗（用户口径）
            _LOGGER.warning("清洗范围探针调用失败（按老逻辑照常清洗）：%r", exc)
            return None

        raw = (
            response
            if isinstance(response, (str, dict))
            else getattr(response, "content", None)
        )
        data = _extract_json(raw)
        if data is None:
            _LOGGER.warning("清洗范围探针返回的不是可解析 JSON（按老逻辑照常清洗）")
            return None

        scope = CleanupScope(
            watermarks=_boxes(data.get("watermarks")),
            keep=_keeps(data.get("keep")),
        )
        self._remember(key, scope)
        return scope
