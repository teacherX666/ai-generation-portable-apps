"""自己拼「四个漂浮罐子浇四只甜甜圈」的参考图（不靠生成模型搬罐子）。

为什么这么做（2026-10-21 用户口径）：
  「不是把罐子放在地上，而是用原图中场景里的罐子让他漂浮起来，浇在甜甜圈上，并且一一对应」
生成模型只要「搬动/复制」罐子就会改器型（前 6 版全败）。所以这里：
  * 罐子 = **图1 里那只罐子的原始像素**，只做色相旋转（器型/盖子/提手/金卡扣天然一致）；
  * 漂浮 = 我用坐标把它们摆在盘子上方（构造保证）；
  * 一一对应 = 第 i 个罐子 → 第 i 只甜甜圈，酱流按列画（构造保证）；
  * 甜甜圈 = 从图3 抠出四只**已经淋好酱**的。

用法：python _diag/make_ref_composite2.py
输出：_diag/anchor/composite/浮动四罐_淋酱.png
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
ANCHOR = ROOT / "_diag" / "anchor"
OUT_DIR = ANCHOR / "composite"

FIG1 = SCENE / "ref_image-1.png"
FIG3 = SCENE / "ref_image-3.png"
JAR = ANCHOR / "fig1_jar_single.png"

#: 图1 里桌面区（含三只罐子与盘子）——放大 1.5 倍后当底
CROP = (0, 1550, 1600, 2750)
SCALE = 1.5
#: 原罐子所在区域（图1 绝对坐标）——要抹掉，避免与漂浮的四个打架
JAR_AREA = (30, 1880, 840, 2450)
#: 拿来补桌面的干净木纹（图1 绝对坐标）
WOOD_PATCH = (1050, 1900, 1500, 2400)
#: 盘子（图1 绝对坐标）→ 漂浮罐子的落点与甜甜圈的行
PLATE = (560, 2136, 1040, 2420)

COLORS = {"红": 0.0, "黄": 50.0, "蓝": 220.0, "紫": 285.0}
#: 图3 里四色酱在 cv2 HSV(H:0-179) 下的范围
SAUCE_RANGES = {"红": [(0, 8), (170, 179)], "黄": [(20, 35)], "蓝": [(95, 128)], "紫": [(128, 165)]}


def recolor(image: Image.Image, target_hue: float) -> Image.Image:
    """把罐子主色相旋到目标色相（保留明暗/高光 → 陶瓷质感与器型不变）。"""
    hsv = np.asarray(image.convert("HSV")).astype(np.float32)
    hue = hsv[:, :, 0] * 360.0 / 255.0
    sat = hsv[:, :, 1] / 255.0
    mask = sat > 0.25
    source = float(np.median(hue[mask])) if mask.any() else 0.0
    delta = (target_hue - source) % 360.0
    hsv[:, :, 0] = np.where(mask, ((hue + delta) % 360.0) * 255.0 / 360.0, hsv[:, :, 0])
    hsv[:, :, 1] = np.where(mask, np.clip(sat * 1.05, 0, 1) * 255.0, hsv[:, :, 1])
    return Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")


def extract_donut(fig3: Image.Image, color: str) -> Image.Image | None:
    """从图3 抠出一只甜甜圈（按酱色找 blob，再按椭圆裁掉粉色背景）。"""
    rgb = np.asarray(fig3.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = np.zeros(hue.shape, dtype=np.uint8)
    for low, high in SAUCE_RANGES[color]:
        mask |= ((hue >= low) & (hue <= high)).astype(np.uint8)
    mask = (mask & (sat > 80) & (val > 80)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(stats[best, cv2.CC_STAT_LEFT]), int(stats[best, cv2.CC_STAT_TOP]),
                  int(stats[best, cv2.CC_STAT_WIDTH]), int(stats[best, cv2.CC_STAT_HEIGHT]))
    # 酱只在甜甜圈上半部：向下、向两侧扩，得到整只甜甜圈
    cx, cy = x + w / 2, y + h / 2
    half_w, half_h = w * 1.15, h * 1.45
    box = (int(cx - half_w), int(cy - half_h * 0.75), int(cx + half_w), int(cy + half_h * 0.95))
    crop = fig3.crop(box).convert("RGBA")
    # 椭圆蒙版（去掉粉色背景与浅紫盘）
    mask_img = Image.new("L", crop.size, 0)
    ImageDraw.Draw(mask_img).ellipse((2, 2, crop.width - 3, crop.height - 3), fill=255)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(2))
    crop.putalpha(mask_img)
    return crop


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig1 = Image.open(FIG1).convert("RGB")
    fig3 = Image.open(FIG3).convert("RGB")
    jar0 = Image.open(JAR).convert("RGB")

    # 1) 底：桌面区放大
    base = fig1.crop(CROP).resize(
        (int((CROP[2] - CROP[0]) * SCALE), int((CROP[3] - CROP[1]) * SCALE)), Image.LANCZOS)

    def to_base(box_abs):
        return tuple(int(v * SCALE) for v in (
            box_abs[0] - CROP[0], box_abs[1] - CROP[1], box_abs[2] - CROP[0], box_abs[3] - CROP[1]))

    # 2) 抹掉原来那三只罐子：拿旁边的干净木纹盖上去
    jar_box = to_base(JAR_AREA)
    wood = fig1.crop(WOOD_PATCH).resize((jar_box[2] - jar_box[0], jar_box[3] - jar_box[1]), Image.LANCZOS)
    base.paste(wood, (jar_box[0], jar_box[1]))

    # 3) 四个漂浮罐子（图1 的罐子像素换色）
    plate_box = to_base(PLATE)
    col_w = (plate_box[2] - plate_box[0]) / 4
    jar_target_w = int(col_w * 0.78)
    jar_h = int(jar0.height * jar_target_w / jar0.width)
    jar_bottom = plate_box[1] - int(60 * SCALE)      # 悬在盘子上方
    donut_y = plate_box[1] + int(20 * SCALE)

    for index, (color, hue) in enumerate(COLORS.items()):
        cx = int(plate_box[0] + col_w * (index + 0.5))
        jar = recolor(jar0, hue).resize((jar_target_w, jar_h), Image.LANCZOS)
        if index == 2:      # 蓝色在 HSV 里偏暗，稍微提亮，四色观感更均衡
            arr = np.asarray(jar).astype(np.float32)
            jar = Image.fromarray(np.clip(arr * 1.12, 0, 255).astype(np.uint8))
        base.paste(jar, (cx - jar_target_w // 2, jar_bottom - jar_h))

        # 4) 甜甜圈（图3 里淋好酱的那只，按颜色取）
        donut = extract_donut(fig3, color)
        if donut is not None:
            dw = int(col_w * 0.9)
            donut = donut.resize((dw, int(donut.height * dw / donut.width)), Image.LANCZOS)
            base.paste(donut, (cx - donut.width // 2, donut_y), donut)

        # 5) 酱流：从罐口竖直/斜向落到本列甜甜圈
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        mouth = (cx, jar_bottom - int(6 * SCALE))
        target = (cx, donut_y + int(18 * SCALE))
        width_top, width_bottom = max(6, int(col_w * 0.10)), max(10, int(col_w * 0.16))
        draw.polygon([
            (mouth[0] - width_top // 2, mouth[1]), (mouth[0] + width_top // 2, mouth[1]),
            (target[0] + width_bottom // 2, target[1]), (target[0] - width_bottom // 2, target[1]),
        ], fill=(235, 235, 245, 235))
        base.paste(overlay, (0, 0), overlay)

    out = OUT_DIR / "浮动四罐_淋酱.png"
    base.save(out)
    print("输出:", out, base.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
