from collections.abc import Mapping
import re
from typing import Any, Callable

from feishu_generation_agent.domain.plan import ImageReference, SEEDANCE_PROMPT_MAX_CHARS


_MEDIA_LABELS = {
    "image": ("图片", "参考图", "张参考图"),
    "video": ("视频", "参考视频", "个参考视频"),
    "audio": ("音频", "参考音频", "个参考音频"),
}
#: 单个镜头的最短时长（秒）。**当前不用于校验**（镜头数量与时间以文档分镜为准），
#: 仅留作文档/调试参考。
_MIN_SHOT_SECONDS = 2.0

_SHOT_MARKER = re.compile(
    r"镜头\s*(\d+)"
    r"(?:\s*[（(][^）)\n]{1,20}[）)])?"
    r"\s*[：:]"
)
_ABSOLUTE_SECONDS = re.compile(
    # 2026-09-17：拉丁 s 与中文「秒」一视同仁。以前只认「秒」，用户返工反馈里贴的
    # 「镜头 1（0~2s）」直接漏过校验进了提示词，禁用规则形同虚设。
    r"\d+(?:\.\d+)?\s*[-–—~～至到]\s*\d+(?:\.\d+)?\s*(?:秒|s|S)"
)
#: 带捕获组的时间轴解析：`0-2s` / `0s~2s` / `0 至 2 秒` / `[1s-4s]` 都认。
_TIMELINE_SEGMENT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)?\s*[-–—~～至到]\s*"
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)"
)


def _parse_timeline_segments(prompt: str) -> list[tuple[float, float]]:
    """按出现顺序取出提示词里的整数秒时间区间。

    只认带单位（秒/s/S）的区间，避免把「尺寸 1700-2500」这类数字范围误当时间轴。
    """
    return [
        (float(match.group(1)), float(match.group(2)))
        for match in _TIMELINE_SEGMENT.finditer(prompt)
    ]


#: 正文里的**自述总时长**写法（2026-10-10，P0-a）。
#:
#: 实测根因（`_quality_analysis/yarn/REPORT_YARN.md` §6.2 P0-a）：用户在审批页把时长
#: 5→8，只把最显眼的表头「镜头 1（0—5秒）」手改成「0—8秒」，正文里的「预计5秒」与
#: 5 秒的三段式结构原封不动 —— 应用不校验，于是原样提交，成片前 4.08 秒画面完全冻结。
#:
#: ⚠️ 刻意**不收光秃秃的「时长」**：「镜头时长 2 秒」「每段时长 3 秒」是**分镜级**
#: 时长，收进来会把正常的多段提示词判成不一致（真实语料里就有这种写法）。
_SELF_DURATION_STATED = re.compile(
    r"(?:预计|共|总计|总时长|全片时长|成片时长)\s*[:：为]?\s*"
    r"(\d+(?:\.\d+)?)\s*(?:秒|[sS]\b)"
)


def _stated_durations(prompt: str) -> list[float]:
    return [float(match.group(1)) for match in _SELF_DURATION_STATED.finditer(prompt)]


def duration_consistency_issues(
    prompt: object,
    duration: object,
) -> list[str]:
    """②-a-1：正文自述的时长必须与任务的 `duration` 一致（**阻塞** issue）。

    这是 r5「时长改了、正文没改、应用不拦」的直接兜底：只要正文里写了
    「预计 N 秒 / 共 N 秒 / 总计 N 秒 / 总时长 N 秒」，N 就必须等于 `duration`。

    只做「相等」判定，不猜哪个是对的 —— 不一致时把两边都写进提示，让用户自己改
    正文或改参数。
    """
    if not isinstance(prompt, str) or not prompt:
        return []
    if not isinstance(duration, int) or isinstance(duration, bool):
        return []
    stated = sorted({value for value in _stated_durations(prompt) if value != duration})
    if not stated:
        return []
    rendered = "、".join(f"{value:g}" for value in stated)
    return [
        f"Seedance 提示词正文自述时长（{rendered} 秒）与任务时长（{duration} 秒）不一致："
        f"请把正文里所有时长表述（如「预计 N 秒」「共 N 秒」）与时间轴一并改成 "
        f"{duration} 秒，或把任务时长改回正文写的秒数。"
        "（实测后果：时长与提示词不一致会让模型用「画面冻结」去填多出来的秒数）"
    ]


#: 「正文自述时长 ≠ 任务时长」这条阻断项的文案前缀（文案唯一来源就是上面的
#: `duration_consistency_issues`）。
STATED_DURATION_BLOCKING_PREFIX = "Seedance 提示词正文自述时长（"
#: 复用守卫判据 1（本次时长 ≠ 上一版时长）的文案前缀与「上一版秒数」抓取 ——
#: 文案里自己给了出口：「或把时长改回上一版的秒数」，用户照做之后这条就该消失。
#: 文案产生方见 `graph.runtime.GraphRuntime._reuse_guard_reasons`。
REUSE_DURATION_CHANGED_PREFIX = "本次时长是 "
_REUSE_GUARD_PREVIOUS_SECONDS = re.compile(
    r"上一版计划里的提示词是按\s*(\d+)\s*秒写的"
)

#: 已删除的「参考图保真校验」写过的阻断项前缀（2026-10-28）。
#:
#: 那条校验用正则匹配「一颗/三颗/十颗」，会误伤「数**十颗**」「十**几颗**」这种**本来就
#: 表示"多"**的写法，把正常提示词判成"写少了"并卡住审批（用户原话：「把这个阻塞删了，碍事」）。
#: 写入方已整块删除，但**存量快照里还留着这条字符串** —— run `6005a4b9`（切瑞士卷 TASK-01）
#: 就是这样：该 run 创建于我删代码之前，计划里的 `blocking_issues` 仍带着它，
#: `TaskPlan.approve()` 见非空即抛错，用户怎么都批不了。
#:
#: 处理原则与「时长族」完全一致：**派生出来的结论，在被依赖的那一刻必须按当前代码口径重算**，
#: 不能当数据存着不管。下面在同一个循环里把它清掉 —— 这个函数挂在所有合并路径
#: （读视图 / `patch_task` / `_validate_decision` / 合并客户端任务）上，清一次就全通。
LEGACY_FIDELITY_BLOCKING_PREFIX = "参考图保真校验未过"

#: 复用守卫写过的「格式类」阻断项前缀（2026-10-30 起改为**只提示不拦**）。
#:
#: 用户原话：「本来就没有图片6，他还是给我阻塞了，把这个阻塞的删了」——实锤 run
#: `e92f45bb`：提示词里只有 `@图片1~@图片4` 与 `@视频1`，快照里的阻塞项却写着
#: 「提示词里引用了 @图片6」：**判据是从当时的 prompt 推出来的存量字符串**，
#: 提示词后来修好了也没人重算它，`TaskPlan.approve()` 见非空就抛错，用户怎么改都批不了。
#:
#: 这两族（悬空引用 / 锚点空洞）现在只进 `warnings`（写入方见
#: `graph.runtime.GraphRuntime._reuse_guard_reasons`），下面把**存量字符串**一并清掉。
REFERENCE_MENTION_BLOCKING_PREFIX = "提示词引用了 @"
DANGLING_HOLE_BLOCKING_PREFIX = "提示词里有被削掉主语的空洞"
#: 上面两族现在写给用户的措辞（带「不拦审批」那句），存量条目可能没有，所以按前缀匹配。
_REFERENCE_FORMAT_BLOCKING_PREFIXES = (
    REFERENCE_MENTION_BLOCKING_PREFIX,
    DANGLING_HOLE_BLOCKING_PREFIX,
)


def refresh_duration_blocking_issues(
    payload: dict[str, Any],
    *,
    explicit_duration_edit: bool = False,
) -> None:
    """按**当前**的 `prompt` / `duration` 重算「时长不一致」阻断项（就地改 payload）。

    **为什么必须重算**（2026-10-13「小猫钓鱼」审批一直报「无效」的根因）：
    这类阻断项是**存量字符串** —— 由复用守卫（`_reuse_guard_reasons`）或改时长写进
    `blocking_issues`，但改时长（`patch_task`）与改提示词都不会重新评估它。
    实锤（run `788ab355`）：用户把任务时长从 18 改成 12、正文本来就是「共12秒」、
    时间轴 0-8s/8-12s（**两边已经一致**），可旧快照还写着「12 秒与任务时长 18 秒不一致」
    → `TaskPlan.approve()` 见 `blocking_issues` 非空就抛错 → 审批 422，
    界面上只显示「审批无效」，用户无论怎么改都批不了。

    **为什么放在 domain 层**（2026-10-21）：`blocking_issues` 是**服务端派生字段**，
    但审批请求体里带着浏览器那一份任务（`DecisionRequest.tasks`），浏览器可能拿着
    **过期快照**（改完时长还没轮询到新视图就点了批准）→ 合并进计划后
    `approved_subset()` 看到旧字符串就拒。生产实锤：run `c4163b7b` 服务端 checkpoint
    里 `duration=10`、`blocking_issues` 已空，审批仍然秒失败
    （日志堆栈 `graph/nodes.py:1716`，即 `human_approval` 节点内的 `approved_subset`）。
    因此重算必须挂在**所有合并路径**共用的 `reconcile_task_asset_coverage` 上，
    而不是只在 `patch_task` / `_validate_decision` 里各修一次。

    两族文案分头处理（只动「时长」这一族，别的不碰）：

    * 「正文自述时长（X 秒）与任务时长（Y 秒）不一致」→ **整族重算**：按当前
      `prompt`/`duration` 重新判定，一致就清掉、真不一致就补回来（口径与提交时校验
      完全一致，不会放过真的问题）。**读取视图、审批校验、改字段、合并客户端任务都要跑**。
    * 「本次时长是 X 秒，而上一版计划里的提示词是按 Y 秒写的」→ **只在用户显式改时长
      时**（`explicit_duration_edit=True`，即 `patch_task`）才按「已改回 Y」清掉。
      读取视图/合并时**绝不能清**：克隆那一步任务时长可能还没被换成新值，此时
      `duration == Y` 是「还没应用」而不是「用户改回来了」—— 误清会让 2026-10-10 的
      P0 守卫形同虚设（`test_rerun_with_new_duration_does_not_preserve_prompt_verbatim`）。
    """
    raw = payload.get("blocking_issues")
    current = [str(item) for item in raw] if isinstance(raw, list) else []
    duration = payload.get("duration")
    duration_is_int = isinstance(duration, int) and not isinstance(duration, bool)
    kept: list[str] = []
    for item in current:
        if item.startswith(STATED_DURATION_BLOCKING_PREFIX):
            continue  # 这一族下面按当前值重算
        if item.startswith(LEGACY_FIDELITY_BLOCKING_PREFIX):
            continue  # 2026-10-28：写入方已删除，存量字符串一律清掉（见常量注释）
        if item.startswith(_REFERENCE_FORMAT_BLOCKING_PREFIXES):
            # 2026-10-30：悬空引用 / 锚点空洞改为**只提示不拦**。存量快照里还留着它们，
            # 不清掉的话用户永远卡在「审批无效」上（见常量注释里的 run e92f45bb）。
            continue
        if explicit_duration_edit and item.startswith(REUSE_DURATION_CHANGED_PREFIX):
            previous = _REUSE_GUARD_PREVIOUS_SECONDS.search(item)
            if (
                previous is not None
                and duration_is_int
                and duration == int(previous.group(1))
            ):
                continue  # 用户按文案照做、把时长改回上一版 → 「时长变了」这条理由消失
        kept.append(item)
    refreshed = [
        *kept,
        *duration_consistency_issues(payload.get("prompt"), duration),
    ]
    deduped = list(dict.fromkeys(refreshed))
    if deduped != current:
        payload["blocking_issues"] = deduped


def single_whole_clip_timeline_hint(
    prompt: object,
    duration: object,
) -> str | None:
    """②-a-2：长时长单镜头只写了「一整段」时间轴时的人话提示（**非阻塞**）。

    条件：`duration >= 6` 且时间轴**恰好只有 1 段**（= 只写了「0—N 秒」这一整段，
    没有任何内部分段）。r5 正是这个形态：正文里唯一的时间信息就是
    「镜头 1（0—8秒）」，模型只能自己分配，把「开场半秒静止」放大成了 4.08 秒。

    ⚠️ 为什么是**警告**而不是阻塞 issue：真实语料里 `duration>=6` 的 504 条单镜头
    任务有 **243 条（48%）** 都是这个形态（见 `_quality_analysis/yarn-fix/`），
    把它们全部拦在审批页会造成大面积误伤；而且本文件 2026-09-22 的既定口径明写
    「文档分镜只有一行时，单镜头 prompt 不带时间轴也是完全合法的」。所以这里只
    提示、不拦，由规划阶段与用户自行决定要不要补分段。
    """
    if not isinstance(prompt, str) or not prompt:
        return None
    if not isinstance(duration, int) or isinstance(duration, bool):
        return None
    if duration < 6:
        return None
    if len(_parse_timeline_segments(prompt)) != 1:
        return None
    return (
        f"提示词只写了「一整段」时间轴（共 {duration} 秒，没有任何内部分段）："
        "模型只能自己分配节奏，容易把动作挤到后半段、前半段画面冻结。"
        f"建议把这一镜拆成绝对秒数分段（如 0-2s / 2-6s / {duration}s）并写清每段在做什么。"
    )


def timeline_segments(prompt: str) -> list[tuple[int, float, float, str]]:
    """把带时间轴的多分镜 prompt 切成 `(序号, 起始秒, 结束秒, 该段文本)`。

    时间轴已经是多分镜 prompt 的**必填项**（见 `validate_seedance_prompt`），所以
    这里不需要模型额外输出「每段画什么」—— **段落文本本身就是那一段的画面描述**，
    直接拿来当关键帧（分镜图）的生成依据。

    切段边界取三者里最靠前的：**本行末尾（换行）**、「下一个镜头标记」、「下一个时间
    区间」。契约要求「每个镜头占一行」，所以按行切最干净 —— 否则最后一个时间段会把
    后面的「风格与约束」整段吞进去（实测）。
    """
    matches = list(_TIMELINE_SEGMENT.finditer(prompt))
    if not matches:
        return []
    shot_starts = [match.start() for match in _SHOT_MARKER.finditer(prompt)]
    boundaries = sorted(
        [match.start() for match in matches[1:]] + shot_starts
    )
    segments: list[tuple[int, float, float, str]] = []
    for index, match in enumerate(matches, start=1):
        limit = next(
            (boundary for boundary in boundaries if boundary > match.end()),
            len(prompt),
        )
        newline = prompt.find("\n", match.end())
        if 0 <= newline < limit:
            limit = newline
        text = prompt[match.end():limit].strip(" ）)（(：:，,。;；\n\t")
        segments.append(
            (index, float(match.group(1)), float(match.group(2)), text)
        )
    return segments
# 图片契约用：静帧必须有光影交代（顶光/逆光/光线/光影/明暗都命中「光」）
_IMAGE_LIGHTING_KEYWORDS = ("光", "影调", "亮度", "明暗")
# 图片契约用：这些只属于视频，出现即说明 planner 误用了视频契约
_VIDEO_ONLY_VOCABULARY = (
    "运镜",
    "镜头运动",
    "推拉摇移",
    "时长",
    "音效",
    "配音",
    "旁白",
    "背景音乐",
    "BGM",
)
_GENERIC_BINDING_ONLY = re.compile(
    r"(?:"
    r"镜头|总体|以|为|把|当作|调用|读取|根据|结合|进行|"
    r"生成|制作|创作|完成|参考|使用|采用|作为|用作|用于|"
    r"展示|呈现|保持|沿用|提供|控制|决定|"
    r"高清|高画质|超清|清晰|稳定|流畅|不变形|"
    r"无水印|水印|无|有|"
    r"主要|核心|辅助|重要|关键|最终|整体|统一|唯一|优先|一致|"
    r"画面|内容|效果|作品|主体|素材|图片|视频|音频|"
    r"场景|风格|构图|动作|运镜|声音|相关|对应|图|中|的"
    r")+"
)
_CONCRETE_BINDING_SUFFIX = re.compile(
    r"^\s*(?:"
    # 2026-10-08：契约自己就要求写成「@图片N 中的具体主体」/「@图片N：具体主体」，
    # 但规划器实测最常写的是**冒号式**（`@图片1：参考紫白相间条纹太空沙立方体…`）。
    # 不认冒号 → 被判「只罗列没绑定」→ `_exclude_unused_references` 把整张图摘掉，
    # 而摘 token 会在句子里留下空洞（`只以@图片1为唯一基准` → `只以为唯一基准`）。
    r"中\s*的|中|的|：|:|作为|用作|用于|展示|呈现|保持|沿用|"
    r"提供|控制|决定|参考|[（(]"
    r")\s*(?P<description>.*)$"
)


#: 「参考 + 素材」被摘掉后留下的**无主语空洞**：`参考  中连续动作…`。
#:
#: 2026-10-08 生产实测（刮太空沙 run 9bc3fa4b）：只删 token 会同时削掉素材在文本里的
#: 唯一落点 —— `@视频1：参考戴黑色丁腈手套的手…` 变成 `：参考戴黑色丁腈手套的手…`，
#: `参考 @视频1 中连续动作…` 变成 `参考  中连续动作…`。用户看到的「规划阶段直接把
#: 参考视频排除了」有一半是这句话被削掉造成的（另一半是引用被整体摘除）。
_DANGLING_REFERENCE = re.compile(r"参考\s{1,}(?:中\s*的|中的|中|的)")


class ReferenceRemapError(ValueError):
    pass


def dangling_reference_mentions(prompt: str) -> list[str]:
    """prompt 里「参考」后面没有素材的空洞片段（用于断言不留无主语句子）。"""
    return [match.group(0) for match in _DANGLING_REFERENCE.finditer(prompt)]


def _cjk(character: str) -> bool:
    return bool(character) and bool(
        re.match(r"[\u3400-\u4dbf\u4e00-\u9fff]", character)
    )


def _mention_sits_mid_sentence(prompt: str, placeholder: str) -> bool:
    """占位符前后**都是中文字符** = 它原来插在句子中间，删掉必然断句。"""
    start = 0
    while True:
        index = prompt.find(placeholder, start)
        if index < 0:
            return False
        before = prompt[index - 1] if index > 0 else ""
        after_index = index + len(placeholder)
        after = prompt[after_index] if after_index < len(prompt) else ""
        if _cjk(before) and _cjk(after):
            return True
        start = after_index


def reference_removal_leaves_holes(
    prompt: str,
    old_references: list[ImageReference],
    new_references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> bool:
    """按 `remap_prompt_references` 的同一套 token 规则，判断「摘掉这些素材」会不会
    在中文句子里断开。

    2026-10-08 生产实测：`_exclude_unused_references` 摘掉 `image-1` 后，提示词里
    留下 `成片主体、造型、配色与材质只以为唯一基准`（原文 `只以@图片1为唯一基准`）——
    **素材语义在文本里的落点被删成了空洞**，与「规划阶段排除了参考视频」是同一类事故。
    调用方据此拒绝摘除（宁可留着一个已引用素材，也不要给模型一句缺主语的话）。
    """
    try:
        old_positions = _old_reference_positions(old_references, mime_types, prompt)
    except ReferenceRemapError:
        return False
    new_ids = {reference.asset_id for reference in new_references}
    for asset_id, media_type, index in old_positions:
        if asset_id in new_ids:
            continue
        for pattern, _renderer in _reference_patterns(media_type, index):
            for match in pattern.finditer(prompt):
                before = prompt[match.start() - 1] if match.start() > 0 else ""
                after = (
                    prompt[match.end()] if match.end() < len(prompt) else ""
                )
                if _cjk(before) and _cjk(after):
                    return True
    return False


def _drop_reference_mention(prompt: str, placeholder: str) -> str:
    """把一处素材引用**整段**删掉，不留语法空洞。

    素材在句首做主语时（`@视频1：参考…`）连冒号一起收；`参考/不参考 @视频1 中的 X`
    连引导词与「中/的」一起收。纯粹删 token 会留下「：参考…」「参考  中…」这类
    没有主语的句子（生产实测，见 `_DANGLING_REFERENCE`）。
    """
    token = re.escape(placeholder)
    prompt = re.sub(rf"{token}\s*[：:]\s*", "", prompt)
    prompt = re.sub(
        rf"(?:不)?(?:要)?参考\s*{token}\s*(?:中\s*的|中的|中|的)?\s*",
        "",
        prompt,
    )
    # 兜底：紧贴 token 的「中的 / 中 / 的」也要收掉（`@图片2 中的碗` → `碗`），
    # 否则会留下「； 中的碗；」这种半截子。
    prompt = re.sub(rf"{token}\s*(?:中\s*的|中的|中|的)", "", prompt)
    prompt = prompt.replace(placeholder, "")
    return re.sub(r"[，,、；;]\s*(?=[，,、；;。])", "", prompt)


def canonicalize_references(
    references: list[ImageReference],
) -> list[ImageReference]:
    ordered = sorted(references, key=lambda item: item.order)
    if len({item.order for item in ordered}) != len(ordered):
        raise ValueError("reference orders must be unique")
    return [
        reference.model_copy(update={"order": index})
        for index, reference in enumerate(ordered, start=1)
    ]


def reference_tokens(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> dict[str, str]:
    return {
        asset_id: f"@{_MEDIA_LABELS[media_type][0]}{media_index}"
        for asset_id, media_type, media_index in _reference_positions(
            references, mime_types
        )
    }


def remap_prompt_references(
    prompt: str,
    old_references: list[ImageReference],
    new_references: list[ImageReference],
    mime_types: Mapping[str, str],
    *,
    replacement_asset_ids: Mapping[str, str] | None = None,
) -> str:
    old_positions = {
        asset_id: (media_type, media_index)
        for asset_id, media_type, media_index in _old_reference_positions(
            old_references,
            mime_types,
            prompt,
        )
    }
    new_positions = {
        asset_id: (media_type, media_index)
        for asset_id, media_type, media_index in _reference_positions(
            new_references, mime_types
        )
    }
    rewritten = prompt
    placeholders: list[tuple[str, str | None]] = []
    replacements = replacement_asset_ids or {}
    for asset_offset, (asset_id, position) in enumerate(old_positions.items()):
        media_type, old_index = position
        for style_offset, (pattern, renderer) in enumerate(
            _reference_patterns(media_type, old_index)
        ):
            placeholder = f"\ufff0REF{asset_offset}_{style_offset}\ufff1"
            rewritten = pattern.sub(placeholder, rewritten)
            replacement = None
            target_asset_id = replacements.get(asset_id, asset_id)
            if target_asset_id in new_positions:
                new_media_type, new_index = new_positions[target_asset_id]
                replacement = renderer(new_media_type, new_index)
            placeholders.append((placeholder, replacement))

    for placeholder, replacement in placeholders:
        if replacement is not None:
            rewritten = rewritten.replace(placeholder, replacement)
            continue
        rewritten = _drop_reference_mention(rewritten, placeholder)
    return rewritten


def remap_asset_id_tokens(
    prompt: str,
    references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> str:
    canonical_tokens = reference_tokens(references, mime_types)
    media_groups: dict[str, list[tuple[str, int, str]]] = {
        "image": [],
        "video": [],
        "audio": [],
    }
    for asset_id, canonical_token in canonical_tokens.items():
        media_type = _media_type(mime_types.get(asset_id, ""))
        match = re.fullmatch(rf"{media_type}-(\d+)", asset_id)
        if match is None:
            continue
        media_groups[media_type].append(
            (asset_id, int(match.group(1)), canonical_token)
        )

    rewritten = prompt
    replacements: list[tuple[str, str]] = []
    for media_type, items in media_groups.items():
        label = _MEDIA_LABELS[media_type][0]
        count = len(items)
        uses_asset_numbering = any(
            asset_index > count
            and re.search(
                rf"@{label}{asset_index}(?!\d)",
                prompt,
            )
            is not None
            for _, asset_index, _ in items
        )
        if not uses_asset_numbering:
            continue
        for offset, (_, asset_index, canonical_token) in enumerate(items):
            placeholder = f"\ufff0ASSETREF{media_type}{offset}\ufff1"
            rewritten = re.sub(
                rf"@{label}{asset_index}(?!\d)",
                placeholder,
                rewritten,
            )
            replacements.append((placeholder, canonical_token))

    for placeholder, canonical_token in replacements:
        rewritten = rewritten.replace(placeholder, canonical_token)
    return rewritten


def has_multiple_shot_markers(prompt: str) -> bool:
    return len(_SHOT_MARKER.findall(prompt)) >= 2


def tokens_used_in_shots(prompt: str, tokens: Mapping[str, str]) -> set[str]:
    """prompt 里**真正落在某个镜头段内**的素材 token。

    与 `validate_seedance_prompt` 用同一份 `_SHOT_MARKER` 切分 —— 补齐逻辑必须
    跟校验一致，否则补了还是过不了。
    """
    matches = list(_SHOT_MARKER.finditer(prompt))
    if len(matches) < 2:
        return {token for token in tokens.values() if token in prompt}
    used: set[str] = set()
    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(prompt)
        )
        segment = prompt[match.start():end]
        used.update(token for token in tokens.values() if token in segment)
    return used


def unused_reference_assets(
    prompt: str,
    references: list[ImageReference],
    mime_types: Mapping[str, str],
    *,
    require_storyboard: bool,
) -> list[str]:
    """哪些素材**没有被具体用起来**（判定与 `validate_seedance_prompt` 完全一致）。

    - prompt 里根本没出现它的 token；
    - 多分镜时 token 没落在任何镜头段里（只被罗列）；
    - 落在镜头里但没有具体主体/场景/动作/运镜描述（笼统绑定）。
    """
    try:
        tokens = reference_tokens(list(references), mime_types)
    except ValueError:
        return []
    shot_used = tokens_used_in_shots(prompt, tokens)
    unused: list[str] = []
    for asset_id, token in tokens.items():
        if token not in prompt:
            unused.append(asset_id)
            continue
        if require_storyboard and token not in shot_used:
            unused.append(asset_id)
            continue
        if not _has_concrete_reference_binding(prompt, token):
            unused.append(asset_id)
    return unused


def validate_seedance_prompt(
    task: Mapping[str, Any],
    mime_types: Mapping[str, str],
    *,
    require_storyboard: bool,
) -> list[str]:
    prompt = task.get("prompt")
    if not isinstance(prompt, str):
        return ["Seedance prompt 必须是字符串"]

    if len(prompt) > SEEDANCE_PROMPT_MAX_CHARS:
        return [f"Seedance prompt 必须不超过 {SEEDANCE_PROMPT_MAX_CHARS} 字（当前 {len(prompt)} 字），请删除重复解释与次要描述后重试"]
    raw_references = task.get("reference_images")
    if not isinstance(raw_references, list):
        return ["Seedance reference_images 必须是列表"]
    try:
        references = [
            ImageReference.model_validate(reference)
            for reference in raw_references
        ]
    except Exception:
        return ["Seedance reference_images 无法解析"]

    issues: list[str] = []
    ordered = sorted(references, key=lambda item: item.order)
    if [reference.order for reference in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        issues.append("Seedance 参考素材 order 必须按 1…N 连续排列")
    try:
        tokens = reference_tokens(ordered, mime_types)
    except ValueError as exc:
        issues.append(str(exc))
        return issues

    for asset_id, token in tokens.items():
        if re.search(
            rf"(?<![\w-]){re.escape(asset_id)}(?![\w-])",
            prompt,
        ):
            issues.append(
                f"Seedance prompt 不得包含内部素材 ID {asset_id}"
            )
        if token not in prompt:
            issues.append(
                f"Seedance prompt 缺少素材引用 {token}"
            )
            continue
        if not _has_concrete_reference_binding(prompt, token):
            issues.append(
                f"Seedance prompt 中 {token} 必须绑定具体主体、场景、动作、"
                "运镜或声音"
            )

    if not require_storyboard:
        return issues

    # 2026-09-22：以前这里**禁止**绝对秒数，理由是「2.0 只认镜头序号」。但现在用的是
    # Seedance 2.5，官方提示词指南明确「用时间戳或镜头 N 切分，逐段描述」，并给出
    # `0s-3s：… / 3s-8s：…` 的示例 —— 时间戳是 2.5 最主要的新增控制手段。旧规则把
    # 时间轴从提示词里删干净，正是「提示词太模糊、说不清 0-2 秒发生什么」的根因。
    #
    # 时间轴只在**确实分了多个镜头**时强制：镜头数量严格等于文档的分镜，文档分镜只有
    # 一行、或要求一镜到底时，单镜头 prompt 不带时间轴也是完全合法的（2026-09-22：
    # 那条单行分镜的文档被判「必须包含镜头 1、镜头 2」→ 3 次重试后整条运行失败）。
    matches = list(_SHOT_MARKER.finditer(prompt))
    shot_numbers = [int(match.group(1)) for match in matches]
    if shot_numbers and shot_numbers != list(range(1, len(shot_numbers) + 1)):
        issues.append("Seedance 镜头编号必须唯一并按 1…N 连续排列")

    segments = _parse_timeline_segments(prompt)
    if len(matches) >= 2 and not segments:
        issues.append(
            "Seedance 多分镜 prompt 必须按整数秒时间轴分段（如「镜头 1（0-5s）：…"
            "镜头 2（5-10s）：…」），逐段写清场景、动作变化、运镜与声音"
        )
    if segments:
        if segments[0][0] != 0:
            issues.append(
                f"Seedance 时间轴必须从 0s 开始（当前从 {segments[0][0]:g}s 开始）"
            )
        for (_, previous_end), (start, _) in zip(segments, segments[1:]):
            if start != previous_end:
                issues.append(
                    f"Seedance 时间轴必须首尾相接：上一段到 {previous_end:g}s，"
                    f"下一段却从 {start:g}s 开始"
                )
                break
        duration = task.get("duration")
        if isinstance(duration, int) and not isinstance(duration, bool):
            if segments[-1][1] != duration:
                issues.append(
                    f"Seedance 时间轴必须覆盖整个视频：末段到 "
                    f"{segments[-1][1]:g}s，任务时长是 {duration}s"
                )
        # 刻意**不**校验单段时长：镜头数量与时间由文档的分镜决定，文档写 6 个
        # 1 秒镜头也是对的（用户 2026-09-22：「严格按照我的文档的剧本分镜，写了
        # 怎么分就怎么分」）。上一轮加过的「每段至少 2 秒」是错误方向，已撤掉。

    shot_segments: list[tuple[str, str]] = []
    if matches:
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(prompt)
            )
            shot_segments.append((match.group(1), prompt[match.start():end]))
    else:
        # 没有镜头标记（单镜头/一镜到底）：整条 prompt 就是那一个镜头。
        shot_segments.append(("1", prompt))

    token_values = set(tokens.values())
    tokens_used_in_shots: set[str] = set()
    # 只有存在参考素材时才要求每个镜头绑定；纯文生视频没有参考图，
    # 不应被误判为「缺少参考素材绑定」。
    #
    # 2026-09-22 放宽：不再要求「每个素材 token 都出现在某个镜头段里」。
    # Deepwhite 导演技能（已接入，见 integrations/director_skill.py）的提示词格式是
    # 「素材说明」单独成段、写明每个素材**参考什么 / 不参考什么**，再进详细情节 ——
    # 按旧规则会被判「@图片1 只被罗列但没有用于任何实际镜头」，整条运行卡在返修里。
    # 保留的底线是「素材必须真的被提到」，防止整张图凭空丢失。
    if token_values:
        for token in sorted(token_values):
            if token not in prompt:
                issues.append(f"{token} 在 prompt 里完全没有出现")

    if not any(
        keyword in prompt for keyword in ("稳定", "不变形", "连贯")
    ):
        issues.append("Seedance 多分镜 prompt 缺少画面稳定性约束")
    if "水印" not in prompt:
        issues.append("Seedance 多分镜 prompt 缺少无水印约束")
    if "logo" not in prompt.lower():
        issues.append("Seedance 多分镜 prompt 缺少无 Logo 约束")
    return issues


def validate_image_prompt(
    task: Mapping[str, Any],
    mime_types: Mapping[str, str],
) -> list[str]:
    """校验图片（image_to_image）prompt 契约。

    与 validate_seedance_prompt 并列，互不影响：视频契约要求分镜/运镜/声音，
    图片契约反过来禁止这些语汇，并要求光影描述。
    """
    prompt = task.get("prompt")
    if not isinstance(prompt, str):
        return ["图片 prompt 必须是字符串"]
    raw_references = task.get("reference_images")
    if not isinstance(raw_references, list):
        return ["图片 reference_images 必须是列表"]
    try:
        references = [
            ImageReference.model_validate(reference)
            for reference in raw_references
        ]
    except Exception:
        return ["图片 reference_images 无法解析"]

    issues: list[str] = []
    ordered = sorted(references, key=lambda item: item.order)
    if [reference.order for reference in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        issues.append("图片参考素材 order 必须按 1…N 连续排列")
    try:
        tokens = reference_tokens(ordered, mime_types)
    except ValueError as exc:
        issues.append(str(exc))
        return issues

    for asset_id, token in tokens.items():
        if re.search(rf"(?<![\w-]){re.escape(asset_id)}(?![\w-])", prompt):
            issues.append(f"图片 prompt 不得包含内部素材 ID {asset_id}")
        if token not in prompt:
            issues.append(f"图片 prompt 缺少素材引用 {token}")

    if not any(keyword in prompt for keyword in _IMAGE_LIGHTING_KEYWORDS):
        issues.append(
            "图片 prompt 缺少光影描述（例：戏剧化顶光 + 侧逆光、明媚的光线）"
        )

    for vocabulary in _VIDEO_ONLY_VOCABULARY:
        if vocabulary in prompt:
            issues.append(f"图片 prompt 禁止视频语汇 {vocabulary}")

    if _ABSOLUTE_SECONDS.search(prompt):
        issues.append("图片 prompt 禁止时间轴秒数，静帧没有时间维度")

    return issues


def _reference_positions(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> list[tuple[str, str, int]]:
    counts = {"image": 0, "video": 0, "audio": 0}
    positions: list[tuple[str, str, int]] = []
    for reference in sorted(references, key=lambda item: item.order):
        media_type = _media_type(mime_types.get(reference.asset_id, ""))
        counts[media_type] += 1
        positions.append(
            (reference.asset_id, media_type, counts[media_type])
        )
    return positions


def _old_reference_positions(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
    prompt: str,
) -> list[tuple[str, str, int]]:
    sequential = _reference_positions(references, mime_types)
    orders = sorted(reference.order for reference in references)
    if orders == list(range(1, len(orders) + 1)):
        return sequential
    if len({media_type for _, media_type, _ in sequential}) > 1:
        raise ReferenceRemapError(
            "旧任务的混合媒体参考编号存在断档，无法安全判断素材身份；"
            "请重新规划任务或重新添加参考素材"
        )

    by_asset_id = {reference.asset_id: reference for reference in references}
    positions: list[tuple[str, str, int]] = []
    for asset_id, media_type, sequential_index in sequential:
        visible_index = by_asset_id[asset_id].order
        if visible_index == sequential_index:
            positions.append((asset_id, media_type, sequential_index))
            continue
        sequential_used = _reference_mentioned(
            prompt,
            media_type,
            sequential_index,
        )
        visible_used = _reference_mentioned(
            prompt,
            media_type,
            visible_index,
        )
        if sequential_used and visible_used:
            raise ReferenceRemapError(
                "ambiguous legacy reference numbering; "
                f"both {_MEDIA_LABELS[media_type][0]}{sequential_index} and "
                f"{_MEDIA_LABELS[media_type][0]}{visible_index} are present"
            )
        positions.append(
            (
                asset_id,
                media_type,
                visible_index if visible_used else sequential_index,
            )
        )
    return positions


def _reference_mentioned(
    prompt: str,
    media_type: str,
    index: int,
) -> bool:
    return any(
        pattern.search(prompt) is not None
        for pattern, _ in _reference_patterns(media_type, index)
    )


def _has_concrete_reference_binding(prompt: str, token: str) -> bool:
    separators = "，。；;\n"
    token_pattern = re.compile(r"@(图片|视频|音频)\d+")
    for occurrence in re.finditer(re.escape(token), prompt):
        ends = [
            position
            for separator in separators
            if (position := prompt.find(separator, occurrence.end())) >= 0
        ]
        end = min(ends, default=len(prompt))
        suffix = prompt[occurrence.end() : end]
        binding = _CONCRETE_BINDING_SUFFIX.match(suffix)
        if binding is None:
            continue
        without_tokens = token_pattern.sub(
            "",
            binding.group("description"),
        )
        cjk_text = "".join(
            re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", without_tokens)
        )
        if (
            len(cjk_text) >= 2
            and _GENERIC_BINDING_ONLY.fullmatch(cjk_text) is None
        ):
            return True
    return False


def _media_type(mime_type: str) -> str:
    for candidate in ("image", "video", "audio"):
        if mime_type.startswith(f"{candidate}/"):
            return candidate
    raise ValueError(f"unsupported reference MIME type: {mime_type}")


def _reference_patterns(
    media_type: str,
    index: int,
) -> list[tuple[re.Pattern[str], Callable[[str, int], str]]]:
    plain, reference, ordinal = _MEDIA_LABELS[media_type]
    boundary = r"(?!\d)"
    return [
        (
            re.compile(rf"@{plain}{index}{boundary}"),
            lambda kind, value: f"@{_MEDIA_LABELS[kind][0]}{value}",
        ),
        (
            re.compile(rf"第\s*{index}\s*{ordinal}{boundary}"),
            lambda kind, value: (
                f"第{value}{_MEDIA_LABELS[kind][2]}"
            ),
        ),
        (
            re.compile(rf"{reference}\s*{index}{boundary}"),
            lambda kind, value: (
                f"{_MEDIA_LABELS[kind][1]}{value}"
            ),
        ),
        (
            re.compile(rf"(?<![@\u4e00-\u9fff]){plain}\s*{index}{boundary}"),
            lambda kind, value: f"{_MEDIA_LABELS[kind][0]}{value}",
        ),
    ]
