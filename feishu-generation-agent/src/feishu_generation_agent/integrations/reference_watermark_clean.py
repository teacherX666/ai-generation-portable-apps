"""参考图水印清洗（自动、前置、**带保真护栏**）—— 2026-10-19 重做。

用户口径：
「我记得之前有这个功能就是只识别带水印的图重新生成，后面因为他会把我的原图改掉，跟原图不一样
 所以去掉了，你看能不能按我之前这个方案解决我的痛点」。

历史（仓库记录，2026-10-10 撤掉的原因）：
* 旧实现是**无条件跑**（K=0 也跑）✗，判据只看 `reason` 里的关键词 ✗；
* 而且它**静默替换了用户的原图** ✗ —— 清洗结果与实物不一致时，用户**看不到、也拦不住** ✓。

本次设计（三道护栏，专治上面那两个问题）：
1. **只处理"真的带水印"的图**：先做水印检测（亮 + 低饱和 + 细笔画 + 位于画面边缘/角落），
   检测不到就**一个像素都不动**；
2. **保真护栏**：清洗后与原图做**逐像素比对**（缩到 128×128 灰度比均值差）——只有
   `mean_abs_diff <= 6` **且**检测显示"水印已消失"**才采用**；否则**丢弃清洗结果、保留原图**；
3. **原图永不改动**：清洗结果另存 `-nowm.png`，只在**提交给上游用的副本**上替换路径；
   台账（`WatermarkCleanupRecord`）写明原图/清洗版/相似度/是否采用，交给执行层写进任务事件。

依赖只用 **PIL**（feishu venv 里没有 numpy/cv2），图片清洗走本机 nano-banana 应用（8797）。

── 2026-10-22：**整图均值护栏看不见"局部被抹掉"**，用户的红圈就是被这样洗掉的 ──

用户报：「9090 端口飞书模块的红车引导效果一直不好」。查下来是一次**静默删除用户资产**：

* 生产 run `4452169a`（redcqchina《Sandfall》红车片头）的参考图 `image-1` 是游戏截图，
  用户在**车阵里手绘了一个红圈**圈住那辆红色小车 —— 这就是"是哪辆车"的**唯一指认标记**，
  提示词里写的就是「图一红圈中的红车」「@图片1 右上角红圈标注的那辆红色小车」。
* 自动清洗把它当成"叠加标记"重画掉了：原图有圈、提交版 `-nowm` 里**圈没有了**
  （本地视觉模型两次判读一致：原图「有一个手绘红色圆圈」，清洗版「没有」）。
* 而 `_mean_abs_diff` 报 **7.52 ≤ 12 → 照样采用**。原因是 128×128 灰度均值把一条细圈
  **稀释没了**：同一对图的**最差 32×32 局部块**差异是 **63.7**，而两张"只洗掉角落水印文字"
  的正常清洗只有 **14.8 / 9.4**。
* 后果：模型拿到的参考图里没有任何指认标记 → 只能瞎猜是哪辆红车 → 同一个项目
  从 10-21 返工到 10-22 至少 6 轮（`carried_rework_requirements` 里全是"红车是哪一辆"）。

因此补两道护栏（2026-10-22）：

* **局部块护栏**（`_worst_block_diff`）：除整图均值外，再比"最差局部块"的差异，
  超 `_LOCAL_DIFF_MAX` 就**丢弃清洗结果、保留原图** —— 这条对"抹掉画面里某样东西"
  敏感，对"只去掉角落一小块水印"不敏感，正好把两者分开；
* **手绘标记前置判据**（`mentions_user_annotation`）：视觉描述里提到红圈/标注框等
  **用户手绘标记**的图**根本不去洗**（少一次图片模型调用，也绝不冒险重画）。判据在生产
  378 条参考图描述上校准过：命中 7 条、逐条核对**全是**真的手绘标注，而「围成圆圈站立」
  「手绘质感」「方向箭头」「情绪标记」「木质门框」这些同词不同义的一条都没误伤。
  注意：多模态规划流水线下常常**没有**视觉描述，所以这条是**尽力而为**，
  真正兜底的是上面那条局部块护栏。

── 2026-10-23：局部块护栏**只看最差单块**，把"水印被洗掉"误判成"抹掉了内容" ──

用户报：「9090 端口飞书模块去水印怎么失效了，手机卖点两次任务都有水印」。

* 生产 run `aa2b426b`（10-23 13:08）两张参考图台账都是 `kept`：`最差局部差异 42 > 35`、
  `39 > 35` → 丢弃清洗结果、回退成**带水印的原图**。而实际看图确认：原图右下角有「即梦AI」
  水印，清洗版**水印已经没了**、主体完好 —— **清洗成功了，是护栏把好结果扔了**。
* 根因：`_worst_block_diff` 取的是 **1024 个块里的最大值**（`_LOCAL_BLOCK` 是**网格数**，
  不是块的像素数），而**水印所在块的差异本来就大** —— "把水印洗掉"这件事本身就会制造一个
  高分块，于是"改得越成功越容易被拒"。全量 108 对生产图对里 **32 对（30%）超阈值被拒**，
  分布 6→255 连续、35 恰好落在中位数附近，**根本没有分界**。
* 修法：判定改成**合取** —— 最差块 > 35 **且** 受影响块数 ≥ `_LOCAL_DIFF_MIN_BLOCKS`(4)。
  实测区分度：水印被洗掉 = **0~3 块**（改动局限在水印那一小块）；画面内容被改写 =
  **52 / 56 块**（多条横带 / 一整圈）。全量拒绝率 **30% → 14%**，17 对被误杀的翻回采用。
* ⚠️ 这条判据**不知道水印在哪**：试过用 `_watermark_strokes` 的像素判据做"位置掩膜"，
  实测它把 **33%~64% 的画面**都标成"水印"（该判据本是给"全图计数"用的，不是定位用的），
  掩膜版会放行一切 —— 已放弃。所以现在判的是改动**范围**，不是改动**位置**。
* **还没做（真正的根治）**：模型清洗是**整张重绘**，不是"只改水印区"。所以清洗质量每次都不一样。
  ⚠️ 2026-10-28 已补三件：
  ① **确定性路径**（`_inpaint_watermark_boxes` → `tools/inpaint_reference_watermark.py`）：
     探针给到水印框时，用 `cv2.inpaint` **只改水印像素**，其余像素与原图逐点一致 ——
     "只洗水印不洗字"的正解，且不经过模型、没有色调漂移、没有拼接补丁；
  ② **重绘后分辨率还原**（`_match_resolution`）：模型 2K 重绘会把 1080×1920 放大成
     1584×2816 —— 不还原的话所有"按像素框"的判断都会指到错误的画面区域（这是 10-28 的真凶）；
  ③ **保护区守卫**（`_protect_guard`）：只在走到模型那条回退路时才起作用。
  ❌ **踩过并已删除的弯路**：拿模型整张重绘的结果再"把保护区按原图像素贴回" ——
  模型重绘后色调本来就变了，贴回去必然留下一块可见补丁（用户原话：「拼接的啥啊」）。
  ⚠️ 注意区分：用户 2026-10-23 否决的是"局部重绘**拼接**"；而 `cv2.inpaint` 的确定性修补
  改动范围严格等于水印掩膜、不引入任何外部像素，所以它是可行且首选的。

── 2026-11-02：「清洗功能失效了，水印没法去掉了」= **成功的模型清洗被护栏拒 + 空操作的
   确定性修补被记账成「已去水印」**（两个洞叠在一起，用户看到的是"洗了个寂寞"） ──

用户报：「为什么金蛋视频的清洗功能失效了，水印没法去掉了」。生产 run `39aaa705` / `66522685`
（文档 `G1JvwUaqEiayuakKT6Ic1Zk9ngg`，两张参考图的水印都是右下角的「豆包AI生成」）两条都
`status=adopted`、文案写着"已去水印"，而水印**原封不动**。逐层量下来的证据：

| 版本 | 整图均值 | 最差块 | 受影响块 | 水印框笔画能量留存 | 结论 |
|---|---|---|---|---|---|
| 模型重绘（被拒） | 2.14 ≤ 12 ✔ | 46~53 > 35 ✘ | **4** ≥ 4 ✘ | **24% / 23%** | 水印**洗掉了**、保护区留存 97~106%——好结果，被拒 |
| 确定性修补（采用） | 0.00 | 0~1 | 0 ✔ | **100% / 100%** | 只改了 44 / 93 个像素，水印原样 |

* **洞 ①：局部块护栏不知道"水印在哪"**。探针早就把水印框（`0.78,0.955,0.995,0.995`）给了
  出来，护栏却照样把框里"水印被洗掉"造成的块差算成"改写了画面内容"。那次 4 个超阈块
  **全部**压在（32×32 网格的）row 31 / col 26,27,29,30 上 —— 落在框里。10-23 的校准口径
  "水印被洗掉 = 0~3 块"只对**小**水印成立；「豆包AI生成」这么一块就是 4 块，正好卡在
  `_LOCAL_DIFF_MIN_BLOCKS = 4` 的刀口上。**修法**：`_local_change_stats(exclude=水印框)`
  —— 框里的块不参与判定，框外照旧（红车红圈 56 块全在框外，照样拦得住）；框太大
  （>15% 的块）就不豁免，宁可保守。
* **洞 ②：确定性修补没有自证**。`cv2.inpaint` 只改了 44 个像素、水印原样，但工具退出码是 0，
  `clean_reference_image` 就 `adopted=True` + 文案"已去水印"，`effective_cleanup_path` 还会
  **优先**把这个空操作文件提交出去 —— 于是"界面说洗好了、成片里水印还在"。
  **修法**：`_watermark_still_there` —— 修补后水印框内的**笔画能量**必须降到原来的 85% 以下，
  否则判"没洗掉"、**保留原图**并在文案里写明（用户可点「重新清洗」）。
  校准：真洗掉 = 留存 23~24%；空操作 = 100%，两侧离阈值都很远。
* **洞 ③：跨运行记忆把这个空操作文件钉死了**。用户点过"用清洗版"之后，
  `reference-cleanup-decisions.json` 记着 `selected = ...-nowm.png`（那份空操作文件），
  于是**之后每一次新运行都走"沿用上一版"分支、压根不再洗** —— 用户点多少次重新生成，
  拿到的都是同一张带水印的图。**修法**：继承校验除了保护区守卫，也要过
  `_watermark_still_there`；过不了就**忘掉这条记忆**、本次重新清洗。
* **方法论**：
  1. **"成功"必须有独立的度量来证** —— 退出码 0 / 工具没报错都不算。这套流水线已经栽过
     同族两次（2026-10-20 徽章"处理中"、2026-10-28 探针静默返回 None）：凡"某一步说自己成了"
     的地方，都要有一个**测出来的**判据。
  2. **护栏阈值必须拿"它要保护的那件事"校准** —— `4` 这个数字当初是拿 0~3 块的样本定的，
     这次碰到 4 块就翻车。
  3. 判据能用**已知的定位信息**（探针框）就别只用"范围启发式"：位置信息早就在手上，
     上一轮因为 2026-10-23 那次"像素掩膜标了 33%~64% 画面"的失败把它一并放弃了。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace as _dc_replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageFilter, ImageStat

_LOGGER = logging.getLogger(__name__)

#: 默认并发张数（见 `Settings.reference_watermark_cleanup_concurrency` 的说明）。
#: 实测每张 73~90 秒，串行 10 张就是 12~15 分钟；这里没有"必须串行"的理由，
#: 唯一的共享状态是台账 JSON，已经用锁 + 原子替换护住（`_write_status`）。
_DEFAULT_CONCURRENCY = 3
#: 台账读-改-写的互斥锁：并发清洗时多张图同时更新同一个 JSON，不加锁会**丢更新**
#: （前端就会少几张图的状态）。与 2026-10-12 `history.json` 那次事故同一个雷。
_STATUS_LOCK = asyncio.Lock()

#: 本机图片应用（nano-banana）：清洗用的最小编辑走它，不额外引依赖。
_IMAGE_APP = "http://127.0.0.1:8797"
#: 相似度阈值（缩到 128×128 灰度后的平均绝对差；越小越像）。
#:
#: 实测校准：图片模型做"最小编辑去字"**必然轻微重绘纹理** —— 拿生产参考图（r1）实测
#: 差异 = 6.11；把阈值卡在 6 会把**合理清洗**也拒掉 ✗。这里放宽到 12，同时配合
#: "原图永久保留 + 审批页放前后对比"，把"跟原图不一样"这件事**变成可见、可回退**——
#: 这正是 2026-10-10 那次撤掉功能的真正教训（旧实现是**静默替换** ✗）。
_FIDELITY_MAX_DIFF = 12.0
#: 交给图片模型的指令：**最小编辑、只去平台水印**，图内文字一律保留。
#:
#: 2026-10-28 用户口径（原话）：「清洗时规定，**只清洗平台水印，图中的文字要保留**」。
#: 旧指令写的是"把这张图里**所有叠加在画面上的文字**去掉" —— 范围太大，模型会把
#: **画面内容里的文字**（品牌字样、道具与界面文字）一起擦掉。实测代价：image-2 的
#: 清洗版把爱心中心的「Jewel Loop」洗糊了（台账里最差局部差异 63 > 35 → 被护栏拒绝），
#: 用户看到的是"清洗把我要的字洗没了，只能退回原图"。
#: 所以现在的界线是**两类文字**：平台叠加的水印/角标 = 洗掉；画面自带的文字 = 一个字都不许动。
_CLEAN_PROMPT = (
    "**最小编辑，只做一件事**：把**叠加在画面上的第三方平台水印与 AI 工具角标**去掉"
    "（例如右下角的「即梦AI」「豆包AI生成」、账号名、平台 logo 一类），"
    "被水印挡住的地方按周围纹理自然补上。"
    "**画面里其它任何文字都必须原样保留、一字不改**：品牌字样（如 Jewel Loop）、标题、"
    "包装与道具上的字、游戏/界面文字（计数、按钮、提示）、手写标注与箭头 —— 这些是"
    "**画面内容**，**不许擦除、不许改写、不许模糊、不许移位**；"
    "不确定某处文字是不是平台水印时，**一律保留**。"
    "**除水印区域外，画面的一切保持完全不变**：构图、主体、颜色、光影、清晰度与所有文字；"
    "不要新增元素、不要重画主体、不要改变风格。"
)
#: 半透明叠加字的"对比度"区间：下限排除噪点、上限**排除纯白高对比的界面元素**
#: （游戏 UI 的 "0/6"、箭头、图标都是纯白高对比 ✗，不能当成水印去洗）。
_STROKE_MIN_DIFF = 24
_STROKE_MAX_DIFF = 90

# ── 局部块护栏（2026-10-22，见模块 docstring 的「红圈被洗掉」事故） ───────────────
#: 差图切成 `_LOCAL_BLOCK × _LOCAL_BLOCK` 块，这里判的是**最差那一块**的灰度平均差。
#: 块数取 32：一个块 ≈ 画幅的 3%，小到能框住"被抹掉的一条圈/一个标记"，
#: 大到不会被单像素级重绘噪声带偏。
_LOCAL_BLOCK = 32
#: 局部块差异上限。实测校准（生产真实图对，`_diag/replay_annotation_guard.py` 的原始输出）：
#:   * 只洗掉角落水印文字的正常清洗 → 最差块 **15.0 / 6.0**（应当采用）；
#:   * 把手绘红圈整条抹掉           → 最差块 **66.0**（必须拒绝）。
#: 取 35 落在两者中间，两侧都有 2 倍以上余量。
_LOCAL_DIFF_MAX = 35.0
#: **还必须至少这么多块被改动**才算"抹掉了内容"（2026-10-23 事故后补的合取条件）。
#:
#: 事故：只看**最差单块**时，**水印本身的变化**就会被当成"抹掉了内容" —— 一次"只在右下角
#: 水印处改动、其余像素几乎不变"的**完美清洗**，max 同样顶到 39~42 > 35 → 被拒 →
#: 回退成带水印的原图。用户看到的就是「去水印失效」（手机卖点两次任务都有水印）。
#:
#: 为什么块数能把两者分开（全部 108 对生产图对实测）：
#:   * 水印被洗掉（改动**局限在水印那一小块**）→ 受影响块 **0~3**；
#:   * 画面内容被改写（多条横带 / 一整圈）    → 受影响块 **52 / 56**；
#:   * 只洗掉角落水印的合成样本                → 0 块。
#: 取 4：合成"细红圈被抹"样本是 7 块（仍被拦 ✔），带水印的干净清洗是 0~2 块（放行 ✔）。
#:
#: ⚠️ 这条判据**不知道水印在哪**（检测器只能给"像水印的像素占比"，取位置会把 33%~64% 的
#: 画面误标成水印，实测不可用），所以它是"改动**范围**"的启发式，不是"改动**位置**"的判据。
#: 水印特别大的图仍可能被拒 —— 那种情况下正确的修法是让清洗更忠实（只改水印区），见模块
#: docstring 末尾的「还没做」。
_LOCAL_DIFF_MIN_BLOCKS = 4

# ── 局部块护栏的「水印框豁免」（2026-11-02，见模块 docstring 的「金蛋」事故） ────────
#
# 事故：探针**已经明确告诉**我们水印在右下角那个框里，而局部块护栏却照样把这个框里
# "水印被洗掉"造成的块差算成"改写了画面内容" —— 一次成功的清洗被拒，退回确定性修补，
# 而那次修补是个空操作（只改了 44 个像素），水印原封不动地交了出去。
#
# 所以：局部块护栏只看**水印框以外**的改动。红圈/手绘标记这类"用户指认标记"都在画面里，
# 不在角落水印框里，照样拦得住（实测红车红圈 = 56 块，全在框外）。
_LOCAL_EXCLUDE_PAD = 0.01
#: 允许被豁免的块数上限（占 1024 块的比例）：探针给的框必须**小**才敢豁免。
#: 框一大，这条护栏就等于不存在 —— 宁可退回旧行为（保守）。实测生产水印框 ≈ 16 块。
_LOCAL_EXCLUDE_MAX_RATIO = 0.15

# ── 确定性修补的「自证」判据（2026-11-02） ────────────────────────────────────
#: 修补后水印框内的**笔画能量**必须降到原来的这个比例以下，才算"水印真的被洗掉了"。
#: 校准（2026-11-02 生产真实图对，同一张图的两种洗法）：
#:   * 模型重绘把「豆包AI生成」洗掉（成功）→ 留存 **24% / 23%**；
#:   * 确定性修补实际只改了 44 / 93 个像素、水印原样 → 留存 **100% / 100%**（假成功）。
#: 取 0.85：正常清洗离得很远，假成功卡得很死。
_WATERMARK_GONE_MAX_RATIO = 0.85
#: 水印框内笔画能量的最低基数：低于它说明框里本来就没有笔画，算比值没意义（放行）。
_WATERMARK_ENERGY_FLOOR = 2.0
#: 水印框外扩（与 `tools/inpaint_reference_watermark.py` 的 `--pad` 保持一致）。
_WATERMARK_BOX_PAD = 0.02

# ── 用户手绘标记判据（2026-10-22） ────────────────────────────────────────────
#: 「手绘标记」= 标记名词 + 标注动词。两个方向都收（「红色圆圈标注」/「标注框」），
#: 单独出现的「圆圈」「箭头」「标注」一律不算 —— 游戏画面里"车上的方向箭头""围成圆圈
#: 站立""情绪标注""木质门框""手绘质感"都是同词不同义，收进来就会把正常清洗全禁掉。
_ANNOTATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:手绘|手画|手工|后期叠加|叠加的)?\s*(?:红|黑|蓝|黄|绿|白|紫|橙)?\s*"
        r"(?:圆圈|圆环|椭圆|圈|矩形框|方框|线框|标注框)\s*"
        r"(?:标注|标记|标出|画出|圈出|圈住|圈起|圈选|框选|高亮|选中|指示)"
    ),
    re.compile(
        r"(?:标注|标记|标出|画出|圈出|圈住|圈起|圈选|框选|高亮|选中)\s*(?:的)?\s*"
        r"(?:红|黑|蓝|黄|绿|白|紫|橙)?\s*(?:圆圈|圆环|矩形框|方框|线框|红圈)"
    ),
    re.compile(
        r"(?:红色|红|黑色|蓝色|黄色|白色|绿色)?\s*(?:矩形|方形|圆形|椭圆|空心)?\s*"
        r"(?:标注|标记)(?:框|圈|圆圈|环|矩形框)"
    ),
    re.compile(r"红圈"),
)
#: 命中手绘标记时给用户看的原因（写进任务事件）。
ANNOTATION_SKIP_REASON = (
    "检测到你手绘的标注（红圈/红框）——为保住这个指认标记，本次不做去水印、原图照原样提交"
)


def _description_text(description: object) -> str:
    """把视觉描述（pydantic 模型 / dict / 字符串）摊平成一段可搜索的文本。"""
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    dumper = getattr(description, "model_dump_json", None)
    if callable(dumper):
        try:
            return str(dumper())
        except Exception:  # noqa: BLE001
            pass
    try:
        return json.dumps(description, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(description)


def mentions_user_annotation(description: object) -> bool:
    """这张图的**视觉描述**里有没有提到用户手绘的标记（红圈、标注框…）。

    判据是"高精度、宁漏不误伤"：误判的代价是**不做去水印**（水印可能被成片复现），
    漏判则由 `_worst_block_diff` 的局部块护栏兜底。校准数据见模块 docstring。
    """
    text = _description_text(description)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _ANNOTATION_PATTERNS)


@dataclass(frozen=True, slots=True)
class CleanupRecord:
    """一张参考图的清洗台账（写进任务事件，用户可见）。"""

    asset_id: str
    original: Path
    cleaned: Path | None
    fidelity: float | None
    adopted: bool
    reason: str
    #: 2026-10-28：这一版把**画面里的文字/内容洗掉了**（保护区守卫判定）。
    #: 这类版本**不允许生效、也不允许被用户选定** —— 用户口径：「不要洗字我只要洗水印」。
    content_lost: bool = False
    #: 2026-10-28：这一版是**确定性修补**（只改水印像素、其余原样，见
    #: `tools/inpaint_reference_watermark.py`）。确定性版本视为"保字安全"，
    #: 在生效判定里**优先于"回退原图"** —— 否则入库那一环偶尔失败一次，
    #: 用户就会看到「我明明洗好了，重新运行又变回带水印的原图」。
    deterministic: bool = False


# ───────────────────────── 后台清洗 + 前端可见的状态台账 ─────────────────────────
#
# 用户口径（2026-10-20）：「规划成功后先把右边界面显示出来，然后提示正在去水印，
# 对应参考图给出前端反馈」。所以清洗**不能挡住规划**：
#   * 规划一完成，界面立刻出来；
#   * 清洗在**后台任务**里逐张跑，每张的状态写进 `outputs/runs/<run_id>/watermark-cleanup.json`；
#   * 运行视图把这份台账带给前端 → 每张参考图旁边显示「去水印中… / 已去水印 / 未检测到水印 /
#     保留原图（差异过大）」；
#   * 参考图缩略图接口在读图时**优先给清洗版**（清洗完成后缩略图自动变成干净的那张）；
#   * 提交前执行层仍会兜底清洗一次（幂等：已洗过直接复用 `-nowm` 文件）。


def cleanup_status_path(run_id: str, outputs_dir: Path) -> Path:
    return outputs_dir / "runs" / run_id / "watermark-cleanup.json"


def read_cleanup_status(run_id: str, outputs_dir: Path) -> dict[str, dict]:
    """读这份状态台账；读不到就返回空 dict（前端据此不显示状态）。"""
    path = cleanup_status_path(run_id, outputs_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── 用户决定（2026-10-23）────────────────────────────────────────────────────
#: 用户对某张参考图清洗结果的决定：
#:   * `auto`（缺省）—— 沿用护栏的自动判定（`status == "adopted"` 才用清洗版）；
#:   * `cleaned` —— **用户看过图，决定用清洗版**（即使护栏判了 `kept` 也照用）；
#:   * `original` —— 用户决定用原图（即使护栏采用了清洗版也不用）。
#:
#: 用户 2026-10-23 口径：「可以让用户看到清洗后的图片自由选择原图或者继续重新清洗」，
#: 并明确**不做局部重绘**（拼接时会有明显接缝），所以清洗始终是整张重绘、由人来拍板。
DECISION_AUTO = "auto"
DECISION_CLEANED = "cleaned"
DECISION_ORIGINAL = "original"
DECISIONS: tuple[str, ...] = (DECISION_AUTO, DECISION_CLEANED, DECISION_ORIGINAL)


def resolve_cleaned_path(raw: object) -> Path | None:
    """把台账里的清洗版路径解析成**绝对路径**。

    台账里存的是相对路径（`asset.local_path` 本身就是相对的，清洗版就写在它旁边），
    而调用方要拿它跟 `data_dir`/`outputs_dir` 的**绝对**允许根做 `is_relative_to` 校验 ——
    相对路径永远判否 → 预览 404。用户 2026-10-20 的现象就是这条：「我飞书模块去水印但是
    预览没了」——凡是已采用清洗版的参考图预览全空白（1 张没检测到水印的照旧正常，所以
    看着像"部分丢了"）。
    """
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def cleaned_suffix(version: int = 1) -> str:
    """清洗版文件名后缀：第 1 版沿用既有的 `-nowm`，重洗版本是 `-nowm-2` / `-nowm-3`…

    **保留历史版本**（2026-10-23 用户口径）：重绘有随机性，重洗很可能拿到更好的一版，
    所以要能退回上一版、来回对比 —— 覆盖掉就没法比了。
    """
    return "-nowm" if int(version) <= 1 else f"-nowm-{int(version)}"


def version_lost_content(entry: dict[str, Any] | None, path: object) -> bool:
    """台账里这一版是否被守卫判定"洗掉了画面内容"。

    2026-10-28：用户口径「不要洗字我只要洗水印」——**把用户的字洗掉的版本不是合法选项**，
    哪怕用户在界面上点选了它（老规矩"用户看过图有最终决定权"在这里会让坏结果照样生效，
    实测就是这么穿上来的：台账写着"已保留原图"，但 decision=cleaned 指着那一版）。
    """
    if not entry or not path:
        return False
    wanted = str(path)
    for item in _versions(entry):
        if str(item.get("path") or "") == wanted and bool(item.get("content_lost")):
            return True
    return False


def effective_cleanup_path(entry: dict[str, Any] | None) -> Path | None:
    """这张参考图**当前该用哪个清洗版**；返回 None = 用原图。

    这是"哪个版本生效"的唯一判据 —— 预览（`runtime.get_reference_file`）与**提交**
    （`nodes._clean_media_assets`）都必须走它。2026-10-23 之前提交路径压根不看台账、
    自己重新洗一遍，于是「审批页看到的那版 ≠ 真正提交的那版」，用户的选择也无从生效。

    2026-10-28：`decision == "cleaned"` 但选定的那一版**把画面内容洗掉了**时，
    **一律回退原图**（不让坏版本生效 —— 用户要的是"只去水印不动字"）。
    """
    if not entry:
        return None
    decision = str(entry.get("decision") or DECISION_AUTO)
    if decision == DECISION_ORIGINAL:
        return None
    if decision == DECISION_CLEANED:
        # 有多个历史版本时优先用户点选的那一版，否则用最新一版。
        chosen = entry.get("selected") or entry.get("cleaned")
        if version_lost_content(entry, chosen):
            return None
        return resolve_cleaned_path(chosen)
    if str(entry.get("status") or "") != "adopted":
        # 护栏判 kept（例如某次模型重绘把画面改花了）→ 但**磁盘上可能已经有一版
        # 确定性修补结果**（只改水印像素、字原样）：那一版永远比"回退带水印原图"好。
        # 2026-10-28 用户报「我明明用的是清洗版，重新运行后图片又变回未清洗的带水印版」——
        # 入库环节偶尔没跑成时，就是靠这条兜住的。
        best = latest_usable_version_path(entry)
        return resolve_cleaned_path(best) if best else None
    return resolve_cleaned_path(entry.get("cleaned"))


def latest_usable_version_path(entry: dict[str, Any] | None) -> str | None:
    """台账里**最近一版可以放心用**的清洗文件（确定性修补优先、跳过洗坏内容的）。

    只认 `deterministic=True` 且没被判 `content_lost`、且文件还在盘上的版本。
    `deterministic` 这个字段是 2026-10-28 才加的，所以**同时认理由文字**——
    几分钟前刚写下的那些老记录里已经有"确定性修补"这几个字，不能因为缺字段就把它们漏掉。
    """
    if not entry:
        return None

    def _is_deterministic(item: dict[str, Any]) -> bool:
        return bool(item.get("deterministic")) or "确定性修补" in str(
            item.get("reason") or ""
        )

    candidates = [
        item
        for item in _versions(entry)
        if _is_deterministic(item)
        and not bool(item.get("content_lost"))
        and resolve_cleaned_path(item.get("path")) is not None
        and Path(
            str(resolve_cleaned_path(item.get("path")))
        ).is_file()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: int(item.get("version") or 0), reverse=True)
    return str(candidates[0].get("path"))


# ── 清洗范围探针的数据结构与保护区守卫（2026-10-28）──────────────────────────
#
# 用户口径：「不要洗字我只要洗水印」+「还是用原本的清洗，但在清洗之前先丢给模型确认清洗范围」。
# 探针（见 `integrations/cleanup_scope.py`）给出两组归一化 bbox：该洗的水印、不许动的画面内容。
# 这里负责两件事：① 把范围写进清洗指令；② 洗完之后在 keep 区**验一遍**，掉了就拒。
#
# 为什么必须要 ②：实测 run `82133315`（image-2）——清洗把水印去掉了、**也把品牌字
# "Jewel Loop" 洗掉了**，而灰度护栏判它"已去水印并采用"（最差块 31 ≤ 35、受影响 0 块）。
# 低对比度的浮雕字消失，整图均值与局部灰度块都看不出来；只有"盯着那块区域看边缘能量"才看得见。
@dataclass(frozen=True, slots=True)
class CleanupScope:
    """一次探针的结论：水印在哪、画面内容在哪。坐标是归一化的 (x0, y0, x1, y1)。"""

    watermarks: tuple[tuple[float, float, float, float], ...] = ()
    keep: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()

    def is_empty(self) -> bool:
        return not self.watermarks and not self.keep

    def keep_labels(self) -> list[str]:
        return [label for label, _box in self.keep]


#: 保护区外扩：视觉模型给的框有误差，宁可多保护一点（值 = 画面宽/高的比例）。
_PROTECT_PAD_RATIO = 0.04
#: 保护区"边缘能量"允许的最低留存比例：清洗后 / 清洗前 < 这个值 → 判定画面内容被洗掉。
#:
#: 校准数据（2026-10-28）：
#:   * **字被洗掉的真实图对**（image-2 原图 → 13:18 清洗版 / 10:58 重洗版）→ 留存 **55%**；
#:   * **正常清洗**（image-1 原图 → 清洗版，已采用）→ 留存 **73%**；
#:   * 全部 **157 对**历史清洗图对的保护区留存分布：最小 32% / 5% 分位 64% / 中位 99%。
#:   * **对照组**：整图边缘能量留存 78%（坏）vs 76%（好）——**完全分不开**，
#:     这就是为什么必须盯"探针指定那块区域"，而不是看整图。
#: 取 0.70 落在 55% 与 73% 之间，两侧余量都够；误拒的代价是"保留原图（带水印）+ 用户手动选"，
#: 比"把用户的品牌字洗掉"轻得多 —— 用户 2026-10-28：「我给用户选择的空间，可以用原图」。
_PROTECT_EDGE_MIN_RATIO = 0.70


def bbox_tuple(value: object) -> tuple[float, float, float, float] | None:
    """把探针给的 bbox 规整成合法的归一化四元组；不合法返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        nums = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = nums
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0, y0 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, y0))
    x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return (x0, y0, x1, y1)


def scope_cache_key(asset: Any, path: Path) -> str | None:
    """探针结果的缓存键：优先用素材自带的 sha256，取不到就算文件内容 sha。"""
    sha = str(getattr(asset, "sha256", "") or "").strip()
    if sha:
        return sha
    return source_image_sha(path)


def file_sha256(path: Path) -> str | None:
    """文件的 sha256（素材指纹：提交前拿它核对"是不是同一张图"）。"""
    return source_image_sha(path)


def recorded_version_sha(entry: dict[str, Any] | None, path: object) -> str | None:
    """台账里这一版记录的 sha256（老记录没有该字段 → None，表示"无从核对"）。"""
    if not entry or not path:
        return None
    wanted = str(path)
    for item in _versions(entry):
        if str(item.get("path") or "") == wanted:
            sha = str(item.get("sha256") or "").strip()
            return sha or None
    return None


def _asset_stub(asset_id: str, path: Path) -> Any:
    """给探针用的最小素材替身（重洗路径手上只有路径，没有 MediaAsset）。

    ⚠️ 字段必须**齐全**：`build_planning_media_parts` 会读 `download_error`。
    2026-10-28 事故：这里漏了 `download_error` → 重洗时的探针每次都抛
    `AttributeError` → 探针返回 None → **保护区守卫形同不存在** → 重洗照样把
    用户的品牌字洗掉（用户报"重新清洗了中间字还是没了"）。
    """
    return SimpleNamespace(
        asset_id=asset_id,
        mime_type="image/png",
        local_path=path,
        sha256="",
        download_error=None,
    )


def _protected_boxes(
    scope: CleanupScope | None, width: int, height: int
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """keep 区外扩后换算成像素框（左上右下）。"""
    if scope is None or not scope.keep:
        return []
    pad_x, pad_y = int(width * _PROTECT_PAD_RATIO), int(height * _PROTECT_PAD_RATIO)
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for label, (x0, y0, x1, y1) in scope.keep:
        left = max(0, int(x0 * width) - pad_x)
        top = max(0, int(y0 * height) - pad_y)
        right = min(width, int(x1 * width) + pad_x)
        bottom = min(height, int(y1 * height) + pad_y)
        if right - left >= 8 and bottom - top >= 8:
            boxes.append((label, (left, top, right, bottom)))
    return boxes


def _edge_energy(path: Path, box: tuple[int, int, int, int]) -> float | None:
    """框内的**边缘能量**（灰度 + FIND_EDGES 后的均值）—— 文字/笔画没了这里会明显掉。"""
    try:
        with Image.open(path) as opened:
            gray = opened.convert("L").filter(ImageFilter.FIND_EDGES)
            region = gray.crop(box)
            stat = ImageStat.Stat(region)
            return float(stat.mean[0])
    except Exception:  # noqa: BLE001
        return None


def _watermark_boxes_px(
    scope: CleanupScope | None,
) -> list[tuple[float, float, float, float]]:
    """探针给的水印框 → 归一化且**外扩过**的四元组（与修补工具的口径一致）。"""
    if scope is None or not scope.watermarks:
        return []
    boxes: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in scope.watermarks:
        box = (
            max(0.0, x0 - _WATERMARK_BOX_PAD),
            max(0.0, y0 - _WATERMARK_BOX_PAD),
            min(1.0, x1 + _WATERMARK_BOX_PAD),
            min(1.0, y1 + _WATERMARK_BOX_PAD),
        )
        if box[2] - box[0] < 0.01 or box[3] - box[1] < 0.01:
            continue
        boxes.append(box)
    return boxes


def _watermark_still_there(
    before: Path, after: Path, scope: CleanupScope | None
) -> str | None:
    """确定性修补之后，水印框里的**笔画**还在不在；还在就返回给用户看的原因。

    为什么必须自己验一遍（2026-11-02 生产 run `39aaa705`「金蛋」事故）：
    `cv2.inpaint` 那次只改了 **44 个像素**、水印「豆包AI生成」原封不动（笔画能量留存
    **100%**），但工具进程退出码是 0，上层就记账成「已去水印并采用」。用户在审批页看到的
    是「说洗好了、水印还在」= 「清洗功能失效了，水印没法去掉了」。

    **只信"还在"这一侧**：能量明显下降（或框里本来就没笔画）一律放行 —— 往严的方向卡会
    把正常修补也判成失败、退回带水印的原图。
    """
    boxes = _watermark_boxes_px(scope)
    if not boxes:
        return None
    try:
        with Image.open(before) as opened:
            width, height = opened.size
    except Exception:  # noqa: BLE001
        return None
    remaining: list[str] = []
    for index, (x0, y0, x1, y1) in enumerate(boxes, start=1):
        box = (
            max(0, int(x0 * width)),
            max(0, int(y0 * height)),
            min(width, int(x1 * width)),
            min(height, int(y1 * height)),
        )
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        old = _edge_energy(before, box)
        new = _edge_energy(after, box)
        if old is None or new is None or old <= _WATERMARK_ENERGY_FLOOR:
            continue
        ratio = new / old
        if ratio > _WATERMARK_GONE_MAX_RATIO:
            label = f"第{index}个水印框" if len(boxes) > 1 else "水印框"
            remaining.append(f"{label}的笔画能量只降到 {ratio:.0%}")
    if not remaining:
        return None
    return (
        "确定性修补没能把水印洗掉（" + "、".join(remaining[:3]) + "），本次保留原图"
    )


def _feather_mask(size: tuple[int, int], feather: int) -> Image.Image:
    """做一个四周羽化的贴回蒙版（避免硬边拼缝）。"""
    width, height = size
    f = max(1, min(feather, width // 3, height // 3))
    horizontal = Image.new("L", (width, 1), 0)
    for x in range(width):
        edge = min(x, width - 1 - x)
        horizontal.putpixel((x, 0), 255 if edge >= f else int(255 * edge / f))
    horizontal = horizontal.resize((width, height))
    vertical = Image.new("L", (1, height), 0)
    for y in range(height):
        edge = min(y, height - 1 - y)
        vertical.putpixel((0, y), 255 if edge >= f else int(255 * edge / f))
    vertical = vertical.resize((width, height))
    return ImageChops.multiply(horizontal, vertical)


def _deterministic_python() -> str | None:
    """找一个能 `import cv2, numpy` 的解释器（复用绿幕归一那套探测，结果有缓存）。"""
    try:
        from feishu_generation_agent.integrations.green_screen import resolve_python

        return resolve_python("")
    except Exception:  # noqa: BLE001
        return None


def _inpaint_watermark_boxes(original: Path, out: Path, scope: CleanupScope | None) -> bool:
    """**确定性去水印**：只改水印像素，其余像素与原图逐点一致。成功返回 True。

    用户 2026-10-28 口径：「只洗水印不洗字」。走模型是整张重绘（色调/纹理都会变），
    于是任何"把某块贴回去"的合成都会留可见补丁（用户原话：「拼接的啥啊」）。
    水印在角落时，用 `cv2.inpaint` 按邻近像素补掉最干净：**改动范围严格等于水印掩膜**。
    """
    if scope is None or not scope.watermarks:
        return False
    python = _deterministic_python()
    if not python:
        return False
    tool = (
        Path(__file__).resolve().parents[3]
        / "tools"
        / "inpaint_reference_watermark.py"
    )
    if not tool.is_file():
        return False
    command = [python, str(tool), "--image", str(original), "--out", str(out)]
    for box in scope.watermarks:
        command += ["--box", *[f"{value:.4f}" for value in box]]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("确定性去水印调用失败，回退模型清洗", exc_info=True)
        return False
    if done.returncode != 0 or not out.is_file():
        _LOGGER.warning(
            "确定性去水印未成功，回退模型清洗：%s",
            (done.stdout or done.stderr or "")[:200],
        )
        return False
    return True


def _match_resolution(reference: Path, target: Path) -> bool:
    """把 `target` 缩放到与 `reference` **同尺寸**（Lanczos）；已一致则不动。

    2026-10-28 事故（真凶）：模型按 `image_size: 2K` 重绘，会把 1080×1920 **放大成
    1584×2816**。于是"保护区坐标"在两个文件里指的**不是同一块画面**：
      * 守卫拿原图的像素框去裁清洗版 → 量到的是别处，判"边缘细节只剩 41%"（假结论）；
      * "把保护区按原图像素贴回"也贴到了放大画布上的错误坐标 → 字照样没了。
    统一还原到原图尺寸后，坐标才可比、贴回才贴对，提交给模型的参考图分辨率也与原图一致。
    """
    try:
        with Image.open(reference) as ref:
            want = ref.size
        with Image.open(target) as img:
            if img.size == want:
                return False
            resized = img.convert("RGB").resize(want, Image.LANCZOS)
        resized.save(target)
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.warning("清洗版分辨率还原失败（按原样继续）target=%s", target, exc_info=True)
        return False


#: 2026-10-28：清洗把**原本明亮**的像素涂成**近黑**的占比门槛。
#:
#: 用户报「前两张有水印的图，右下角都被替换成了纯黑色」——查下来是**图片模型**那条路：
#: 它被要求"去掉右下角水印"，结果把那一块**涂黑了**（10-28 13:23 的三张，实测涂黑占比
#: 0.135%~0.187%）。确定性修补不会有这个问题，但它只在水印框已知时才走；
#: 走模型回退路时必须由这道判据兜住 ——「去水印」绝不允许变成「涂黑」。
#:
#: 校准（同样用 240 宽采样、`原图>70 且 清洗后<26` 的占比）：
#:   * 三张被涂黑的真实成品 → **0.135% / 0.171% / 0.187%**（必须拒）；
#:   * 两版正常清洗（压口红确定性版 / image-1） → **0.000%**（必须放行）。
#: 取 0.08%：比正常样本高两个数量级以上，比最坏样本低 1.7 倍。
_BLACK_PATCH_MAX_RATIO = 0.0008


def _black_patch_ratio(original: Path, cleaned: Path) -> float | None:
    """清洗把多少"原本明亮"的像素涂成了近黑（按缩放后的小图采样，返回占比）。

    只比较**同尺寸**的两张（模型重绘会被 `_match_resolution` 先还原，所以这里通常是同尺寸）。
    """
    try:
        with Image.open(original) as opened:
            size = opened.size
            small_o = opened.convert("L").resize((240, max(1, int(240 * size[1] / size[0]))))
        with Image.open(cleaned) as opened2:
            if opened2.size != size:
                return None
            small_c = opened2.convert("RGB").resize(small_o.size)
    except Exception:  # noqa: BLE001
        return None
    bright = small_o.load()
    after = small_c.load()
    total = small_o.width * small_o.height
    hits = 0
    for y in range(small_o.height):
        for x in range(small_o.width):
            if bright[x, y] > 70:
                r, g, b = after[x, y]
                if max(r, g, b) < 26:
                    hits += 1
    return hits / max(1, total)


def _protect_guard(
    before: Path,
    after: Path,
    scope: CleanupScope | None,
) -> str | None:
    """清洗后在 keep 区比对边缘能量；画面内容被洗掉就返回拒绝原因。

    **只信"掉了"这一侧**：能量不降（或略升）一律放行 —— 清洗本来就会轻微重绘纹理，
    往严的方向卡会把正常清洗全拒掉（回退成带水印的原图，用户看到的就是"去水印失效"）。
    """
    if scope is None or not scope.keep:
        return None
    try:
        with Image.open(before) as opened:
            width, height = opened.size
    except Exception:  # noqa: BLE001
        return None
    losses: list[str] = []
    for label, box in _protected_boxes(scope, width, height):
        old = _edge_energy(before, box)
        new = _edge_energy(after, box)
        if old is None or new is None or old <= 0.5:
            continue
        ratio = new / old
        if ratio < _PROTECT_EDGE_MIN_RATIO:
            losses.append(f"{label}（边缘细节只剩 {ratio:.0%}）")
    if not losses:
        return None
    return (
        "清洗抹掉了画面里的文字/内容（" + "、".join(losses[:3]) + "），已保留原图"
    )


def _clean_prompt_with_scope(scope: CleanupScope | None) -> str:
    """把探针给的清洗范围并进清洗指令（探针失败时就是原来的静态指令）。"""
    if scope is None or scope.is_empty():
        return _CLEAN_PROMPT
    parts = [_CLEAN_PROMPT]
    if scope.watermarks:
        parts.append(
            "\n【本次只允许清理这些区域（归一化坐标 x0,y0,x1,y1）】"
            + "；".join(
                f"({x0:.2f},{y0:.2f},{x1:.2f},{y1:.2f})"
                for x0, y0, x1, y1 in scope.watermarks
            )
            + "。这些区域里若没有水印，就保持原样。"
        )
    if scope.keep:
        parts.append(
            "\n【这些画面内容必须原样保留、一个像素都不许改】"
            + "；".join(
                f"{label} ({x0:.2f},{y0:.2f},{x1:.2f},{y1:.2f})"
                for label, (x0, y0, x1, y1) in scope.keep
            )
            + "。它们不是水印，是画面内容（品牌字、界面字、手绘标注等），"
            "严禁擦除、改写、模糊或移位。"
        )
    return "".join(parts)


# ── 用户选择的跨运行记忆（2026-10-28）────────────────────────────────────────
#
# 事故：用户在审批页给某张参考图选了「用清洗版」（或「用原图」），**下一版运行**一进来
# 台账就被重置成 `auto`，而 `auto` 又恰好是"护栏拒绝 → 回退原图" —— 用户看到的是
# 「清洗功能重做之后失效了，都给我换成原图了」。
#
# 根因：台账是**按运行**存的（`outputs/runs/<run_id>/watermark-cleanup.json`），而每点一次
# 「重新生成计划 / 重跑」都会**新建一个 run**，于是上一版的选择对新 run 不可见。
#
# 这里加一层按**原始图片内容 sha256** 的持久记忆：同一份素材，用户上次做过的决定，
# 新运行直接继承（连清洗都不再跑一次 —— 每张图一次图片模型调用要 73~90 秒）。
#
# 为什么按 sha256 而不是 `asset_id`：`image-1/image-2/image-3` 是按**文档里图片出现的顺序**
# 编的，用户在文档里插一张图就会整体错位、张冠李戴；sha256 只跟图片内容走。
_DECISIONS_FILENAME = "reference-cleanup-decisions.json"
_DECISIONS_LOCK = asyncio.Lock()


def decisions_store_path(data_dir: Path) -> Path:
    return Path(data_dir) / _DECISIONS_FILENAME


def source_image_sha(path: Path) -> str | None:
    """原始参考图的内容 sha256 —— 跨运行认「同一张图」的唯一键。算不出返回 None。"""
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def read_cleanup_decisions(data_dir: Path) -> dict[str, dict]:
    """读跨运行的决定记忆；读不到/坏了都当空（绝不让它拖垮清洗）。"""
    path = decisions_store_path(data_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        _LOGGER.warning("读参考图清洗决定记忆失败，按无记忆处理", exc_info=True)
        return {}


def lookup_cleanup_decision(data_dir: Path | None, source_sha: str | None) -> dict | None:
    """这张原图（按内容 sha）上次被用户怎么决定的；没有就 None。"""
    if not data_dir or not source_sha:
        return None
    record = read_cleanup_decisions(Path(data_dir)).get(str(source_sha))
    if not isinstance(record, dict):
        return None
    if str(record.get("decision") or "") not in (DECISION_CLEANED, DECISION_ORIGINAL):
        return None
    return record


async def remember_cleanup_decision(
    data_dir: Path | None,
    source_sha: str | None,
    decision: str,
    *,
    selected: str | None = None,
    asset_id: str | None = None,
) -> None:
    """记住用户对这张原图的选择（或 `auto` = 忘掉）。失败只警告，绝不影响主流程。"""
    if not data_dir or not source_sha:
        return
    path = decisions_store_path(Path(data_dir))
    tmp: Path | None = None
    try:
        async with _DECISIONS_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = read_cleanup_decisions(Path(data_dir))
            if decision == DECISION_AUTO:
                data.pop(str(source_sha), None)
            else:
                data[str(source_sha)] = {
                    "decision": decision,
                    "selected": selected if decision == DECISION_CLEANED else None,
                    "asset_id": asset_id,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(data):x}.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
            tmp = None
    except Exception:  # noqa: BLE001
        _LOGGER.warning("写参考图清洗决定记忆失败", exc_info=True)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def _mutate_status(
    run_id: str,
    outputs_dir: Path,
    asset_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """在锁内**读-改-写**单张图的清洗状态；失败绝不影响主流程。

    **并发安全**：多张图会同时写同一个 JSON，所以 ① 整个读-改-写包在 `_STATUS_LOCK` 里
    （不然就是"读旧的 → 各自写回"→ 丢更新，前端少几张图的状态）；② 落盘用
    **唯一 tmp 名 + os.replace**（固定 `.tmp` 在多任务下会互相截断 —— 与 2026-10-12
    `history.json` 被毁那次同源）。
    """
    tmp: Path | None = None
    try:
        async with _STATUS_LOCK:
            path = cleanup_status_path(run_id, outputs_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = read_cleanup_status(run_id, outputs_dir)
            entry = data.get(asset_id) or {}
            mutate(entry)
            data[asset_id] = entry
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(entry):x}.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
            tmp = None
    except Exception:  # noqa: BLE001
        _LOGGER.warning("写水印清洗状态失败 asset=%s", asset_id, exc_info=True)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def _write_status(
    run_id: str, outputs_dir: Path, asset_id: str, **fields: object
) -> None:
    """只更新给定字段（`None` 的跳过），其余字段保持不动。"""

    def _apply(entry: dict[str, Any]) -> None:
        entry.update(
            {key: value for key, value in fields.items() if value is not None}
        )

    await _mutate_status(run_id, outputs_dir, asset_id, _apply)


async def cleanup_assets_in_background(
    assets: list[Any],
    *,
    run_id: str,
    outputs_dir: Path,
    concurrency: int | None = None,
    annotation_reason: Callable[[Any], Awaitable[str | None]] | None = None,
    data_dir: Path | None = None,
    scope_probe: Callable[[Any], Awaitable[CleanupScope | None]] | None = None,
) -> None:
    """后台清洗参考图并实时更新状态台账（**不阻塞规划与界面**）。

    2026-10-20：改成**受控并发**。以前是一个串行 `for` 循环 —— 而每张图要跑一次图片
    模型（实测 73~90 秒），10 张就是 12~15 分钟，用户体感"图片一多就特别慢"。
    每张图之间没有任何依赖，唯一的共享状态是台账 JSON（已加锁 + 原子写）。
    `concurrency=None` 时取 `_DEFAULT_CONCURRENCY`；上游限流时把它调小即可。

    `annotation_reason`（2026-10-22 新增）：调用方给的回调，"这张图上有用户手绘的标记
    （红圈/标注框）"就返回原因字符串 → 这张图**根本不去洗**（少一次图片模型调用，也绝不
    冒险重画用户的指认标记）。返回 None 表示没检到。回调由调用方提供是因为它要读
    **视觉描述缓存**（只有 `nodes` 那层拿得到 repository）。
    """
    limit = max(1, int(concurrency or _DEFAULT_CONCURRENCY))
    semaphore = asyncio.Semaphore(limit)

    async def clean_one(asset: Any) -> None:
        async with semaphore:
            # 「一定有结论」不变量（2026-10-28）：以前这里任何一步抛异常，这张图在台账里
            # **什么都不留** —— 而提交阶段只看台账，于是"没结论"被当成"没洗过"，
            # 素材就会在用户不知情的情况下变成原图（用户原话：「素材变了是绝对不允许的」）。
            # 现在无论成败都留一条结论，且结论里写明这一版是什么。
            try:
                await _clean_one_body(asset)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "参考图清洗异常，按原图并留结论 asset=%s",
                    getattr(asset, "asset_id", ""),
                    exc_info=True,
                )
                await _write_status(
                    run_id, outputs_dir, getattr(asset, "asset_id", ""),
                    status="failed",
                    message=f"清洗异常（{type(exc).__name__}），本次按原图（不会静默换素材）",
                )

    async def _clean_one_body(asset: Any) -> None:
        asset_id = str(getattr(asset, "asset_id", "") or "")
        mime = str(getattr(asset, "mime_type", "") or "")
        path = Path(getattr(asset, "local_path", ""))
        if not asset_id or not mime.startswith("image/") or not path.is_file():
            return
        if "-nowm" in path.stem:
            # 幂等：已经是清洗版就别再花一次图片模型调用。
            # 提交前的兜底清洗（`nodes._clean_media_assets`）一直有这道判断，
            # 后台这条漏了 —— 两条路径的口径必须一致（2026-10-20 补）。
            return
        source_sha = (
            await asyncio.to_thread(source_image_sha, path) if data_dir else None
        )
        # 探针只问一次（同一张图在一次清洗里可能被用来做"继承校验"和"正常清洗"两处）。
        probed: list[CleanupScope | None] = []

        async def _probe_scope(target: Any) -> CleanupScope | None:
            if probed:
                return probed[0]
            value: CleanupScope | None = None
            if scope_probe is not None:
                try:
                    value = await scope_probe(target)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("清洗范围探针失败，按老逻辑清洗", exc_info=True)
                    value = None
            probed.append(value)
            return value
        remembered = lookup_cleanup_decision(data_dir, source_sha)
        if (
            remembered is not None
            and str(remembered.get("decision")) == DECISION_ORIGINAL
        ):
            # 用户上一版明确选了"用原图" → 连清洗都不跑（每张 73~90 秒的图片模型调用）。
            await _write_status(
                run_id, outputs_dir, asset_id,
                status="kept",
                message="沿用你在上一版的选择：用原图（本次不再清洗）",
            )
            await _write_status(
                run_id, outputs_dir, asset_id, decision=DECISION_ORIGINAL
            )
            return
        scope_cache_note: list[str] = []
        if (
            remembered is not None
            and str(remembered.get("decision")) == DECISION_CLEANED
        ):
            inherited = resolve_cleaned_path(remembered.get("selected"))
            if inherited is not None and inherited.is_file():
                # 用户上一版选定的那一版**原样**还在盘上 → 直接沿用，不再重绘
                # （重绘有随机性，重洗一次反而可能更差，还可能覆盖掉他选的那一版）。
                #
                # ⚠️ 2026-10-28 补：**继承来的这一版也必须过保护区守卫**。
                # 事故：用户 13:22 选定的"清洗版"正是 13:18 把品牌字洗掉的那个产物，
                # 继承逻辑原样搬过来 → 之后每次运行都跳过清洗、那个坏文件被永久复用，
                # 用户看到的是「我重新清洗了、中间还是被洗掉了」（其实压根没洗）。
                # 守卫说它坏了就**忘掉这条记忆**，往下走正常清洗（带着探针的范围）。
                #
                # ⚠️ 2026-11-02 再补：**还要过"水印真的没了"这一关**。同一天的另一半：
                # 被选定的那一版其实是**空操作的确定性修补**（只改 44 个像素、水印原样），
                # 记忆把这张带水印的图**永久钉死**在每次新运行里 —— 用户点多少次重新生成，
                # 拿到的都是同一张带水印的图，也就是他说的「水印没法去掉了」。
                inherit_scope = await _probe_scope(asset)
                if inherit_scope is not None:
                    bad = await asyncio.to_thread(
                        _protect_guard, path, inherited, inherit_scope
                    )
                    if bad is None:
                        bad = await asyncio.to_thread(
                            _watermark_still_there, path, inherited, inherit_scope
                        )
                    if bad is not None:
                        await remember_cleanup_decision(
                            data_dir,
                            source_sha,
                            DECISION_AUTO,
                            asset_id=asset_id,
                        )
                        # 这句要**留到最终台账里**（下面拼进 record.reason）——
                        # 否则用户只看到"重新洗了一版"，不知道为什么又洗了。
                        scope_cache_note.append(
                            f"上一版选定的清洗版已失效（{bad}），本次重新清洗"
                        )
                        await _write_status(
                            run_id, outputs_dir, asset_id,
                            status="running",
                            message=scope_cache_note[-1],
                        )
                    else:
                        await record_cleanup_attempt(
                            run_id,
                            outputs_dir,
                            asset_id,
                            CleanupRecord(
                                asset_id,
                                path,
                                inherited,
                                None,
                                True,
                                "沿用你在上一版选定的清洗版（未重新清洗）",
                            ),
                            version=1,
                        )
                        await _write_status(
                            run_id, outputs_dir, asset_id,
                            decision=DECISION_CLEANED, selected=str(inherited),
                        )
                        return
                else:
                    await record_cleanup_attempt(
                        run_id,
                        outputs_dir,
                        asset_id,
                        CleanupRecord(
                            asset_id,
                            path,
                            inherited,
                            None,
                            True,
                            "沿用你在上一版选定的清洗版（未重新清洗）",
                        ),
                        version=1,
                    )
                    await _write_status(
                        run_id, outputs_dir, asset_id,
                        decision=DECISION_CLEANED, selected=str(inherited),
                    )
                    return
        if annotation_reason is not None:
            try:
                reason = await annotation_reason(asset)
            except Exception:  # noqa: BLE001
                reason = None
            if reason:
                # 用户手绘标记优先于水印（2026-10-22）：不去洗，但要让界面看得到。
                await _write_status(
                    run_id, outputs_dir, getattr(asset, "asset_id", ""), status="skipped", message=reason
                )
                return
        await _write_status(
            run_id, outputs_dir, asset_id, status="running", message="正在去水印…"
        )
        # 水印检测是纯 Python 逐像素循环：丢到线程里，别在并发时占住事件循环。
        if not await asyncio.to_thread(has_overlay_watermark, path):
            await _write_status(
                run_id, outputs_dir, asset_id,
                status="skipped", message="未检测到水印",
            )
            return
        # 探针（2026-10-28）：清洗之前先问一次模型"洗哪儿、哪儿不许动"。
        # 只在**确实要洗**时才调（每张一次多模态调用，按 sha 缓存）。失败 = None，
        # 按老逻辑照常洗（用户口径：「失败也可以洗，我给用户选择的空间」）。
        scope = await _probe_scope(asset)
        # 只在**真的拿到范围**时才传 scope：没有探针的调用方（含测试替身）
        # 保持原来的签名，存量调用零改动。
        record = (
            await clean_reference_image(path, asset_id=asset_id)
            if scope is None
            else await clean_reference_image(
                path, asset_id=asset_id, scope=scope
            )
        )
        if scope is not None and not scope.is_empty():
            # 让审批页看得见"这次是带着范围洗的"（用户要能看到系统在保护什么）。
            record = _dc_replace(
                record,
                reason=record.reason
                + "；清洗范围由模型确认（保留："
                + "、".join(scope.keep_labels()[:3] or ["水印区以外的全部画面"])
                + "）",
            )
        if scope_cache_note:
            record = _dc_replace(
                record, reason="；".join([*scope_cache_note, record.reason])
            )
        # 顶层字段 + `versions` 一起落账（`versions` 供前端列历史版本、来回切）。
        await record_cleanup_attempt(
            run_id, outputs_dir, asset_id, record, version=1
        )
        if (
            remembered is not None
            and str(remembered.get("decision")) == DECISION_CLEANED
            and record.cleaned is not None
        ):
            # 上次选了"清洗版"，但选定的那个文件已经不在了（被覆盖/清掉）→
            # 继承的是**"要用清洗版"这个意图**，指向本次新产出的这一版。
            await _write_status(
                run_id, outputs_dir, asset_id,
                decision=DECISION_CLEANED, selected=str(record.cleaned),
            )

    await asyncio.gather(*(clean_one(asset) for asset in assets))


def _watermark_strokes(image: Image.Image) -> float:
    """粗估「叠加水印」面积占比：亮 + 低饱和 + 比周围更亮的细笔画，且偏向画面边缘。"""
    gray = image.convert("L")
    small = gray.resize((160, int(160 * image.height / max(1, image.width))))
    background = small.filter(ImageFilter.MedianFilter(size=9))
    diff = ImageChops.subtract(small, background)
    hsv = image.convert("HSV").resize(small.size)
    sat = hsv.getchannel("S")
    val = hsv.getchannel("V")
    hits = 0
    total = small.width * small.height
    diff_px, sat_px, val_px = diff.load(), sat.load(), val.load()
    for y in range(small.height):
        for x in range(small.width):
            if (
                _STROKE_MIN_DIFF < diff_px[x, y] <= _STROKE_MAX_DIFF
                and sat_px[x, y] < 70
                and val_px[x, y] > 150
            ):
                hits += 1
    return hits / max(1, total)


def has_overlay_watermark(path: Path) -> bool:
    """这张参考图有没有叠加水印（没有就一个像素都不动）。"""
    try:
        with Image.open(path) as image:
            return _watermark_strokes(image) >= 0.0025
    except Exception:  # noqa: BLE001
        return False


def _mean_abs_diff(a: Path, b: Path) -> float | None:
    """缩到 128×128 灰度后的平均绝对差：越小越像（保真护栏的核心指标）。"""
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            ga = ia.convert("L").resize((128, 128))
            gb = ib.convert("L").resize((128, 128))
            stat = ImageStat.Stat(ImageChops.difference(ga, gb))
            return float(stat.mean[0])
    except Exception:  # noqa: BLE001
        return None


def _block_diff_means(a: Path, b: Path) -> list[int] | None:
    """把差图切成 `_LOCAL_BLOCK × _LOCAL_BLOCK` 块，返回**每块的灰度平均差**。

    `resize(..., Image.BOX)` 在**下采样**时每个输出像素正好是**对应源区域的平均值**，
    所以 `resize((32, 32), BOX)` 的输出就是 32×32 = 1024 个块的均值。
    纯 PIL 实现（feishu venv 里没有 numpy/cv2），保真度告警绝不抛异常。
    """
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            ga = ia.convert("L")
            gb = ib.convert("L")
            if gb.size != ga.size:
                # 图片模型会换分辨率（实测 1080×1920 → 1584×2816），先对齐再比。
                gb = gb.resize(ga.size)
            blocks = ImageChops.difference(ga, gb).resize(
                (_LOCAL_BLOCK, _LOCAL_BLOCK), Image.BOX
            )
            # 不用 `getdata()`：Pillow 14 起会移除它（本机已报 DeprecationWarning）。
            flatten = getattr(blocks, "get_flattened_data", None)
            return list(flatten()) if callable(flatten) else list(blocks.getdata())
    except Exception:  # noqa: BLE001
        return None


def _worst_block_diff(a: Path, b: Path) -> float | None:
    """**最差局部块**的灰度平均绝对差（2026-10-22 新增，治"局部被悄悄抹掉"）。

    为什么整图均值不够：一条手绘红圈只占画幅千分之几，缩到 128×128 比均值几乎为 0 ——
    生产事故里正是这样以 7.52（≤ 12）"通过"了保真护栏，却把用户的指认标记整条删了。
    """
    means = _block_diff_means(a, b)
    return None if means is None else float(max(means))


def _excluded_block_mask(
    image_path: Path, boxes: tuple[tuple[float, float, float, float], ...]
) -> list[bool] | None:
    """32×32 块网格里，哪些块压在「探针确认的水印框」上（做局部块护栏时跳过它们）。

    返回 `None` = **不豁免**（没有框 / 框太大 / 读不出尺寸）→ 退回旧行为（保守）。
    框太大的豁免等于把护栏拆掉：漏水方向是"用户手绘标记被洗掉"，那一侧绝不能让。
    """
    if not boxes:
        return None
    try:
        with Image.open(image_path) as opened:
            width, height = opened.size
    except Exception:  # noqa: BLE001
        return None
    grid = _LOCAL_BLOCK * _LOCAL_BLOCK
    mask = [False] * grid
    excluded = 0
    for x0, y0, x1, y1 in boxes:
        padded = (
            max(0.0, min(1.0, x0 - _LOCAL_EXCLUDE_PAD)),
            max(0.0, min(1.0, y0 - _LOCAL_EXCLUDE_PAD)),
            max(0.0, min(1.0, x1 + _LOCAL_EXCLUDE_PAD)),
            max(0.0, min(1.0, y1 + _LOCAL_EXCLUDE_PAD)),
        )
        px0, py0, px1, py1 = padded
        if px1 <= px0 or py1 <= py0:
            continue
        col0 = max(0, int(px0 * _LOCAL_BLOCK))
        col1 = min(_LOCAL_BLOCK, int(px1 * _LOCAL_BLOCK) + 1)
        row0 = max(0, int(py0 * _LOCAL_BLOCK))
        row1 = min(_LOCAL_BLOCK, int(py1 * _LOCAL_BLOCK) + 1)
        for row in range(row0, row1):
            for col in range(col0, col1):
                index = row * _LOCAL_BLOCK + col
                if not mask[index]:
                    mask[index] = True
                    excluded += 1
    if excluded == 0 or excluded > _LOCAL_EXCLUDE_MAX_RATIO * grid:
        return None
    return mask


def _local_change_stats(
    a: Path,
    b: Path,
    *,
    exclude: tuple[tuple[float, float, float, float], ...] = (),
) -> tuple[float, int] | None:
    """(最差块差异, 受影响块数) —— 判定用的**合取**判据，理由见 `_LOCAL_DIFF_MIN_BLOCKS`。

    2026-10-23 事故：只看最差单块时，**水印本身被洗掉**也会被判成"抹掉了内容"（水印所在
    块的差异本来就大）→ 一次完美清洗被拒、回退成带水印的原图。补上"受影响块数"这一维后，
    两者在全量生产图对上分得很开：水印被洗掉 = 0~3 块；画面内容被改写 = 52~56 块。

    `exclude`（2026-11-02）：探针确认的**水印框**（归一化坐标）—— 落在框里的块不参与判定。
    框里的改动本来就是"水印被洗掉"这件事本身，不该算成"改写了画面内容"（金蛋那次事故：
    4 个超阈块**全部**压在探针给的水印框上，于是成功的清洗被拒）。框太大时不豁免，
    见 `_excluded_block_mask`。
    """
    means = _block_diff_means(a, b)
    if means is None:
        return None
    mask = _excluded_block_mask(a, tuple(exclude))
    if mask is not None:
        kept = [value for value, skip in zip(means, mask) if not skip]
        if kept:
            means = kept
    return float(max(means)), sum(1 for m in means if m > _LOCAL_DIFF_MAX)




async def _image_app_reachable(client: httpx.AsyncClient) -> bool:
    """图片应用（8797）能不能连上。

    必须**先探一次**再决定要不要洗：测试环境/没起图片应用时，如果直接发清洗请求会挂到超时 ✗
    （实测把两个审批图测试拖红了 ✓）。探不通就当作"本机不具备清洗能力"，**保留原图** ✓。
    """
    try:
        response = await client.get(f"{_IMAGE_APP}/api/config", timeout=2.0)
        return response.status_code < 500
    except Exception:  # noqa: BLE001
        return False


async def clean_reference_image(
    path: Path,
    *,
    asset_id: str,
    output_dir: Path | None = None,
    version: int = 1,
    scope: CleanupScope | None = None,
) -> CleanupRecord:
    """对一张参考图做水印清洗。**模型重绘优先；护栏拒绝才退回确定性修补**。

    调用方拿到 `adopted=True` 才把提交用的副本换成 `cleaned`；原图**始终不动**。

    两条路（用户 2026-10-29 口径：「退回重绘清洗」）：
    ① **模型整张重绘**（首选）：整张一起重画 ⇒ 看不出局部补丁，就是用户印象里
       "完全不留痕迹"的那种效果；代价是不确定性，所以洗完必须过三道护栏 ——
       **保护区守卫**（把品牌字/画面内容洗掉 → 拒）、**涂黑守卫**（把水印区涂成纯黑 → 拒）、
       灰度保真护栏（整体改写过大 → 拒）；
    ② **确定性修补**（回退）：① 被拒或模型不可用时，改用本地 `cv2.inpaint` 只改水印那几个
       像素 —— 绝不碰画面其它内容（压口红「品牌字被洗掉」、文档4「水印区被涂黑」都是模型版干的，
       这条回退兜住它们）。代价是有纹理的背景上会留一点补丁感。
    两条路都不行 → 保留原图（把模型版的拒绝原因带上，界面看得见为什么）。

    `version`（2026-10-23）：重洗时传 2、3… → 产出 `-nowm-2` / `-nowm-3`，
    **不覆盖**之前那几版（用户要能来回对比、退回更满意的一版）。

    `scope`（2026-10-28）：探针给出的清洗范围 —— 写进清洗指令，并用于保护区守卫。
    """
    # 水印检测 / 保真比对都是纯 Python + PIL 的逐像素循环，丢线程里执行，
    # 免得 N 张图并发时把事件循环占死（那样"并发"就退化成串行了）。
    if not await asyncio.to_thread(has_overlay_watermark, path):
        return CleanupRecord(asset_id, path, None, None, False, "未检测到叠加水印，未处理")
    target_dir = output_dir or path.parent
    cleaned = target_dir / f"{path.stem}{cleaned_suffix(version)}{path.suffix}"
    # ① **模型整张重绘优先**（用户 2026-10-29：「退回重绘清洗」）—— 整张重画才看不出补丁。
    model_record = await _model_clean_reference(
        path, cleaned=cleaned, asset_id=asset_id, scope=scope
    )
    if model_record.adopted and model_record.cleaned is not None:
        return model_record
    # ② 模型版被护栏拒（涂黑 / 洗掉画面内容 / 改写过大）或模型不可用 → 退回确定性修补。
    if await asyncio.to_thread(_inpaint_watermark_boxes, path, cleaned, scope):
        # **修补必须自证**（2026-11-02「金蛋」事故）：工具退出码 0 不等于"水印掉了" ——
        # 实测它可能只改几十个像素、水印原样留着。洗不掉就**不许**记账成「已去水印」，
        # 否则用户在审批页看到的是"说洗好了、水印还在"，也就是他嘴里的「清洗功能失效了」。
        still_there = await asyncio.to_thread(_watermark_still_there, path, cleaned, scope)
        if still_there is None:
            fidelity = await asyncio.to_thread(_mean_abs_diff, path, cleaned)
            return CleanupRecord(
                asset_id,
                path,
                cleaned,
                fidelity,
                True,
                "模型重绘未通过护栏"
                + (f"（{model_record.reason}）" if model_record.reason else "")
                + "，已改用确定性修补：只改水印那几个像素、画面其余与原图逐点一致",
                deterministic=True,
            )
        return _dc_replace(
            model_record,
            reason=(
                f"{model_record.reason or '模型重绘未通过护栏'}"
                f"；{still_there}。可在审批页点「重新清洗」再试一版"
            ),
        )
    # 两条路都不行 → 保留原图（带上模型版的拒绝原因，界面看得见为什么）。
    return model_record


async def _model_clean_reference(
    path: Path,
    *,
    cleaned: Path,
    asset_id: str,
    scope: CleanupScope | None,
) -> CleanupRecord:
    """模型整张重绘（含分辨率还原 + 三道护栏）。被拒/失败都返回 `adopted=False` 的记录。"""
    try:
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            if not await _image_app_reachable(client):
                return CleanupRecord(
                    asset_id, path, None, None, False, "本机图片应用不可达，保留原图"
                )
            data = {
                "provider": "volcengine",
                "model": "ep-20260912121809-6wt62",
                "mode": "img2img",
                "aspect_ratio": "auto",
                "image_size": "2K",
                "response_format": "url",
                "repeat_count": "1",
                "prompt": _clean_prompt_with_scope(scope),
                "output_name": f"{path.stem}-nowm",
            }
            created = await client.post(
                f"{_IMAGE_APP}/api/jobs",
                data=data,
                files={"image_1": (path.name, path.read_bytes(), "image/png")},
            )
            if created.status_code >= 300:
                return CleanupRecord(asset_id, path, None, None, False, "清洗请求提交失败")
            job_id = created.json().get("job_id")
            for _ in range(150):
                await asyncio.sleep(8)
                listing = (await client.get(f"{_IMAGE_APP}/api/jobs")).json()
                items = listing.get("jobs") if isinstance(listing, dict) else listing
                job = next((x for x in (items or []) if x.get("job_id") == job_id), {})
                status = job.get("status")
                if status in {"failed", "cancelled"}:
                    return CleanupRecord(asset_id, path, None, None, False, "清洗生成失败")
                if status == "succeeded":
                    out_root = Path(
                        r"C:\Users\123\Documents\LTX\ai-generation-portable-apps\nano-banana\outputs"
                    )
                    saved = False
                    for r in (job.get("results") or []):
                        for img in (r.get("images") or []):
                            found = list(out_root.rglob(img.get("filename") or ""))
                            if found:
                                cleaned.write_bytes(found[0].read_bytes())
                                saved = True
                    if not saved:
                        return CleanupRecord(asset_id, path, None, None, False, "清洗结果取不到")
                    break
            else:
                return CleanupRecord(asset_id, path, None, None, False, "清洗超时")
    except Exception:  # noqa: BLE001
        _LOGGER.warning("参考图水印清洗失败，保留原图 asset=%s", asset_id, exc_info=True)
        return CleanupRecord(asset_id, path, None, None, False, "清洗异常，保留原图")

    fidelity = await asyncio.to_thread(_mean_abs_diff, path, cleaned)
    if fidelity is None:
        return CleanupRecord(asset_id, path, cleaned, None, False, "相似度算不出，保留原图")
    # **分辨率还原**（2026-10-28）：模型 2K 重绘会把图放大，
    # 不还原的话下面所有"按像素框"的判断都会指到错误的画面区域。
    await asyncio.to_thread(_match_resolution, path, cleaned)
    # 保护区守卫（2026-10-28，**先于灰度护栏**）：探针说"这块是画面内容"，洗完就得还在。
    # 实测这条正是灰度护栏抓不到的那个洞：浮雕品牌字消失时最差块只有 31（< 35）。
    # （首选是上面的确定性路径；走到这里说明没拿到水印框或修补失败，只能靠守卫兜底。）
    protect_reason = await asyncio.to_thread(_protect_guard, path, cleaned, scope)
    if protect_reason is not None:
        return CleanupRecord(
            asset_id,
            path,
            cleaned,
            fidelity,
            False,
            protect_reason,
            content_lost=True,
        )
    # 「去水印」不许变成「涂黑」（2026-10-28 用户报「右下角被替换成了纯黑色」）：
    # 模型那条路常把水印区直接涂黑 —— 这种版本一律判不可用、保留原图。
    black_ratio = await asyncio.to_thread(_black_patch_ratio, path, cleaned)
    if black_ratio is not None and black_ratio > _BLACK_PATCH_MAX_RATIO:
        return CleanupRecord(
            asset_id,
            path,
            cleaned,
            fidelity,
            False,
            f"清洗把画面涂黑了（原本明亮处有 {black_ratio:.1%} 的像素变成纯黑），"
            "疑似把水印区直接涂黑，已保留原图",
            content_lost=True,
        )
    if fidelity > _FIDELITY_MAX_DIFF:
        return CleanupRecord(
            asset_id, path, cleaned, fidelity, False,
            f"清洗后与实物差异过大（差异 {fidelity:.1f} > {_FIDELITY_MAX_DIFF:.0f}），已保留原图",
        )
    # 局部块护栏（2026-10-22 加，2026-10-23 收紧条件）：整图均值看不见"把画面里的某样东西
    # 抹掉"（手绘红圈就是这么没的，见模块 docstring）。判定必须**同时**满足两条 —— 最差块
    # 超阈值 **且** 受影响块数达标。只看最差单块会把"水印本身被洗掉"误判成"抹掉了内容"
    # （水印所在块的差异本来就大），于是一次只在角落水印处改动的完美清洗被拒、回退成带水印
    # 的原图，用户看到的就是「去水印失效」。
    #
    # 2026-11-02：还要**排除探针确认的水印框**（`scope.watermarks`）—— 框里的差异就是
    # "水印被洗掉"本身。金蛋那次事故里 4 个超阈块**全部**压在水印框上，直接把一次
    # 成功的清洗（水印笔画能量 18.1 → 4.3，保护区留存 100%）判成了"改写了画面内容"。
    watermark_boxes = tuple(scope.watermarks) if scope is not None else ()
    ignore_watermark_blocks = (
        await asyncio.to_thread(_excluded_block_mask, path, watermark_boxes) is not None
    )
    stats = await asyncio.to_thread(
        _local_change_stats,
        path,
        cleaned,
        exclude=watermark_boxes if ignore_watermark_blocks else (),
    )
    local: float | None = None
    altered = 0
    if stats is not None:
        local, altered = stats
        if local > _LOCAL_DIFF_MAX and altered >= _LOCAL_DIFF_MIN_BLOCKS:
            return CleanupRecord(
                asset_id, path, cleaned, fidelity, False,
                f"清洗改写了画面内容（最差局部差异 {local:.0f} > {_LOCAL_DIFF_MAX:.0f}、"
                f"受影响 {altered}/{_LOCAL_BLOCK * _LOCAL_BLOCK} 块），"
                f"疑似抹掉了手绘标记或画面内容，已保留原图",
            )
    # 刻意**不做**"清洗后再检一次水印"：实测那个启发式判据**分不清水印与画面里的界面文字**
    # （生产参考图上的游戏 UI「0/6」、箭头都会被算进去，清洗后占比反而更高 ✗）。
    # 采用与否只由**保真护栏**决定；"水印到底去掉没有"交给用户在审批页的前后对比里看 ✗→✓。
    detail = f"与实物差异 {fidelity:.1f} ≤ {_FIDELITY_MAX_DIFF:.0f}"
    if local is not None:
        detail += (
            f"、最差局部差异 {local:.0f} ≤ {_LOCAL_DIFF_MAX:.0f}"
            f"（受影响 {altered} 块）"
        )
    if ignore_watermark_blocks:
        detail += "，水印框内的改动不计"
    return CleanupRecord(
        asset_id, path, cleaned, fidelity, True,
        f"已去水印并采用（{detail}；原图保留，可在审批页对比）",
    )


# ── 用户选择 / 重新清洗（2026-10-23）─────────────────────────────────────────


def _versions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("versions")
    return [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []


def next_cleanup_version(entry: dict[str, Any] | None) -> int:
    """下一次清洗该用第几版（已有 N 版 → N+1）。"""
    return len(_versions(entry or {})) + 1


async def record_cleanup_attempt(
    run_id: str,
    outputs_dir: Path,
    asset_id: str,
    record: CleanupRecord,
    *,
    version: int = 1,
) -> None:
    """把一次清洗尝试**追加**进台账。

    * `versions` 保留历史（重绘有随机性，重洗可能拿到更好的一版，要能退回对比）；
    * 顶层 `status` / `message` / `fidelity` / `cleaned` 始终指向**最新一版** ——
      前端和 `effective_cleanup_path` 都读这几个字段，保持向后兼容；
    * 重洗后把 `decision` 重置成 `auto`：旧决定可能指向"已经不是最新的那一版"，
      交给新结果的护栏判定更可预期（用户想覆盖再点一次就行）。
    """

    def _apply(entry: dict[str, Any]) -> None:
        item = {
            "version": int(version),
            # 与既有台账口径一致：Windows 原生路径字符串（`resolve_cleaned_path` 两种都认）。
            "path": str(record.cleaned) if record.cleaned else None,
            # 素材指纹（2026-10-28）：提交前用它核对"这一版还是不是当时那一版"。
            # 清洗版文件名会随版本复用（`-nowm.png` 每次覆盖），所以**必须**存指纹，
            # 否则"审批页看到的那版"可能被后来的一次清洗悄悄换掉。
            "sha256": (
                source_image_sha(record.cleaned) if record.cleaned else None
            ),
            "adopted": bool(record.adopted),
            "reason": record.reason,
            "fidelity": record.fidelity,
            "at": datetime.now(timezone.utc).isoformat(),
            # 这一版是否把画面内容洗掉了 —— 生效判定要用（见 `effective_cleanup_path`）。
            "content_lost": bool(record.content_lost),
            # 确定性修补版本：生效判定里优先于"回退原图"（见 `effective_cleanup_path`）。
            "deterministic": bool(record.deterministic),
        }
        versions = [
            v for v in _versions(entry) if int(v.get("version") or 0) != int(version)
        ]
        versions.append(item)
        versions.sort(key=lambda v: int(v.get("version") or 0))
        entry["versions"] = versions
        entry["status"] = "adopted" if record.adopted else "kept"
        entry["message"] = record.reason
        if record.fidelity is not None:
            entry["fidelity"] = record.fidelity
        if record.cleaned is not None:
            entry["cleaned"] = str(record.cleaned)
        entry["decision"] = DECISION_AUTO
        entry.pop("selected", None)

    await _mutate_status(run_id, outputs_dir, asset_id, _apply)


async def set_cleanup_decision(
    run_id: str,
    outputs_dir: Path,
    asset_id: str,
    decision: str,
    *,
    selected: str | None = None,
    data_dir: Path | None = None,
    source_path: Path | None = None,
) -> bool:
    """记录用户对这张参考图的选择（`auto` / `cleaned` / `original`）；非法值返回 False。

    用户 2026-10-23 口径：「可以让用户看到清洗后的图片自由选择原图或者继续重新清洗」——
    护栏从此降级为**建议**：用户看过图之后有最终决定权（含"护栏判 kept，但我就要用清洗版"）。

    `data_dir` + `source_path`（2026-10-28）：把这个选择**按原图内容 sha 记到跨运行记忆**里，
    下一版运行（新 run）自动继承 —— 以前每次重跑都被重置成 `auto`，用户看到的是"我选的白选了"。
    """
    if decision not in DECISIONS:
        return False
    # 不许选定"把画面内容洗掉了"的那一版（用户口径：不要洗字）。
    if decision == DECISION_CLEANED:
        entry = read_cleanup_status(run_id, outputs_dir).get(asset_id) or {}
        chosen = selected or entry.get("cleaned")
        if version_lost_content(entry, chosen):
            return False

    def _apply(entry: dict[str, Any]) -> None:
        entry["decision"] = decision
        if selected:
            entry["selected"] = selected
        elif decision != DECISION_CLEANED:
            entry.pop("selected", None)

    await _mutate_status(run_id, outputs_dir, asset_id, _apply)
    await remember_cleanup_decision(
        data_dir,
        await asyncio.to_thread(source_image_sha, source_path)
        if source_path is not None
        else None,
        decision,
        selected=selected,
        asset_id=asset_id,
    )
    return True


async def reclean_reference_image(
    path: Path,
    *,
    run_id: str,
    outputs_dir: Path,
    asset_id: str,
    data_dir: Path | None = None,
    scope_probe: Callable[[Any], Awaitable[CleanupScope | None]] | None = None,
) -> CleanupRecord:
    """重洗一张参考图：产出**新版本**（`-nowm-2` / `-nowm-3`…），旧版本保留。

    重洗就是再跑一次整张重绘（**刻意不做局部重绘** —— 用户 2026-10-23：「局部重绘拼接的
    时候会有明显漏洞」）。重绘本来就有随机性，所以重洗是拿到更干净一版的最实际手段。

    `data_dir`（2026-10-28）：重洗意味着"上一版我不满意、重新来过"，所以顺手**抹掉**
    这张原图的跨运行记忆 —— 否则下一次新 run 会把用户更早那次的选择又搬回来。

    `scope_probe`（2026-10-28）：重洗同样先确认清洗范围，不然重洗会再把字洗掉一次
    （用户就是这么连着中招的）。
    """
    entry = read_cleanup_status(run_id, outputs_dir).get(asset_id) or {}
    version = next_cleanup_version(entry)
    await _write_status(
        run_id, outputs_dir, asset_id, status="running", message="正在重新去水印…"
    )
    scope: CleanupScope | None = None
    if scope_probe is not None:
        try:
            scope = await scope_probe(_asset_stub(asset_id, path))
        except Exception:  # noqa: BLE001
            _LOGGER.warning("重洗前的清洗范围探针失败，按老逻辑清洗", exc_info=True)
            scope = None
    record = (
        await clean_reference_image(path, asset_id=asset_id, version=version)
        if scope is None
        else await clean_reference_image(
            path, asset_id=asset_id, version=version, scope=scope
        )
    )
    await record_cleanup_attempt(
        run_id, outputs_dir, asset_id, record, version=version
    )
    if data_dir is not None:
        await remember_cleanup_decision(
            data_dir,
            await asyncio.to_thread(source_image_sha, path),
            DECISION_AUTO,
            asset_id=asset_id,
        )
    return record
