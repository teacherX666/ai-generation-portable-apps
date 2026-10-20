# -*- coding: utf-8 -*-
"""参考图去水印**并发化**的回归测试（2026-10-20）。

用户报：「我现在这个去水印的功能不能并发吗，图片一多耗时就会特别长」。

**实测证据**（生产事件时间差，每张图一次图片模型调用）：

| run | image-1 | image-2 | image-3 | 每张间隔 |
|---|---|---|---|---|
| `52d5a5ac` | 09:36:42 | 09:37:55 | 09:39:17 | 73s / 82s |
| `60440912` | 09:06:26 | 09:07:56 | — | 90s |
| `029345fc` | 08:59:35 | 09:00:48 | — | 73s |
| `df97b07e` | 08:46:43 | 08:48:04 | — | 81s |

串行 10 张就是 **12~15 分钟**。而每张图之间**没有任何依赖** —— 唯一的共享状态是台账
`watermark-cleanup.json`（读-改-写），所以并发前必须先给它加锁，否则会丢更新
（前端少几张图的状态；与 2026-10-12 `history.json` 被固定 tmp 名毁掉那次同源）。

本测试的**判别力**：`test_cleanup_is_concurrent_not_serial` 用"每张睡 0.3 秒 × 6 张"
把并发与串行分开 —— 串行 ≥1.8s，3 并发 ≈0.6s，断言 <1.2s。串行实现下必红。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import pytest
from PIL import Image

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import (
    CleanupRecord,
)


def _png(path: Path) -> Path:
    Image.new("RGB", (64, 64), (120, 140, 110)).save(path)
    return path


class _Asset:
    def __init__(self, path: Path, asset_id: str) -> None:
        self.asset_id = asset_id
        self.mime_type = "image/png"
        self.local_path = path


def _fake_cleaner(*, delay: float, adopted: bool, live: dict | None = None):
    async def fake(path: Path, *, asset_id: str, output_dir=None) -> CleanupRecord:
        if live is not None:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(delay)
        if live is not None:
            live["now"] -= 1
        return CleanupRecord(
            asset_id, path, None, None, adopted, "fake：未采用，保留原图"
        )

    return fake


@pytest.fixture
def always_watermarked(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过真实的水印检测（那是纯像素启发式，与本测试要验的并发无关）。"""
    monkeypatch.setattr(module, "has_overlay_watermark", lambda path: True)


async def test_cleanup_is_concurrent_not_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_watermarked: None
) -> None:
    """6 张图 × 每张 0.3 秒：串行 ≥1.8s，3 并发 ≈0.6s。"""
    monkeypatch.setattr(
        module, "clean_reference_image", _fake_cleaner(delay=0.3, adopted=False)
    )
    assets = [_Asset(_png(tmp_path / f"a{i}.png"), f"image-{i}") for i in range(6)]

    started = time.perf_counter()
    await module.cleanup_assets_in_background(
        assets, run_id="run-x", outputs_dir=tmp_path, concurrency=3
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 1.2, (
        f"6 张 0.3 秒的图跑了 {elapsed:.2f}s —— 这是串行（1.8s 以上）的特征；"
        "并发 3 应该在 0.6s 左右"
    )


async def test_concurrency_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_watermarked: None
) -> None:
    """并发**受上限约束**：不能把上游一次打满（会撞 429）。"""
    live = {"now": 0, "peak": 0}
    monkeypatch.setattr(
        module,
        "clean_reference_image",
        _fake_cleaner(delay=0.15, adopted=False, live=live),
    )
    assets = [_Asset(_png(tmp_path / f"b{i}.png"), f"image-{i}") for i in range(9)]

    await module.cleanup_assets_in_background(
        assets, run_id="run-x", outputs_dir=tmp_path, concurrency=2
    )

    assert live["peak"] == 2, f"峰值并发应恰好等于上限，实测 {live['peak']}"
    assert live["peak"] <= 2


async def test_every_asset_gets_a_ledger_entry_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_watermarked: None
) -> None:
    """台账读-改-写在并发下**不能丢更新**（否则前端少几张图的状态）。

    没有 `_STATUS_LOCK` 时，8 张图同时"读旧 → 各自写回"必然丢掉几条。
    """
    monkeypatch.setattr(
        module, "clean_reference_image", _fake_cleaner(delay=0.02, adopted=False)
    )
    assets = [_Asset(_png(tmp_path / f"c{i}.png"), f"image-{i}") for i in range(8)]

    await module.cleanup_assets_in_background(
        assets, run_id="run-x", outputs_dir=tmp_path, concurrency=4
    )

    status = module.read_cleanup_status("run-x", tmp_path)
    missing = [a.asset_id for a in assets if a.asset_id not in status]
    assert not missing, f"这些图的台账被并发写丢了：{missing}"
    assert all(status[a.asset_id]["status"] == "kept" for a in assets)


async def test_non_images_and_already_cleaned_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_watermarked: None
) -> None:
    """非图片 / 已洗过（`-nowm`）的素材不许再触发一次付费调用。"""
    calls: list[str] = []

    async def fake(path: Path, *, asset_id: str, output_dir=None) -> CleanupRecord:
        calls.append(asset_id)
        return CleanupRecord(asset_id, path, None, None, False, "fake")

    monkeypatch.setattr(module, "clean_reference_image", fake)
    video = _Asset(_png(tmp_path / "v.png"), "video-1")
    video.mime_type = "video/mp4"
    done = _Asset(_png(tmp_path / "d-nowm.png"), "image-done")
    fresh = _Asset(_png(tmp_path / "f.png"), "image-fresh")

    await module.cleanup_assets_in_background(
        [video, done, fresh], run_id="run-x", outputs_dir=tmp_path
    )

    assert calls == ["image-fresh"], f"只该洗这一张，实际：{calls}"


def test_wiring_guard_concurrency_is_actually_wired() -> None:
    """接线守门：两条清洗路径都必须真的并发，而不是只改了注释。"""
    background = inspect.getsource(module.cleanup_assets_in_background)
    assert "asyncio.gather" in background, "后台清洗必须并发"
    assert "Semaphore" in background, "并发必须有限流"
    assert "to_thread" in background, "水印检测是 CPU 循环，必须让出事件循环"

    from feishu_generation_agent.graph import nodes

    submit = inspect.getsource(nodes._clean_media_assets)
    assert "asyncio.gather" in submit, "提交前的兜底清洗也必须并发"
    assert "Semaphore" in submit
    assert "list(await asyncio.gather(" in submit, "必须保序：下游按下标引用素材"

    ingest = inspect.getsource(nodes.ingest_source)
    assert "reference_watermark_cleanup_concurrency" in ingest, (
        "并发张数要来自配置，否则用户没法按上游限流情况调"
    )


def test_ledger_write_is_atomic_and_locked() -> None:
    """台账写入必须加锁 + 原子替换（固定 tmp 名在并发下会互相截断）。"""
    source = inspect.getsource(module._write_status)
    assert "_STATUS_LOCK" in source
    assert "os.replace" in source


def test_default_concurrency_is_three() -> None:
    from feishu_generation_agent.config import Settings

    assert Settings().reference_watermark_cleanup_concurrency == 3


async def test_serial_behaviour_is_reproducible_with_concurrency_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, always_watermarked: None
) -> None:
    """**判别力自检**：把并发压成 1，耗时必须回到串行量级。

    没有这条，上面那条 `<1.2s` 有可能只是因为桩根本没睡够（假绿）。
    """
    monkeypatch.setattr(
        module, "clean_reference_image", _fake_cleaner(delay=0.2, adopted=False)
    )
    assets = [_Asset(_png(tmp_path / f"d{i}.png"), f"image-{i}") for i in range(4)]

    started = time.perf_counter()
    await module.cleanup_assets_in_background(
        assets, run_id="run-x", outputs_dir=tmp_path, concurrency=1
    )
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.8, (
        f"并发=1 时 4 张 0.2 秒的图只跑了 {elapsed:.2f}s —— 说明桩没真的等待，"
        "上面那条并发断言就没有判别力了"
    )
