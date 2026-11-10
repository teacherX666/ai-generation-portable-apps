"""客观检查「四个罐子 ↔ 四只甜甜圈」有没有错位/交叉（不靠问模型）。

原理：罐子和酱都是**按颜色区分**的。分别取"罐子带"和"酱/甜甜圈带"两横条，
算每种颜色像素的 x 质心；如果两带里颜色的**左右顺序**不一致 → 就是错位/交叉。

用法：python _diag/analyze_alignment.py <图片路径> [--verbose]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

#: PIL 的 H 是 0-255（0-360 映射）。这四个范围覆盖红/黄/蓝/紫的高饱和像素。
HUE_RANGES = {
    "红": [(0, 12), (244, 255)],
    "黄": [(26, 52)],
    "蓝": [(140, 186)],
    "紫": [(186, 232)],
}


def hue_mask(hue: np.ndarray, ranges: list[tuple[int, int]]) -> np.ndarray:
    mask = np.zeros_like(hue, dtype=bool)
    for low, high in ranges:
        mask |= (hue >= low) & (hue <= high)
    return mask


def main() -> int:
    path = Path(sys.argv[1])
    verbose = "--verbose" in sys.argv
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB")).astype(np.int16)
        hsv = np.asarray(image.convert("HSV")).astype(np.int16)
    height, width = hsv.shape[0], hsv.shape[1]
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    colored = (sat > 70) & (val > 60)
    print(f"{path.name}  {width}x{height}  彩色像素占比 {colored.mean():.1%}")

    # 每个颜色的行分布，用来判断"罐子带"和"酱带"在哪
    per_color_rows = {}
    for name, ranges in HUE_RANGES.items():
        mask = hue_mask(hue, ranges) & colored
        per_color_rows[name] = mask
        rows = np.where(mask.any(axis=1))[0]
        if rows.size:
            # 行分布的上下两个密集区（罐子在上、酱在下）
            top, bottom = rows.min(), rows.max()
            print(f"  {name}: 像素 {mask.sum():>7d}  行范围 {top}-{bottom}  行中位 {int(np.median(rows))}")

    # 用所有彩色像素的行直方图切两带：上半段（含罐子）与下半段（含酱/甜甜圈）
    row_profile = colored.sum(axis=1)
    if verbose:
        step = max(1, height // 40)
        for y in range(0, height, step):
            print(f"    y={y:>5d} {'#' * int(60 * row_profile[y] / max(1, row_profile.max()))}")
    # 找行分布的两个峰（上半峰=罐子，下半峰=酱/甜甜圈）
    top_half = row_profile[: height // 2]
    bottom_half = row_profile[height // 2 :]
    jar_peak = int(np.argmax(top_half))
    sauce_peak = int(np.argmax(bottom_half)) + height // 2
    print(f"  行峰：上半 {jar_peak}（罐子带） / 下半 {sauce_peak}（酱/甜甜圈带）")

    def band_centroids(center: int, half: int) -> dict[str, tuple[float, int]]:
        low, high = max(0, center - half), min(height, center + half)
        out: dict[str, tuple[float, int]] = {}
        for name, ranges in HUE_RANGES.items():
            mask = hue_mask(hue[low:high], ranges) & colored[low:high]
            count = int(mask.sum())
            if count < 200:
                out[name] = (float("nan"), count)
                continue
            xs = np.where(mask.any(axis=0))[0]
            weights = mask.sum(axis=0)
            centroid = float((xs * weights[xs]).sum() / weights[xs].sum())
            out[name] = (centroid, count)
        return out

    jar_band = band_centroids(jar_peak, max(40, height // 12))
    sauce_band = band_centroids(sauce_peak, max(40, height // 12))
    print("\n  罐子带（x 质心，越小越靠左）：")
    for name in HUE_RANGES:
        cx, count = jar_band[name]
        print(f"    {name}: x={cx:8.1f}  ({count} px)")
    print("  酱/甜甜圈带：")
    for name in HUE_RANGES:
        cx, count = sauce_band[name]
        print(f"    {name}: x={cx:8.1f}  ({count} px)")

    jar_order = sorted(
        (name for name in HUE_RANGES if not np.isnan(jar_band[name][0])),
        key=lambda n: jar_band[n][0],
    )
    sauce_order = sorted(
        (name for name in HUE_RANGES if not np.isnan(sauce_band[name][0])),
        key=lambda n: sauce_band[n][0],
    )
    print(f"\n  罐子带 左→右颜色顺序: {jar_order}")
    print(f"  酱带   左→右颜色顺序: {sauce_order}")
    if len(jar_order) == 4 and jar_order == sauce_order:
        print("  => 颜色顺序一致：**没有交叉**（每个罐子的酱落在同侧、一一对应成立）")
        return 0
    print("  => ⚠ 顺序不一致或颜色缺失：**存在错位/交叉**，或某色没被检出")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
