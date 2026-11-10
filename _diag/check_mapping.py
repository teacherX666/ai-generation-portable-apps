"""以「金黄甜甜圈」为锚，检查四列的一一对应（纯像素，不靠视觉模型）。

步骤：
  1) 找金黄色的甜甜圈团块（下半区，按 x 聚类）→ 得到每只甜甜圈的 x 中心；
  2) 每只甜甜圈**正上方**取样 → 判断盖在它上面的酱是什么颜色；
  3) 盘子以上的带里找四个彩色团块 → 罐子列（颜色 + x）；
  4) 按 x 就近配对：罐子色 vs 甜甜圈酱色，逐列核对。

用法：python _diag/check_mapping.py <图片路径> [--debug]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

RANGES = {  # PIL hue 0-255
    "红": [(0, 12), (244, 255)],
    "黄": [(22, 52)],
    "蓝": [(140, 186)],
    "紫": [(186, 232)],
}
GOLD = [(15, 40)]          # 金黄甜甜圈
DMAP = {"红": (220, 40, 40), "黄": (240, 210, 40), "蓝": (40, 90, 220), "紫": (150, 60, 220)}


def mask_of(hue, sat, val, ranges, sat_min=70, val_min=60):
    mask = np.zeros(hue.shape, dtype=bool)
    for low, high in ranges:
        mask |= (hue >= low) & (hue <= high)
    return mask & (sat > sat_min) & (val > val_min)


def color_name(hue_pixels: np.ndarray) -> str:
    best, best_count = "?", 0
    for name, ranges in RANGES.items():
        count = 0
        for low, high in ranges:
            count += int(((hue_pixels >= low) & (hue_pixels <= high)).sum())
        if count > best_count:
            best, best_count = name, count
    return best


def clusters_x(mask: np.ndarray, min_px: int = 150) -> list[tuple[float, int, int, int]]:
    """按列聚类 → (x 质心, 像素数, x0, x1)，只保留够大的团块。"""
    cols = mask.sum(axis=0)
    xs = np.where(cols > 0)[0]
    if xs.size == 0:
        return []
    groups: list[list[int]] = [[xs[0]]]
    for prev, cur in zip(xs, xs[1:]):
        if cur - prev > max(10, mask.shape[1] // 50):
            groups.append([])
        groups[-1].append(cur)
    out = []
    for group in groups:
        index = np.array(group)
        total = int(cols[index].sum())
        if total < min_px:
            continue
        weights = cols[index]
        centroid = float((index * weights).sum() / weights.sum())
        out.append((centroid, total, int(index.min()), int(index.max())))
    return out


def main() -> int:
    path = Path(sys.argv[1])
    debug = "--debug" in sys.argv
    with Image.open(path) as image:
        width, height = image.size
        hsv = np.asarray(image.convert("HSV")).astype(np.int16)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    print(f"\n{path.name} ({width}x{height})")

    gold = mask_of(hue, sat, val, GOLD, sat_min=90, val_min=110)
    gold[: height // 2] = False                     # 甜甜圈在下半区
    donuts = clusters_x(gold, min_px=1200)
    print(f"  金黄甜甜圈团块: {len(donuts)}")
    for centroid, total, x0, x1 in sorted(donuts, key=lambda d: d[0]):
        print(f"      x={centroid:7.1f}  宽 {x1 - x0:4d}  px={total}")

    # 甜甜圈顶部（金黄像素的最高行）
    rows = np.where(gold.any(axis=1))[0]
    top = int(rows.min()) if rows.size else height
    print(f"  甜甜圈区最高行 y={top}（酱就盖在这附近）")

    # 每只甜甜圈正上方的酱色
    print("  每只甜甜圈上方的酱色：")
    donut_sauce = []
    for centroid, *_ in sorted(donuts, key=lambda d: d[0]):
        x0 = int(max(0, centroid - 70))
        x1 = int(min(width, centroid + 70))
        y0 = int(max(0, top - 90))
        y1 = int(min(height, top + 40))
        box_hue = hue[y0:y1, x0:x1]
        box_sat = sat[y0:y1, x0:x1]
        box_val = val[y0:y1, x0:x1]
        colored = (box_sat > 70) & (box_val > 60)
        pixels = box_hue[colored]
        name = color_name(pixels) if pixels.size > 200 else "?(酱太少或没有)"
        count = int(pixels.size)
        donut_sauce.append((centroid, name, count))
        print(f"      x={centroid:7.1f}  酱={name}  采样像素={count}")

    # 盘子上方的罐子列
    band_top = max(0, top - 700)
    band_bottom = max(0, top - 150)
    print(f"  罐子带 y={band_top}-{band_bottom}")
    jar_columns: list[tuple[float, str, int]] = []
    for name, ranges in RANGES.items():
        mask = mask_of(hue, sat, val, ranges)
        sub = np.zeros_like(mask)
        sub[band_top:band_bottom] = mask[band_top:band_bottom]
        for centroid, total, x0, x1 in clusters_x(sub, min_px=2500):
            jar_columns.append((centroid, name, total))
    jar_columns.sort(key=lambda c: c[0])
    for centroid, name, total in jar_columns:
        print(f"      {name}  x={centroid:7.1f}  px={total}")

    print("\n  ==== 逐列核对（罐子色 vs 正下方甜甜圈的酱色）====")
    if len(jar_columns) != 4:
        print(f"  ⚠ 罐子列检出 {len(jar_columns)} 个（期望 4）—— 画面上罐子可能不是四个/被遮挡")
    if len(donuts) != 4:
        print(f"  ⚠ 甜甜圈检出 {len(donuts)} 个（期望 4）")
    bad = 0
    for centroid, jar_color, _ in jar_columns:
        nearest = min(donut_sauce, key=lambda d: abs(d[0] - centroid)) if donut_sauce else None
        if not nearest:
            continue
        offset = nearest[0] - centroid
        verdict = "对齐" if abs(offset) < 120 and nearest[1] == jar_color else (
            "颜色不符" if abs(offset) < 120 else f"水平错位 {offset:+.0f}px"
        )
        if verdict != "对齐":
            bad += 1
        print(f"  罐子 {jar_color}(x={centroid:.0f}) → 最近甜甜圈 x={nearest[0]:.0f} 酱={nearest[1]}  [{verdict}]")
    print("  结论:", "四列一一对应、颜色相符" if bad == 0 else f"**{bad} 列有问题（错位或颜色不符）**")
    if debug:
        print("  颜色图例:", DMAP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
