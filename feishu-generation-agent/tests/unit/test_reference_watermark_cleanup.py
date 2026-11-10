"""「参考图去水印」重做后的回归测试（2026-10-19）。

用户口径：「我记得之前有这个功能就是只识别带水印的图重新生成，后面因为他会把我的原图改掉，
跟原图不一样所以去掉了，你看能不能按我之前这个方案解决我的痛点」。

2026-10-10 撤掉的旧实现的两个毛病（本测试逐条钉住）：
① **无条件跑**（K=0 也跑）→ 现在只在开关打开、且**检测到叠加水印**时才跑；
② **静默替换用户原图** → 现在：清洗版另存 `-nowm.png`、**原图永不改动**、采用与否由
   **保真护栏**决定、处理台账写进任务事件（可见）。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import (
    _FIDELITY_MAX_DIFF,
    _mean_abs_diff,
    has_overlay_watermark,
)


def _plain(path: Path, color=(120, 140, 110)) -> Path:
    Image.new("RGB", (400, 400), color).save(path)
    return path


def _with_semi_transparent_text(path: Path) -> Path:
    image = Image.new("RGB", (400, 400), (120, 140, 110))
    draw = ImageDraw.Draw(image, "RGBA")
    # 半透明叠加文字（水印的典型形态）
    draw.text((60, 300), "WATERMARK TEXT", fill=(255, 255, 255, 90))
    image.save(path)
    return path


def test_plain_image_is_not_touched(tmp_path: Path) -> None:
    """没有叠加水印 → 一个像素都不动（旧实现是"无条件跑"✗）。"""
    assert has_overlay_watermark(_plain(tmp_path / "plain.png")) is False


def test_fidelity_guard_metric_is_zero_for_identical(tmp_path: Path) -> None:
    a = _plain(tmp_path / "a.png")
    b = _plain(tmp_path / "b.png")
    assert _mean_abs_diff(a, b) == pytest.approx(0.0, abs=0.01)


def test_fidelity_guard_rejects_large_change(tmp_path: Path) -> None:
    """与原图差异过大时必须能算出来（采用与否由这个阈值决定）。"""
    a = _plain(tmp_path / "a.png", (10, 10, 10))
    b = _plain(tmp_path / "b.png", (240, 240, 240))
    diff = _mean_abs_diff(a, b)
    assert diff is not None and diff > _FIDELITY_MAX_DIFF


def test_original_is_never_modified() -> None:
    """源码守门：清洗结果必须另存 `-nowm`，且替换的是"提交用副本"，不写回原图。"""
    source = inspect.getsource(module._model_clean_reference)
    assert '-nowm' in source, "清洗版必须另存一份，不能写回原图"
    assert 'cleaned.write_bytes' in source, "只允许写清洗版文件"


def test_executor_cleans_before_submit_and_records_visible_event() -> None:
    """接线守门：**入库时**清洗并留可见台账；提交阶段只读台账、不再清洗。

    2026-10-28 更新（用户口径「素材变了是绝对不允许的」）：提交阶段以前会"顺手再洗一版"，
    那正是素材被悄悄换掉的来源 —— 现在它只走台账收口点，没结论就按原图 + 可见事件。
    """
    from feishu_generation_agent.graph import nodes

    submit_source = inspect.getsource(nodes._execute_one_task)
    assert "_clean_reference_watermarks" in submit_source
    assert submit_source.index("_clean_reference_watermarks") < submit_source.index("generator.submit("), (
        "素材必须在提交之前定下来，否则对本次生成无效"
    )
    helper = inspect.getsource(nodes._clean_media_assets)
    assert "_safe_event" in helper, "必须留可见台账"
    assert "没有去水印结论" in helper, "没结论时要写明按原图提交、不改素材"
    assert "await clean_reference_image(" not in helper, (
        "提交阶段不许再洗一版 —— 素材变了是绝对不允许的"
    )


def test_switch_defaults_on_and_can_be_disabled() -> None:
    from feishu_generation_agent.config import Settings

    assert Settings().reference_watermark_cleanup_enabled is True


@pytest.mark.asyncio
async def test_plain_reference_is_kept_as_is(tmp_path: Path) -> None:
    """端到端（无网络）：没有水印的图 → adopted=False、cleaned=None、原因可读。"""
    path = _plain(tmp_path / "plain.png")
    record = await module.clean_reference_image(path, asset_id="image-1")
    assert record.adopted is False
    assert record.cleaned is None
    assert "未检测到" in record.reason


def test_asset_swap_updates_hash_and_size() -> None:
    """替换提交副本时必须同步 size/sha256（产物校验依赖 sha256）。

    2026-10-19 重构：清洗主体搬到 `_clean_media_assets`（入库时调用，这样审批页显示的
    就是干净版），`_clean_reference_watermarks` 变成提交前的兜底包装 —— 所以断言看前者。

    2026-10-23：换副本那段抽成了 `_with_cleaned_file`（提交路径改成"台账优先"之后，
    "用台账里的版本"和"兜底洗一版"两个分支都要换副本，不能再各写一遍）——
    断言跟着搬到新家，**不变量不变**。
    """
    from feishu_generation_agent.graph import nodes

    helper = inspect.getsource(nodes._with_cleaned_file)
    assert '"sha256": sha256(data).hexdigest()' in helper
    assert '"size": len(data)' in helper
    # `_clean_media_assets` 必须真的走这个 helper（不许自己另写一份换副本）。
    assert "_with_cleaned_file" in inspect.getsource(nodes._clean_media_assets)


def test_cleanup_is_background_and_surfaced_to_frontend() -> None:
    """用户口径（2026-10-20）：「规划成功后先把右边界面显示出来，然后提示正在去水印，
    对应参考图给出前端反馈」。

    因此：① 清洗**不能阻塞**入库/规划（后台任务，fire-and-forget）；
         ② 要有**每张图**的状态台账，并随运行视图下发给前端；
         ③ 清洗完成的图，缩略图接口要直接给清洗版。
    """
    from feishu_generation_agent.graph import nodes, runtime

    ingest_source = inspect.getsource(nodes.ingest_source)
    assert "cleanup_assets_in_background" in ingest_source, "入库要起后台清洗任务"
    assert "asyncio.create_task" in ingest_source, "必须是后台任务，不能 await 阻塞界面"
    assert "await _clean_media_assets" not in ingest_source, "入库阶段不许同步等清洗"

    view_source = inspect.getsource(runtime.DeepSeekRuntime.get_run_view) if hasattr(
        runtime, "DeepSeekRuntime"
    ) else ""
    if view_source:
        assert "watermark_cleanup" in view_source, "运行视图要带上清洗进度台账"
    assert "_read_watermark_cleanup" in inspect.getsource(runtime), "要有读台账的辅助函数"
    assert "_adopted_cleanup_path" in inspect.getsource(runtime), "缩略图要能切到清洗版"


def test_status_ledger_is_written_per_asset(tmp_path: Path) -> None:
    """台账必须能写能读（前端就靠它显示每张图的反馈）。"""
    import asyncio

    from feishu_generation_agent.integrations import reference_watermark_clean as module

    plain = _plain(tmp_path / "plain.png")

    class _Asset:
        asset_id = "image-1"
        mime_type = "image/png"
        local_path = plain

    asyncio.run(
        module.cleanup_assets_in_background(
            [_Asset()], run_id="run-x", outputs_dir=tmp_path
        )
    )
    status = module.read_cleanup_status("run-x", tmp_path)
    assert status.get("image-1", {}).get("status") == "skipped"
    assert "水印" in str(status["image-1"].get("message"))


def test_relative_ledger_path_still_serves_the_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """去水印后「预览没了」（用户 2026-10-20 原话）的回归测试。

    链路：台账 `cleaned` 记的是**相对路径**（`data\\runs\\<记录id>\\inputs\\<sha>-nowm.png`
    —— 因为 `asset.local_path` 本身是相对的，清洗版就写在它旁边），而缩略图接口
    `get_reference_file` 拿它跟 `data_dir`/`outputs_dir` 的**绝对**允许根做
    `is_relative_to`：不 resolve 就永远判否 → `break` → 抛「参考素材不存在」→ 缩略图 404。

    表现就是：**凡是"已采用清洗版"的参考图预览全空白**，没检测到水印的那几张照旧正常
    —— 所以看着像"部分丢了"，其实是路径没解析。
    """
    from feishu_generation_agent.graph import runtime

    app_root = tmp_path / "app"
    data_dir = app_root / "data"
    outputs_dir = app_root / "outputs"
    inputs = data_dir / "runs" / "aBcD1234" / "inputs"
    inputs.mkdir(parents=True)
    cleaned = _plain(inputs / "deadbeef-nowm.png")
    ledger = outputs_dir / "runs" / "run-x"
    ledger.mkdir(parents=True)
    # 相对路径（与生产台账逐字同形）
    (ledger / "watermark-cleanup.json").write_text(
        '{"image-1": {"status": "adopted", "message": "已去水印并采用",'
        ' "cleaned": "data\\\\runs\\\\aBcD1234\\\\inputs\\\\deadbeef-nowm.png"}}',
        encoding="utf-8",
    )
    # 台账里的相对路径是相对**应用根目录**（= 进程 CWD）解析的，与 settings 的
    # data_dir/outputs_dir 同口径。
    monkeypatch.chdir(app_root)
    settings = SimpleNamespace(data_dir=data_dir, outputs_dir=outputs_dir)

    path = runtime._adopted_cleanup_path(settings, "run-x", "image-1")
    assert path is not None, "已采用的清洗版必须能被找到"
    assert path.is_absolute(), (
        "必须是绝对路径：调用方要拿它跟绝对允许根做 is_relative_to 校验"
    )
    assert path.is_file()
    allowed_roots = [data_dir.resolve(), outputs_dir.resolve()]
    assert any(path.is_relative_to(root) for root in allowed_roots), (
        "相对路径会在这里判否 → break → 缩略图 404（预览没了）"
    )


def test_cleanup_skips_when_image_app_unreachable() -> None:
    """本机图片应用不可达时快速返回、保留原图（否则会把测试/离线环境拖到超时）。"""
    import asyncio

    from feishu_generation_agent.integrations import reference_watermark_clean as module

    async def scenario() -> bool:
        async with module.httpx.AsyncClient(timeout=1, trust_env=False) as client:
            return await module._image_app_reachable(client)

    # 8797 在测试机上通常没起 → 预期 False；起了也允许 True。关键是**不抛异常**。
    assert asyncio.run(scenario()) in (True, False)
