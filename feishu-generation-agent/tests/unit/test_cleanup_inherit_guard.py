"""回归：**继承来的清洗版也要过保护区守卫**（2026-10-28）。

用户报「好像效果还是不对啊，中间还是被洗掉了」。查下来：那次（run `bcb5ebfe`）
**压根没洗** —— 台账写的是「沿用你在上一版选定的清洗版（未重新清洗）」，直接搬了
13:18 那个**已经把品牌字洗掉**的产物。原因是用户 13:22 在界面上点选过"清洗版"，
跨运行记忆把那个坏文件固定住了，之后每次运行都跳过清洗、原样复用。

所以规矩补一条：**继承来的版本同样要过守卫**；守卫说它坏了 → 忘掉这条记忆、
本次重新清洗（带着探针的范围洗）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import CleanupScope

KEEP_BOX = (0.30, 0.42, 0.72, 0.60)


def _image(path: Path, *, strokes: bool, watermark: bool = True) -> Path:
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


class _Asset:
    def __init__(self, asset_id: str, path: Path, sha: str) -> None:
        self.asset_id = asset_id
        self.mime_type = "image/png"
        self.local_path = path
        self.sha256 = sha


def test_inherited_cleaned_version_is_re_checked_and_dropped(tmp_path: Path) -> None:
    """上一版选定的清洗版**已经把字洗掉了** → 不许再原样搬，本次重新清洗。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _image(tmp_path / "inputs" / "ref.png", strokes=True)
    broken = _image(tmp_path / "inputs" / "ref-nowm.png", strokes=False)  # 字没了
    sha = module.source_image_sha(source)
    asset = _Asset("image-2", source, sha)

    scope = CleanupScope(keep=(("品牌字", KEEP_BOX),))

    async def scenario() -> None:
        # 用户上一版点选了"清洗版"（那一版是坏的）→ 记忆里存着
        await module.remember_cleanup_decision(
            data_dir, sha, module.DECISION_CLEANED, selected=str(broken), asset_id="image-2"
        )
        cleaned_calls: list[object] = []

        async def _fake_clean(path: Path, *, asset_id: str, scope=None, version: int = 1):  # noqa: ANN001
            cleaned_calls.append(scope)
            return module.CleanupRecord(
                asset_id, path, broken, 3.0, True, "重洗（带范围）"
            )

        original_clean = module.clean_reference_image
        module.clean_reference_image = _fake_clean  # type: ignore[assignment]
        try:
            await module.cleanup_assets_in_background(
                [asset],
                run_id="run-1",
                outputs_dir=outputs_dir,
                data_dir=data_dir,
                scope_probe=lambda _asset: _return(scope),
            )
        finally:
            module.clean_reference_image = original_clean  # type: ignore[assignment]

        entry = module.read_cleanup_status("run-1", outputs_dir)["image-2"]
        assert "已失效" in entry["message"], "必须写明上一版选定的清洗版失效并重新清洗"
        assert cleaned_calls, "必须真的重新洗一遍，而不是继续沿用坏文件"
        # 记忆必须被清掉，否则下一版又会搬回那个坏文件。
        assert module.lookup_cleanup_decision(data_dir, sha) is None

    asyncio.run(scenario())


def test_inherited_version_that_still_has_the_watermark_is_dropped(
    tmp_path: Path,
) -> None:
    """**2026-11-02「金蛋」事故的另一半**：被选定的那一版其实是空操作（水印原样）→
    跨运行记忆会把这张带水印的图**永久钉死**（每次新运行都"沿用"，压根不洗），
    用户看到的就是「水印没法去掉了」。所以继承校验必须同时问一句"水印真的没了吗"。
    """
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _image(tmp_path / "inputs" / "ref.png", strokes=True, watermark=True)
    # "清洗版"和原图在水印那一块**一模一样** —— 典型空操作。
    noop = _image(tmp_path / "inputs" / "ref-nowm.png", strokes=True, watermark=True)
    sha = module.source_image_sha(source)
    asset = _Asset("image-1", source, sha)
    scope = CleanupScope(watermarks=((0.72, 0.92, 0.995, 0.995),))

    async def scenario() -> None:
        await module.remember_cleanup_decision(
            data_dir, sha, module.DECISION_CLEANED, selected=str(noop), asset_id="image-1"
        )
        cleaned_calls: list[object] = []

        async def _fake_clean(path: Path, *, asset_id: str, scope=None, version: int = 1):  # noqa: ANN001
            cleaned_calls.append(scope)
            return module.CleanupRecord(asset_id, path, noop, 3.0, True, "重洗")

        original_clean = module.clean_reference_image
        module.clean_reference_image = _fake_clean  # type: ignore[assignment]
        try:
            await module.cleanup_assets_in_background(
                [asset],
                run_id="run-wm",
                outputs_dir=outputs_dir,
                data_dir=data_dir,
                scope_probe=lambda _asset: _return(scope),
            )
        finally:
            module.clean_reference_image = original_clean  # type: ignore[assignment]

        entry = module.read_cleanup_status("run-wm", outputs_dir)["image-1"]
        assert "已失效" in entry["message"], "水印还在的继承版必须被识破并写明原因"
        assert "水印" in entry["message"]
        assert cleaned_calls, "必须真的重新洗一遍，而不是继续沿用空操作的文件"
        assert module.lookup_cleanup_decision(data_dir, sha) is None, "记忆必须清掉"

    asyncio.run(scenario())


def test_good_inherited_version_is_still_reused(tmp_path: Path) -> None:
    """上一版选定的是**好的**清洗版 → 仍然沿用（省一次图片模型调用）。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _image(tmp_path / "inputs" / "ref.png", strokes=True)
    good = _image(tmp_path / "inputs" / "ref-nowm.png", strokes=True, watermark=False)
    sha = module.source_image_sha(source)
    asset = _Asset("image-1", source, sha)
    scope = CleanupScope(keep=(("品牌字", KEEP_BOX),))

    async def scenario() -> None:
        await module.remember_cleanup_decision(
            data_dir, sha, module.DECISION_CLEANED, selected=str(good), asset_id="image-1"
        )
        called: list[int] = []

        async def _forbidden(*args: object, **kwargs: object) -> object:
            called.append(1)
            raise AssertionError("好的继承版本不该重新清洗")

        original_clean = module.clean_reference_image
        module.clean_reference_image = _forbidden  # type: ignore[assignment]
        try:
            await module.cleanup_assets_in_background(
                [asset],
                run_id="run-2",
                outputs_dir=outputs_dir,
                data_dir=data_dir,
                scope_probe=lambda _asset: _return(scope),
            )
        finally:
            module.clean_reference_image = original_clean  # type: ignore[assignment]

        assert called == []
        entry = module.read_cleanup_status("run-2", outputs_dir)["image-1"]
        assert entry["message"] == "沿用你在上一版选定的清洗版（未重新清洗）"
        assert module.effective_cleanup_path(entry) == good

    asyncio.run(scenario())


def test_probe_is_only_called_once_per_asset(tmp_path: Path) -> None:
    """探针在一次清洗里只问一次（继承校验 + 正常清洗共用同一份结果）。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _image(tmp_path / "inputs" / "ref.png", strokes=True)
    broken = _image(tmp_path / "inputs" / "ref-nowm.png", strokes=False)
    sha = module.source_image_sha(source)
    asset = _Asset("image-2", source, sha)

    async def scenario() -> None:
        await module.remember_cleanup_decision(
            data_dir, sha, module.DECISION_CLEANED, selected=str(broken), asset_id="image-2"
        )
        calls: list[int] = []

        async def _probe(_asset: object) -> CleanupScope:
            calls.append(1)
            return CleanupScope(keep=(("品牌字", KEEP_BOX),))

        async def _fake_clean(path: Path, *, asset_id: str, scope=None, version: int = 1):  # noqa: ANN001
            return module.CleanupRecord(asset_id, path, broken, 3.0, True, "重洗")

        original_clean = module.clean_reference_image
        module.clean_reference_image = _fake_clean  # type: ignore[assignment]
        try:
            await module.cleanup_assets_in_background(
                [asset],
                run_id="run-3",
                outputs_dir=outputs_dir,
                data_dir=data_dir,
                scope_probe=_probe,
            )
        finally:
            module.clean_reference_image = original_clean  # type: ignore[assignment]

        assert len(calls) == 1, f"探针问多了：{len(calls)} 次"

    asyncio.run(scenario())


async def _return(value: object) -> object:
    return value


def _unused() -> None:  # pragma: no cover - 仅为保持 SimpleNamespace 导入被使用
    SimpleNamespace()
