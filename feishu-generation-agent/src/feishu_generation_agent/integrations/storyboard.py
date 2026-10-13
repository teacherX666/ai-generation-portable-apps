"""规划阶段生成分镜关键帧（每个时间段 K 张候选图）。

用户 2026-09-22：「先生成几个分镜图供用户选择，然后生成出来的视频效果会更好、
需要修改的次数也会更少」。Seedance 2.5 官方能力表里有「关键帧参考」：把多张独立
分镜图作为 reference_image 输入时，生成画面会**相对严格对齐输入图**；只给文字、
不给画面锚点，模型只能自由发挥 —— 这正是反复返工的来源。

切段是**确定性**的：时间轴已经是多分镜 prompt 的必填项
（`reference_contract.validate_seedance_prompt`），而段落正文本身就是那一段的画面
描述。所以规划模型**不需要多输出任何字段**，这里纯代码就能把每段的关键帧骨架建出来。

本模块只做纯逻辑（切帧、拼静帧提示词）；真正调图片模型、落盘、登记素材在
`graph/nodes.py`（那里才有生成器查找与轮询基础设施）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from feishu_generation_agent.domain.plan import GenerationTask, StoryboardFrame
from feishu_generation_agent.domain.reference_contract import (
    _TIMELINE_SEGMENT,
    timeline_segments,
)

#: 分镜候选图的素材来源标记（区别于文档素材，覆盖门禁只看文档素材）。
STORYBOARD_ORIGIN = "storyboard"

#: 静帧提示词里「总体设定 / 风格与约束」的摘取上限，避免把整段提示词抄进图片提示词。
_STYLE_CONTEXT_MAX_CHARS = 260


@dataclass(frozen=True, slots=True)
class StoryboardConfig:
    """分镜候选的生成参数。`candidates <= 0` 表示整体关闭。"""

    candidates: int = 2
    concurrency: int = 4
    image_size: str = "2K"

    @property
    def enabled(self) -> bool:
        return self.candidates > 0


def style_context(task: GenerationTask) -> str:
    """prompt 里**不属于任何时间段**的句子（总体设定 + 风格与约束）。

    分镜关键帧必须跟成片同一个风格（吉卜力/卡通/写实…），否则拿它当关键帧参考
    会把风格带偏。这些句子通常写在时间轴之外的段落里。
    """
    lines: list[str] = []
    for raw in (task.prompt or "").splitlines():
        line = raw.strip()
        if not line or _TIMELINE_SEGMENT.search(line):
            continue
        lines.append(line)
    context = " ".join(lines).strip()
    if len(context) > _STYLE_CONTEXT_MAX_CHARS:
        context = context[:_STYLE_CONTEXT_MAX_CHARS].rstrip("，,。;； ")
    return context


def build_storyboard_frames(task: GenerationTask) -> list[StoryboardFrame]:
    """从带时间轴的 prompt 切出每个时间段的关键帧骨架（此时还没有图）。

    没有时间轴（单镜头 / 文生视频 / 旧数据）就返回空列表 —— 上层据此跳过生成。
    """
    frames: list[StoryboardFrame] = []
    for index, start, end, text in timeline_segments(task.prompt or ""):
        if not text:
            continue
        frames.append(
            StoryboardFrame(index=index, start=start, end=end, prompt=text)
        )
    return frames


def keyframe_prompt(task: GenerationTask, frame: StoryboardFrame) -> str:
    """把「这一段的正文」拼成一张静帧的图片提示词。

    这一段是**第 start-end 秒**的画面，所以描述里只保留该段的场景/主体/动作瞬间，
    再补上成片的整体风格与画质约束。
    """
    parts = [
        f"《{task.title}》第 {frame.start:g}-{frame.end:g} 秒的关键帧静帧",
        frame.prompt,
    ]
    context = style_context(task)
    if context:
        parts.append(context)
    parts.append(
        f"{task.aspect_ratio} 构图，单帧画面，主体明确、构图完整、光影自然，"
        "画面清晰锐利；不要字幕、不要水印、不要 logo"
    )
    return "。".join(part.strip("。") for part in parts if part.strip()) + "。"


def keyframe_reference_line(frames: list[StoryboardFrame], first_token_index: int) -> str:
    """生成视频时，把选中的关键帧在 prompt 里点名。

    官方提示词指南：参考素材要写清「参考什么」；关键帧参考要说明每张图对应哪一段。
    `first_token_index` 是这批关键帧在 @图片N 编号里的起始序号（文档参考图排在前面）。
    """
    lines: list[str] = []
    for offset, frame in enumerate(frames):
        if not frame.selected:
            continue
        token = f"@图片{first_token_index + offset}"
        lines.append(
            f"{token} 是第 {frame.start:g}-{frame.end:g} 秒的关键帧，"
            "该时间段画面严格对齐这张图的构图与主体"
        )
    if not lines:
        return ""
    return "关键帧参考：" + "；".join(lines) + "。"


def merge_selection(
    current: list[StoryboardFrame], incoming: object
) -> list[dict[str, object]]:
    """按 index 把前端回传的选图结果并进服务端的帧，**保留服务端生成的候选图**。

    前端只该回传 `{index, selected}`（候选图是服务端生成的，前端既不该也无法重传）。
    只认自己生成的 asset_id，防止前端塞一个任意 id 进来。
    """
    by_index = {frame.index: frame for frame in (current or [])}
    if not isinstance(incoming, list):
        return [frame.model_dump(mode="json") for frame in (current or [])]
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            continue
        frame = by_index.get(index)
        if frame is None:
            continue
        selected = raw.get("selected")
        if selected is not None and selected not in frame.candidates:
            continue
        frame.selected = selected
        if selected is not None:
            # 用户重新点了某张候选 → 撤销「这一段不要关键帧」的标记。
            frame.selection_cleared = False
    return [frame.model_dump(mode="json") for frame in by_index.values()]


def default_selection(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    """用户没点过就按「第一张候选」算（契约里的默认行为）。

    不做这一步，用户直接点批准会**一张关键帧都不带**（2026-09-22 实跑踩到：
    候选图都生成了，`selected` 全是 null）。

    但**用户显式删掉过**这一段的关键帧时（`selection_cleared`）不补默认值 ——
    「删了 = 这一段不要关键帧」，补一张就又是「删了等于没删」。
    """
    filled: list[dict[str, object]] = []
    for frame in frames:
        candidates = frame.get("candidates") or []
        selected = frame.get("selected")
        if not selected and candidates and not frame.get("selection_cleared"):
            selected = candidates[0]
        filled.append({**frame, "selected": selected})
    return filled


def reference_ids_for_selection(
    frames: list[Any],
    generated_ids: set[str] | None = None,
) -> set[str]:
    """`apply_selection()` 应该从 `reference_images` 里摘掉再重新追加的 asset_id 集合。

    规则只有一条：**这次会被重新追加进去的图**才算分镜图，也就是
    「服务端生成的分镜候选」∪「当前被某一段选中的图」。

    为什么不能把 `reference_plan` 里所有 `role="shot"` 的图都算进去（旧写法）：
    某张 shot 图如果**没被任何一段选中**（例如那一段的候选全生成失败），它会被
    从参考图里摘掉、又没人把它加回来 —— 参考图就这样静默少一张。而复用的 shot 图
    必然被它那一段选中，所以「选中集合」已经覆盖了复用的情况，不需要额外并 shot id。
    """
    ids: set[str] = {
        str(asset_id) for asset_id in (generated_ids or set()) if asset_id
    }
    for frame in frames or []:
        selected = (
            frame.get("selected")
            if isinstance(frame, dict)
            else getattr(frame, "selected", None)
        )
        if selected:
            ids.add(str(selected))
    return ids


def apply_selection(
    task: dict[str, object],
    frames: list[dict[str, object]],
    storyboard_ids: set[str],
) -> None:
    """把选中的关键帧变成任务的参考图 + 提示词里的一句点名（就地改）。

    幂等：每次选图先摘掉上一次加进去的（按 storyboard 素材 id 识别），再按时间段
    顺序追加。这样用户反复改选不会越堆越多，规划阶段的默认选择也能用同一套逻辑。
    """
    base_refs = sorted(
        (
            ref
            for ref in (task.get("reference_images") or [])
            if isinstance(ref, dict) and ref.get("asset_id") not in storyboard_ids
        ),
        key=lambda ref: (
            ref.get("order") if isinstance(ref.get("order"), int) else 0
        ),
    )
    selected = [
        StoryboardFrame.model_validate(frame)
        for frame in frames
        if frame.get("selected")
    ]
    refs = list(base_refs)
    for frame in selected:
        refs.append(
            {
                "asset_id": frame.selected,
                "role": "reference_image",
                "order": 0,
            }
        )
    # **顺序必须整体重排**：被摘掉的图原本可能夹在中间（例如第 1 张是分镜图、
    # 后面才是角色图），只给新追加的编号会让剩下的 base_refs 保留旧号 →
    # 「同一任务的图片顺序不能重复」直接 400（2026-09-23 实测：orders 变成
    # [2,3,3,4,5]）。这里统一从 1 开始连号，相对顺序不变。
    for order, ref in enumerate(refs, start=1):
        ref["order"] = order
    task["reference_images"] = refs
    prompt = str(task.get("prompt") or "")
    marker = "\n关键帧参考："
    if marker in prompt:
        prompt = prompt.split(marker)[0].rstrip()
    line = keyframe_reference_line(selected, len(base_refs) + 1)
    task["prompt"] = f"{prompt}\n{line}" if line else prompt
