"""**确定性去水印**：只改水印像素，其余像素与原图逐点一致（2026-10-28 用户口径）。

用户原话：「我不是让你改守卫，而是让你只洗水印不洗字」→「拼接的啥啊」。

* 模型清洗是**整张重绘**：色调/纹理都会变，于是任何"把某块贴回去"的合成都会留可见补丁
  （上一版就是这么被用户抓到的）；
* 正解：探针给到水印框时，用 `cv2.inpaint` 按邻近像素把水印补掉 ——
  **改动范围严格等于水印掩膜**，画面其余部分与原图逐点相同，字一个像素都不动。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import CleanupScope

TOOL = (
    Path(module.__file__).resolve().parents[3]
    / "tools"
    / "inpaint_reference_watermark.py"
)


def test_inpaint_tool_exists_and_takes_normalized_boxes() -> None:
    """工具必须存在、且按**归一化**框接收水印位置（与探针输出同口径）。"""
    assert TOOL.is_file(), f"确定性修补工具不在：{TOOL}"
    source = TOOL.read_text(encoding="utf-8")
    assert "--box" in source and "nargs=4" in source
    assert "cv2.inpaint" in source, "必须用确定性修补，而不是再叫一次图片模型"
    assert "dilate" in source, "半透明水印边缘要膨胀盖住，否则会留残影"


def test_inpaint_tool_avoids_black_dots_on_textured_background() -> None:
    """**黑点回归**（用户 2026-10-28：「清洗右下角会有黑点」）：

    水印下面是黑大理石 + 白纹；旧判据把白纹也当水印挖掉 → 从暗底补回来就是一坨黑糊。
    工具必须：① 按连通域**筛掉长条白纹**（只留水印那种小碎块）；
    ② 补完做**"不许更暗"**的钳制；③ 自检 `dark_holes` 并把结果打进 JSON。
    """
    source = TOOL.read_text(encoding="utf-8")
    assert "connectedComponentsWithStats" in source, "必须按连通域筛掉大理石白纹"
    assert "max_component" in source
    assert "dark_holes" in source, "必须自检黑点"
    assert "clamp" in source, "必须有'不许比周围更暗'的钳制"


def test_inpaint_result_json_carries_the_self_check() -> None:
    """工具的输出契约：`dark_holes` 要能被上层读到（以后排查一眼就知道是不是修补留的）。"""
    source = TOOL.read_text(encoding="utf-8")
    assert '"dark_holes": dark_holes' in source
    assert '"dropped_components"' in source


def test_model_redraw_first_with_deterministic_fallback() -> None:
    """**接线（2026-10-29 用户口径「退回重绘清洗」）**：模型整张重绘优先（看不出补丁），
    被护栏拒或模型不可用时**才**退回确定性修补（只改水印像素）。"""
    source = inspect.getsource(module.clean_reference_image)
    assert "_model_clean_reference" in source and "_inpaint_watermark_boxes" in source
    assert source.index("_model_clean_reference") < source.index(
        "_inpaint_watermark_boxes"
    ), "模型重绘必须排在确定性修补之前（用户要的是重绘那种'不留痕迹'的效果）"
    assert "model_record.adopted" in source, "模型版过了护栏就直接用它"


def test_no_paste_back_patchwork_anymore() -> None:
    """上一版"把保护区贴回"的合成已删（它正是那块可见补丁的来源）。"""
    source = inspect.getsource(module)
    assert "_restore_protected_regions" not in source


def test_inpaint_is_skipped_without_watermark_boxes(tmp_path: Path) -> None:
    out = tmp_path / "out.png"
    assert (
        module._inpaint_watermark_boxes(
            Path("in.png"), out, CleanupScope(keep=(("品牌字", (0.3, 0.4, 0.7, 0.6)),))
        )
        is False
    )
    assert module._inpaint_watermark_boxes(Path("in.png"), out, None) is False
    assert not out.exists()


def test_deterministic_patch_must_prove_the_watermark_is_gone(tmp_path: Path) -> None:
    """**2026-11-02「金蛋」事故**：`cv2.inpaint` 只改了 44 个像素、水印原样，却因为工具
    退出码是 0 被记账成「已去水印并采用」—— 用户在审批页看到"说洗好了、水印还在"。

    判据：修补后水印框内的**笔画能量**必须明显下降。真洗掉 = 留存 23~24%；空操作 = 100%。
    """
    from PIL import Image, ImageDraw

    scope = CleanupScope(watermarks=((0.78, 0.955, 0.995, 0.995),))

    def _strokes(path: Path, *, erased: bool) -> Path:
        image = Image.new("RGB", (1600, 2848), (120, 120, 120))
        draw = ImageDraw.Draw(image)
        if not erased:
            for left in (1300, 1450):
                for offset in range(0, 70, 14):
                    draw.rectangle(
                        [left + offset, 2765, left + offset + 8, 2828],
                        fill=(250, 250, 250),
                    )
        image.save(path)
        return path

    before = _strokes(tmp_path / "before.png", erased=False)
    noop = _strokes(tmp_path / "noop.png", erased=False)      # 什么都没洗掉
    real = _strokes(tmp_path / "real.png", erased=True)       # 水印真的没了

    reason = module._watermark_still_there(before, noop, scope)
    assert reason is not None and "笔画能量" in reason, "空操作必须被识破并说清原因"
    assert module._watermark_still_there(before, real, scope) is None, "真洗掉必须放行"
    # 没有探针框 / 框里本来就没笔画 → 不判（退回旧行为，别把正常修补治死）。
    assert module._watermark_still_there(before, noop, None) is None
    assert module._watermark_still_there(before, noop, CleanupScope()) is None


def test_clean_reference_image_verifies_before_adopting_the_patch() -> None:
    """接线守门：**先验**确定性修补的结果，再记账成"已去水印"（顺序不能反）。"""
    source = inspect.getsource(module.clean_reference_image)
    assert "_watermark_still_there" in source, "必须验"
    assert source.index("_watermark_still_there") < source.index("deterministic=True"), (
        "验证必须在'采用确定性修补'那个出口之前"
    )
    assert "重新清洗" in source, "洗不掉时要给用户可操作的下一步"


def test_kept_verdict_still_uses_an_existing_deterministic_version(tmp_path: Path) -> None:
    """**结果侧兜底**：某次模型重绘被判 kept 时，磁盘上已有的**确定性修补版**必须优先生效。

    用户 2026-10-28：「我明明用的是清洗版，重新运行后图片又改成了未清洗的带水印的版本」
    —— 入库那环偶尔没跑成，提交阶段只看台账结论（kept → 回退原图）就会把水印送出去。
    """
    data = tmp_path / "inputs"
    data.mkdir(parents=True)
    good = data / "ref-nowm.png"
    good.write_bytes(b"cleaned")
    entry = {
        "status": "kept",
        "decision": "auto",
        "cleaned": str(good),
        "versions": [
            {"version": 1, "path": str(good), "adopted": False,
             "deterministic": True, "content_lost": False},
            {"version": 2, "path": str(data / "ref-nowm-2.png"), "adopted": False,
             "deterministic": False, "content_lost": True},
        ],
    }
    assert module.effective_cleanup_path(entry) == good

    # 没有确定性版本时，仍然老实回退原图（不伪造结论）。
    entry["versions"] = [entry["versions"][1]]
    assert module.effective_cleanup_path(entry) is None


def test_submission_never_cleans_and_never_swaps_material() -> None:
    """**素材不变量**（用户 2026-10-28：「素材变了是绝对不允许的」）：

    提交阶段**只读台账**（`effective_cleanup_path` 是唯一收口点）、**绝不重新清洗** ——
    否则"审批页看到的那版"会被悄悄换成另一次重绘的结果；没有结论时按原图提交并留可见事件。
    """
    from feishu_generation_agent.graph import nodes

    source = inspect.getsource(nodes._clean_media_assets)
    assert "await clean_reference_image(" not in source, "提交阶段不许再洗一版"
    assert "effective_cleanup_path" in source
    assert "_reference_cleanup_entry" in source
    assert "recorded_version_sha" in source and "file_sha256" in source, "要有素材指纹核对"
    assert "没有去水印结论" in source, "没结论时要留可见事件"


def test_resolution_restore_still_guards_the_model_fallback(tmp_path: Path) -> None:
    """模型回退路仍要还原分辨率（2K 重绘会把 1080×1920 放大成 1584×2816）。"""
    from PIL import Image

    original = tmp_path / "ref.png"
    Image.new("RGB", (400, 500), (10, 20, 30)).save(original)
    cleaned = tmp_path / "ref-nowm.png"
    Image.new("RGB", (586, 733), (10, 20, 30)).save(cleaned)
    assert module._match_resolution(original, cleaned) is True
    with Image.open(cleaned) as fixed:
        assert fixed.size == (400, 500)
    source = inspect.getsource(module._model_clean_reference)
    assert source.index("_match_resolution") < source.index("_protect_guard")
