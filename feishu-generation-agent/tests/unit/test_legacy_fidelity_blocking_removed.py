"""存量「参考图保真校验未过」阻断项必须被清掉（2026-10-28）。

背景：那条校验的判据是正则匹配「一颗/三颗/十颗」，会误伤「数**十颗**」「十**几颗**」
这种本来就表示"多"的写法，把正常提示词判成"写少了"并卡住审批。
用户原话：「把这个阻塞删了，碍事」→ 写入方已整块删除。

**但光删写入方不够**：`blocking_issues` 是**落进计划快照的存量数据**。
生产实锤 run `6005a4b9`（切瑞士卷 TASK-01）：该 run 创建于删代码之前，
计划里仍带着这条字符串，`TaskPlan.approve()` 见非空即抛错，用户怎么都批不了。
"""
from __future__ import annotations

from feishu_generation_agent.domain.reference_contract import (
    LEGACY_FIDELITY_BLOCKING_PREFIX,
    refresh_duration_blocking_issues,
)

_LEGACY = (
    "参考图保真校验未过：读图事实显示素材里是「大量彼此独立的小滴 / 连成一整片」"
    "这类成片内容，但提示词【素材说明】把它写成了「十颗」这种小数量。"
)
_LEGIT = "任务时长与正文自述不一致：正文写了 8 秒"


def test_legacy_fidelity_blocking_is_dropped_and_others_kept() -> None:
    payload = {
        "prompt": "0-8秒：一只猫在跑。共8秒。",
        "duration": 8,
        "blocking_issues": [_LEGACY, _LEGIT],
    }
    refresh_duration_blocking_issues(payload)
    remaining = payload["blocking_issues"]
    # 判别力自检：把 `LEGACY_FIDELITY_BLOCKING_PREFIX` 那条 `continue` 去掉，这里必红
    assert not any(m.startswith(LEGACY_FIDELITY_BLOCKING_PREFIX) for m in remaining)
    assert not any("参考图保真校验" in m for m in remaining)
    # 不允许误伤别的族
    assert any("正文写了 8 秒" in m for m in remaining)


def test_only_the_legacy_prefix_is_dropped_not_anything_similar() -> None:
    """前缀精确匹配：别把「参考图保真」开头的其它文案也一起清掉。"""
    other = "参考图保真度不足：素材与分镜主题不符，请替换素材"
    payload = {"prompt": "共8秒。", "duration": 8, "blocking_issues": [other]}
    refresh_duration_blocking_issues(payload)
    assert other in payload["blocking_issues"]


def test_empty_and_missing_blocking_issues_are_safe() -> None:
    for payload in (
        {"prompt": "共8秒。", "duration": 8},
        {"prompt": "共8秒。", "duration": 8, "blocking_issues": []},
        {"prompt": "共8秒。", "duration": 8, "blocking_issues": None},
    ):
        refresh_duration_blocking_issues(payload)
        assert not payload.get("blocking_issues")
