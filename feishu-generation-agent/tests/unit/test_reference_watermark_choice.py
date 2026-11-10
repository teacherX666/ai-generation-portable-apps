# -*- coding: utf-8 -*-
"""用户选择去水印版本 + 重新清洗（2026-10-23）的回归测试。

用户口径：「可以让用户看到清洗后的图片自由选择原图或者继续重新清洗」，并明确
**不做局部重绘**（「局部重绘拼接的时候会有明显漏洞」）。

背景：局部块护栏误杀（2026-10-23 那次 30% 拒绝率）之外，**更根本的问题是用户没有决定权** ——
护栏拒绝时界面连清洗版都不给看；而且提交路径压根不看台账、自己重新洗一遍，所以
「审批页看到的那版 ≠ 真正提交的那版」。本文件守住三件事：

1. `effective_cleanup_path` 认用户决定（`cleaned` 即使护栏判 kept 也照用、
   `original` 即使采用过也不用、`auto` 沿用护栏判定）—— 它是"哪个版本生效"的**唯一收口点**；
2. 台账保留**历史版本**，重洗产出 `-nowm-2`… 且不覆盖旧的；
3. **提交路径先读台账、再决定要不要洗**（有结论就不许重洗）。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from PIL import Image

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import (
    DECISION_AUTO,
    DECISION_CLEANED,
    DECISION_ORIGINAL,
    CleanupRecord,
    cleaned_suffix,
    effective_cleanup_path,
    next_cleanup_version,
    read_cleanup_status,
    reclean_reference_image,
    record_cleanup_attempt,
    set_cleanup_decision,
)


def _png(path: Path, color: tuple[int, int, int] = (120, 140, 110)) -> Path:
    Image.new("RGB", (80, 80), color).save(path)
    return path


def _record(asset_id: str, cleaned: Path | None, *, adopted: bool = True) -> CleanupRecord:
    """构造一条清洗结果（字段顺序见 `CleanupRecord` 定义）。"""
    return CleanupRecord(
        asset_id,
        cleaned.parent / "orig.png" if cleaned else Path("orig.png"),
        cleaned,
        2.0 if cleaned else None,
        adopted,
        "测试用",
    )


# ── 1. 用户决定 ─────────────────────────────────────────────────────────────


def test_effective_cleanup_path_falls_back_to_guard_verdict() -> None:
    """没用户决定时，沿用护栏判定：采用过才用清洗版。"""
    assert effective_cleanup_path(None) is None
    assert effective_cleanup_path({}) is None
    assert effective_cleanup_path({"status": "kept", "cleaned": "a.png"}) is None
    assert effective_cleanup_path({"status": "running", "cleaned": "a.png"}) is None
    adopted = effective_cleanup_path({"status": "adopted", "cleaned": "a.png"})
    assert adopted is not None and adopted.name == "a.png"


def test_effective_cleanup_path_honours_user_decision() -> None:
    """**核心**：用户的选择压倒护栏 —— 这正是"自由选择"的落点。"""
    # 护栏判 kept（疑似抹掉内容），但用户看过图决定用清洗版 → 必须照用。
    chosen = effective_cleanup_path(
        {"status": "kept", "cleaned": "nowm.png", "decision": DECISION_CLEANED}
    )
    assert chosen is not None and chosen.name == "nowm.png"
    # 护栏采用了清洗版，但用户决定用原图 → 必须用原图。
    assert (
        effective_cleanup_path(
            {"status": "adopted", "cleaned": "nowm.png", "decision": DECISION_ORIGINAL}
        )
        is None
    )
    # auto = 交回护栏判定。
    assert (
        effective_cleanup_path(
            {"status": "kept", "cleaned": "nowm.png", "decision": DECISION_AUTO}
        )
        is None
    )


def test_effective_cleanup_path_prefers_selected_version() -> None:
    """有多个历史版本时，优先用户点选的那一版，否则用最新一版。"""
    entry = {
        "status": "kept",
        "cleaned": "v2.png",
        "decision": DECISION_CLEANED,
        "selected": "v1.png",
    }
    picked = effective_cleanup_path(entry)
    assert picked is not None and picked.name == "v1.png"
    entry.pop("selected")
    latest = effective_cleanup_path(entry)
    assert latest is not None and latest.name == "v2.png"


def test_effective_cleanup_path_ignores_empty_cleaned() -> None:
    assert effective_cleanup_path({"status": "adopted", "cleaned": None}) is None
    assert (
        effective_cleanup_path(
            {"status": "adopted", "cleaned": "", "decision": DECISION_CLEANED}
        )
        is None
    )


# ── 2. 历史版本 ─────────────────────────────────────────────────────────────


def test_cleaned_suffix_keeps_first_version_backwards_compatible() -> None:
    """第 1 版沿用既有的 `-nowm`（历史台账、既有代码都靠它），重洗才是 `-nowm-2`。"""
    assert cleaned_suffix(1) == "-nowm"
    assert cleaned_suffix(2) == "-nowm-2"
    assert cleaned_suffix(3) == "-nowm-3"


def test_next_cleanup_version_counts_existing_versions() -> None:
    assert next_cleanup_version(None) == 1
    assert next_cleanup_version({}) == 1
    assert next_cleanup_version({"versions": [{"version": 1}]}) == 2
    assert (
        next_cleanup_version({"versions": [{"version": 1}, {"version": 2}]}) == 3
    )


def test_record_cleanup_attempt_appends_history_and_points_top_level_at_latest(
    tmp_path: Path,
) -> None:
    """每次尝试都进 `versions`；顶层字段始终指**最新一版**（前端与收口点都读它）。"""
    first = tmp_path / "a-nowm.png"
    second = tmp_path / "a-nowm-2.png"
    _png(first)
    _png(second, (10, 20, 30))

    asyncio.run(
        record_cleanup_attempt(
            "run-x", tmp_path, "image-1", _record("image-1", first, adopted=False),
            version=1,
        )
    )
    asyncio.run(
        record_cleanup_attempt(
            "run-x", tmp_path, "image-1", _record("image-1", second, adopted=True),
            version=2,
        )
    )

    entry = read_cleanup_status("run-x", tmp_path)["image-1"]
    assert [v["version"] for v in entry["versions"]] == [1, 2]
    assert entry["status"] == "adopted", "顶层状态 = 最新一版"
    assert entry["cleaned"].endswith("a-nowm-2.png")
    assert entry["versions"][0]["adopted"] is False, "第一版的结论要留着"
    # 重洗之后决定重置成 auto：旧决定可能指向"已经不是最新的那一版"。
    assert entry["decision"] == DECISION_AUTO


def test_record_cleanup_attempt_is_idempotent_per_version(tmp_path: Path) -> None:
    """同一个版本号重复落账不能堆出两条（重试/重入时会发生）。"""
    cleaned = tmp_path / "a-nowm.png"
    _png(cleaned)
    for _ in range(3):
        asyncio.run(
            record_cleanup_attempt(
                "run-x", tmp_path, "image-1", _record("image-1", cleaned), version=1
            )
        )
    entry = read_cleanup_status("run-x", tmp_path)["image-1"]
    assert len(entry["versions"]) == 1


def test_reclean_writes_new_version_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch
) -> None:
    """**重洗不许覆盖旧版本** —— 用户要能来回对比、退回更满意的那一版。"""
    original = _png(tmp_path / "orig.png")
    old_version = tmp_path / "orig-nowm.png"
    _png(old_version, (1, 1, 1))

    # 先造一版历史（否则 next_cleanup_version 会当成第 1 版）。
    asyncio.run(
        record_cleanup_attempt(
            "run-x", tmp_path, "image-1", _record("image-1", old_version), version=1
        )
    )

    calls: list[int] = []

    async def _fake_clean(path: Path, *, asset_id: str, output_dir=None, version: int = 1):
        calls.append(version)
        new_file = tmp_path / f"{path.stem}{cleaned_suffix(version)}{path.suffix}"
        _png(new_file, (2, 2, 2))
        return _record(asset_id, new_file, adopted=True)

    monkeypatch.setattr(module, "clean_reference_image", _fake_clean)

    record = asyncio.run(
        reclean_reference_image(
            original, run_id="run-x", outputs_dir=tmp_path, asset_id="image-1"
        )
    )

    assert calls == [2], "重洗必须用第 2 版"
    assert record.cleaned is not None and record.cleaned.name == "orig-nowm-2.png"
    assert old_version.is_file(), "旧版本文件必须还在（覆盖掉就没法对比了）"
    entry = read_cleanup_status("run-x", tmp_path)["image-1"]
    assert [v["version"] for v in entry["versions"]] == [1, 2]


# ── 3. 决定写台账 ───────────────────────────────────────────────────────────


def test_set_cleanup_decision_writes_and_rejects_unknown(tmp_path: Path) -> None:
    asyncio.run(
        set_cleanup_decision("run-x", tmp_path, "image-1", DECISION_CLEANED)
    )
    entry = read_cleanup_status("run-x", tmp_path)["image-1"]
    assert entry["decision"] == DECISION_CLEANED

    assert (
        asyncio.run(set_cleanup_decision("run-x", tmp_path, "image-1", "乱填")) is False
    )
    assert read_cleanup_status("run-x", tmp_path)["image-1"]["decision"] == DECISION_CLEANED


def test_set_cleanup_decision_clears_stale_selection(tmp_path: Path) -> None:
    """选回 auto / original 时清掉 `selected`，免得它在下次切到 cleaned 时"复活"。"""
    asyncio.run(
        set_cleanup_decision(
            "run-x", tmp_path, "image-1", DECISION_CLEANED, selected="v1.png"
        )
    )
    assert read_cleanup_status("run-x", tmp_path)["image-1"]["selected"] == "v1.png"
    asyncio.run(set_cleanup_decision("run-x", tmp_path, "image-1", DECISION_ORIGINAL))
    assert "selected" not in read_cleanup_status("run-x", tmp_path)["image-1"]


# ── 4. 接线守卫 ─────────────────────────────────────────────────────────────


def test_submission_path_consults_ledger_before_cleaning() -> None:
    """**提交路径只读台账、不再清洗**（2026-10-28 口径：素材变了是绝对不允许的）。

    以前这里在没有结论时会"顺手洗一版" —— 那正是素材被悄悄换掉的来源。
    现在：有结论 → 用 `effective_cleanup_path` 判定的那一版（并核对文件指纹）；
    没结论 → 按原图提交 + 可见事件，绝不替换素材。
    """
    from feishu_generation_agent.graph import nodes

    source = inspect.getsource(nodes._clean_media_assets)
    one = source[source.index("async def clean_one"):]
    assert "_reference_cleanup_entry" in one, "必须先读台账"
    assert "effective_cleanup_path" in one, "必须用唯一收口点判定用哪版"
    assert "await clean_reference_image(" not in one, "提交阶段不许再洗一版（会换掉素材）"
    assert "recorded_version_sha" in one, "提交前要核对清洗版指纹"
    assert "_with_cleaned_file" in one, "换副本要走带 sha256/size 的 helper"


def test_decision_and_reclean_endpoints_exist() -> None:
    """两个端点必须挂上（前端就靠它们把选择传回来）。"""
    from feishu_generation_agent.web import app as web_app

    source = inspect.getsource(web_app)
    assert "/references/{asset_id}/watermark-decision" in source
    assert "/references/{asset_id}/reclean" in source
    # 取图要支持按版本取（重洗留了历史版本，界面要能来回切）。
    assert "version: int | None = None" in source


def test_preview_and_submission_share_one_chokepoint() -> None:
    """预览与提交**必须**走同一个判据，否则两处会各判一套、又变回不一致。"""
    from feishu_generation_agent.graph import nodes, runtime

    assert "effective_cleanup_path" in inspect.getsource(runtime._adopted_cleanup_path)
    assert "effective_cleanup_path" in inspect.getsource(runtime)
    assert "effective_cleanup_path" in inspect.getsource(nodes._clean_media_assets)
