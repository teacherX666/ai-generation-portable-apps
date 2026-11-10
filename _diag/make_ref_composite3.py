"""自己拼「四个漂浮罐子浇四只甜甜圈」的**干净版**参考图。

用户 2026-10-21 口径：「用原图中场景里的罐子让他漂浮起来，浇在甜甜圈上，并且一一对应」。
上一版拼图被视觉模型挑出四个毛病（残留旧罐子/旧甜甜圈、酱流是白条、拼接痕明显、颜色不对），
这一版逐条修：
  * 底图把**旧罐子与旧盘子一起用木纹盖掉**（不再有残留 → 数量不会变 6）；
  * 甜甜圈从**图2**（干净的浅紫盘 + 4 只金黄甜甜圈）里抠，避开图1 盘上的旧甜甜圈；
  * 每个甜甜圈上面画**对应颜色的酱**（带高光与滴落），不再是"多色混在一起"；
  * 酱流用**罐子自己的颜色**画（上一版画成白色是 bug），从罐口落到本列甜甜圈；
  * 罐子仍是**图1 那只的原始像素**换色（器型/盖子/提手/金卡扣天然一致），摆在盘子上方漂浮。

用法：python _diag/make_ref_composite3.py
输出：_diag/anchor/composite/浮动四罐_干净拼图.png
"""
from __future__ import annotations

import colorsys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
ANCHOR = ROOT / "_diag" / "anchor"
OUT_DIR = ANCHOR / "composite"

FIG1 = SCENE / "ref_image-1.png"
FIG2 = SCENE / "ref_image-2.png"
JAR = ANCHOR / "fig1_jar_single.png"

CROP = (0, 1550, 1600, 2750)
SCALE = 1.5
PROPS_AREA = (20, 1870, 1120, 2480)      # 图1 里所有道具（旧罐子 + 旧盘子）——整块盖掉
WOOD_PATCH = (1080, 1880, 1560, 2420)    # 干净木纹来源
PLATE = (560, 2136, 1040, 2420)          # 甜甜圈落点（沿用图1 盘子的位置）

COLORS = [("红", 0.0, (208, 38, 38)), ("黄", 50.0, (240, 200, 40)),
          ("蓝", 220.0, (40, 92, 214)), ("紫", 285.0, (150, 66, 214))]
#: 图2 里金黄甜甜圈在 cv2 HSV 下的范围
DONUT_RANGES = [(12, 38)]


def recolor(image: Image.Image, target_hue: float) -> Image.Image:
    hsv = np.asarray(image.convert("HSV")).astype(np.float32)
    hue = hsv[:, :, 0] * 360.0 / 255.0
    sat = hsv[:, :, 1] / 255.0
    mask = sat > 0.25
    source = float(np.median(hue[mask])) if mask.any() else 0.0
    delta = (target_hue - source) % 360.0
    hsv[:, :, 0] = np.where(mask, ((hue + delta) % 360.0) * 255.0 / 360.0, hsv[:, :, 0])
    hsv[:, :, 1] = np.where(mask, np.clip(sat * 1.08, 0, 1) * 255.0, hsv[:, :, 1])
    return Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")


def extract_donuts(fig2: Image.Image, count: int = 4) -> list[Image.Image]:
    """从图2 抠出金黄甜甜圈（按色相找 blob，椭圆蒙版去掉背景）。"""
    rgb = np.asarray(fig2.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = np.zeros(hue.shape, dtype=np.uint8)
    for low, high in DONUT_RANGES:
        mask |= ((hue >= low) & (hue <= high)).astype(np.uint8)
    mask = (mask & (sat > 90) & (val > 120)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    total, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    blobs = sorted(range(1, total), key=lambda i: -stats[i, cv2.CC_STAT_AREA])[:count]
    blobs.sort(key=lambda i: (stats[i, cv2.CC_STAT_LEFT] // (fig2.width // 3), stats[i, cv2.CC_STAT_LEFT]))
    out: list[Image.Image] = []
    for index in blobs:
        x, y, w, h = (int(stats[index, cv2.CC_STAT_LEFT]), int(stats[index, cv2.CC_STAT_TOP]),
                      int(stats[index, cv2.CC_STAT_WIDTH]), int(stats[index, cv2.CC_STAT_HEIGHT]))
        pad = int(w * 0.10)
        box = (max(0, x - pad), max(0, y - pad), min(fig2.width, x + w + pad), min(fig2.height, y + h + pad))
        crop = fig2.crop(box).convert("RGBA")
        m = Image.new("L", crop.size, 0)
        ImageDraw.Draw(m).ellipse((2, 2, crop.width - 3, crop.height - 3), fill=255)
        crop.putalpha(m.filter(ImageFilter.GaussianBlur(2)))
        out.append(crop)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig1 = Image.open(FIG1).convert("RGB")
    fig2 = Image.open(FIG2).convert("RGB")
    jar0 = Image.open(JAR).convert("RGB")

    base = fig1.crop(CROP).resize(
        (int((CROP[2] - CROP[0]) * SCALE), int((CROP[3] - CROP[1]) * SCALE)), Image.LANCZOS)

    def to_base(box_abs):
        return tuple(int(v * SCALE) for v in (
            box_abs[0] - CROP[0], box_abs[1] - CROP[1], box_abs[2] - CROP[0], box_abs[3] - CROP[1]))

    # 1) 整块道具区用木纹盖掉（旧罐子 + 旧盘子 + 旧甜甜圈都不再出现）
    props = to_base(PROPS_AREA)
    wood_src = fig1.crop(WOOD_PATCH)
    patch_w, patch_h = props[2] - props[0], props[3] - props[1]
    wood = Image.new("RGB", (patch_w, patch_h))
    for y in range(0, patch_h, wood_src.height):
        for x in range(0, patch_w, wood_src.width):
            wood.paste(wood_src, (x, y))
    base.paste(wood, (props[0], props[1]))

    donuts = extract_donuts(fig2, 4)
    print("从图2 抠出甜甜圈:", len(donuts))

    plate = to_base(PLATE)
    col_w = (plate[2] - plate[0]) / 4
    jar_w = int(col_w * 0.82)
    jar_h = int(jar0.height * jar_w / jar0.width)
    jar_bottom = plate[1] - int(70 * SCALE)
    donut_y = plate[1] - int(10 * SCALE)

    for index, (name, hue, rgb_color) in enumerate(COLORS):
        cx = int(plate[0] + col_w * (index + 0.5))

        # 2) 甜甜圈 + 对应颜色的酱（画在甜甜圈上半部，带高光与滴落）
        if index < len(donuts):
            donut = donuts[index]
            dw = int(col_w * 0.95)
            donut = donut.resize((dw, int(donut.height * dw / donut.width)), Image.LANCZOS)
            dx, dy = cx - donut.width // 2, donut_y
            base.paste(donut, (dx, dy), donut)
            sauce = Image.new("RGBA", base.size, (0, 0, 0, 0))
            sd = ImageDraw.Draw(sauce)
            cap = (dx + int(donut.width * 0.16), dy + int(donut.height * 0.10),
                   dx + donut.width - int(donut.width * 0.16), dy + int(donut.height * 0.62))
            sd.ellipse(cap, fill=(*rgb_color, 236))
            sd.ellipse((cap[0] + int(donut.width * 0.16), cap[1] + int(donut.height * 0.06),
                        cap[0] + int(donut.width * 0.42), cap[1] + int(donut.height * 0.22)),
                       fill=(255, 255, 255, 120))
            for drip in range(2):
                ddx = dx + int(donut.width * (0.30 + 0.28 * drip))
                sd.ellipse((ddx, cap[3] - int(donut.height * 0.06), ddx + int(donut.width * 0.12),
                            cap[3] + int(donut.height * 0.16)), fill=(*rgb_color, 226))
            base.paste(sauce.filter(ImageFilter.GaussianBlur(2)), (0, 0), sauce.filter(ImageFilter.GaussianBlur(2)))

        # 3) 漂浮的罐子（图1 的像素换色）
        jar = recolor(jar0, hue).resize((jar_w, jar_h), Image.LANCZOS)
        if name == "蓝":
            arr = np.asarray(jar).astype(np.float32)
            jar = Image.fromarray(np.clip(arr * 1.15, 0, 255).astype(np.uint8))
        base.paste(jar, (cx - jar_w // 2, jar_bottom - jar_h))

        # 4) 酱流：**用本列罐子的颜色**（上一版画成白色是 bug）
        stream = Image.new("RGBA", base.size, (0, 0, 0, 0))
        st = ImageDraw.Draw(stream)
        top = (cx, jar_bottom - int(4 * SCALE))
        bottom = (cx, donut_y + int(16 * SCALE))
        w_top, w_bottom = max(5, int(col_w * 0.09)), max(9, int(col_w * 0.15))
        st.polygon([(top[0] - w_top // 2, top[1]), (top[0] + w_top // 2, top[1]),
                    (bottom[0] + w_bottom // 2, bottom[1]), (bottom[0] - w_bottom // 2, bottom[1])],
                   fill=(*rgb_color, 240))
        base.paste(stream.filter(ImageFilter.GaussianBlur(1)), (0, 0), stream.filter(ImageFilter.GaussianBlur(1)))

    out = OUT_DIR / "浮动四罐_干净拼图.png"
    base.save(out)
    print("输出:", out, base.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
