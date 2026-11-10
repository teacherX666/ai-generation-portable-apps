"""手绘标记护栏的回归测试（2026-10-22 建立，2026-10-23 扩）。

用户报（第一次）：「9090 端口飞书模块的红车引导效果一直不好」。
用户报（第二次）：「去水印怎么失效了，手机卖点两次任务都有水印」。

**事故一（2026-10-22）**：redcqchina《Sandfall》红车片头（生产 run `4452169a`）的参考图
`image-1` 是游戏截图，用户在车阵里**手绘了一个红圈**圈住那辆红车 —— 提示词里写的就是
「图一红圈中的红车」，这是"是哪辆车"的唯一指认标记。自动"去水印"把它整条重画掉了，而旧的
保真护栏（128×128 灰度**整图均值** = 7.52 ≤ 12）照样采用 → 模型拿到的参考图里没有圈 →
认错车 → 同一项目从 10-21 返工到 10-22 至少 6 轮。
**修法**：① 局部块护栏；② 手绘标记前置判据（视觉描述里提到红圈/标注框的图根本不去洗）。

**事故二（2026-10-23）**：上面那条局部块护栏**只看最差单块**，而**水印所在块的差异本来
就大** —— 一次"只在右下角水印处改动、其余像素几乎不动"的**完美清洗**照样被判 `42 > 35`
拒绝、回退成带水印的原图（run `aa2b426b` 两张图都是 `kept`）。全量 108 对生产图对里
**30% 被误拒**。**修法**：判定改成**合取** —— 最差块超阈值 **且** 受影响块数达标
（水印被洗掉 = 0~3 块；画面内容被改写 = 52~56 块）。拒绝率 30% → 14%。

本测试的判别力自检：
* `test_local_guard_sees_what_the_mean_guard_misses` —— 细圈被抹掉：旧护栏会采用、新护栏必须拒绝；
* `test_local_guard_ignores_isolated_redraw_spikes` —— 改动局限在水印那一小块：必须**采用**
  （这条红了就是事故二复发）；
* `test_local_guard_still_rejects_widespread_rewrite` —— 画面被大范围改写：必须拒绝。
"""

from __future__ import annotations

import inspect
import random
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFilter

from feishu_generation_agent.integrations import reference_watermark_clean as module
from feishu_generation_agent.integrations.reference_watermark_clean import (
    ANNOTATION_SKIP_REASON,
    _FIDELITY_MAX_DIFF,
    _LOCAL_DIFF_MAX,
    _LOCAL_DIFF_MIN_BLOCKS,
    _local_change_stats,
    _mean_abs_diff,
    _worst_block_diff,
    mentions_user_annotation,
)


def _guard_rejects(a: Path, b: Path) -> bool:
    """复刻 `clean_reference_image` 里局部块护栏的**合取**判定（2026-10-23 起）。"""
    stats = _local_change_stats(a, b)
    assert stats is not None
    local, altered = stats
    return local > _LOCAL_DIFF_MAX and altered >= _LOCAL_DIFF_MIN_BLOCKS

# 生产里那条真实视觉描述（run 4452169a 的 image-1，逐字抄自 vision_cache）。
PRODUCTION_CIRCLE_DESCRIPTION = {
    "asset_id": "image-1",
    "subjects": ["彩色沙画风景画（上方区域）", "多辆彩色玩具车/巴士（下方区域）", "进度指示器", "红色圆圈标记"],
    "scene": "手机游戏截图，分为上下两部分：上方是带圆角边框的沙画风景插图；下方是米黄色背景上排列的多辆彩色小车。",
    "composition": "垂直布局：顶部为沙画参考图，中间有进度条，底部为游戏区域（车辆网格）。红色手绘圆圈标注了右上角某辆红色车辆。",
    "actions": ["各辆车上印有方向箭头", "一辆红色车辆被红色手绘圆圈圈出"],
    "visible_text": ["0 / 6"],
    "probabilities": None,
}


def _game_screenshot(path: Path, *, ring: bool, rerender: bool = False) -> Path:
    """合成一张"游戏截图"：彩色小车网格 +（可选）手绘红圈。

    `rerender=True` 模拟图片模型的"最小编辑"：换分辨率 + 轻微模糊（实测生产清洗版是
    1016×1752 → 1568×2704，整图均值差异 7.5 就是这种重绘噪声）。
    """
    random.seed(7)
    width, height = 600, 900
    image = Image.new("RGB", (width, height), (238, 228, 200))
    draw = ImageDraw.Draw(image)
    colors = [(230, 70, 60), (70, 120, 220), (240, 190, 60), (90, 180, 110), (240, 240, 240)]
    for row in range(6):
        for col in range(5):
            x = 20 + col * 115
            y = 380 + row * 85
            color = colors[(row * 5 + col) % len(colors)]
            draw.rounded_rectangle([x, y, x + 95, y + 65], radius=12, fill=color, outline=(60, 60, 60), width=3)
            draw.rectangle([x + 30, y + 8, x + 65, y + 30], fill=(40, 40, 40))   # 车窗
    draw.rectangle([240, 250, 360, 300], fill=(255, 255, 255), outline=(30, 30, 30), width=3)
    if ring:
        # 手绘红圈：右上角那辆车的正面（细笔画、高饱和红）
        draw.ellipse([300, 400, 470, 545], outline=(228, 40, 40), width=8)
    if rerender:
        image = image.resize((int(width * 1.54), int(height * 1.54)), Image.LANCZOS)
        image = image.filter(ImageFilter.GaussianBlur(0.6))
    image.save(path)
    return path


def test_local_guard_sees_what_the_mean_guard_misses(tmp_path: Path) -> None:
    """**判别力自检**：细圈被抹掉时，旧护栏会采用、新护栏必须拒绝。

    这条如果红了，说明"红圈被洗掉"这个事故根本没被拦住 —— 修复是假的。
    """
    original = _game_screenshot(tmp_path / "orig.png", ring=True)
    cleaned = _game_screenshot(tmp_path / "cleaned.png", ring=False, rerender=True)

    mean = _mean_abs_diff(original, cleaned)
    worst = _worst_block_diff(original, cleaned)
    assert mean is not None and worst is not None
    assert mean <= _FIDELITY_MAX_DIFF, (
        f"整图均值 = {mean:.1f}：旧护栏本来就会采用（这正是事故的成因）"
    )
    assert worst > _LOCAL_DIFF_MAX, (
        f"最差局部块 = {worst:.1f} 必须超过阈值 {_LOCAL_DIFF_MAX}，否则红圈还是会被洗掉"
    )
    assert _guard_rejects(original, cleaned), (
        "细圈被抹掉的图必须被**合取**判据拒绝（最差块超阈值 且 受影响块数达标）"
    )


def test_local_guard_ignores_isolated_redraw_spikes(tmp_path: Path) -> None:
    """**2026-10-23 事故的判别力自检**：改动局限在一小块时，必须**采用**。

    生产形态：一次"只在右下角水印处改动、其余像素几乎不动"的清洗，最差块同样会顶到
    39~42（水印所在块差异本来就大）> 35 —— 只看最差单块就会把它拒掉、回退成带水印的原图，
    用户看到的就是「去水印失效」。加了"受影响块数"这一维后必须放行。

    这条如果红了，说明"水印被洗掉"又会被误判成"抹掉了内容"。
    """
    random.seed(23)
    width, height = 600, 900
    original = tmp_path / "orig.png"
    Image.new("RGB", (width, height), (238, 228, 200)).save(original)

    # 清洗版：只在右下角一小块"水印区"改动（≈1 个 32×32 网格块内），其余完全一致。
    # 要用**高对比**小块：水印（白字/角标）与底色差得远，块均值轻松越过 35。
    cleaned_image = Image.open(original).copy()
    draw = ImageDraw.Draw(cleaned_image)
    draw.rectangle([508, 818, 524, 842], fill=(0, 0, 0))
    cleaned = tmp_path / "orig-nowm.png"
    cleaned_image.save(cleaned)

    stats = _local_change_stats(original, cleaned)
    assert stats is not None
    local, altered = stats
    assert local > _LOCAL_DIFF_MAX, f"这一小块差异要够大（实测 {local:.0f}），才复现得了事故"
    assert altered < _LOCAL_DIFF_MIN_BLOCKS, (
        f"受影响块数 = {altered}，应当少于 {_LOCAL_DIFF_MIN_BLOCKS}（改动很局部）"
    )
    assert not _guard_rejects(original, cleaned), (
        "改动局限在水印那一小块时必须采用 —— 拒掉就是用户看到的「去水印失效」"
    )


def test_local_guard_still_rejects_widespread_rewrite(tmp_path: Path) -> None:
    """反向判别力：画面被**大范围改写**（多条横带）时必须拒绝，别把护栏治成摆设。"""
    original = _game_screenshot(tmp_path / "wide.png", ring=False, rerender=True)
    cleaned_image = Image.open(original).copy()
    draw = ImageDraw.Draw(cleaned_image)
    for y in (150, 400, 650, 900, 1150):
        draw.rectangle([120, y, 780, y + 60], fill=(10, 10, 10))
    cleaned = tmp_path / "wide-nowm.png"
    cleaned_image.save(cleaned)
    assert _guard_rejects(original, cleaned), "大范围改写画面内容必须被拒绝"


def test_local_guard_still_accepts_corner_watermark_removal(tmp_path: Path) -> None:
    """只洗掉角落那块半透明水印文字时，新护栏**不能**把正常清洗也拒掉（不能治死功能）。"""
    random.seed(11)
    width, height = 600, 900
    base = Image.new("RGB", (width, height), (238, 228, 200))
    draw = ImageDraw.Draw(base)
    for row in range(6):
        for col in range(5):
            x, y = 20 + col * 115, 380 + row * 85
            draw.rounded_rectangle([x, y, x + 95, y + 65], radius=12, fill=(200, 120, 90))
    original = tmp_path / "wm.png"
    base.save(original)
    cleaned_image = base.copy()
    cleaner = ImageDraw.Draw(cleaned_image, "RGBA")
    cleaner.text((330, 800), "WATERMARK TEXT", fill=(255, 255, 255, 90))
    # 原图是**清水印前**的那张：反过来构造即可（把水印画在"清洗版"上再反转）
    cleaned = tmp_path / "wm-nowm.png"
    cleaned_image.save(cleaned)
    original, cleaned = cleaned, original          # 原图带水印、清洗版没有

    worst = _worst_block_diff(original, cleaned)
    assert worst is not None and worst <= _LOCAL_DIFF_MAX, (
        f"角落水印清洗的最差局部块 = {worst:.1f}，不该超过 {_LOCAL_DIFF_MAX} —— "
        f"否则正常去水印会被这条新护栏全部拒掉"
    )


# ── 2026-11-02「金蛋」事故：水印框内的改动不该算"改写了画面内容" ──────────────────
#
# 生产几何（run `39aaa705` / `66522685`，1600×2848，水印「豆包AI生成」在右下角）：
# 探针给的水印框 = (0.78, 0.955, 0.995, 0.995)，把水印洗掉在 32×32 网格上正好是
# **4 块**（row 31 / col 26,27,29,30）→ 卡在 `_LOCAL_DIFF_MIN_BLOCKS = 4` 的刀口上，
# 一次成功清洗被判"改写了画面内容"、退回确定性修补，而那次修补是个空操作。

_PRODUCTION_WATERMARK_BOX = (0.78, 0.955, 0.995, 0.995)


def _production_shape_image(
    path: Path, *, watermark: bool, bands: bool = False
) -> Path:
    """复刻生产图的几何：底部一行水印笔画落在探针框内，可选"画面内容被抹掉"的横带。"""
    width, height = 1600, 2848
    image = Image.new("RGB", (width, height), (120, 120, 120))
    draw = ImageDraw.Draw(image)
    if watermark:
        # 落在这 4 个网格块上：col 26,27 与 col 29,30（row 31）—— 与生产实测的 4 块一致。
        for base in (1300, 1350, 1450, 1500):
            for offset in (0, 14, 28, 42):
                draw.rectangle(
                    [base + offset, 2765, base + offset + 8, 2828],
                    fill=(250, 250, 250),
                )
    if bands:                                           # 框外：被"模型"抹掉的画面内容
        for y in (900, 1200, 1500):
            draw.rectangle([200, y, 1400, y + 120], fill=(10, 10, 10))
    image.save(path)
    return path


def _guard_rejects_with(tmp_path: Path, before: Path, after: Path, exclude) -> bool:
    stats = _local_change_stats(before, after, exclude=exclude)
    assert stats is not None
    local, altered = stats
    return local > _LOCAL_DIFF_MAX and altered >= _LOCAL_DIFF_MIN_BLOCKS


def test_local_guard_excludes_changes_inside_the_probed_watermark_box(
    tmp_path: Path,
) -> None:
    """**2026-11-02 事故的判别力自检**：水印框内的改动必须被豁免。

    旧行为（不豁免）在这张图上**必然拒绝**一次成功的水印清洗 —— 前半段断言就是复现事故；
    豁免之后必须放行。这条红了 = 用户又会看到「清洗功能失效了，水印没法去掉了」。
    """
    original = _production_shape_image(tmp_path / "orig.png", watermark=True)
    cleaned = _production_shape_image(tmp_path / "cleaned.png", watermark=False)

    stats = _local_change_stats(original, cleaned)
    assert stats is not None
    local, altered = stats
    assert local > _LOCAL_DIFF_MAX, f"水印被洗掉必须制造出超阈块（实测 {local:.0f}）"
    assert altered >= _LOCAL_DIFF_MIN_BLOCKS, (
        f"受影响块数 = {altered}，生产实测就是 4 —— 这正是事故卡住的地方"
    )
    assert _guard_rejects(original, cleaned), "不豁免时这条判据必然拒绝（复现事故）"

    assert not _guard_rejects_with(
        tmp_path, original, cleaned, (_PRODUCTION_WATERMARK_BOX,)
    ), "探针确认的水印框内的改动必须豁免，否则成功清洗永远过不了"


def test_local_guard_exemption_does_not_disarm_content_rewrite(tmp_path: Path) -> None:
    """反向判别力：豁免**只**对水印框生效 —— 框外的"抹掉画面内容"照样必须拒绝。"""
    original = _production_shape_image(
        tmp_path / "orig_bands.png", watermark=True, bands=True
    )
    cleaned = _production_shape_image(tmp_path / "cleaned_bands.png", watermark=False)
    assert _guard_rejects_with(
        tmp_path, original, cleaned, (_PRODUCTION_WATERMARK_BOX,)
    ), "框外大范围改写画面内容，即使豁免水印框也必须拒绝"


def test_local_guard_exemption_is_refused_for_huge_boxes(tmp_path: Path) -> None:
    """框太大就不豁免（豁免等于把护栏拆掉）：宁可退回旧行为。"""
    image = _production_shape_image(tmp_path / "huge.png", watermark=True)
    assert module._excluded_block_mask(image, ((0.0, 0.0, 1.0, 1.0),)) is None
    assert module._excluded_block_mask(image, ()) is None
    assert module._excluded_block_mask(
        image, (_PRODUCTION_WATERMARK_BOX,)
    ) is not None


def test_worst_block_diff_is_zero_for_identical(tmp_path: Path) -> None:
    a = _game_screenshot(tmp_path / "a.png", ring=True)
    b = _game_screenshot(tmp_path / "a2.png", ring=True)
    assert _worst_block_diff(a, b) == 0.0


def test_worst_block_diff_tolerates_resolution_change(tmp_path: Path) -> None:
    """清洗版换分辨率（实测 1016×1752 → 1568×2704）也要能比，不能抛。"""
    a = _game_screenshot(tmp_path / "a.png", ring=True)
    b = tmp_path / "b.png"
    Image.open(a).resize((900, 1350)).save(b)
    assert _worst_block_diff(a, b) is not None


def test_clean_reference_image_consults_the_local_guard() -> None:
    """源码守门：清洗结果必须过**局部块**护栏（只比整图均值就是这次事故的成因）。

    2026-10-23 起判定是**合取**：最差块超阈值 **且** 受影响块数达标 —— 少了后一条，
    "把水印洗掉"这件事本身就会把好结果拒掉（见 `test_local_guard_ignores_isolated_redraw_spikes`）。
    """
    source = inspect.getsource(module._model_clean_reference)
    assert "_local_change_stats" in source, "必须比局部块"
    assert "_LOCAL_DIFF_MAX" in source, "必须按局部块阈值判定"
    assert "_LOCAL_DIFF_MIN_BLOCKS" in source, (
        "必须同时要求受影响块数达标（2026-10-23 事故：只看最差单块会把水印本身的变化误判成抹内容）"
    )
    # 局部块判定必须发生在"返回采用"之前（采用那句话是这条函数唯一的成功出口）。
    assert source.index("_local_change_stats") < source.index("已去水印并采用"), (
        "局部块护栏必须在 return 采用之前生效"
    )


def test_mentions_user_annotation_on_production_description() -> None:
    """生产那条真的手绘红圈描述必须命中（事故图就是它）。"""
    assert mentions_user_annotation(PRODUCTION_CIRCLE_DESCRIPTION) is True
    assert mentions_user_annotation(
        {"subjects": ["红色圆圈标注（5个）"], "scene": "视频播放器界面"}
    ) is True
    assert mentions_user_annotation(
        {"subjects": ["红色矩形标注框（共4个）"], "scene": "户外草地"}
    ) is True


def test_mentions_user_annotation_ignores_same_word_other_meaning() -> None:
    """同词不同义的描述不能误伤 —— 误判的代价是**该洗的图不洗了**（水印会被成片复现）。

    这几条都逐字取自生产 `vision_cache`：它们不含任何用户手绘标记。
    """
    negatives = [
        "六名身穿足球服的老年男性围成圆圈俯视镜头",          # 人围成圈，不是画的圈
        "数字插画／动漫风格，色彩饱和明亮，具有手绘质感",      # 手绘质感 = 画风
        "车辆上标注白色方向箭头（向上、向下、向左、向右）",    # 游戏 UI 的方向箭头
        "包含镜头编号、拍摄角度、动作描述、情绪标注和运镜说明",  # 情绪"标注"是文字
        "屋檐从上方框住场景，背景为一排带灯光的旧式街道建筑",  # 门框/取景框
        "底部居中有文字标注",                                # 文字标注
        "图片右下角标注为AI生成图像",                        # 水印说明
    ]
    for text in negatives:
        assert mentions_user_annotation({"scene": text}) is False, text


def test_mentions_user_annotation_accepts_pydantic_and_none() -> None:
    from feishu_generation_agent.domain.document import VisionDescription

    description = VisionDescription.model_validate(
        {
            **PRODUCTION_CIRCLE_DESCRIPTION,
            "style": "数字游戏UI截图",
            "characters": [],
            "colors": ["红色", "米黄色"],
            "probable_role": "游戏截图参考图",
            "uncertainties": [],
        }
    )
    assert mentions_user_annotation(description) is True
    assert mentions_user_annotation(None) is False
    assert mentions_user_annotation("") is False


def test_annotation_gate_runs_before_cleaning_and_only_reads_cache() -> None:
    """接线守门：① 真正发清洗请求之前先判手绘标记；② 只读缓存、**绝不打模型**；③ 后台那条也要判。

    2026-10-28 更新：提交阶段改成"只读台账、不再清洗"（素材不变量），
    所以"先判标记再洗"这条顺序守门看的是**入库后台清洗**那条路
    （`cleanup_assets_in_background`，它才是真的会发清洗请求的地方）。
    """
    from feishu_generation_agent.graph import nodes
    from feishu_generation_agent.integrations import reference_watermark_clean as clean

    clean_source = inspect.getsource(clean.cleanup_assets_in_background)
    assert "annotation_reason" in clean_source
    assert clean_source.index("annotation_reason") < clean_source.index(
        "clean_reference_image"
    ), "手绘标记的判定必须在真正发清洗请求之前"

    helper = inspect.getsource(nodes._user_annotation_skip_reason)
    assert "cached_description" in helper, "只能读缓存"
    assert "analyze(" not in helper, "不许为判据多打一次模型调用"

    ingest = inspect.getsource(nodes.ingest_source)
    assert "annotation_reason=" in ingest, "后台清洗那条路也要带上同一个判据"
    # 提交阶段不再清洗 → 也就不会绕过这个判据去洗别人的手绘标记。
    assert "await clean_reference_image(" not in inspect.getsource(
        nodes._clean_media_assets
    )


def test_user_annotation_skip_reason_reads_cached_description() -> None:
    """`cached_description` 命中 → 返回给用户看的原因；没有分析器/缓存 → None（不报错）。"""
    import asyncio

    from feishu_generation_agent.graph import nodes

    class _Analyzer:
        def __init__(self, description: object) -> None:
            self._description = description
            self.analyze_called = False

        async def cached_description(self, asset: object) -> object:
            return self._description

        async def analyze(self, asset: object) -> object:      # pragma: no cover
            self.analyze_called = True
            raise AssertionError("判据绝不允许触发模型调用")

    asset = SimpleNamespace(asset_id="image-1")

    hit = _Analyzer(PRODUCTION_CIRCLE_DESCRIPTION)
    reason = asyncio.run(
        nodes._user_annotation_skip_reason(SimpleNamespace(vision_analyzer=hit), asset)
    )
    assert reason == ANNOTATION_SKIP_REASON
    assert hit.analyze_called is False

    miss = _Analyzer(None)
    assert asyncio.run(
        nodes._user_annotation_skip_reason(SimpleNamespace(vision_analyzer=miss), asset)
    ) is None
    # 没有 vision_analyzer（多模态流水线的常见形态）→ 不报错、返回 None，
    # 由局部块护栏兜底。
    assert asyncio.run(
        nodes._user_annotation_skip_reason(SimpleNamespace(vision_analyzer=None), asset)
    ) is None


def test_background_cleanup_honours_the_annotation_gate(tmp_path: Path) -> None:
    """后台那条路：判据说"有手绘标记"就**一个像素都不动**，并把原因写进台账给前端。"""
    import asyncio

    image = _game_screenshot(tmp_path / "shot.png", ring=True)

    class _Asset:
        asset_id = "image-1"
        mime_type = "image/png"
        local_path = image

    async def always_annotated(asset: object) -> str | None:
        return ANNOTATION_SKIP_REASON

    asyncio.run(
        module.cleanup_assets_in_background(
            [_Asset()],
            run_id="run-annotated",
            outputs_dir=tmp_path,
            annotation_reason=always_annotated,
        )
    )
    status = module.read_cleanup_status("run-annotated", tmp_path)
    assert status["image-1"]["status"] == "skipped"
    assert status["image-1"]["message"] == ANNOTATION_SKIP_REASON
    assert not (tmp_path / "shot-nowm.png").exists(), "有手绘标记的图不许产出清洗版"


def test_background_cleanup_without_gate_still_works(tmp_path: Path) -> None:
    """不传判据时行为不变（纯色图没有水印 → skipped/未检测到水印），别把老路径改坏。

    刻意用**纯色图**：`has_overlay_watermark` 直接判否 → 不会去连本机图片应用（8797）
    —— 测试绝不允许因为本机应用在跑就真的提交一次去水印任务（那是真花钱的）。
    """
    import asyncio

    image = tmp_path / "plain.png"
    Image.new("RGB", (400, 400), (120, 140, 110)).save(image)

    class _Asset:
        asset_id = "image-2"
        mime_type = "image/png"
        local_path = image

    asyncio.run(
        module.cleanup_assets_in_background(
            [_Asset()], run_id="run-plain", outputs_dir=tmp_path
        )
    )
    status = module.read_cleanup_status("run-plain", tmp_path)
    assert status["image-2"]["status"] == "skipped"
    assert "未检测到" in status["image-2"]["message"]
