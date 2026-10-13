"""AI 短剧自动化全流程 · 阶段定义与提示词装配。

设计原则（对齐仓库既有做法）：
- 技能正文（skills/ 下的 .md/.txt）直接作为 system prompt 的主体，跟
  `seedance/SKILL.md`、`director/SKILL.md` 完全同一套做法，不发明新机制。
- 阶段划分照抄《AI随风 · AI 影视弹性工作流》的 8 阶段，不另起炉灶。
- 提示词装配是纯函数（可单测），执行与落盘在 runner.py。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parent
SKILLS_DIR = APP_ROOT / "skills"
SKILLS_INDEX = SKILLS_DIR / "SKILLS.json"

# 单个技能正文进入 system prompt 的字符上限。总纲 ≈7.5k、分镜 V3 ≈25k，
# 全量叠加会接近 40k 字符；这里给每个技能留足正文，同时用 TOTAL_BUDGET
# 兜住总量，避免某次把上下文打满。
DEFAULT_SKILL_BUDGET = 30000
TOTAL_BUDGET = 60000


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    role: str
    rel_path: str
    stages: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return SKILLS_DIR / self.rel_path


@dataclass(frozen=True)
class Stage:
    id: str
    order: int
    title: str
    subtitle: str
    artifact: str
    instruction: str
    skills: tuple[str, ...]
    inputs: tuple[str, ...] = ()
    optional: bool = False
    temperature: float = 0.4
    max_tokens: int = 6000


# 8 阶段 —— 顺序与命名照抄《AI随风 · AI 影视弹性工作流》第 0 章的八阶段表。
STAGES: tuple[Stage, ...] = (
    Stage(
        id="concept",
        order=1,
        title="概念锚定",
        subtitle="把一句话想法锁成可执行的题材、受众、钩子与情绪曲线",
        artifact="01-concept.md",
        instruction=(
            "产出「概念锚定」文档：题材与类型、目标受众、一句话卖点（logline）、"
            "核心钩子与爽点节奏、主角欲望与阻力、结局走向、整体情绪曲线。"
            "用二级标题分节，每节不超过 8 行。"
        ),
        skills=("suifeng-workflow", "viral-video-prompt-engine", "knowledge-video-director"),
    ),
    Stage(
        id="character_bible",
        order=2,
        title="角色圣经",
        subtitle="主要角色的外观锚点、性格、说话方式与跨镜一致性约束",
        artifact="02-character-bible.md",
        instruction=(
            "产出「角色圣经」：每个角色一节，包含身份、外观锚点（发型/发色/瞳色/体型/"
            "固定服装，必须逐字可复用的短标签）、性格、说话方式、与其他角色关系、"
            "以及跨镜一致性约束（哪些特征任何镜头都不许变）。主角 1-3 人优先。"
        ),
        skills=("suifeng-workflow", "realistic-character-prompt"),
        inputs=("concept",),
    ),
    Stage(
        id="worldview",
        order=3,
        title="世界观（可选）",
        subtitle="时代、地域、规则与视觉基调；现实题材可跳过",
        artifact="03-worldview.md",
        instruction=(
            "产出「世界观」：时代与地域、社会/技术规则、势力与阶层、"
            "视觉基调（色调/光线/材质/镜头语言倾向）。若是现实题材，"
            "写明「现实向，无超自然设定」并只保留地域与视觉基调。"
        ),
        skills=("suifeng-workflow",),
        inputs=("concept",),
        optional=True,
    ),
    Stage(
        id="outline",
        order=4,
        title="分场大纲",
        subtitle="把故事拆成场次表，标注时点、地点、人物、事件与情绪落点",
        artifact="04-outline.md",
        instruction=(
            "产出「分场大纲」：表格形式，每场一行，列为 场次号 / 时点 / 地点 / "
            "出场角色 / 事件与冲突 / 情绪落点 / 预估时长（秒）。"
            "场次总量按目标成片时长反推，单场不超过 60 秒。"
        ),
        skills=("suifeng-workflow", "h3-official-3d-animation", "novel-to-wanxiang-storyboard"),
        inputs=("concept", "character_bible", "worldview"),
    ),
    Stage(
        id="script",
        order=5,
        title="剧本深度融合与排版",
        subtitle="分场大纲落成带对白、动作、镜头指示的可拍剧本",
        artifact="05-script.md",
        instruction=(
            "产出「剧本」：逐场展开，含场景描述、人物动作、对白（引号包裹并标注角色与情绪）、"
            "音效与镜头指示。对白必须口语化、可念；不要旁白解说动作。"
            "每场末尾附一行「本场节拍：…」。"
        ),
        skills=("suifeng-workflow", "script-to-storyboard-v3", "h3-script-video-prompter", "novel-to-wanxiang-storyboard"),
        inputs=("concept", "character_bible", "worldview", "outline"),
    ),
    Stage(
        id="assets",
        order=6,
        title="角色资产",
        subtitle="角色/场景/道具三类资产的出图提示词（含三视图与风格前置）",
        artifact="06-assets.md",
        instruction=(
            "产出「资产提示词」：分三节 —— 角色资产（每人：正面/侧面/背面或三视图提示词 + "
            "表情/服装变体）、场景资产（每场一张主视角图提示词）、道具资产（关键道具）。"
            "风格提示词全流程前置锁定，所有资产共用同一句风格串，逐字复用不得改写。"
        ),
        skills=("suifeng-workflow", "script-to-storyboard-v3", "realistic-character-prompt"),
        inputs=("character_bible", "worldview", "script"),
    ),
    Stage(
        id="performance",
        order=7,
        title="表演设计",
        subtitle="把情绪翻译成可见的手势、走位、道具与有动机的光",
        artifact="07-performance.md",
        instruction=(
            "产出「表演设计」：逐场逐镜列出 谁 / 做什么 / 怎么动 / 去哪；"
            "把每个情绪落点翻译成可见的表演节拍（手势、走位、道具、有动机的光）。"
            "禁止用「悲伤/愤怒/震撼」这类情绪词代替表演。"
            "打斗或特效场次额外标注力向、接触点与特效锚点。"
        ),
        skills=("suifeng-workflow", "fight-prompt-master", "seedance-2.5-fight-prompter", "fight-fx-anchor-prompter", "h3-official-3d-animation"),
        inputs=("script", "character_bible", "assets"),
    ),
    Stage(
        id="video_prompts",
        order=8,
        title="视频提示词",
        subtitle="逐镜视频提示词：Seedance 2.5 与 MiniMax H3 两套口径",
        artifact="08-video-prompts.md",
        instruction=(
            "产出「视频提示词」：逐镜输出。每镜包含 —— 镜号 / 时长（秒）/ 景别与机位 / "
            "主体与动作 / 环境与光 / 运镜及动机 / 对白与音效 / 引用素材（@图片N 或 <Subject N>）。"
            "先给 Seedance 2.5 口径（自然语言中文、单段 4-15 秒、15 秒内最多 5 镜），"
            "再给 MiniMax H3 口径（英文标签 + 中文叙述，含 <Subject N>/<Audio N> 定义，"
            "英文定义后用括号标注中文名）。结尾给一行「禁止：…」负面约束。"
        ),
        skills=(
            "suifeng-workflow",
            "script-to-storyboard-v3",
            "h3-ref2va-optimizer",
            "h3-script-video-prompter-full",
            "h3-json-director",
            "minimax-h3-video-prompter",
            "h3-official-prompt-writing",
            "novel-to-wanxiang-h3",
            "fight-fx-anchor-prompter",
            "seedance-2.5-fight-prompter",
        ),
        inputs=("script", "assets", "performance"),
    ),
)

STAGE_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}
STAGE_ORDER: tuple[str, ...] = tuple(s.id for s in STAGES)

MODES: tuple[dict[str, str], ...] = (
    {"id": "short_drama", "label": "短剧（无旁白，靠对白与表演）"},
    {"id": "narrated", "label": "解说（80% 旁白推动）"},
    {"id": "mv", "label": "音乐 MV（画面服从音乐结构）"},
    {"id": "knowledge", "label": "知识/带货长视频（结构化解说）"},
)
MODE_BY_ID = {m["id"]: m for m in MODES}


def load_skills() -> list[Skill]:
    """读 skills/SKILLS.json；文件缺失时返回空表而不是抛错（UI 仍可用）。"""
    if not SKILLS_INDEX.exists():
        return []
    try:
        raw = json.loads(SKILLS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[Skill] = []
    for item in raw.get("skills", []):
        try:
            out.append(
                Skill(
                    id=str(item["id"]),
                    title=str(item.get("title") or item["id"]),
                    role=str(item.get("role") or ""),
                    rel_path=str(item["file"]),
                    stages=tuple(item.get("stages") or ()),
                )
            )
        except KeyError:
            continue
    return out


def load_skill_text(skill: Skill, budget: int = DEFAULT_SKILL_BUDGET) -> str:
    try:
        text = skill.path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if budget > 0 and len(text) > budget:
        text = text[:budget] + "\n\n…（技能正文超出预算，此处截断）"
    return text


def skill_texts_for_stage(stage: Stage, total_budget: int = TOTAL_BUDGET) -> list[tuple[Skill, str]]:
    """按阶段声明的技能顺序取正文，并对总量做预算收敛。"""
    by_id = {s.id: s for s in load_skills()}
    picked: list[tuple[Skill, str]] = []
    used = 0
    for sid in stage.skills:
        skill = by_id.get(sid)
        if skill is None:
            continue
        remaining = max(2000, total_budget - used)
        text = load_skill_text(skill, budget=min(DEFAULT_SKILL_BUDGET, remaining))
        if not text:
            continue
        used += len(text)
        picked.append((skill, text))
    return picked


_PREAMBLE = """你是一个「AI 短剧自动化全流程」的执行模型。下面会依次给出本阶段需要遵守的技能规范，
请把它们当作硬约束，不要复述、不要总结规范本身，直接产出本阶段产物。

【工作模式 · 必须遵守】
1. 非交互：永远不要反问用户、不要要求补充信息；信息不完整就按最合理默认补全并直接产出。
2. 只输出本阶段产物正文。不要写「好的」「以下是」这类客套，不要用代码围栏把整篇包起来。
3. 中文输出（技能里明确要求英文标签/英文音频骨架的地方除外）。
4. 上游产物是既成事实：保持一致，不得擅自改名、改设定、改风格串。确实冲突时，
   在正文末尾用一行「⚠️ 冲突：…」标出，但仍按上游版本执行。
"""


def stage_prompt(
    stage: Stage,
    *,
    topic: str,
    mode: str,
    upstream: dict[str, str] | None = None,
    total_budget: int = TOTAL_BUDGET,
) -> list[dict[str, str]]:
    """装配某阶段的 chat messages。纯函数，可单测。"""
    upstream = upstream or {}
    mode_label = MODE_BY_ID.get(mode, {}).get("label", mode or "短剧")

    parts: list[str] = [_PREAMBLE]
    for skill, text in skill_texts_for_stage(stage, total_budget=total_budget):
        parts.append(f"\n\n========== 技能规范：{skill.title} ==========\n{text}")

    system = "".join(parts)

    blocks: list[str] = [
        f"【项目主题】\n{topic.strip() or '（未填写）'}",
        f"【成片模式】\n{mode_label}",
    ]
    for sid in stage.inputs:
        text = (upstream.get(sid) or "").strip()
        if not text:
            continue
        src = STAGE_BY_ID.get(sid)
        label = f"{src.order:02d} {src.title}" if src else sid
        blocks.append(f"【上游产物 · {label}】\n{text}")

    blocks.append(
        f"【本阶段任务 · 第 {stage.order} 阶段：{stage.title}】\n{stage.instruction}\n\n"
        f"阶段产物文件名固定为 `{stage.artifact}`。直接输出该文件的内容。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def stage_payload(stage: Stage) -> dict:
    return {
        "id": stage.id,
        "order": stage.order,
        "title": stage.title,
        "subtitle": stage.subtitle,
        "artifact": stage.artifact,
        "optional": stage.optional,
        "inputs": list(stage.inputs),
        "skills": list(stage.skills),
    }


def stages_payload() -> list[dict]:
    return [stage_payload(s) for s in STAGES]
