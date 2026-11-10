"""前端契约：直连任务必须走**统一的那对接口**，且删除键不被多维表格的判据误挡。

为什么用源码级契约测试（而不是跑一遍 DOM）：这台机器上 app.js 没有 JS 单测运行器
（前端测试跑在自制极简假 DOM 上，只覆盖少数纯函数），而今天 2026-10-30 连续两次
生产事故都出在**前端打错接口**上：

1. 「往次成片」的按钮漏传通道 → 直连任务被当成多维表格任务，一点「重新运行」就报
   「多维表格运行不存在」（`viewRecentRun` 现在自己认通道）；
2. 直连任务没有删除按钮、也不受「超过 10 条自动回收」限制 → 用户要求「两者改成一套
   接口」，于是删除/恢复收成 `/api/runs/{id}/archive|restore`，前端只能打这两个。

这两条都是"改回去也不会有任何测试红"的那类回归，所以在这里钉住接口名与判据。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "feishu_generation_agent"
    / "web"
    / "static"
    / "app.js"
)

#: 「**运行级**硬删」的形态：模板串在 run_id 之后**就结束了**（`/api/runs/${id}`），
#: 后面没有别的路径段。收窄过一次（2026-11-04）—— 见下面那条断言里的说明。
HARD_DELETE_RE = re.compile(
    r'api\(\s*`/api/runs/\$\{[^}`]*\}`\s*,\s*\{\s*method:\s*"DELETE"'
)


@pytest.fixture(scope="module")
def source() -> str:
    return APP_JS.read_text("utf-8")


def test_delete_and_restore_use_the_unified_endpoints(source: str) -> None:
    """删除 = `/api/runs/{id}/archive`，恢复 = `/api/runs/{id}/restore`（两类共用）。"""
    assert "/archive`, {" in source, "删除必须走统一的归档接口"
    assert "/restore`" in source, "恢复必须走统一的恢复接口"
    # 直连任务以前是逐版本硬删（`DELETE /api/runs/{id}`），不可恢复 —— 不能回来。
    # （app.js 里另外几处 DELETE 是"重置规划提示词"、"删除参考素材"、"从本次运行移除素材"，
    #   都与**任务本身**无关。）
    # ⚠️ 2026-11-04 收窄：原来的正则 `[^`]*` 会把 `/api/runs/{id}/assets/{asset_id}` 这类
    # **素材级** DELETE 一起扫掉 —— 而那是「已排除的素材」行上的「删除」按钮（用户明确要的
    # 功能：从这次运行移除该素材、文件保留、不碰运行本身）。护栏的不变量是「不许硬删**运行**」，
    # 所以只匹配"模板串在 run_id 之后就没有别的路径段"的形态。
    assert not HARD_DELETE_RE.search(source), (
        "任务删除不该再有硬删路径：删除语义已统一为「进回收站、可恢复」"
    )


def test_the_hard_delete_guard_still_catches_a_run_level_delete() -> None:
    """判别力自检：正则收窄之后，**运行级硬删**必须仍然被拦。

    没有这条，上面那条断言就可能变成"恒真"（写什么都过）—— 仓里栽过同款
    （2026-10-23 的重启门禁 SQL 判据恒真，空转 2.8 小时）。
    """
    forbidden = 'await api(`/api/runs/${state.runId}`, { method: "DELETE" });'
    allowed = (
        'await api(\n'
        '  `/api/runs/${state.runId}/assets/${encodeURIComponent(item.asset_id)}`,\n'
        '  { method: "DELETE" },\n);'
    )
    assert HARD_DELETE_RE.search(forbidden), "运行级硬删必须被拦"
    assert not HARD_DELETE_RE.search(allowed), "素材级的移除不该被这条护栏误伤"


def test_direct_rows_are_not_blocked_by_the_bitable_busy_rule(source: str) -> None:
    """直连行「有没有删除键」只看**真的在跑**，不看多维表格那套待审批/待确认成片。"""
    assert "directBusyRecords" in source
    assert "SELF_PROGRESSING_RUN_STATUSES.has(run.status)" in source
    # `busy()`（决定 active，进而决定 canDeleteRun）必须对直连任务分支
    assert 'run?.mode === "direct"' in source
    assert "RUN_BUSY_STATUSES.has(run?.status)" in source


def test_view_recent_run_resolves_the_channel_itself(source: str) -> None:
    """「查看这一版 / 当前任务」这类入口不该依赖调用方传对通道。

    2026-10-30 实锤：同一个直连任务，界面 15:27 打 `/api/runs/{id}/rerun`（对）、
    15:39 打 `/api/bitable/runs/{id}/rerun`（404「多维表格运行不存在」）——
    根因就是 `viewRecentRun` 的通道参数有默认值、调用方漏传。
    """
    assert "const known = (state.bitable.recentRuns || []).find(" in source, (
        "viewRecentRun 必须能从列表里自己认出这条运行属于哪条通道"
    )
