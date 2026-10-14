"""绿幕/抠图镜头必须写成**物理约束**，不能只写「纯净/干净」。

**用户原话**（2026-10-13）：「刮奶油的视频的绿幕会出现阴影等影响抠图的东西，怎么在以后的
生成视频里避免，即绿幕就是纯色绿幕」。

**实测**：那条 run（`7753b768`）的提示词里**已经写了**「露出干净通透的纯净绿幕」
「绿幕干净通透无杂色」—— 照样出阴影。因为：

* 「纯净 / 干净 / 无杂色」是**风格形容词**，模型理解成「色调干净」；
* 而绿幕在模型眼里是一块**被场景光照亮、会接住主体投影**的物理表面 → 自带明暗渐变、
  暗角、投影、噪点纹理 → 抠图出灰边/毛边。

所以这里守的是：**出现绿幕/抠图时，规划后自动补一段物理约束**（纯色背板、不参与光照、
同一色值、影子不落在绿幕上、不溢色），且**幂等**、**不把提示词推过长度上限**。
"""

from __future__ import annotations

from feishu_generation_agent.domain.plan import SEEDANCE_PROMPT_MAX_CHARS
from feishu_generation_agent.integrations.planner import (
    _ensure_green_screen_constraints,
)


def _payload(prompt: str) -> dict:
    return {"tasks": [{"task_id": "task-1", "prompt": prompt}]}


def _prompt(payload: dict) -> str:
    return str(payload["tasks"][0]["prompt"])


def test_green_screen_shot_gets_physical_constraints() -> None:
    """镜头里提到绿幕 → 补上物理约束，并且补在**镜头那句**上（模型主要照镜头段生成）。"""
    payload = _payload(
        "【一句话概述】刮刀刮开奶油露出绿幕。\n"
        "【详细情节】\n"
        "镜头1（0-8s）：刮刀自右向左刮动，刮过处露出干净通透的纯净绿幕，边缘平整不毛糙。"
    )

    _ensure_green_screen_constraints(payload)

    prompt = _prompt(payload)
    for expected in ("同一色值", "无明暗渐变", "影子不得落在绿幕上", "不溢色到主体"):
        assert expected in prompt, expected
    shot_at = prompt.index("镜头1（0-8s）")
    assert prompt.index("同一色值") > shot_at, "必须补在镜头那句里，不能只补在概述"


def test_green_screen_constraint_is_idempotent() -> None:
    """已经带物理约束的提示词不再追加（否则每轮越写越长）。"""
    payload = _payload(
        "【详细情节】\n"
        "镜头1（0-8s）：露出绿幕，整片同一色值、不参与场景光照，无明暗渐变。"
    )
    before = _prompt(payload)

    _ensure_green_screen_constraints(payload)

    assert _prompt(payload) == before


def test_green_screen_constraint_skipped_without_green_screen() -> None:
    """没有绿幕/抠图的镜头不许被塞这段话（普通场景的阴影是正常的）。"""
    payload = _payload("【详细情节】\n镜头1（0-8s）：小黄鸭在月夜小道上行走，地面有它的影子。")
    before = _prompt(payload)

    _ensure_green_screen_constraints(payload)

    assert _prompt(payload) == before


def test_green_screen_constraint_respects_prompt_length_cap() -> None:
    """长度守卫：补完会超过上限就**不补**（宁可不补，也不能让校验不过）。"""
    filler = "细碎的奶油纹理与糖豆铺满画面，" * 160
    payload = _payload(
        f"【详细情节】\n镜头1（0-8s）：刮开奶油露出纯色绿幕。{filler}"
    )
    before = _prompt(payload)
    assert len(before) > SEEDANCE_PROMPT_MAX_CHARS - 200, "前置条件：已经接近上限"

    _ensure_green_screen_constraints(payload)

    assert _prompt(payload) == before
    assert len(_prompt(payload)) <= SEEDANCE_PROMPT_MAX_CHARS


def test_constraint_lands_on_single_shot_timeline_line() -> None:
    """单镜头的时间轴行是 `1—8s：…`（没有「镜头N」）也要认。

    2026-10-13 生产回放实测：刮奶油 / 刮雪那两条都是这种写法，只认「镜头N」会把约束
    补到【一句话概述】上，而模型主要照时间轴那段生成。
    """
    payload = _payload(
        "【一句话概述】刮刀刮开奶油露出绿幕，预计8秒。\n"
        "【详细情节】\n"
        "1—8s：刮刀自右向左刮动，刮过处露出纯净绿幕，边缘平整不毛糙。"
    )

    _ensure_green_screen_constraints(payload)

    prompt = _prompt(payload)
    assert prompt.index("同一色值") > prompt.index("1—8s："), "要补在时间轴那句上"
    assert prompt.index("同一色值") > prompt.index("【详细情节】")


def test_green_screen_constraint_handles_prompt_without_shot_lines() -> None:
    """单镜头/没有镜头行时也要补（补在提到绿幕的那句上）。"""
    payload = _payload("一只手持刮刀刮开奶油，露出纯净绿幕。")
    _ensure_green_screen_constraints(payload)
    assert "同一色值" in _prompt(payload)


def test_seed_rules_include_green_screen_rule() -> None:
    """绿幕口径同时进**长期记忆**的种子（每次规划都会带上）。"""
    from feishu_generation_agent.integrations.user_rules import SEED_RULES

    joined = "".join(SEED_RULES)
    assert "绿幕" in joined and "同一色值" in joined
