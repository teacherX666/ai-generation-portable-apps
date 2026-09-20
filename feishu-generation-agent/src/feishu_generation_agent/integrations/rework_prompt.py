"""返工提示词的「净化」逻辑：要求只累积不覆盖，融合失败也有安全兜底。

背景（2026-09-16）：旧实现在追加返工要求前，会先把提示词里已有的
`【返工要求】` 段**整段删掉**，于是「上一轮刚修好的问题」在下一轮必然复发。
本模块把「历次要求」从提示词正文里解耦出来单独累积，并保证三件事：

1. 任何一次返工都不会丢掉历史要求（`merge_requirements`），且**融合基准取「上一版」
   而不是冻结的第一版** —— 从第一版重写一遍会让前几轮的优化在融合有遗漏时回退
   （2026-09-17 修）；
2. 提示词永远不超过 `SEEDANCE_PROMPT_MAX_CHARS`，且**永不抛错**
   （旧实现在超长时直接让返工失败，用户拿不到补救路径）；
3. AI 融合结果必须先通过契约校验（长度 / 素材引用集合 / 标记）才能采用，
   不合约时先重试（真实模型会偶发违规），重试用尽才回退安全拼接——
   坏结果绝不进生产，且**每次拒绝都带原因进日志**（2026-09-16：这条路径
   曾经完全静默，线上只能看到「返工要求又被直接贴在末尾」）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from inspect import Parameter, signature
from typing import Any

from feishu_generation_agent.domain.plan import (
    SEEDANCE_PROMPT_MAX_CHARS,
    _REFERENCE_TOKEN,
)

_LOGGER = logging.getLogger(__name__)

#: 判重时忽略的标点与空白。
_NEGATIVE_NOISE = re.compile(r"[\s，。；、,.;:：!！?？()（）【】\[\]\"'“”‘’]")

#: 归一词表：把「同一件事的不同说法」收敛到同一个 key。
#:
#: 只收**明显等价**的说法，不做语义推断；长词写在前面（先替换「半空中」再替换
#: 「半空」）。归一只决定「算不算重复」，不改写最终保留的那条原文。
_NEGATIVE_CANONICAL: tuple[tuple[str, str], ...] = (
    ("不要", "不得"),
    ("不能", "不得"),
    ("不可", "不得"),
    ("禁止", "不得"),
    ("严禁", "不得"),
    ("切勿", "不得"),
    ("半空中", "空中"),
    ("半空", "空中"),
    ("正上方", "上方"),
    ("以外位置", "非笔尖"),
    ("其他位置", "非笔尖"),
    ("其它位置", "非笔尖"),
    ("生成", "出现"),
    ("掉落", "落下"),
    ("滴落", "落下"),
    ("掉下", "落下"),
    ("坠落", "落下"),
    ("飘落", "落下"),
    ("偏移", "偏离"),
    ("移动", "偏离"),
    ("偏斜", "偏离"),
    ("飘移", "偏离"),
    ("横向", "偏离"),
    ("斜向", "偏离"),
    ("任何", ""),
    ("所有", ""),
    ("一律", ""),
    ("完全", ""),
    ("彻底", ""),
)

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


def negative_key(item: str) -> str:
    """把一条「必须避免」归一成判重用的 key（**只用于判重，不改写保留的原文**）。"""
    text = _NEGATIVE_NOISE.sub("", item)
    for source, target in _NEGATIVE_CANONICAL:
        text = text.replace(source, target)
    return text


#: 近似重复的判定阈值（字符集合 Jaccard）。
#:
#: 归一词表只能并掉「半空中/半空/空中」这类**逐词等价**的写法；实测墨滴任务里
#: 「…上方或侧面生成」vs「…上方或侧面滴落」归一后相似度 0.75、「…横向或斜向偏移」
#: vs「…横向偏移或斜向飘落」0.85 —— 这些是同一句约束的改写。0.75 是量出来的：
#: 再低会开始吃掉**不同**规则（实测「不得出现水印或乱码文字」vs「不要出现水印、
#: 贴纸、乱码文字或字幕」相似度 0.69，必须留在两条）。
_SIMILARITY_THRESHOLD = 0.75


def _similarity(left: str, right: str) -> float:
    first, second = set(left), set(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _same_subject(left: str, right: str) -> bool:
    """只并**同一主语族**的条目（都以「墨滴…」「画面…」开头）。

    不同主语的条目长得再像也不并 —— 相似度只是启发式，不能让「人物不得悬浮」
    被「画面不得变形」吃掉。
    """
    return bool(left) and bool(right) and left[:2] == right[:2]


def _digits(text: str) -> tuple[str, ...]:
    """条目里出现的数字。"""
    return tuple(re.findall(r"\d+", text))


def _is_rewording(left: str, right: str) -> bool:
    """两条是不是「同一约束的换皮写法」。

    **数字不同的两条一律不算** —— 「镜头1 不得抖动」和「镜头2 不得抖动」、
    「约束-001」「约束-002」长得几乎一样但是不同规则（实测把提交预算裁剪的用例
    全并成一条）。
    """
    if not _same_subject(left, right) or _digits(left) != _digits(right):
        return False
    return _similarity(left, right) >= _SIMILARITY_THRESHOLD


#: 「必须避免」最多留几条 —— 否定句堆太多会把"不要出现的东西"反复喂给模型，
#: 反而加深它的印象（用户 2026-09-17 的原话）。超过就保留**最新的**几条：
#: 负向块保序（旧→新），最新的才对应这一轮要改的问题；而「无水印 / 无 Logo /
#: 画面稳定」这类通用风格约束**提示词正文里本来就写着**，丢了也不会漏。
_NEGATIVE_MAX_ITEMS = 10

#: 概念词表：把「同一件事的不同说法」映射到同一个概念标签。
#:
#: 比相似度可靠得多 —— 实测脱毛那条 57 项里，「墨滴不得脱离笔尖正下方的下落轨迹」
#: 与「墨滴下落轨迹不得偏离」集合相似度只有 0.47，但概念集合互为子集，就是同一件事。
_NEGATIVE_CONCEPTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("凭空", "半空", "空中", "以外", "其他位置", "其它位置"), "来源"),
    (("笔尖", "笔锋", "正下方"), "笔尖"),
    (("轨迹", "偏离", "偏移", "横向", "斜向", "飘落"), "轨迹"),
    (("上方", "侧面"), "侧面"),
    (("水印",), "水印"),
    (("logo", "品牌"), "标识"),
    (("乱码", "字幕", "文字", "贴纸"), "文字"),
    (("分身", "换形", "装扮", "形象突变"), "主体一致"),
    (("悬浮", "穿模", "重心", "漂浮"), "物理合理"),
    (("变形", "抖动", "跳帧", "稳定"), "画面稳定"),
    (("模糊", "锐化", "画质", "低质"), "画质"),
    (("声音", "配乐", "音效"), "声音"),
    (("人物", "真人"), "真人"),
)

#: 主语与概念的切分点。
_NEGATIVE_SUBJECT_SPLIT = re.compile(
    r"不得|不要|不能|不可|禁止|严禁|切勿|必须|应当|应该"
)


def _negative_subject(item: str) -> str:
    """取主语（前 4 个字），用于限定只在同一主语内合并。

    在**归一后的 key** 上取：「不要出现任何 logo…」先变成「不得出现logo…」，
    这样它和「不得出现 logo 或品牌特征」才落在同一个主语上。
    以否定词开头（没有主语）时退回 key 的前 4 个字，避免把「不要添加字幕」和
    「不要改变纸船颜色」当成同一主语。
    """
    key = negative_key(item)
    head = _NEGATIVE_SUBJECT_SPLIT.split(key, maxsplit=1)[0]
    return (head or key)[:4]


def _negative_concepts(item: str) -> frozenset[str]:
    lowered = item.lower()
    return frozenset(
        label
        for words, label in _NEGATIVE_CONCEPTS
        if any(word in lowered for word in words)
    )


def _negative_is_same_rule(left: str, right: str) -> bool:
    """两条是不是同一件事：主语相同 + 概念集合互为子集。

    **两边都得命中概念词表**才算：空集合是任何集合的子集，不加这条守卫会把
    「不要添加字幕」和「不要改变纸船颜色」并成一条（实测踩到）。
    """
    if _negative_subject(left) != _negative_subject(right):
        return False
    left_concepts = _negative_concepts(left)
    right_concepts = _negative_concepts(right)
    if left_concepts and right_concepts:
        return (
            left_concepts <= right_concepts or right_concepts <= left_concepts
        )
    # 有一边没命中概念词表：退回相似度判重（含「数字不同就不算同一条」的守卫，
    # 否则「约束-001」「约束-002」会被并成一条）。
    return _is_rewording(negative_key(left), negative_key(right))


def merge_negative_constraints(
    existing: Iterable[str],
    incoming: Iterable[str],
    *,
    max_items: int = _NEGATIVE_MAX_ITEMS,
) -> list[str]:
    """合并「必须避免」：同一件事只留一条，并**限制总条数**。

    用户报（2026-09-17）：「负面约束还是会叠加一堆导致反复强调某个不能出现的东西而
    加深他的印象」。实测脱毛那条 **57 条 / 925 字**，其中大量是同一件事的换皮
    （相似度抓不住，但概念集合互为子集）。

    两件事一起做：
    1. **按概念合并**：主语相同 + 概念集合互为子集 → 同一件事，保留最长写法；
    2. **限量**：超过 `max_items` 时只留最新的（否定句堆太多本身就是负向提示的反效果，
       而通用风格约束正文里已经写着）。
    """
    merged: list[str] = []
    for group in (existing, incoming):
        for raw in group:
            if not isinstance(raw, str):
                continue
            item = raw.strip()
            if not item:
                continue
            position = next(
                (
                    index
                    for index, kept in enumerate(merged)
                    if _negative_is_same_rule(kept, item)
                ),
                None,
            )
            if position is None:
                merged.append(item)
                continue
            if len(item) > len(merged[position]):
                merged[position] = item
    if len(merged) <= max_items:
        return merged
    dropped = merged[: len(merged) - max_items]
    _LOGGER.warning(
        "「必须避免」超过 %d 条，已丢弃最旧 %d 条（通用风格约束正文里有）：%s",
        max_items,
        len(dropped),
        "；".join(item[:30] for item in dropped[:6]),
    )
    return merged[len(dropped):]


def reference_token_counts(text: str) -> Counter[str]:
    """统计 `@图片N` / `@视频N` / `@音频N` 多重集，用于校验融合没丢/没加素材。"""
    return Counter(_REFERENCE_TOKEN.findall(text or ""))


#: 融合被拒后的重试次数。
#:
#: 2026-09-16 生产事故：实测同一输入、同一 temperature=0，真实模型仍会偶发
#: 违反契约（3 次里 2 次成功、1 次失败），而旧代码一次失败就回退安全拼接——
#: 用户看到的是「返工要求又变成直接贴在提示词末尾」。偶发失败应该重试。
_FUSION_ATTEMPTS = 3

#: 融合被限流（429 / TPM 超限）时的退避基数：第 N 次失败等 N×这个秒数再试。
#:
#: 2026-09-18 线上实测：融合模型 deepseek-v4-1-flash 报
#: `429 ModelAccountTpmRateLimitExceeded`，三次重试**连发**全撞上限 → 直接走
#: "把要求原样贴末尾"的兜底（用户看到的就是"正文完全不改、结尾多一坨"）。
_RATE_LIMIT_BACKOFF_SECONDS = 20.0

#: 判定"这次失败是限流"的关键词（langchain/openai 的报错文案）。
_RATE_LIMIT_MARKERS = ("429", "rate limit", "ratelimit", "tpm", "too many requests")


def _is_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def fusion_rejection_reason(
    fused: str | None,
    *,
    base_prompt: str,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> str | None:
    """融合结果为什么不合约；通过契约时返回 None。

    这是验收契约的**唯一真相**：`is_acceptable_fusion` 只是它的布尔包装，
    回退前的日志也用它。这样「为什么这次没融合」在生产上有据可查——此前这条
    路径完全静默，只能看到结果里莫名多出一段【返工要求】。

    素材引用的判据是**集合**而不是多重集：要求「提及次数一模一样」会把
    「同一张图少提了一次」这种无害改写也判死（实测 @图片1 出现 5 次→4 次被拒），
    真正要防的是「整张图丢了」和「凭空多出一张图」。
    """
    if not isinstance(fused, str):
        return "融合结果不是文本"
    text = fused.strip()
    if not text:
        return "融合结果为空"
    if len(text) > max_chars:
        return f"融合结果超长（{len(text)} > {max_chars}）"
    if REWORK_MARKER in text:
        return f"融合结果里带出了 {REWORK_MARKER} 标记"
    base_tokens = set(reference_token_counts(base_prompt))
    fused_tokens = set(reference_token_counts(text))
    lost = base_tokens - fused_tokens
    if lost:
        return "融合结果丢了素材引用：" + "、".join(sorted(lost))
    added = fused_tokens - base_tokens
    if added:
        return "融合结果多出素材引用：" + "、".join(sorted(added))
    return None


def is_acceptable_fusion(
    fused: str | None,
    *,
    base_prompt: str,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> bool:
    """融合结果是否可以采纳。任一契约不满足即返回 False（调用方回退拼接）。"""
    return (
        fusion_rejection_reason(
            fused, base_prompt=base_prompt, max_chars=max_chars
        )
        is None
    )


def rework_inputs(task: Any, feedback: str) -> tuple[str, list[str]]:
    """从任务上取出「上一版正文 + 全部历史要求 + 本次要求」。

    两条重跑路径（图节点 `review_artifacts` 与多维表格 `clone_run_for_approval`）
    都走这里，保证「只累积不覆盖」的语义只有一份实现。

    融合基准是**上一版**（`task.prompt` 去掉兜底尾巴后的正文），不是冻结的第一版。
    用第一版当基准意味着每一轮都要从最初那份重写一遍：融合只要有一点遗漏，前几轮
    已经修好的东西就回退了（2026-09-17 用户报的「甚至会把我之前的重做优化给回退」）。
    以「上一版」为基准，之前几轮的优化天然被保留，本轮只在其上叠加。

    要求清单仍然只累积不覆盖（含从兜底尾巴里救回来的旧要求）：融合器要按整份
    清单核对，漏掉任何一条都不会被静默忘记。
    """
    previous_body, legacy_requirements = split_legacy_requirements(
        getattr(task, "prompt", "") or ""
    )
    base_prompt = (
        previous_body
        or getattr(task, "rework_base_prompt", None)
        or ""
    )
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


def _accepts_visual_context(fuser: Any) -> bool:
    """融合器接不接受 `visual_context`。

    用签名探测而不是直接传：老的/测试里的融合器只收两个参数，直接传会 TypeError
    （与 planner 里判断知识库参数是同一套做法）。
    """
    try:
        parameters = signature(fuser).parameters
    except (TypeError, ValueError):
        return False
    if "visual_context" in parameters:
        return True
    return any(
        parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _ensure_requirements_in_body(
    fused: str,
    must_avoid: Sequence[str] | None,
    *,
    max_chars: int,
) -> str:
    """把只出现在「必须避免」里的要求**补写进正文**。

    用户 2026-09-18：「为什么我返工要求这么多他却不改一点提示词」—— 融合把
    「绝对不能露出手机屏幕」这类**否定式要求**全放进了 `must_avoid`、正文一个字不改；
    而调用方已按用户口径（「不要在约束里加东西，直接描述在正文就行」）**不再把
    must_avoid 并进负向约束** —— 结果这些要求彻底丢了。

    这里兜一层：正文里没提到的，作为正文句子补上去（不加「禁止项」小标题，
    直接描述）。

    **这是最后一道兜底**：正常情况下融合模型会按提示词要求"就地在正文里改"；
    只有它漏掉时才会走到这里（追加在末尾），至少不会像以前那样彻底丢要求。
    """
    missing: list[str] = []
    for raw in must_avoid or []:
        item = str(raw).strip().rstrip("。；;")
        if not item or item in fused:
            continue
        if item not in missing:
            missing.append(item)
    if not missing:
        return fused
    addition = "；".join(missing) + "。"
    room = max_chars - len(fused.rstrip()) - 1
    if room <= 0:
        return fused
    if len(addition) > room:
        addition = addition[: max(0, room - 1)].rstrip("；;，, ") + "。"
    return f"{fused.rstrip()}\n{addition}"


async def build_rework_prompt(
    base_prompt: str,
    requirements: Sequence[str],
    *,
    fuse: ReworkFuser | None = None,
    visual_context: str = "",
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> tuple[str, bool, list[str]]:
    """把「原始提示词 + 全部历史返工要求」变成一条可直接生成的新提示词。

    返回 `(prompt, truncated, must_avoid)`。`must_avoid` 是融合时顺带产出的
    「必须避免」清单（口语要求已改写成可判定的物理描述），由调用方并入任务的
    `negative_constraints`——那条通道会以「必须避免：…」整块附在提交文本末尾，
    比埋在正文中段的否定句更容易被执行，同时给正文腾出长度。

    融合器缺失、抛错或结果不合约时回退安全拼接，此时 `must_avoid` 为空。

    融合结果不合约时会**重试** `_FUSION_ATTEMPTS` 次再回退：真实模型在同一
    输入下仍会偶发违反契约，一次失败就整段贴会让用户以为「返工没生效」。
    上游抛错不重试（httpx 客户端已带 max_retries），避免真故障时把返工拖长。
    """
    if fuse is not None:
        accepts_context = _accepts_visual_context(fuse)
        for attempt in range(1, _FUSION_ATTEMPTS + 1):
            try:
                raw = await (
                    fuse(
                        base_prompt,
                        list(requirements),
                        visual_context=visual_context,
                    )
                    if accepts_context
                    else fuse(base_prompt, list(requirements))
                )
            except Exception as exc:
                if _is_rate_limited(exc) and attempt < _FUSION_ATTEMPTS:
                    # 连发重试只会继续撞 TPM 上限，等一下再来（2026-09-18 线上 429）。
                    delay = _RATE_LIMIT_BACKOFF_SECONDS * attempt
                    _LOGGER.warning(
                        "返工提示词融合被限流，%.0f 秒后重试（第 %d/%d 次）",
                        delay,
                        attempt,
                        _FUSION_ATTEMPTS,
                    )
                    await asyncio.sleep(delay)
                    continue
                _LOGGER.warning(
                    "返工提示词 AI 融合调用失败，回退安全拼接", exc_info=True
                )
                break
            parsed = parse_fusion_payload(raw)
            if parsed is None:
                _LOGGER.warning(
                    "返工提示词融合结果无法解析（第 %d/%d 次尝试）",
                    attempt,
                    _FUSION_ATTEMPTS,
                )
                continue
            fused, must_avoid = parsed
            reason = fusion_rejection_reason(
                fused, base_prompt=base_prompt, max_chars=max_chars
            )
            if reason is None:
                fused = _ensure_requirements_in_body(
                    fused, must_avoid, max_chars=max_chars
                )
                return fused, False, must_avoid
            _LOGGER.warning(
                "返工提示词融合结果不合契约（第 %d/%d 次尝试）：%s",
                attempt,
                _FUSION_ATTEMPTS,
                reason,
            )
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