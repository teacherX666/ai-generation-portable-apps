"""把 Deepwhite 导演拍摄技能（v3.7）接进规划器。

用户 2026-09-22：「这个 skill 能不能直接放到这个模块里…改来改去效果还是垃圾，
直接把这个 skill 接上」。所以这里把技能包整体随代码发布，并把它的**提示词规则**
注入规划器的 system prompt —— 提示词**怎么写**由技能说了算，应用只保留机械契约
（TaskPlan JSON、任务字段、@图片N 编号、时长/分辨率这些运行时字段）。

技能原始结构（`skills/deepwhite-director-shooting-v3-7/`）：

- `SKILL.md`：状态机 + Gate 协议（多轮人工确认流程，本管线只借用其原则，
  真正的人工闸口是审批页）；
- `reference/*.md`：真正的规则（提示词结构、时间戳、素材映射、剧本忠实、
  文戏动势、风格、微节拍、摄影机情绪、动态场景四件套…）；
- `scripts/*.py`、`templates/*.md`：交付物校验与 HTML 模板，本管线不用。

技能自己说「不要为了可能有用预加载所有参考文件」，但本管线是**一次调用出计划**、
没有多轮读取的机会，所以默认全量注入；`DIRECTOR_SKILL_SCOPE=core` 可只注入核心
四篇（省 token）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: 随代码发布的技能 id（目录名）。
DIRECTOR_SKILL_ID = "deepwhite-director-shooting-v3-7"

#: 技能包根目录。
_SKILL_ROOT = Path(__file__).resolve().parent.parent / "skills" / DIRECTOR_SKILL_ID

#: 核心四篇：规则基线 + 提示词模板 + 剧本忠实 + 文戏动势连续性。
#:
#: 这四篇决定「提示词长什么样」和「不许改剧本」，是省 token 模式下也必须在的。
_CORE_DOCS = (
    "SEEDANCE_2_5_RULES.md",
    "PROMPT_PATTERNS.md",
    "SCRIPT_FIDELITY.md",
    "DRAMA_SHOT_MOMENTUM.md",
)

#: 全量注入时的阅读顺序：先规则基线，再模板，再各专项。
_DOC_ORDER = (
    "SEEDANCE_2_5_RULES.md",
    "PROMPT_PATTERNS.md",
    "SCRIPT_FIDELITY.md",
    "DRAMA_SHOT_MOMENTUM.md",
    "MOTION_MODE_ROUTER.md",
    "DYNAMIC_MOTION_PHYSICS.md",
    "DYNAMIC_PLANNING.md",
    "DYNAMIC_PROMPT_CONTRACT.md",
    "DYNAMIC_SPATIAL_LOCK.md",
    "DYNAMIC_DIAGNOSIS_QC.md",
    "STYLE_BLOCK.md",
    "MICRO_BEATS.md",
    "CAMERA_EMOTION.md",
    "SPATIAL_BLOCKING.md",
    "PLANNING_CAPACITY.md",
    "ASSET_CONFIRMATION.md",
)

#: 注入内容的字符上限。技能说「不设固定字符数上限」，但整包 77KB 全塞进每次规划
#: 调用里代价太高；这个上限只用于**截断**（超了按上面的顺序保留前面的），
#: 默认值 ≈ 核心四篇 + 主要专项，实测约 26k token。
DEFAULT_MAX_CHARS = 60000


def skill_root() -> Path:
    return _SKILL_ROOT


def skill_is_available() -> bool:
    return (_SKILL_ROOT / "reference").is_dir()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=4)
def director_skill_rules(
    *,
    scope: str = "all",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """技能规则全文（带标题分隔），按 `_DOC_ORDER` 拼接后截断到 `max_chars`。

    读不到技能包时返回空串 —— 规划器退回自带规则，绝不让规划整体失败。
    """
    reference = _SKILL_ROOT / "reference"
    if not reference.is_dir():
        return ""
    names = _CORE_DOCS if scope == "core" else _DOC_ORDER
    chunks: list[str] = []
    used = 0
    for name in names:
        text = _read(reference / name).strip()
        if not text:
            continue
        block = f"<!-- {name} -->\n{text}\n"
        if used + len(block) > max_chars:
            # 截断也要保住「这篇存在」的信息，否则模型不知道还有规则没读到。
            room = max_chars - used
            if room > 400:
                chunks.append(block[:room].rstrip() + "\n（本篇因长度上限被截断）\n")
            break
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks).strip()


def director_skill_prompt_section(
    *,
    scope: str = "all",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """注入规划器 system prompt 的那一段（含使用说明）。"""
    rules = director_skill_rules(scope=scope, max_chars=max_chars)
    if not rules:
        return ""
    return (
        "【导演技能规则（Deepwhite 导演拍摄技能 v3.7，作者：DeepWhite）】\n"
        "下面的规则**决定提示词怎么写**，优先级高于本契约里任何关于提示词措辞、"
        "结构、时间戳安排和素材描述的自带说明；本契约里关于 TaskPlan JSON、任务字段、"
        "@图片N 编号、时长/分辨率/音频这些**机械要求**仍然必须遵守。\n"
        "技能是多轮人工确认流程，本管线只借用它的**规则**：需要用户拍板的地方"
        "（资产策略、故事板方式、空间调度）由审批页承担，不要因此停下来反问。\n"
        "技能要求产出的「文字分镜 / 故事板 / 交付文档」本管线不产出，"
        "你只输出 TaskPlan JSON；但提示词必须符合技能的模板与规则。\n\n"
        f"{rules}\n"
    )
