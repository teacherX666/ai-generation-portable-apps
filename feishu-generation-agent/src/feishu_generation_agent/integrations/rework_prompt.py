"""返工提示词的「净化」逻辑：要求只累积不覆盖，融合失败也有安全兜底。

背景（2026-09-16）：旧实现在追加返工要求前，会先把提示词里已有的
`【返工要求】` 段**整段删掉**，于是「上一轮刚修好的问题」在下一轮必然复发。
本模块把「历次要求」从提示词正文里解耦出来单独累积，并保证三件事：

1. 任何一次返工都不会丢掉历史要求（`merge_requirements`）；
2. 提示词永远不超过 `SEEDANCE_PROMPT_MAX_CHARS`，且**永不抛错**
   （旧实现在超长时直接让返工失败，用户拿不到补救路径）；
3. AI 融合结果必须先通过契约校验（长度 / 素材令牌 / 标记）才能采用，
   否则回退到安全拼接——坏结果绝不进生产。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from feishu_generation_agent.domain.plan import (
    SEEDANCE_PROMPT_MAX_CHARS,
    _REFERENCE_TOKEN,
)

_LOGGER = logging.getLogger(__name__)

#: AI 融合器的形状：`(原始提示词, 全部要求) -> 融合结果或 None`。
ReworkFuser = Callable[[str, list[str]], Awaitable["str | None"]]

#: 与历史数据兼容的标记；新流程不再把它写进提示词，仅在兜底拼接时使用。
REWORK_MARKER = "【返工要求】"

# 兼容历史上被写成列表的返工条目（`- xxx` / `1. xxx` / `1、xxx`）。
_LEGACY_ENTRY_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+\s*[.、)])\s*")


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text[:limit]


def split_legacy_requirements(prompt: str) -> tuple[str, list[str]]:
    """拆出提示词里历史遗留的 `【返工要求】` 段。

    返回 `(base_prompt, requirements)`。没有标记时原样返回、要求为空——
    这样老 run 的返工历史能被接住，不会在升级后凭空丢失。
    """
    text = prompt or ""
    if REWORK_MARKER not in text:
        return text, []
    head, _, tail = text.partition(REWORK_MARKER)
    requirements: list[str] = []
    for line in tail.splitlines():
        cleaned = _LEGACY_ENTRY_PREFIX.sub("", line).strip()
        if cleaned:
            requirements.append(cleaned)
    return head.rstrip(), requirements


def merge_requirements(*groups: Iterable[str]) -> list[str]:
    """按顺序累加多组要求，去重（保留首次出现的位置），忽略空白项。

    这是「说过的绝不再丢」的唯一入口：调用方只做累加，永不做覆盖。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            if not isinstance(raw, str):
                continue
            item = raw.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def reference_token_counts(text: str) -> Counter[str]:
    """统计 `@图片N` / `@视频N` / `@音频N` 多重集，用于校验融合没丢/没加素材。"""
    return Counter(_REFERENCE_TOKEN.findall(text or ""))


def is_acceptable_fusion(
    fused: str | None,
    *,
    base_prompt: str,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> bool:
    """融合结果是否可以采纳。任一契约不满足即返回 False（调用方回退拼接）。"""
    if not isinstance(fused, str):
        return False
    text = fused.strip()
    if not text or len(text) > max_chars:
        return False
    if REWORK_MARKER in text:
        return False
    return reference_token_counts(text) == reference_token_counts(base_prompt)


def rework_inputs(task: Any, feedback: str) -> tuple[str, list[str]]:
    """从任务上取出「冻结的 base + 全部历史要求 + 本次要求」。

    两条重跑路径（图节点 `review_artifacts` 与多维表格 `clone_run_for_approval`）
    都走这里，保证「只累积不覆盖」的语义只有一份实现。
    """
    legacy_base, legacy_requirements = split_legacy_requirements(
        getattr(task, "prompt", "") or ""
    )
    base_prompt = getattr(task, "rework_base_prompt", None) or legacy_base
    requirements = merge_requirements(
        getattr(task, "rework_requirements", None) or [],
        legacy_requirements,
        [feedback],
    )
    return base_prompt, requirements


def parse_fusion_payload(raw: Any) -> tuple[str, list[str]] | None:
    """归一化融合结果：`(提示词, 必须避免清单)`；不合契约返回 None。

    兼容两种返回：纯文本提示词（老契约），或
    `{"prompt": "...", "must_avoid": ["..."]}`（双通道契约）。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return text, []
    if isinstance(raw, dict):
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        entries = raw.get("must_avoid")
        must_avoid: list[str] = []
        if isinstance(entries, list):
            must_avoid = [
                item.strip()
                for item in entries
                if isinstance(item, str) and item.strip()
            ]
        return prompt.strip(), must_avoid
    return None


async def build_rework_prompt(
    base_prompt: str,
    requirements: Sequence[str],
    *,
    fuse: ReworkFuser | None = None,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> tuple[str, bool, list[str]]:
    """把「原始提示词 + 全部历史返工要求」变成一条可直接生成的新提示词。

    返回 `(prompt, truncated, must_avoid)`。`must_avoid` 是融合时顺带产出的
    「必须避免」清单（口语要求已改写成可判定的物理描述），由调用方并入任务的
    `negative_constraints`——那条通道会以「必须避免：…」整块附在提交文本末尾，
    比埋在正文中段的否定句更容易被执行，同时给正文腾出长度。

    融合器缺失、抛错或结果不合约时回退安全拼接，此时 `must_avoid` 为空。
    """
    if fuse is not None:
        try:
            raw = await fuse(base_prompt, list(requirements))
        except Exception:
            _LOGGER.warning("返工提示词 AI 融合失败，回退安全拼接", exc_info=True)
            raw = None
        parsed = parse_fusion_payload(raw)
        if parsed is not None:
            fused, must_avoid = parsed
            if is_acceptable_fusion(
                fused, base_prompt=base_prompt, max_chars=max_chars
            ):
                return fused, False, must_avoid
    prompt, truncated = build_fallback_prompt(
        base_prompt, requirements, max_chars=max_chars
    )
    return prompt, truncated, []


def build_fallback_prompt(
    base_prompt: str,
    requirements: Sequence[str],
    *,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> tuple[str, bool]:
    """安全兜底：把**全部**要求拼在提示词后，永远不超长、永远不抛错。

    返回 `(prompt, truncated)`。空间不足时优先保要求、牺牲原始画面描述；
    连要求都放不下时从**最旧**的条目开始丢，最新的返工诉求优先保住。
    """
    base = base_prompt or ""
    entries = [
        item.strip()
        for item in requirements
        if isinstance(item, str) and item.strip()
    ]
    if not entries:
        return _clip(base, max_chars), len(base) > max_chars

    truncated = False
    kept = list(entries)
    while kept:
        block = REWORK_MARKER + "\n".join(kept)
        if len(block) + 1 <= max_chars:
            break
        if len(kept) == 1:
            room = max(0, max_chars - 1 - len(REWORK_MARKER))
            kept = [kept[0][:room]]
            truncated = True
            break
        kept.pop(0)
        truncated = True

    block = REWORK_MARKER + "\n".join(kept)
    base_room = max_chars - 1 - len(block)
    if base_room <= 0:
        return _clip(block, max_chars), True
    clipped_base = base[:base_room].rstrip()
    if len(clipped_base) != len(base):
        truncated = True
    return _clip(f"{clipped_base}\n{block}", max_chars), truncated