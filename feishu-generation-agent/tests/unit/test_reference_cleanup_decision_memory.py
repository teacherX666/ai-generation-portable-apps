"""参考图「去水印」的两条 2026-10-28 口径（用户报的两个问题）。

① **清洗只针对平台水印，图内文字一律保留**。
   用户原话：「清洗时规定，只清洗平台水印，图中的文字要保留」。
   旧指令写的是"把所有叠加在画面上的文字去掉"，范围过大 —— 实测把 image-2 爱心中心的
   「Jewel Loop」洗糊了（台账里最差局部差异 63 > 35，护栏拒绝 → 用户只能退回原图）。

② **用户的"用原图 / 用清洗版"选择要跨运行继承**。
   用户原话：「图片清洗功能重做之后是不是会失效，你这都给我换成原图了」。
   真因：台账按 run 存，而每点一次「重新生成计划」都是新 run → 上一版的选择被重置成
   `auto`，auto 又恰好是"护栏拒绝 → 原图"。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from PIL import Image

from feishu_generation_agent.integrations import reference_watermark_clean as module


class _Asset:
    def __init__(self, asset_id: str, path: Path) -> None:
        self.asset_id = asset_id
        self.mime_type = "image/png"
        self.local_path = path


def _plain(path: Path, color=(120, 140, 110)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 400), color).save(path)
    return path


# ── ① 清洗指令：只去平台水印，图内文字保留 ───────────────────────────────────


def test_clean_prompt_targets_only_platform_watermarks() -> None:
    prompt = module._CLEAN_PROMPT
    assert "平台水印" in prompt and "角标" in prompt, "必须点名只洗平台水印/角标"
    assert "原样保留" in prompt and "不许擦除" in prompt, "必须明确要求保留图内文字"
    # 旧口径必须消失：它让模型把画面内容里的字也当成叠加文字擦掉。
    assert "所有**叠加在画面上的文字去掉**" not in prompt


def test_clean_prompt_tells_model_to_keep_when_unsure() -> None:
    """判不准时**保留**（用户口径的兜底方向：宁可留着水印，也别删用户的字）。"""
    assert "一律保留" in module._CLEAN_PROMPT


# ── ② 跨运行记忆 ─────────────────────────────────────────────────────────────


def test_memory_roundtrip_and_auto_clears(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    source = _plain(tmp_path / "ref.png")
    source_sha = module.source_image_sha(source)
    assert source_sha

    async def scenario() -> None:
        assert module.lookup_cleanup_decision(data_dir, source_sha) is None
        await module.remember_cleanup_decision(
            data_dir, source_sha, module.DECISION_ORIGINAL, asset_id="image-2"
        )
        record = module.lookup_cleanup_decision(data_dir, source_sha)
        assert record and record["decision"] == module.DECISION_ORIGINAL
        # `auto` = 忘掉这条记忆（用户把选择拨回自动）。
        await module.remember_cleanup_decision(data_dir, source_sha, module.DECISION_AUTO)
        assert module.lookup_cleanup_decision(data_dir, source_sha) is None

    asyncio.run(scenario())


def test_remembered_original_skips_cleaning_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户上一版选了「用原图」→ 新 run 直接继承，连清洗都不跑（省一次 73~90 秒调用）。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _plain(tmp_path / "inputs" / "ref.png")
    asset = _Asset("image-3", source)

    async def scenario() -> None:
        calls: list[str] = []

        async def _forbidden(*args: object, **kwargs: object) -> object:
            calls.append("clean")
            raise AssertionError("继承了「用原图」就不该再跑清洗")

        monkeypatch.setattr(module, "clean_reference_image", _forbidden)
        await module.set_cleanup_decision(
            "run-1",
            outputs_dir,
            "image-3",
            module.DECISION_ORIGINAL,
            data_dir=data_dir,
            source_path=source,
        )
        await module.cleanup_assets_in_background(
            [asset], run_id="run-2", outputs_dir=outputs_dir, data_dir=data_dir
        )
        assert calls == []
        entry = module.read_cleanup_status("run-2", outputs_dir)["image-3"]
        assert entry["decision"] == module.DECISION_ORIGINAL
        assert "上一版" in entry["message"]
        assert module.effective_cleanup_path(entry) is None, "原图生效"

    asyncio.run(scenario())


def test_remembered_cleaned_reuses_the_exact_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户上一版选定的清洗版还在盘上 → 原样沿用（重绘有随机性，重洗可能更差）。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _plain(tmp_path / "inputs" / "ref.png")
    chosen = _plain(tmp_path / "inputs" / "ref-nowm-2.png", (10, 12, 14))
    asset = _Asset("image-2", source)

    async def scenario() -> None:
        async def _forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("已选定版本的清洗版还在 → 不该重新清洗")

        monkeypatch.setattr(module, "clean_reference_image", _forbidden)
        await module.set_cleanup_decision(
            "run-1",
            outputs_dir,
            "image-2",
            module.DECISION_CLEANED,
            selected=str(chosen),
            data_dir=data_dir,
            source_path=source,
        )
        await module.cleanup_assets_in_background(
            [asset], run_id="run-2", outputs_dir=outputs_dir, data_dir=data_dir
        )
        entry = module.read_cleanup_status("run-2", outputs_dir)["image-2"]
        assert entry["decision"] == module.DECISION_CLEANED
        assert module.effective_cleanup_path(entry) == chosen, "用的必须是他选的那一版"

    asyncio.run(scenario())


def test_reclean_forgets_the_remembered_decision(tmp_path: Path) -> None:
    """重洗 = 重新来过 → 抹掉记忆，免得更早那次的旧选择又被搬回来。"""
    outputs_dir = tmp_path / "outputs"
    data_dir = tmp_path / "data"
    source = _plain(tmp_path / "inputs" / "ref.png")
    source_sha = module.source_image_sha(source)

    async def scenario() -> None:
        await module.remember_cleanup_decision(
            data_dir, source_sha, module.DECISION_ORIGINAL, asset_id="image-1"
        )
        assert module.lookup_cleanup_decision(data_dir, source_sha) is not None
        # 无水印的图 → `clean_reference_image` 直接早退，不联网。
        await module.reclean_reference_image(
            source,
            run_id="run-3",
            outputs_dir=outputs_dir,
            asset_id="image-1",
            data_dir=data_dir,
        )
        assert module.lookup_cleanup_decision(data_dir, source_sha) is None

    asyncio.run(scenario())


def test_wiring_passes_the_data_dir_through() -> None:
    """接线守门：记忆必须真的被接上（不接 = 白写）。"""
    from feishu_generation_agent.graph import nodes, runtime

    assert "data_dir=services.settings.data_dir" in inspect.getsource(nodes.ingest_source)
    runtime_source = inspect.getsource(runtime)
    assert "data_dir=Path(self.settings.data_dir)" in runtime_source
    assert "source_path=" in runtime_source, "选定时要能算出原图 sha"


def test_decision_memory_failure_never_breaks_cleaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记忆坏了只丢记忆，不许让清洗失败（与台账同一条兜法）。"""

    async def scenario() -> None:
        def _boom(self: Path) -> str:
            raise OSError("disk on fire")

        monkeypatch.setattr(Path, "read_text", _boom)
        await module.remember_cleanup_decision(
            tmp_path / "data", "deadbeef", module.DECISION_ORIGINAL
        )
        assert module.read_cleanup_decisions(tmp_path / "data") == {}

    asyncio.run(scenario())
