"""原图自带的水印 / 文字 / UI **不是排除参考图的理由**（用户 2026-10-12 改口径）。

用户原话：

    不参考原图右下角的平台水印、标识与任何标题文字。文档里并没说不参考，
    实际上要参考，但是经常给我排除

**实测规模（最近 60 个运行）**：提示词里带「不参考…水印/标识/文字」的 **55 个（92%）**；
123 张图片素材里 19 张被判 `usable=false`，其中 **17 张（89%）** 的原因是水印/标识类 ——
也就是说「排除」几乎全由水印触发。成因有三处，都在规划侧：

1. `_SEEDANCE_PLANNING_CONTRACT` 把「有水印、平台标识」写进 `usable=false` 的判据；
2. 同一份契约要求提示词补「无水印和无 Logo 约束」，规划器就把它理解成"原图里的也别参考"；
3. 导演技能包的模板要求每个素材写「参考什么 + 不参考什么」，默认示例里有「不参考…文字」，
   规划器于是把原图的角标文字填进"不参考"那一半。

**用户选定的口径（2026-10-12）**：
① 原图**整体照原样参考** —— 游戏 UI、计数器、标题、产品品牌字样都是要复现的画面内容；
② **只**排除第三方 AI 工具 / 外部平台叠加的角标水印（即梦AI、豆包AI生成、Jewel Coloring、
   小红书、抖音号…）；
③ 水印/文字/标识**不再触发 `usable=false`**，原图一律留在 `reference_images` 里。
"""

from __future__ import annotations

import asyncio

from feishu_generation_agent.domain.document import NormalizedDocument
from feishu_generation_agent.integrations.director_skill import (
    director_skill_prompt_section,
)
from feishu_generation_agent.integrations.planner import (
    _PLAN_SYSTEM_PROMPT,
    REFERENCE_FIDELITY_OVERRIDE,
    DeepSeekPlanner,
)


class _FakeModel:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, messages, config=None):  # noqa: ANN001
        self.messages.append(messages)

        class _Response:
            content = '{"document_summary":"摘要","tasks":[]}'

        return _Response()


def _document() -> NormalizedDocument:
    return NormalizedDocument.model_validate(
        {
            "document_id": "doc-1",
            "title": "测试",
            "revision": 1,
            "blocks": [],
            "text_view": "剧本：一镜到底。",
            "media_assets": [],
            "source_type": "docx",
            "source_token": "tok",
        }
    )


def _system_prompt(skill_section: str) -> str:
    model = _FakeModel()
    planner = DeepSeekPlanner(model, max_output_count=4, skill_section=skill_section)
    try:
        asyncio.run(planner.plan(_document(), []))
    except Exception:  # noqa: BLE001 —— 计划为空会报错，这里只要 system prompt
        pass
    assert model.messages, "planner 没有调用模型"
    return model.messages[0][0]["content"]


# --- 契约层：水印不再让素材「不可用」 ---


def test_watermark_is_no_longer_an_unusable_trigger() -> None:
    """旧判据「有水印、平台标识 → usable=false」必须消失（它就是 89% 排除的来源）。"""
    assert "有水印、平台标识" not in _PLAN_SYSTEM_PROMPT


def test_watermark_text_ui_explicitly_do_not_make_a_material_unusable() -> None:
    assert "不构成" in _PLAN_SYSTEM_PROMPT
    assert "usable=false" in _PLAN_SYSTEM_PROMPT


def test_contract_requires_referencing_the_whole_original() -> None:
    assert "原图整体照原样参考" in _PLAN_SYSTEM_PROMPT


def test_output_side_no_watermark_rule_is_scoped_to_third_parties() -> None:
    """「成片不要出现水印/Logo」保留，但只针对第三方 —— 不能拿来排除原图自带内容。"""
    assert "第三方" in _PLAN_SYSTEM_PROMPT


# --- 游戏画面是产品内容，不是噪声（用户 2026-10-12 追加口径） ---
#
# 用户原话：「我的意思是捡帽子这种游戏画面直接给我提示词排除了，但是我其实需要游戏画面」。
#
# 真实案例（捡帽子那条，run `2cc6e0a0` / `ad723dc0` / `70152c13`）：
#   提示词：「…不参考画面下方的**停车解谜游戏界面**、白色手指图标、"0/6"数字、道路色带
#            与"Sandfall""Jam B"品牌字样」
#            「…不参考图中的**游戏界面、计数框、汽车图案、横条 UI**…」
#   reference_plan：「整张为游戏广告截图…下半部是停车解谜游戏界面…不能直接作为干净参考」
#   —— 被排除掉的正是用户要的**游戏画面本身**（这是游戏广告/玩法演示的需求）。
#
# 规模：最近 80 个运行里 **25 个（31%）** 把「游戏界面/关卡/计数/UI」写进了「不参考」。
#
# 上一轮只点名了「游戏 UI / 计数器 / 图标」，规划器照样把「游戏界面 / 关卡画面 /
# 横条 UI / 道具图标」当噪声 —— 所以这一轮把这些具体元素**逐个点名**。

GAME_MARKER = "游戏画面就是产品内容"


def test_game_screen_clause_is_spelled_out_in_the_contract() -> None:
    assert GAME_MARKER in _PLAN_SYSTEM_PROMPT
    for token in ("游戏界面", "计数框", "横条 UI", "道具"):
        assert token in _PLAN_SYSTEM_PROMPT, token


def test_game_screenshot_is_not_a_reason_to_call_a_material_unusable() -> None:
    """「这是一张游戏截图 / 界面」不许当 `usable=false` 的理由。"""
    assert GAME_MARKER in _PLAN_SYSTEM_PROMPT
    # 同一个判据句里必须写明它「也不构成 usable=false」
    start = _PLAN_SYSTEM_PROMPT.index(GAME_MARKER)
    assert "不构成" in _PLAN_SYSTEM_PROMPT[start : start + 400]


def test_unusable_trigger_mismatch_is_scoped_away_from_game_screens() -> None:
    """「与对应分镜明显不符」只用于**主题确实不同**的素材，不适用于游戏界面本身。"""
    marker = "与对应分镜明显不符"
    assert marker in _PLAN_SYSTEM_PROMPT
    window = _PLAN_SYSTEM_PROMPT[
        _PLAN_SYSTEM_PROMPT.index(marker) : _PLAN_SYSTEM_PROMPT.index(marker) + 600
    ]
    assert GAME_MARKER in window, "游戏画面那条判据必须紧跟 unusable 判据（同一段）"


def test_override_repeats_the_game_screen_clause() -> None:
    """覆盖块（排在技能段之后）里也要有 —— 规划器在那里读「参考/不参考」怎么写。"""
    assert GAME_MARKER in REFERENCE_FIDELITY_OVERRIDE
    for token in ("游戏界面", "计数框", "横条 UI", "道具"):
        assert token in REFERENCE_FIDELITY_OVERRIDE, token


def test_override_closes_the_exclusion_list_to_third_party_watermarks_only() -> None:
    """「不参考」**只允许**写一件事：第三方 AI 工具 / 外部平台的角标水印。

    2026-10-27 口径收窄（P0）：旧覆盖块把「与本次画面无关的维度（原图的静止姿势、
    原机位、原光线）」列为**允许**排除 —— 那正是**关掉参考图的开关**（用户
    2026-10-13 铁律）。实测代价：需求1「铲蜡」8 个提示词版本里 5 个写着
    「不参考原图静止平铺的俯拍机位与原图均匀布光」，成片的桌面、光位与蜡滴布局
    全部脱离参考图。
    """
    text = REFERENCE_FIDELITY_OVERRIDE
    assert "只允许" in text
    assert "第三方" in text
    assert "即梦" in text
    assert "usable=false" in text
    # 必须**明文禁止**把机位 / 构图 / 光线 / 光位 / 色温 / 背景 / 场景 / 姿势写进「不参考」
    assert "严禁" in text
    for token in ("机位", "构图", "光线", "光位", "色温", "背景", "姿势"):
        assert token in text, token
    # 并给出正向替代写法（不能只说不许）
    assert "正向的运动描述" in text


def test_override_never_lists_the_reference_camera_or_light_as_excludable() -> None:
    """判别力自检：旧口径把「原机位 / 原光线」放进**允许**清单，这条必须红。

    守的是「不许再有关掉参考图的口子」，不是某一句的字面。
    """
    text = REFERENCE_FIDELITY_OVERRIDE
    # 旧的封闭清单写法必须消失
    assert "只允许写两件事" not in text
    # 「原机位 / 原光线」不得作为**允许排除**的维度出现
    assert "原机位" not in text
    assert "原光线" not in text
    # 也不得把禁句当反例抄进来（点名即投喂：规划器会照抄示例句式，压口红任务已实锤）
    assert "不参考原图" not in text


# --- 覆盖块：必须排在技能段之后 ---


def test_fidelity_override_is_appended_after_the_skill_section() -> None:
    """技能包的「不参考…文字」示例在后，只有后置覆盖能压住它（recency）。"""
    with_skill = _system_prompt(director_skill_prompt_section(scope="core"))

    assert REFERENCE_FIDELITY_OVERRIDE in with_skill
    assert with_skill.index("导演技能规则") < with_skill.index(
        REFERENCE_FIDELITY_OVERRIDE
    )


def test_fidelity_override_is_not_appended_without_a_skill_section() -> None:
    """没有技能段时**不能**附加任何东西 —— 两条既有契约都必须保住。

    ① `exact_system_prompt` 要**逐字回放**检查点快照（生产走的就是这条，
       见 `test_planner.py::test_exact_system_prompt_override_replays_checkpoint_verbatim`）；
    ② 没有技能段时系统提示词必须与 `planner_system_prompt()` 完全一致
       （`test_default_and_portal_planner_system_prompts_are_composed_safely`）。

    所以覆盖块跟技能段捆在一起，没有技能段的场合由契约正文里的同一条口径兜底。
    """
    assert REFERENCE_FIDELITY_OVERRIDE not in _system_prompt("")


def test_contract_carries_the_same_rule_for_the_no_skill_case() -> None:
    """契约正文里的兜底必须把口径写全（用户口径不依赖技能开不开）。"""
    assert "原图整体照原样参考" in _PLAN_SYSTEM_PROMPT
    assert "不构成" in _PLAN_SYSTEM_PROMPT
    assert "水印、平台标识、界面文字、标题与品牌字样" in _PLAN_SYSTEM_PROMPT


def test_contract_puts_the_reference_image_above_the_document_prose() -> None:
    """契约正文必须写明：文字只管动作/节奏，画面内容以参考图为唯一基准。

    这是「以事件描述为主、参考感觉为辅」这句文档口径的**唯一对冲**。
    实测根因（2026-10-27 需求1「铲蜡」run `453e8074`）：规划器按该口径字面执行，
    在 `warnings` 里自己写着「以文档事件描述为主、图片质感为辅」，
    于是把参考图的浅色木纹桌面改成了深色，并把「三个色区、每区上百颗小滴」
    概括成「三颗蜡滴（左红中蓝右浅绿）」。
    """
    text = _PLAN_SYSTEM_PROMPT
    assert "以事件描述为主、参考感觉为辅" in text  # 口径被点名
    assert "只适用于动作、节奏与时间分配" in text  # 但被收窄
    assert "为唯一基准" in text
    assert "以参考图为准" in text
    # 不得用概括文字覆盖参考图里可数可见的事实
    assert "可数" in text


def test_override_repeats_the_priority_rule() -> None:
    """覆盖块（排在技能段之后）里也要有优先级条款 —— 技能段会把它压过去。"""
    assert "以事件描述为主、参考感觉为辅" in REFERENCE_FIDELITY_OVERRIDE
    assert "只适用于动作、节奏与时间分配" in REFERENCE_FIDELITY_OVERRIDE
    assert "以参考图为准" in REFERENCE_FIDELITY_OVERRIDE
