"""一次「任务单纯」的读图调用（用户 2026-10-27 方案 C）。

**背景**：切到 `planning_pipeline=multimodal` 之后，参考图由规划器在**同一次**调用里顺带读
（`nodes.analyze_images` 在该模式下直接返回 `vision_descriptions: []`，Claude 分析器不参与）。
那次调用同时扛着整份契约（system prompt 约 3.9 万字）+ 文档 + 素材，它的任务是「写 Seedance
提示词」而不是「读图」。实测代价（需求1「铲蜡」run `453e8074`）：把参考图里
「三个色区、每区上百颗独立小滴、右侧连成一片」概括成「三颗蜡滴（左红中蓝右浅绿）」，
把中性偏冷的光写成「暖调」，成片因此不照参考图。

**本模块补什么**：一次**只喂图 + 只读图**的调用 —— 同一个 DeepSeek 多模态模型、同一套凭据，
不换供应商、不引入跨模型转述。产出可数可见的结构化事实，两个用途：
1. **注入规划**：`VisionDescription` 随 `visions` 进规划器的用户消息（planner `_planning_prompt`）；
2. **保真校验**：`planner._check_reference_facts_fidelity` 拿它比对成片提示词里的素材描述。

**失败一律不阻断规划**：返回空列表 + 一条可读 issue。这是刻意的 —— 读图是增强，不是前置条件；
但**绝不再静默**（2026-10-27 之前 `vision_cache` 连续多天为 0 却零告警，正是排查困难的根源）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from feishu_generation_agent.domain.document import MediaAsset, VisionDescription

_LOGGER = logging.getLogger(__name__)

#: 一次读图最多带几张（与 `planning_media.PLANNING_MEDIA_MAX_ITEMS` 同一量级）。
FACTS_MAX_ITEMS = 12

_FACTS_SYSTEM_PROMPT = """你是严格的图片事实转录工具。只描述图片中直接可见的内容，不补充图片之外的信息。

铁律：
1. 必须给出**可数、可见**的事实。严禁把「大量彼此独立的小滴」写成「几颗」，
   严禁把「三个色区」写成「三颗」，严禁用「同色系」「暖光」这类模糊措辞代替实际观察。
2. 每个颜色/材质区域都要单独列一条，写清：位置、颜色、占画面的大致比例、
   形态（是**大量独立小滴**，还是**连成一整片**）、数量级（几颗 / 几十颗 / 上百颗 / 连成一片）。
3. 光线要写方向（从左/右/上/下打来）与色温（冷 / 中性 / 暖，能给开尔文就给）。
4. 桌面或背景要写材质与**明度**（浅色还是深色）。
5. visible_text 逐字抄录图片里实际可见的文字；没有就空数组，不要猜。
6. 严格只输出 JSON，不要任何解释、Markdown 代码围栏或额外说明。"""

_FACTS_USER_PROMPT = """逐张读取下面这些参考图，对每一张输出一条记录，整体输出一个 JSON 对象：

{"images": [
  {"index": 1,
   "regions": ["左上：深红色，约占画面 35%，由大量彼此独立、大小不一的小滴组成，数量级上百",
               "左下：蓝色，约占 25%，同样为大量彼此独立的小滴，排列较疏",
               "右侧：浅绿色，约占 30%，连成一整片连续的片状，表面布满细密凸起"],
   "surface": "浅色原木/浅米色木纹桌面，哑光，可见细密横向木纹",
   "camera": "正俯拍（镜头垂直向下、与桌面平行），全景深",
   "light": "从左上方打来，色温中性偏冷（约 5500K）",
   "visible_text": [],
   "subjects": ["红色蜡滴区", "蓝色蜡滴区", "浅绿色蜡片区"],
   "colors": ["深红", "蓝", "浅绿", "浅米色"],
   "uncertainties": []}
]}

`index` 与图片顺序一一对应（第 1 张图 index=1，第 2 张 index=2……）。
`regions` 必须覆盖画面里所有主要颜色/材质区域，不得只写其中一个。
只输出这一个 JSON 对象。"""


def _extract_json(raw: object) -> dict[str, Any] | None:
    """从模型回复里取出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ``` 围栏
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


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _facts_model(model: Any) -> Any:
    """按仓库约定给读图调用下发「关思考 + JSON 输出」。

    方舟上的 DeepSeek v4 **必须显式下发** `thinking={"type": "disabled"}`，否则
    `max_tokens` 会全烧在 `reasoning_content` 上、`content` 返回空串（AGENTS.md 已记）。
    读图是一次轻调用，不需要推理预算 —— 与 `DeepSeekPlanner._audit_model` 同一套写法：
    `model_copy(update=...)` 而不是 `bind(extra_body=...)`，因为 langchain 不会把
    `bind` 的 extra_body 转发到 API。

    测试桩（只有 `ainvoke`）原样返回，保持可测。
    """
    disabled = {"thinking": {"type": "disabled"}}
    if hasattr(model, "model_copy"):
        base = model.model_copy(
            update={"extra_body": disabled, "request_timeout": 120}
        )
    elif hasattr(model, "bind"):
        base = model
    else:
        return model
    if hasattr(base, "bind"):
        try:
            return base.bind(response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001 —— 不支持 response_format 就用原样
            return base
    return base


class DeepSeekImageFacts:
    """用**同一个** DeepSeek 多模态模型做一次专门的读图。"""

    def __init__(self, model: Any, *, max_items: int = FACTS_MAX_ITEMS) -> None:
        self._model = _facts_model(model)
        self._max_items = max_items

    async def read(
        self,
        assets: list[MediaAsset],
        parts: list[dict[str, Any]],
    ) -> tuple[list[VisionDescription], list[str]]:
        """`assets` 与 `parts` 必须**同序、同长**（调用方负责保证）。

        返回 `(facts, issues)`；任何异常都转成 issues，绝不抛出。
        """
        if not assets or len(assets) != len(parts):
            if assets and len(assets) != len(parts):
                return [], [
                    f"读图跳过：素材数 {len(assets)} 与媒体 part 数 {len(parts)} 不一致"
                ]
            return [], []

        pairs = list(zip(assets, parts))[: self._max_items]
        dropped = len(assets) - len(pairs)

        content: list[dict[str, Any]] = [
            {"type": "text", "text": _FACTS_USER_PROMPT}
        ]
        content.extend(part for _, part in pairs)

        messages = [
            {"role": "system", "content": _FACTS_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

        try:
            response = await self._model.ainvoke(
                messages, config={"callbacks": []}
            )
        except Exception as exc:  # noqa: BLE001 —— 读图失败绝不阻断规划
            _LOGGER.warning("专门读图调用失败：%r", exc)
            return [], [f"读图调用失败（规划照常进行）：{type(exc).__name__}: {exc}"]

        raw = (
            response
            if isinstance(response, (str, dict))
            else getattr(response, "content", None)
        )
        data = _extract_json(raw)
        if data is None:
            _LOGGER.warning("专门读图返回的内容不是可解析的 JSON")
            return [], ["读图返回的内容不是可解析的 JSON（规划照常进行）"]

        records = data.get("images")
        if not isinstance(records, list):
            return [], ["读图返回的 JSON 缺少 images 数组（规划照常进行）"]

        by_index: dict[int, dict[str, Any]] = {}
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            index = record.get("index")
            key = index if isinstance(index, int) else position + 1
            by_index.setdefault(key, record)

        facts: list[VisionDescription] = []
        issues: list[str] = []
        for position, (asset, _part) in enumerate(pairs):
            record = by_index.get(position + 1)
            if record is None:
                issues.append(f"读图未覆盖 {asset.asset_id}（第 {position + 1} 张）")
                continue
            regions = _as_str_list(record.get("regions"))
            subjects = _as_str_list(record.get("subjects")) or regions
            surface = _as_str(record.get("surface"))
            camera = _as_str(record.get("camera"))
            light = _as_str(record.get("light"))
            facts.append(
                VisionDescription(
                    asset_id=asset.asset_id,
                    subjects=subjects,
                    scene=surface,
                    style=light,
                    composition=camera,
                    characters=[],
                    actions=[],
                    visible_text=_as_str_list(record.get("visible_text")),
                    colors=_as_str_list(record.get("colors")),
                    probable_role="reference_image",
                    uncertainties=_as_str_list(record.get("uncertainties")),
                    regions=regions,
                    surface=surface,
                    camera=camera,
                    light=light,
                )
            )

        if dropped > 0:
            issues.append(
                f"读图只覆盖前 {len(pairs)} 张素材，另有 {dropped} 张未读"
            )
        return facts, issues
