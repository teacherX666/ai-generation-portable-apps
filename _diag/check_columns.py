"""纯像素检查「四列是否对齐」：罐子行与酱行里，四种颜色的左右顺序是否一致。

不依赖视觉模型：把每一行的彩色像素按 x 间隔聚成若干列，取每列的主色与 x 质心，
分别找"罐子行"（上半区有 4 列的那一行）和"酱行"（下半区有 4 列的那一行），
比较两行的颜色顺序。顺序不一致 = 错位/交叉。

用法：python _diag/check_columns.py <图片路径> [--rows]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

HUE_RANGES = {  # PIL 的 H：0-255
    "红": [(0, 12), (244, 255)],
    "黄": [(22, 52)],
    "蓝": [(140, 186)],
    "紫": [(186, 232)],
}
COLOR_ORDER = ["红", "黄", "蓝", "紫"]


def color_of(hue_values: np.ndarray) -> str:
    counts = {}
    for name, ranges in HUE_RANGES.items():
        mask = np.zeros_like(hue_values, dtype=bool)
        for low, high in ranges:
            mask |= (hue_values >= low) & (hue_values <= high)
        counts[name] = int(mask.sum())
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "?"


def row_columns(hue: np.ndarray, sat: np.ndarray, val: np.ndarray, y: int) -> list[tuple[str, float, int]]:
    colored = (sat[y] > 70) & (val[y] > 60)
    xs = np.where(colored)[0]
    if xs.size < 30:
        return []
    width = hue.shape[1]
    gap = max(8, width // 60)
    groups: list[list[int]] = [[xs[0]]]
    for prev, cur in zip(xs, xs[1:]):
        if cur - prev > gap:
            groups.append([])
        groups[-1].append(cur)
    out: list[tuple[str, float, int]] = []
    for group in groups:
        if len(group) < 12:
            continue
        index = np.array(group)
        out.append((color_of(hue[y, index]), float(index.mean()), len(group)))
    return out


def scan(path: Path, verbose: bool = False) -> None:
    with Image.open(path) as image:
        width, height = image.size
        hsv = np.asarray(image.convert("HSV")).astype(np.int16)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    print(f"\n{path.name}  {width}x{height}")

    best_upper: tuple[int, list] | None = None
    best_lower: tuple[int, list] | None = None
    for y in range(0, height, 6):
        cols = row_columns(hue, sat, val, y)
        if len(cols) != 4:
            continue
        colors = [c[0] for c in cols]
        # 四列颜色互不相同才算"一行里同时出现四色"
        if len(set(colors)) < 4:
            continue
        if verbose:
            print(f"  y={y:>5d} " + " | ".join(f"{c[0]}@{c[1]:.0f}({c[2]})" for c in cols))
        if y < height // 2:
            best_upper = (y, cols)
        else:
            best_lower = (y, cols)

    for label, found in (("上半区（罐子带）", best_upper), ("下半区（酱/甜甜圈带）", best_lower)):
        if not found:
            print(f"  {label}: 没找到「四色四列」的行")
            continue
        y, cols = found
        cols = sorted(cols, key=lambda c: c[1])
        print(f"  {label}: y={y}")
        for color, cx, count in cols:
            print(f"      {color}  x={cx:7.1f}  ({count} px)")
        print(f"      → 左到右顺序: {[c[0] for c in cols]}")

    if not (best_upper and best_lower):
        return
    upper_order = [c[0] for c in sorted(best_upper[1], key=lambda c: c[1])]
    lower_order = [c[0] for c in sorted(best_lower[1], key=lambda c: c[1])]
    upper_x = [round(c[1]) for c in sorted(best_upper[1], key=lambda c: c[1])]
    lower_x = [round(c[1]) for c in sorted(best_lower[1], key=lambda c: c[1])]
    print(f"  罐子列 x: {upper_x}")
    print(f"  酱列   x: {lower_x}")
    ok = upper_order == lower_order
    print("  结论:", "顺序一致 → 一一对应成立（没有交叉）" if ok else "**顺序不一致 → 存在错位/交叉**")


def main() -> int:
    verbose = "--rows" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            continue
        scan(Path(arg), verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
