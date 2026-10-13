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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
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


#: 整篇「重新生成计划」时最多从**多少条历史运行**里收集返工要求。
#:
#: 2026-10-10「铲」任务实测：用户点「重新生成计划」后新 run 从
#: `ingest_source → plan_requirements` 重跑，新任务的 `rework_requirements` 是**空列表**
#: —— 上一轮攒下的「蜡是比较硬化状态的」直接消失，用户不得不在两小时后重说一遍。
#: 于是「上一版质感对了，这一版质感又变回去了」。
CARRIED_LOOKBACK_RUNS = 20

#: 继承回来的返工要求上限（与 `_history_context_for_plan` 的默认 limit 对齐）。
CARRIED_REQUIREMENTS_LIMIT = 12


def collect_rework_requirements(
    states: Iterable[Any],
    *,
    limit: int = CARRIED_REQUIREMENTS_LIMIT,
) -> list[str]:
    """从若干**运行状态**里收集累积的返工要求（去重、保序、只留最近 limit 条）。

    传入顺序必须是**由旧到新**：截断时留住的才是最近的 requirement。
    计划存在哪条通道都能认（`draft_plan` / `approved_plan` / `task_plan`），
    坏数据（None、非 dict、非列表）一律跳过 —— 继承历史绝不能让新 run 起不来。
    """
    collected: list[str] = []
    for state in states or ():
        if not isinstance(state, Mapping):
            continue
        for plan_key in ("draft_plan", "approved_plan", "task_plan"):
            plan = state.get(plan_key)
            if not isinstance(plan, Mapping):
                continue
            tasks = plan.get("tasks")
            if not isinstance(tasks, (list, tuple)):
                continue
            for task in tasks:
                if not isinstance(task, Mapping):
                    continue
                for item in task.get("rework_requirements") or ():
                    text = str(item).strip()
                    if text and text not in collected:
                        collected.append(text)
    if limit is None or limit < 0:
        return collected
    if limit == 0:
        return []
    return collected[-limit:]


def inherit_rework_requirements(
    plan: Any,
    requirements: Sequence[str],
) -> int:
    """把继承来的返工要求并进计划里**每条任务**的 `rework_requirements`。

    返回被改写的任务数。累积语义与 `merge_requirements` 一致（继承的在前、
    去重、不覆盖任务自己已有的），所以这条要求不会在下一轮又被丢掉。
    """
    entries = merge_requirements(requirements or [])
    if not entries or not isinstance(plan, Mapping):
        return 0
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return 0
    stamped = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        existing = merge_requirements(task.get("rework_requirements") or [])
        merged = merge_requirements(entries, existing)
        if merged == existing:
            continue
        task["rework_requirements"] = merged
        stamped += 1
    return stamped


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
_FUSION_ATTEMPTS = 4

#: 融合被限流（429 / TPM 超限）时的退避基数：第 N 次失败等 N×这个秒数再试。
#:
#: 2026-09-18 线上实测：融合模型 deepseek-v4-1-flash 报
#: `429 ModelAccountTpmRateLimitExceeded`。重跑通常紧跟在"生成 + 审片"之后，
#: 那时 TPM 额度已经用掉一大截，所以退避要够长（30/60/90 秒 ≈ 3 分钟）。
#: 重跑是用户手动触发的，等一下可以接受；**绝不能**像以前那样连发撞墙后
#: 直接把要求贴到正文末尾（用户看到的就是"结尾多一坨"）。
_RATE_LIMIT_BACKOFF_SECONDS = 30.0

#: 判定"这次失败是限流"的关键词（langchain/openai 的报错文案）。
_RATE_LIMIT_MARKERS = ("429", "rate limit", "ratelimit", "tpm", "too many requests")


def _is_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


#: 镜头编号（用来卡"不许改结构"）。
_SHOT_MARKER = re.compile(r"镜头\s*\d+")

#: 只匹配**汇总段**的抬头 —— 用户在意的就是这种"结尾又多一坨"。
#:
#: 刻意不含泛化的「约束」（原文里本来就有「风格与约束：…」），也不含「返工要求」
#: （那句由 REWORK_MARKER 单独拦，正常融合文本里出现这个词不该误伤）。
_SUMMARY_BLOCK = re.compile(
    r"(补充约束|新增约束|必须避免|其他要求|补充说明"
    # 2026-09-22 实测又长出这些抬头：融合器把「镜头之间的衔接」单独写成一整段
    # 「镜头衔接：…」贴在末尾，同样属于「要求没并进正文」。
    r"|镜头衔接|段间衔接|段落衔接|衔接说明|一致性要求|连续性要求)"
)

#: 「风格与约束」段的抬头 —— 用来把结尾那一段单独切出来量长度。
_STYLE_BLOCK = re.compile(r"(风格与约束|风格约束|画面约束|约束：)")

#: 结尾段允许比基准长多少。
#:
#: 要求必须**就地并进原句**，所以增长应该发生在段落内部；尾巴上多出一大段就说明
#: 融合器又把要求罗列在结尾了。取「1.5 倍」与「+60 字」里更宽松的那个：
#: 把「无水印 / 连贯性」这类合并进风格句的正常改写（几十字）能过，而实测那种
#: 「全片动作连续流畅：…」罗列（+150 字）会被拦住。
_TAIL_GROWTH_RATIO = 1.5
_TAIL_GROWTH_TOLERANCE = 60


def _style_tail(text: str) -> str | None:
    """取「风格与约束」那一段（含抬头）；没有就返回 None。"""
    matches = list(_STYLE_BLOCK.finditer(text or ""))
    if not matches:
        return None
    return text[matches[-1].start():]


#: 只拦「新出现的【】小节」。提示词结构由技能模板固定（一句话概述 / 全局视觉设定 /
#: 详细情节 / 全局连续性与结尾约束…），融合**只该重写已有小节里的句子**。
_SECTION_HEADING = re.compile(r"【([^】]{1,14})】")


def new_section_reason(fused: str, base_prompt: str) -> str | None:
    """融合结果里出现了**原文没有的【】小节**吗？

    用户 2026-09-30（杂技小象）：「现在怎么重做又变了」。真实数据：那一轮只有一条要求
    「场景不要发生突变，动作流畅，不要人机感」，融合却**在结尾整段新增**了一个
    `【场景与动作连续性】` 小节（+212 字），一个字都没并进原有段落。

    两道旧拦截都没挡住它：

    - `_SUMMARY_BLOCK` 只认几种固定抬头（约束 / 汇总 / 补充要求 / 衔接…），新抬头不认识；
    - `trailing_growth_reason` 只是「结尾段增长 ≤ 1.5 倍」的**比例**阈值 —— 基准结尾段
      本来就长（几百字），+212 字根本没超。

    所以改成**结构性判据**：抬头集合只许变小或不变。插在中间、加在结尾都拒。
    """
    base_headings = set(_SECTION_HEADING.findall(base_prompt or ""))
    added = [
        heading
        for heading in _SECTION_HEADING.findall(fused or "")
        if heading not in base_headings
    ]
    if not added:
        return None
    unique = list(dict.fromkeys(added))
    return (
        "融合结果新增了原文没有的小节（"
        + "、".join(f"【{name}】" for name in unique)
        + "）：要求必须就地并进原有段落，不得新开小节"
    )


def trailing_growth_reason(fused: str, base_prompt: str) -> str | None:
    """融合结果是不是在**结尾新加了一大段**。

    用户 2026-09-22 的原话：「怎么重做又变成叠加在结尾了」。实际产出是
    `…风格与约束：…无水印、不变形。全片动作连续流畅：卡车行驶、绑带崩断、…无重复帧。`
    —— 用「全片动作连续流畅：」起头，**绕过了按小标题匹配的 `_SUMMARY_BLOCK`**。

    所以这里改成量长度：结尾那段比基准长了超过阈值就判死（换什么抬头都拦得住）。
    只对**有风格段**的提示词生效（单镜头/文生视频没有段落结构可言，不做这个判断）。
    """
    base_tail = _style_tail(base_prompt)
    if base_tail is None:
        return None
    fused_tail = _style_tail(fused)
    if fused_tail is None:
        return "融合结果丢了「风格与约束」段"
    allowed = max(
        int(len(base_tail) * _TAIL_GROWTH_RATIO),
        len(base_tail) + _TAIL_GROWTH_TOLERANCE,
    )
    if len(fused_tail) > allowed:
        return (
            f"融合结果在结尾新增了汇总段（结尾段 {len(base_tail)}→"
            f"{len(fused_tail)} 字，上限 {allowed}；要求必须就地并进原句）"
        )
    return None


def _same_text(left: str, right: str) -> bool:
    """去掉所有空白后比较 —— 「只删了几个换行」也算一个字没改。"""
    return "".join((left or "").split()) == "".join((right or "").split())


#: 只在汉字串里找「从画面描述抄来的短语」。
_CJK_ONLY = re.compile(r"[\u4e00-\u9fff]+")

#: 命中几条才算「把画面描述写成了约束」（单独一条可能是巧合）。
_PICTURE_COPY_HITS = 2


def _copied_from_picture(
    text: str,
    *,
    base_prompt: str,
    requirements: Sequence[str],
    visual_context: str,
) -> list[str]:
    """找出正文里「只在画面描述里出现过」的短语（≥4 个连续汉字）。

    用户 2026-09-28：「它会改一些我没有说的问题」。实测那轮把看片描述里的物件名
    （黑色多孔电木块 / 银色金属细杆 / 透光底平台 / 水泥砖墙 / 带脚轮工具柜 /
    黄色电缆）抄成了「不出现××」清单 —— 这些既不在原文里、也不在用户的任何一条
    要求里，唯一出处就是那段画面描述。

    只做**字面**判断（最长优先、互不重叠），所以正常改写不会被误伤：正常改写用到的
    措辞要么在原文里、要么在要求里，两者都跳过。
    """
    if not (visual_context or "").strip():
        return []
    allowed = (base_prompt or "") + "\n" + "\n".join(requirements or ())
    taken = [False] * len(text)
    hits: list[str] = []
    for index in range(len(text)):
        if taken[index]:
            continue
        for size in (8, 7, 6, 5, 4):
            window = text[index : index + size]
            if len(window) < size or not _CJK_ONLY.fullmatch(window):
                continue
            if window in allowed or window not in visual_context:
                continue
            hits.append(window)
            for offset in range(index, index + size):
                taken[offset] = True
            break
    return hits


#: 「声音/字幕」类小节的抬头特征。落在这类段落里的改动**不算落在画面上**。
_AUDIO_SECTION = re.compile(r"声音|音频|音效|配乐|BGM|bgm|字幕|音量|声场")

#: 要求本身就在讲声音/字幕时的特征词 —— 这类要求落在声音段是**正确**落点。
_AUDIO_REQUIREMENT = re.compile(
    r"音效|声音|音频|配乐|BGM|bgm|字幕|音量|静音|无声|响声|声场|收录"
)


def _audio_section_spans(text: str) -> list[tuple[int, int]]:
    """所有「声音/字幕」小节的 `(起点, 终点)`（从抬头到下一个抬头或文末）。"""
    headings = list(_SECTION_HEADING.finditer(text or ""))
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(headings):
        if not _AUDIO_SECTION.search(match.group(1)):
            continue
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(text)
        )
        spans.append((match.start(), end))
    return spans


def _without_audio_sections(text: str) -> str:
    """去掉所有「声音/字幕」小节后剩下的正文（去空白，便于比较）。"""
    source = text or ""
    parts: list[str] = []
    cursor = 0
    for start, end in _audio_section_spans(source):
        parts.append(source[cursor:start])
        cursor = end
    parts.append(source[cursor:])
    return "".join("".join(parts).split())


def audio_only_landing_reason(
    fused: str,
    *,
    base_prompt: str,
    requirements: Sequence[str] = (),
) -> str | None:
    """本轮要求讲的是画面，融合却**只在声音段**改了字 —— 判不合格。

    2026-10-10「铲」任务实测（run `cf40359d`）：用户本轮要求是
    「上一版质感对了，但是这一版质感又变回去了，不是硬化后的，然后铲子铲的是
    明确的红色区域，不要铲橙色」；融合结果的**唯一改动**是把
    【全局声音与字幕约束】里的「蜡片卷曲」改成「蜡片脆性断裂卷曲」（+4 字），
    **视觉段一个字没动**。旧契约拦不住：`_same_text` 只拦「逐字返回」，
    新小节 / 结尾段增长两道判据都不适用（改动发生在原有小节内部）。

    判据刻意做成「**非声音段是否被改动过**」而不是关键词匹配：
    只要融合同时改到了正文（哪怕声音段也改了），就放行；只有「画面部分一字未改、
    改动全在声音段」才拒。没有分段结构的单镜头/文生视频提示词直接跳过。
    """
    visuals = [
        str(item).strip()
        for item in requirements or ()
        if isinstance(item, str)
        and item.strip()
        and not _AUDIO_REQUIREMENT.search(item)
    ]
    if not visuals:
        return None
    if not _audio_section_spans(fused):
        return None
    if _without_audio_sections(fused) != _without_audio_sections(base_prompt):
        return None
    return (
        "融合结果只在「声音/字幕」段做了改动（画面部分一字未改），"
        f"但本轮要求讲的是画面：{visuals[0][:30]} —— "
        "视觉/材质/颜色类要求必须就地在视觉段改写"
    )


def fusion_rejection_reason(
    fused: str | None,
    *,
    base_prompt: str,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
    requirements: Sequence[str] = (),
    visual_context: str = "",
) -> str | None:
    """融合结果为什么不合约；通过契约时返回 None。

    这是验收契约的**唯一真相**：`is_acceptable_fusion` 只是它的布尔包装，
    回退前的日志也用它。这样「为什么这次没融合」在生产上有据可查——此前这条
    路径完全静默，只能看到结果里莫名多出一段【返工要求】。

    素材引用的判据是**集合**而不是多重集：要求「提及次数一模一样」会把
    「同一张图少提了一次」这种无害改写也判死（实测 @图片1 出现 5 次→4 次被拒），
    真正要防的是「整张图丢了」和「凭空多出一张图」。

    2026-09-28 新增两条，对应同一周内用户报的两个**反向**故障：

      - **原样退回**：把原文逐字（或只差空白）交回来。实测 14:05 那轮只删了 17 个
        换行符、14:17 那轮逐字节相同，两次都通过了旧契约进了生产，用户看到的是
        「本次返工没有改变提示词正文」。
      - **从画面描述发明约束**：把看片描述里的物件抄成「不许出现××」。实测 14:57
        那轮净 +85 字，列出的物件全是模型在上一版成片里看到的，用户一个都没提。
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
    if _same_text(text, base_prompt):
        return "融合结果与原文一致（没有落实本轮返工要求）"
    base_tokens = set(reference_token_counts(base_prompt))
    fused_tokens = set(reference_token_counts(text))
    lost = base_tokens - fused_tokens
    if lost:
        return "融合结果丢了素材引用：" + "、".join(sorted(lost))
    added = fused_tokens - base_tokens
    if added:
        return "融合结果多出素材引用：" + "、".join(sorted(added))
    # 结构校验（用户 2026-09-18 反复要求「不要越叠越多」）：只拦**结尾新增汇总段**。
    # 刻意不校验镜头数量 —— 融合有时会合理地合并/拆分句子，误判会让返工整轮白跑
    #（回退成"正文不变"），比放过更糟。
    if _SUMMARY_BLOCK.search(text) and not _SUMMARY_BLOCK.search(base_prompt or ""):
        return "融合结果在结尾新增了汇总段（要求必须就地并进原句）"
    # 任何**原文没有的【】小节**都是结构破坏（提示词结构由技能模板固定）。这道判据是
    # 2026-09-30 补的：上面那道只认固定抬头，下面 `trailing_growth_reason` 又只是比例阈值。
    added_sections = new_section_reason(text, base_prompt)
    if added_sections is not None:
        return added_sections
    # 小标题拦不住「全片动作连续流畅：…」这类换皮写法，所以再量一次结尾段长度。
    growth = trailing_growth_reason(text, base_prompt)
    if growth is not None:
        return growth
    # 落点判据（2026-10-10「铲」任务）：画面类要求必须落在**非声音段**的正文里。
    landing = audio_only_landing_reason(
        text, base_prompt=base_prompt, requirements=requirements
    )
    if landing is not None:
        return landing
    copied = _copied_from_picture(
        text,
        base_prompt=base_prompt,
        requirements=requirements,
        visual_context=visual_context,
    )
    if len(copied) >= _PICTURE_COPY_HITS:
        return (
            "融合结果把画面描述里的物件写成了新约束（"
            + "、".join(copied[:4])
            + "）—— 用户没提过这些；画面描述只能用来定位差距，不能当约束来源"
        )
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


def fusion_requirements(feedback: str) -> list[str]:
    """喂给融合器本次要落实的要求：**只取本轮**（用户 2026-09-29：「把累积的规则删了」）。

    历史要求不再每轮重喂。实测证据（爱心气球 `1c84009e`，清单攒到 8 条）：

    - #6「女孩手保持握紧，但气球绳仍从手中滑脱」与 #8「抬手的同时**松开气球绳子**」
      **互斥**；
    - 融合规则是「每条要求都落实，一条都不能遗漏」→ 两种说法都被写进正文
      （同一段里既有「同时手松开，气球绳子脱手」又有「绳子从她握紧的手掌上方滑脱」）
      → 正文自相矛盾，模型二选一挑了「手松开」，用户看到的仍是「松手太刻意」；
    - 同一轮它还**顺手删掉**了上一轮已经写对的 4 条约束（五指始终紧握 / 不主动张开 /
      脱离过程完整可见 / 不凭空消失）—— 药被扔了。

    历史要求**不再作为「必须全部融入」进融合**，但仍累积在 `task.rework_requirements`
    里：重新规划要靠它把历史带过去（`_history_context_for_plan`），记录也不能丢。
    旧修复本身留在**上一版正文**里（融合基准就是上一版），所以不会因此丢失。
    """
    text = (feedback or "").strip()
    return [text] if text else []


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


def _accepts_keyword(fuser: Any, name: str) -> bool:
    """融合器接不接受某个关键字参数。

    用签名探测而不是直接传：老的/测试里的融合器只收两个参数，直接传会 TypeError
    （与 planner 里判断知识库参数是同一套做法）。
    """
    try:
        parameters = signature(fuser).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _accepts_visual_context(fuser: Any) -> bool:
    return _accepts_keyword(fuser, "visual_context")


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
    media_parts: Sequence[dict[str, Any]] | None = None,
    max_chars: int = SEEDANCE_PROMPT_MAX_CHARS,
) -> tuple[str, bool, list[str]]:
    """把「原始提示词 + 全部历史返工要求」变成一条可直接生成的新提示词。

    返回 `(prompt, truncated, must_avoid)`。`must_avoid` 是融合时顺带产出的
    「必须避免」清单（口语要求已改写成可判定的物理描述），由调用方并入任务的
    `negative_constraints`——那条通道会以「必须避免：…」整块附在提交文本末尾，
    比埋在正文中段的否定句更容易被执行，同时给正文腾出长度。

    `media_parts`（用户 2026-09-28：「把调用改成一次多模态调用就行了，不要纯文本」）
    是上一版成片的视频 content part，原样透传给融合器 —— 成片跟原始提示词、返工要求
    一起进同一次请求，模型自己看片，不再读一次文本看片的转述。**每次重试都要重发**，
    否则第 2 次起模型又看不到成片了。

    融合器缺失、抛错或结果不合约时回退安全拼接，此时 `must_avoid` 为空。

    融合结果不合约时会**重试** `_FUSION_ATTEMPTS` 次再回退：真实模型在同一
    输入下仍会偶发违反契约，一次失败就整段贴会让用户以为「返工没生效」。
    上游抛错不重试（httpx 客户端已带 max_retries），避免真故障时把返工拖长。
    """
    if fuse is not None:
        accepts_context = _accepts_keyword(fuse, "visual_context")
        accepts_feedback = _accepts_keyword(fuse, "retry_feedback")
        accepts_media = _accepts_keyword(fuse, "media_parts")
        rejection: str | None = None
        for attempt in range(1, _FUSION_ATTEMPTS + 1):
            extra: dict[str, Any] = {}
            if accepts_context:
                extra["visual_context"] = visual_context
            if accepts_media and media_parts:
                # 每轮都带上成片：重试是因为上一版改得不对，模型更需要看着片改。
                extra["media_parts"] = list(media_parts)
            if accepts_feedback and rejection:
                # 同一输入原样重发 4 次毫无意义（2026-09-28 实测：融合器原样返回时
                # 旧代码就是这么白试了 3 次），把「上一次为什么不合格」一并给它。
                extra["retry_feedback"] = rejection
            try:
                raw = await fuse(base_prompt, list(requirements), **extra)
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
                fused,
                base_prompt=base_prompt,
                max_chars=max_chars,
                requirements=requirements,
                visual_context=visual_context,
            )
            if reason is None:
                # 刻意**不再**把 must_avoid 补进正文：用户 2026-09-18 明确要求
                # 「不要越叠越多」，补写只能追加到末尾，正是他要避免的形态。
                # 要求本身仍在 rework_requirements 里累积，下一轮还会带上。
                return fused, False, must_avoid
            rejection = reason
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
    """融合不可用时的兜底：**保持正文原样**，不再把要求拼到末尾。

    用户 2026-09-18（多次）：「不要越叠越多，就地在正文里改」。以前这里会把要求原样
    拼在正文末尾（带【返工要求】标记），每返工一次就多一坨 —— 正是用户最反感的那种，
    而且"结尾一坨"这个形态本身就说明融合没生效。

    现在宁可不改：要求仍然累积在 `rework_requirements` 里，下一轮融合照样带上；
    界面上会显示「本次返工没有改变提示词正文」，用户能看出这次没生效。

    `requirements` 保留参数只为兼容旧调用点（不再使用）。
    """
    del requirements
    base = base_prompt or ""
    return _clip(base, max_chars), len(base) > max_chars