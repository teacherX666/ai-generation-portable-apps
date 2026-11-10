"""拼图式参考图：罐子用**图1 的真实像素**（换色）×4，甜甜圈用**图3 里淋好酱的**，四列由我算好。

为什么走拼图：Seedream 5.0 Pro 与 Gemini(banana2) 连着 6 版都把罐子"改良"成细长玻璃瓶，
盖子的"扁平圆盘+细长圆柱提手+金色卡扣"和"下宽上窄梨形"都保不住。用户要求「瓶子要用图1 中的瓶子」
—— 唯一能保证的做法就是**直接用图1 的像素**。

产物：_diag/anchor/composite/四列对应_拼图.png
用法：python _diag/make_ref_composite.py
"""
from __future__ import annotations

import colorsys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
ANCHOR = ROOT / "_diag" / "anchor"
OUT_DIR = ANCHOR / "composite"

JAR = ANCHOR / "fig1_jar_single.png"      # 图1 里单个罐子（240x490，完整）
FIG3 = SCENE / "ref_image-3.png"          # 四只淋好酱的甜甜圈（浅紫盘 + 粉背景）
FIG1 = SCENE / "ref_image-1.png"

#: 目标四色的目标色相（0-360）
TARGET_HUES = {"红": 0, "黄": 50, "蓝": 220, "紫": 280}
COLORS = ["红", "黄", "蓝", "紫"]


def recolor(image: Image.Image, hue_target: float) -> Image.Image:
    """把罐子的主色相旋到目标色相，保留明暗/高光（= 保住陶瓷质感与器型）。"""
    rgb = np.asarray(image.convert("RGB")).astype(np.float32) / 255.0
    hsv = np.asarray(image.convert("HSV")).astype(np.float32)
    hue = hsv[:, :, 0] * 360.0 / 255.0
    sat = hsv[:, :, 1] / 255.0
    val = hsv[:, :, 2] / 255.0
    # 只旋"有颜色"的像素（饱和度够），灰白高光/提手保持原样
    mask = sat > 0.25
    source_hue = float(np.median(hue[mask])) if mask.any() else 0.0
    delta = (hue_target - source_hue) % 360.0
    hue = np.where(mask, (hue + delta) % 360.0, hue)
    # 目标色更鲜艳一点，避免黄/蓝偏灰
    sat = np.where(mask, np.clip(sat * 1.05, 0, 1), sat)
    out = np.zeros_like(rgb)
    flat_h = (hue / 360.0)
    for y in range(rgb.shape[0]):
        for x in range(rgb.shape[1]):
            r, g, b = colorsys.hsv_to_rgb(flat_h[y, x], sat[y, x], val[y, x])
            out[y, x] = (r, g, b)
    return Image.fromarray((out * 255).astype(np.uint8))


def sauce_blobs(image: Image.Image) -> dict[str, tuple[int, int, int, int]]:
    """在图3 里按颜色找四只甜甜圈（酱是饱和色，背景是粉/浅紫）。"""
    hsv = np.asarray(image.convert("HSV")).astype(np.int16)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    ranges = {"红": [(0, 12), (240, 255)], "黄": [(20, 52)], "蓝": [(140, 186)], "紫": [(186, 238)]}
    found: dict[str, tuple[int, int, int, int]] = {}
    for name, spans in ranges.items():
        mask = np.zeros(hue.shape, dtype=bool)
        for low, high in spans:
            mask |= (hue >= low) & (hue <= high)
        mask &= (sat > 110) & (val > 90)
        if mask.sum() < 500:
            continue
        ys, xs = np.where(mask)
        # 取最大团块的粗略包围盒：用分位数去掉零散像素
        x0, x1 = int(np.percentile(xs, 3)), int(np.percentile(xs, 97))
        y0, y1 = int(np.percentile(ys, 3)), int(np.percentile(ys, 97))
        found[name] = (x0, y0, x1, y1)
        print(f"    {name}: box=({x0},{y0})-({x1},{y1}) 采样像素={mask.sum():,}")
    return found


def cut_donut(image: Image.Image, box: tuple[int, int, int, int], pad: int = 18) -> Image.Image:
    """裁一只甜甜圈，并把粉色背景抠掉（近似：与角落颜色相近且低饱和的像素设透明）。"""
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(image.width, x1 + pad), min(image.height, y1 + pad)
    crop = image.crop((x0, y0, x1, y1)).convert("RGBA")
    arr = np.asarray(crop).astype(np.int16)
    # 以四角平均色为背景色
    corners = np.concatenate([
        arr[:6, :6].reshape(-1, 4), arr[:6, -6:].reshape(-1, 4),
        arr[-6:, :6].reshape(-1, 4), arr[-6:, -6:].reshape(-1, 4),
    ])
    bg = corners.mean(axis=0)[:3]
    dist = np.sqrt(((arr[:, :, :3] - bg) ** 2).sum(axis=2))
    alpha = np.where(dist < 42, 0, 255).astype(np.uint8)
    arr[:, :, 3] = alpha
    return Image.fromarray(arr.astype(np.uint8))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jar_src = Image.open(JAR).convert("RGB")
    fig3 = Image.open(FIG3).convert("RGB")
    print("在图3 里定位四只甜甜圈：")
    boxes = sauce_blobs(fig3)

    cell_w, cell_h = 480, 900
    canvas = Image.new("RGB", (cell_w * 4, cell_h * 2), (34, 22, 54))
    draw = ImageDraw.Draw(canvas)

    for index, color in enumerate(COLORS):
        jar = recolor(jar_src, TARGET_HUES[color])
        jar = jar.resize((300, int(300 * jar.height / jar.width)), Image.LANCZOS)
        jx = index * cell_w + (cell_w - jar.width) // 2
        canvas.paste(jar, (jx, 60), jar if jar.mode == "RGBA" else None)

        donut_img = None
        if color in boxes:
            donut_img = cut_donut(fig3, boxes[color])
        if donut_img is not None:
            scale = min(1.0, 340 / donut_img.width)
            donut_img = donut_img.resize((int(donut_img.width * scale), int(donut_img.height * scale)), Image.LANCZOS)
            dx = index * cell_w + (cell_w - donut_img.width) // 2
            dy = cell_h + 120
            canvas.paste(donut_img, (dx, dy), donut_img)

        # 列间的竖直指示线（一一对应）
        cx = index * cell_w + cell_w // 2
        draw.line([(cx, 60 + jar.height + 12), (cx, cell_h + 120 - 12)], fill=(255, 210, 120), width=6)
        draw.text((index * cell_w + 16, 16), f"{color}", fill=(255, 255, 255))

    out = OUT_DIR / "四列对应_拼图.png"
    canvas = canvas.filter(ImageFilter.SMOOTH)
    canvas.save(out)
    print("输出:", out, canvas.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
