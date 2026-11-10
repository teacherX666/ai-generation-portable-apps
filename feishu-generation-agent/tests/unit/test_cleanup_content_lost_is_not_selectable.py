"""被守卫判定"洗掉了画面内容"的版本**不许生效、也不许被选定**（2026-10-28）。

用户报「重新清洗了中间字还是没了」。查完发现链路其实全对：

* 探针给了正确的保护区（`爱心中心的品牌字 Jewel Loop [0.28,0.55,0.72,0.72]`）；
* 守卫也判了拒绝（台账原文「清洗抹掉了画面里的文字/内容（…边缘细节只剩 41%），已保留原图」）；
* **但同一份台账里 `decision: "cleaned"`、`selected: …-nowm-3.png`** ——
  按 2026-10-23 的老规矩"用户看过图有最终决定权"，这个手动选择压过了守卫，
  于是那个把字洗掉的版本照样生效。

用户口径是「不要洗字我只要洗水印」——**把用户的字洗掉的版本不是合法选项**。
"""

from __future__ import annotations

from pathlib import Path

from feishu_generation_agent.integrations import reference_watermark_clean as module

GOOD = str(Path("data/runs/doc/inputs/ref-nowm.png"))
BAD = str(Path("data/runs/doc/inputs/ref-nowm-2.png"))


def _entry(**over: object) -> dict:
    entry: dict = {
        "status": "kept",
        "message": "清洗抹掉了画面里的文字/内容…已保留原图",
        "cleaned": BAD,
        "decision": "cleaned",
        "selected": BAD,
        "versions": [
            {"version": 1, "path": GOOD, "adopted": True, "content_lost": False},
            {"version": 2, "path": BAD, "adopted": False, "content_lost": True},
        ],
    }
    entry.update(over)
    return entry


def test_version_lost_content_detects_the_bad_version() -> None:
    entry = _entry()
    assert module.version_lost_content(entry, BAD) is True
    assert module.version_lost_content(entry, GOOD) is False
    assert module.version_lost_content(entry, None) is False
    assert module.version_lost_content(None, BAD) is False


def test_effective_path_refuses_a_content_losing_choice() -> None:
    """**核心回归**：用户选了那一版，但那一版把字洗掉了 → 一律回退原图。"""
    assert module.effective_cleanup_path(_entry()) is None


def test_effective_path_still_honours_a_good_choice() -> None:
    """选的版本没洗坏内容 → 照旧按用户的选择生效（老规矩保留）。"""
    entry = _entry(decision="cleaned", selected=GOOD, cleaned=GOOD, status="adopted")
    assert module.effective_cleanup_path(entry) == module.resolve_cleaned_path(GOOD)


def test_auto_decision_still_uses_the_adopted_version() -> None:
    entry = _entry(decision="auto", status="adopted", cleaned=GOOD)
    entry.pop("selected")
    assert module.effective_cleanup_path(entry) == module.resolve_cleaned_path(GOOD)


def test_protect_rejection_is_marked_content_lost() -> None:
    """接线守门：保护守卫判拒时必须打上 `content_lost`，否则上面两处守卫都不起作用。"""
    import inspect

    source = inspect.getsource(module._model_clean_reference)
    assert "content_lost=True" in source
    record_source = inspect.getsource(module.record_cleanup_attempt)
    assert '"content_lost": bool(record.content_lost)' in record_source


def test_set_decision_rejects_a_content_losing_version(tmp_path: Path) -> None:
    """界面上也不许把它选上（否则用户一点，坏版本又生效）。"""
    import asyncio

    outputs_dir = tmp_path / "outputs"
    ledger = outputs_dir / "runs" / "run-1"
    ledger.mkdir(parents=True)
    (ledger / "watermark-cleanup.json").write_text(
        __import__("json").dumps({"image-2": _entry()}, ensure_ascii=False),
        encoding="utf-8",
    )

    async def scenario() -> None:
        ok = await module.set_cleanup_decision(
            "run-1", outputs_dir, "image-2", module.DECISION_CLEANED, selected=BAD
        )
        assert ok is False, "洗掉内容的版本不该被选定"
        # 选原图仍然允许（用户总有退路）。
        assert await module.set_cleanup_decision(
            "run-1", outputs_dir, "image-2", module.DECISION_ORIGINAL
        )
        # 选一个没洗坏内容的版本也允许。
        assert await module.set_cleanup_decision(
            "run-1", outputs_dir, "image-2", module.DECISION_CLEANED, selected=GOOD
        )

    asyncio.run(scenario())
