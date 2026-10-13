"""把第 8 阶段产物（逐镜视频提示词）解析成可执行的镜头列表。

模型输出的格式不完全可控，所以这里的解析器**必须容错优先**：认得出就按镜切分；
认不出就退化成「整篇当一个镜头」，并把 `parsed=False` 交给 UI 提示用户手动粘贴。
宁可退化，也不要因为格式不匹配就把用户堵死。

实测过的真实形态（2026-10-08，一次真机 08 阶段产物）：
    ## 2. Seedance 2.5 口径 · 逐镜提示词
    ### SH01（0.0–1.4s）
    <散文体提示词>

    ## 3. MiniMax H3 口径 · 逐镜提示词
    ### SH01（0.0–1.4s）
    ```text
    subject_definitions:
    <Subject 1>：…
    detailed_description:
    …
    Negative: …
    ```

因此三条关键规则：
 1. **分节优先**：有「MiniMax H3 口径」这一节就只解析那一节 —— 同一镜在 Seedance 节
    里也出现一次，不分节就会把 3 个镜头解析成 6 个。
 2. **认 SH01 这种代号**，并且时长写在标题的括号里（`0.0–1.4s` 是区间，取终点）。
 3. **剥掉 ```text 围栏**：H3 要的是围栏里的结构化正文，把围栏一起发过去是噪音。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 标签在前：镜 1 / 镜头 1 / Shot 1 / **镜头 1** / ### SH01 / 第 1 镜
_LABEL_FIRST = re.compile(
    r"^[ \t]*(?:"
    r"#{1,6}[ \t]*(?:镜头?|镜[ \t]*头?|shot|SH|S)[ \t]*#?[ \t]*"
    r"|\*{0,2}[ \t]*(?:镜头?|shot|SH|S)[ \t]*"
    r"|\*{0,2}[ \t]*第[ \t]*"
    r")"
    r"(\d{1,3})(?!\s*(?:秒|帧|个|号))"
    r"[ \t]*\*{0,2}[ \t]*[：:、.．)\-—]?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# 编号在前：1. 镜头 / 2、Shot
_NUMBER_FIRST = re.compile(
    r"^[ \t]*\*{0,2}[ \t]*(\d{1,3})[ \t]*[\.、)][ \t]*\*{0,2}[ \t]*"
    r"(?:镜头?|shot)[ \t]*\*{0,2}[ \t]*[：:、.．)\-—]?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# 时长：优先认区间（`0.0–1.4s` = 该镜占 0.0~1.4s，**时长是终点减起点**），
# 再认带标签的「时长：4 秒」，最后才认裸的「4 秒」。
_DURATION_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*[–—~～至\-]\s*(\d+(?:\.\d+)?)\s*(?:秒|s\b)",
    re.IGNORECASE,
)
_DURATION_LABEL = re.compile(
    r"(?:时长|duration|长度)\s*[：:=]?\s*(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)?",
    re.IGNORECASE,
)
_DURATION_BARE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b)", re.IGNORECASE)

_REF_IMAGE = re.compile(r"(?:@图片\s*(\d+)|<Picture\s*(\d+)>|<Subject\s*(\d+)>)", re.IGNORECASE)
_H3_MARK = re.compile(r"(mini\s*max\s*h3|海螺\s*h3|h3\s*口径|h3\s*prompt|h3\s*提示词|ref2va)", re.IGNORECASE)
_H3_SECTION = re.compile(r"(mini\s*max[^\n]*h3|海螺\s*h3|h3\s*口径)", re.IGNORECASE)
_SEEDANCE_MARK = re.compile(r"(seedance\s*2(?:\.\d)?|seedance\s*口径|即梦)", re.IGNORECASE)
_H2 = re.compile(r"(?m)^##[ \t]+(?!#)(.*)$")
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*$", re.DOTALL)
_FENCE_ANY = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```", re.DOTALL)

DEFAULT_SHOT_SECONDS = 4.0
MIN_SHOT_SECONDS = 1.0
MAX_SHOT_SECONDS = 30.0


@dataclass
class Shot:
    index: int
    title: str
    prompt: str
    seconds: float = DEFAULT_SHOT_SECONDS
    refs: list[int] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "prompt": self.prompt,
            "seconds": self.seconds,
            "refs": list(self.refs),
            "chars": len(self.prompt),
        }


def _clean(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


def _strip_fence(text: str) -> str:
    """剥掉 ```text 围栏。

    最后一块镜头后面可能还跟着 `---` 或别的内容，所以整段严格匹配失败时，
    退一步只取「开头就是围栏」的那一段。
    """
    stripped = (text or "").strip()
    match = _FENCE.match(stripped)
    if match:
        return _clean(match.group("body"))
    loose = _FENCE_ANY.search(stripped)
    if loose and not stripped[: loose.start()].strip():
        return _clean(loose.group("body"))
    return _clean(stripped)


def _sections(text: str) -> list[tuple[str, str]]:
    """按二级标题切成 [(标题, 正文)]；没有二级标题时整篇作为一节。"""
    marks = list(_H2.finditer(text))
    if not marks:
        return [("", text)]
    out: list[tuple[str, str]] = []
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        out.append((mark.group(1).strip(), text[mark.end():end]))
    return out


def h3_section(text: str) -> tuple[str, str]:
    """取「MiniMax H3 口径」那一节；取最后一个匹配（通常在后半部分）。"""
    found = ("", "")
    for title, body in _sections(text):
        if _H3_SECTION.search(title):
            found = (title, body)
    return found


def _headings(text: str) -> list[tuple[int, int, int, str]]:
    """合并两种标题写法 → [(start, end, 镜号, 标题尾巴)]，按出现位置排序且不重叠。"""
    found: list[tuple[int, int, int, str]] = []
    for pattern in (_LABEL_FIRST, _NUMBER_FIRST):
        for match in pattern.finditer(text):
            number = int(match.group(1))
            if not 1 <= number <= 999:
                continue
            found.append((match.start(), match.end(), number, (match.group(2) or "").strip()))
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    out: list[tuple[int, int, int, str]] = []
    last_end = -1
    for item in found:
        if item[0] < last_end:
            continue
        out.append(item)
        last_end = item[1]
    return out


def _split_blocks(text: str) -> list[tuple[int, str, str]]:
    """→ [(镜号, 标题, 正文)]。"""
    marks = _headings(text)
    blocks: list[tuple[int, str, str]] = []
    for position, (start, end, number, tail) in enumerate(marks):
        stop = marks[position + 1][0] if position + 1 < len(marks) else len(text)
        blocks.append((number, tail, _clean(text[end:stop])))
    return blocks


def _pick_prompt(body: str, title: str) -> str:
    """块里同时有 Seedance 与 H3 两套口径时优先取 H3；否则整块。"""
    lines = body.splitlines()
    h3_start = next((i for i, line in enumerate(lines) if _H3_MARK.search(line)), None)
    candidate = body
    if h3_start is not None:
        tail = lines[h3_start:]
        cut = next((i for i, line in enumerate(tail) if i > 0 and _SEEDANCE_MARK.search(line)), None)
        if cut:
            tail = tail[:cut]
        joined = "\n".join(tail)
        if len(re.sub(_H3_MARK, "", joined).strip(" ：:—-*\n")) >= 12:
            candidate = joined
    picked = _strip_fence(candidate)
    if len(picked) < 8:
        picked = _clean(title) or picked
    return picked


def _clamp_seconds(value: float) -> float:
    return max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, value))


def _range_seconds(blob: str) -> float:
    """`0.0–1.4s` 这种区间 → 时长 = 终点 − 起点（不是终点本身）。

    真机产物里 `### SH02（1.4–2.8s）` 是「第 2 镜占 1.4~2.8 秒」，
    时长是 1.4 秒；取终点会得到 2.8 秒，多出一倍。
    """
    match = _DURATION_RANGE.search(blob or "")
    if not match:
        return 0.0
    try:
        start, end = float(match.group(1)), float(match.group(2))
    except ValueError:
        return 0.0
    if end < start:
        start, end = end, start
    span = round(end - start, 3)      # 4.0 - 2.8 = 1.2000000000000002，先收敛掉
    return _clamp_seconds(span) if span > 0 else 0.0


def _duration_of(title: str, body: str = "") -> float:
    title, body = title or "", body or ""
    span = _range_seconds(title)              # 优先：标题里的区间
    if span:
        return span
    combined = title + "\n" + body
    match = _DURATION_LABEL.search(combined)  # 其次：显式「时长：N 秒」
    if match:
        try:
            value = float(match.group(1))
            if value > 0:
                return _clamp_seconds(value)
        except ValueError:
            pass
    span = _range_seconds(body)               # 再次：正文里的区间
    if span:
        return span
    match = _DURATION_BARE.search(combined)   # 兜底：裸的「N 秒」
    if match:
        try:
            value = float(match.group(1))
            if value > 0:
                return _clamp_seconds(value)
        except ValueError:
            pass
    return DEFAULT_SHOT_SECONDS


def _refs_of(text: str) -> list[int]:
    return sorted(
        {
            int(g)
            for match in _REF_IMAGE.finditer(text)
            for g in match.groups()
            if g and g.isdigit()
        }
    )


def _parse_body(text: str) -> tuple[list[Shot], bool]:
    blocks = _split_blocks(text)
    shots: list[Shot] = []
    seen: set[int] = set()
    for number, title, body in blocks:
        prompt = _pick_prompt(body, title)
        if len(prompt) < 4:
            continue
        index = number
        while index in seen:
            index += 1
        seen.add(index)
        shots.append(
            Shot(
                index=index,
                title=title or f"镜 {index}",
                prompt=prompt,
                seconds=_duration_of(title, body),
                refs=_refs_of(body),
            )
        )
    if not shots:
        return [], False
    shots.sort(key=lambda s: s.index)
    return shots, True


def parse_shots(text: str) -> tuple[list[Shot], bool]:
    """→ (shots, parsed)。

    parsed=False 表示没认出镜头结构，返回的是「整篇当一个镜头」的兜底结果。
    """
    raw = (text or "").strip()
    if not raw:
        return [], False

    _h3_title, h3_body = h3_section(raw)
    if h3_body.strip():
        shots, ok = _parse_body(h3_body)
        if ok:
            return shots, True

    shots, ok = _parse_body(raw)
    if ok:
        return shots, True

    return [
        Shot(
            index=1,
            title="整篇（未识别出镜头结构）",
            prompt=raw,
            seconds=DEFAULT_SHOT_SECONDS,
            refs=_refs_of(raw),
        )
    ], False


def seconds_to_frames(seconds: float, fps: int = 24) -> int:
    """秒 → 帧，顺带满足 H3 要求的 17k+5 形态（AI Port 也会再校一次，这里先给个近似值）。"""
    frames = max(5, int(round(max(MIN_SHOT_SECONDS, seconds) * max(1, fps))))
    return frames + (5 - (frames % 17)) % 17
