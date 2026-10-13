"""「时长不一致」阻断项必须按**当前**的 prompt/duration 重算（2026-10-13 小猫钓鱼 422）。

**生产事故**：run `788ab355` 状态 `waiting_approval`，用户点审批一直失败
（日志实锤 `POST /api/runs/788ab355.../decision → 422`）。根因是 `TaskPlan.approve()`
只要任务带 `blocking_issues` 就抛错，而那条阻断项是**旧时长（18 秒）下写的存量字符串**：

    正文已经改成「共12秒」、任务时长也已改成 12、时间轴 0-8s/8-12s —— 两边一致，
    但 `blocking_issues` 里还留着「正文自述时长（12 秒）与任务时长（18 秒）不一致」

改时长走的 `patch_task` 只持久化字段、**从不重算**这条，于是用户怎么改都批不了，
界面上只显示「审批无效」。
"""

from __future__ import annotations

from feishu_generation_agent.graph.runtime import refresh_duration_blocking_issues

_STALE = (
    "Seedance 提示词正文自述时长（12 秒）与任务时长（18 秒）不一致："
    "请把正文里所有时长表述（如「预计 N 秒」「共 N 秒」）与时间轴一并改成 18 秒，"
    "或把任务时长改回正文写的秒数。"
)


def test_stale_duration_blocking_issue_is_cleared_once_values_agree() -> None:
    """两边改一致之后，旧阻断项必须消失 —— 否则审批被永久卡死。"""
    payload = {
        "prompt": "镜头1（0-8s）…；镜头2（8-12s）…；采用2个镜头的连续剪辑，共12秒。",
        "duration": 12,
        "blocking_issues": [_STALE, "图片用途不明确"],
    }

    refresh_duration_blocking_issues(payload)

    # 只清「时长不一致」这一族；别的阻断项（真实的其它问题）必须原样保留
    assert payload["blocking_issues"] == ["图片用途不明确"]


def test_genuine_duration_mismatch_is_still_blocked() -> None:
    """真不一致时必须**补回**这一条 —— 重算不能变成「放过」。"""
    payload = {
        "prompt": "单镜头一镜到底，预计5秒。",
        "duration": 8,
        "blocking_issues": [],
    }

    refresh_duration_blocking_issues(payload)

    assert len(payload["blocking_issues"]) == 1
    assert "5 秒" in payload["blocking_issues"][0]
    assert "8 秒" in payload["blocking_issues"][0]


def test_refresh_is_a_noop_when_there_is_nothing_to_fix() -> None:
    """本来就没有时长表述 / 本来就一致 → 不许凭空造出阻断项。"""
    consistent = {
        "prompt": "共12秒，镜头1（0-8s）…",
        "duration": 12,
        "blocking_issues": ["图片用途不明确"],
    }
    refresh_duration_blocking_issues(consistent)
    assert consistent["blocking_issues"] == ["图片用途不明确"]

    silent = {"prompt": "没有任何时长表述", "duration": 8}
    refresh_duration_blocking_issues(silent)
    assert silent.get("blocking_issues") in (None, [])


#: 复用守卫判据 1 的文案（时长变了）：它自己就给了出口「或把时长改回上一版的秒数」。
_REUSE_GUARD_REASON = (
    "本次时长是 8 秒，而上一版计划里的提示词是按 5 秒写的（时间轴与「预计 N 秒」"
    "都没跟着改）。请点「重新运行」并填写返工要求，让模型按新时长重写时间轴与全部"
    "时长表述；或把时长改回上一版的秒数。"
)


def test_reuse_guard_reason_is_cleared_when_duration_is_reverted() -> None:
    """用户按文案照做、把时长改回上一版 → 这条守卫理由必须消失（否则同样永久 422）。

    `explicit_duration_edit=True` = 用户**显式改时长**这条路径（`patch_task`）。
    读取视图时**不能**这么清：克隆那一步时长可能还没被换成新值，`duration == Y` 是
    「还没应用」而不是「用户改回来了」（误清会让 2026-10-10 的 P0 守卫失效）。
    """
    payload = {
        "prompt": "正面近景固定机位，单镜头一镜到底，预计5秒。",
        "duration": 5,                     # 改回上一版的 5 秒
        "blocking_issues": [_REUSE_GUARD_REASON],
    }

    refresh_duration_blocking_issues(payload, explicit_duration_edit=True)

    assert payload["blocking_issues"] == []


def test_reuse_guard_reason_survives_when_read_only() -> None:
    """读取视图（默认参数）时**不许**清守卫那条 —— 否则克隆完就没拦住。"""
    payload = {
        "prompt": "正面近景固定机位，单镜头一镜到底，预计5秒。",
        "duration": 5,
        "blocking_issues": [_REUSE_GUARD_REASON],
    }

    refresh_duration_blocking_issues(payload)

    assert payload["blocking_issues"] == [_REUSE_GUARD_REASON]


def test_reuse_guard_reason_survives_while_duration_still_differs() -> None:
    """时长仍是 8 秒（没照做）→ 这条理由必须留着，不许被放行。"""
    payload = {
        "prompt": "正面近景固定机位，单镜头一镜到底，预计5秒。",
        "duration": 8,
        "blocking_issues": [_REUSE_GUARD_REASON],
    }

    refresh_duration_blocking_issues(payload, explicit_duration_edit=True)

    # 守卫那条留着（时长还没改回去），同时按当前值补一条「正文自述时长」不一致
    assert len(payload["blocking_issues"]) == 2
    assert any("上一版计划里的提示词是按 5 秒写的" in item for item in payload["blocking_issues"])
    assert any(item.startswith("Seedance 提示词正文自述时长（") for item in payload["blocking_issues"])
