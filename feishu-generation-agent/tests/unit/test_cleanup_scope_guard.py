"""清洗范围探针 + 保护区守卫（2026-10-28，用户方案）。

用户口径：「不要洗字我只要洗水印」+「还是用原本的清洗，但在清洗之前先丢给模型确认清洗范围」。

真实依据（run `82133315` 的 image-2）：13:18 那次清洗把「即梦AI」角标去掉了，
**同时把品牌字 "Jewel Loop" 也洗掉了**，而灰度护栏判它"已去水印并采用"
（最差局部差异 31 ≤ 35、受影响 0 块）。所以本测试钉住两件事：
1. 探针给的 keep 区**必须**写进清洗指令；
2. 洗完之后 keep 区的**边缘细节掉了就要拒**（这是灰度护栏看不见的那个洞）。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import (
    CleanupScope,
    _clean_prompt_with_scope,
    _protect_guard,
    bbox_tuple,
    scope_cache_key,
)

KEEP_BOX = (0.30, 0.42, 0.72, 0.60)


def _base(path: Path, *, strokes: bool = True, watermark: bool = True) -> Path:
    """灰底 + 中心一堆细笔画（模拟浮雕文字）+ 右下角一块亮水印。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (400, 500), (150, 110, 130))
    draw = ImageDraw.Draw(image)
    if strokes:
        for i in range(24):
            x = 130 + (i % 12) * 9
            y = 215 + (i // 12) * 14
            draw.line((x, y, x + 4, y + 8), fill=(220, 200, 210), width=2)
            draw.line((x + 2, y, x + 6, y + 8), fill=(90, 60, 80), width=1)
    if watermark:
        draw.rectangle((300, 470, 396, 496), fill=(250, 250, 250))
    image.save(path)
    return path


def test_scope_prompt_carries_regions_and_protections() -> None:
    scope = CleanupScope(
        watermarks=((0.75, 0.94, 0.99, 1.0),),
        keep=(("爱心中心的品牌字 Jewel Loop", KEEP_BOX),),
    )
    prompt = _clean_prompt_with_scope(scope)
    assert "只允许清理这些区域" in prompt
    assert "(0.75,0.94,0.99,1.00)" in prompt
    assert "必须原样保留" in prompt
    assert "Jewel Loop" in prompt and "(0.30,0.42,0.72,0.60)" in prompt
    # 没探针时必须是原样的静态指令（不许悄悄改变旧行为）。
    assert _clean_prompt_with_scope(None) == module._CLEAN_PROMPT
    assert _clean_prompt_with_scope(CleanupScope()) == module._CLEAN_PROMPT


def test_protect_guard_rejects_when_content_detail_is_wiped(tmp_path: Path) -> None:
    """**核心回归**：keep 区的细笔画被洗掉 → 必须拒（灰度护栏当年放行了）。"""
    before = _base(tmp_path / "before.png")
    after = _base(tmp_path / "after.png", strokes=False)  # 字没了，水印也去了
    scope = CleanupScope(keep=(("品牌字", KEEP_BOX),))
    reason = _protect_guard(before, after, scope)
    assert reason is not None, "字被洗掉了却没拦住 —— 这正是用户报的 bug"
    assert "品牌字" in reason and "保留原图" in reason


def test_protect_guard_passes_when_only_watermark_is_removed(tmp_path: Path) -> None:
    """正常清洗（只动角落水印、不动画面内容）必须放行。"""
    before = _base(tmp_path / "before.png")
    after = _base(tmp_path / "after.png", strokes=True, watermark=False)
    scope = CleanupScope(keep=(("品牌字", KEEP_BOX),))
    assert _protect_guard(before, after, scope) is None


def test_protect_guard_is_skipped_without_keep_region(tmp_path: Path) -> None:
    before = _base(tmp_path / "before.png")
    after = _base(tmp_path / "after.png", strokes=False)
    assert _protect_guard(before, after, None) is None
    assert _protect_guard(before, after, CleanupScope(watermarks=((0, 0, 1, 1),))) is None


def test_guard_runs_before_the_grey_guard() -> None:
    """接线守门：保护区守卫必须在灰度护栏**之前**判（灰度看不见低对比度文字的消失）。"""
    source = inspect.getsource(module._model_clean_reference)
    assert source.index("_protect_guard") < source.index("_FIDELITY_MAX_DIFF")


def test_bbox_tuple_validates_and_normalizes() -> None:
    assert bbox_tuple([0.1, 0.2, 0.5, 0.6]) == (0.1, 0.2, 0.5, 0.6)
    assert bbox_tuple([0.5, 0.6, 0.1, 0.2]) == (0.1, 0.2, 0.5, 0.6)  # 反向也能修
    assert bbox_tuple([-1, -1, 2, 2]) == (0.0, 0.0, 1.0, 1.0)  # 越界截断
    assert bbox_tuple([0.1, 0.2, 0.1, 0.6]) is None  # 零宽
    assert bbox_tuple("nope") is None
    assert bbox_tuple([0.1, 0.2, 0.5]) is None


def test_scope_cache_key_prefers_asset_sha(tmp_path: Path) -> None:
    path = _base(tmp_path / "ref.png")
    assert scope_cache_key(SimpleNamespace(sha256="abc123"), path) == "abc123"
    plain = scope_cache_key(SimpleNamespace(sha256=""), path)
    assert plain and len(plain) == 64


def test_probe_failure_is_swallowed_and_cleaning_continues() -> None:
    """探针失败 → 返回 None（照常洗）。用户口径：「失败也可以洗，我给用户选择的空间」。"""
    from feishu_generation_agent.graph import nodes

    async def scenario() -> None:
        class _Boom:
            async def probe(self, asset: object) -> object:
                raise RuntimeError("模型挂了")

        asset = SimpleNamespace(asset_id="image-1")
        assert await nodes._cleanup_scope_probe(SimpleNamespace(), asset) is None
        assert (
            await nodes._cleanup_scope_probe(
                SimpleNamespace(cleanup_scope_probe=_Boom()), asset
            )
            is None
        )

    asyncio.run(scenario())


def test_clean_reference_image_accepts_scope_and_uses_dynamic_prompt() -> None:
    source = inspect.getsource(module._model_clean_reference)
    assert "_clean_prompt_with_scope(scope)" in source, "清洗指令必须带上探针给的范围"
    assert "scope: CleanupScope | None," in source or "scope: CleanupScope | None = None" in source


def test_wiring_passes_the_probe_through() -> None:
    """接线守门：入库清洗与重洗都要带探针，bootstrap 要真的建出来。"""
    from feishu_generation_agent import bootstrap
    from feishu_generation_agent.graph import nodes, runtime

    assert "scope_probe=" in inspect.getsource(nodes.ingest_source)
    assert "data_dir=services.settings.data_dir" in inspect.getsource(nodes.ingest_source)
    assert "DeepSeekCleanupScopeProbe" in inspect.getsource(bootstrap)
    assert "scope_probe" in inspect.getsource(runtime)
