"""把图裁成"罐子带 / 甜甜圈带"两条，再用非引导问题问视觉模型，客观核对一一对应。

用法：python _diag/crop_bands.py <图片路径> <输出目录>
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

HUE_RANGES = {
    "红": [(0, 12), (244, 255)],
    "黄": [(22, 52)],
    "蓝": [(140, 186)],
    "紫": [(186, 232)],
}


def main() -> int:
    path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        width, height = image.size
        hsv = np.asarray(image.convert("HSV")).astype(np.int16)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    colored = (sat > 70) & (val > 60)
    print(f"{path.name}: {width}x{height}")

    row_profile = colored.sum(axis=1)
    # 每行"有几种颜色各自成簇"——罐子带/酱带都会同时出现 4 种
    def distinct_color_rows(mask_row: np.ndarray) -> int:
        xs = np.where(mask_row)[0]
        if xs.size < 50:
            return 0
        groups = 1 + int((np.diff(xs) > width * 0.06).sum())
        return groups

    best: list[tuple[int, int]] = []
    for y in range(0, height, 8):
        count = 0
        for ranges in HUE_RANGES.values():
            mask = np.zeros(width, dtype=bool)
            for low, high in ranges:
                mask |= (hue[y] >= low) & (hue[y] <= high)
            mask &= colored[y]
            if mask.sum() > 120:
                count += 1
        best.append((y, count))
    four = [y for y, count in best if count >= 4]
    print("  四色同时出现的行:", (f"{four[0]}-{four[-1]}" if four else "无"))

    top_half = row_profile[: height // 2]
    bottom_half = row_profile[height // 2 :]
    jar_peak = int(np.argmax(top_half))
    sauce_peak = int(np.argmax(bottom_half)) + height // 2
    bands = {
        "罐子带": (jar_peak, max(90, height // 14)),
        "甜甜圈带": (sauce_peak, max(90, height // 14)),
    }
    for name, (center, half) in bands.items():
        low, high = max(0, center - half), min(height, center + half)
        crop = Image.open(path).crop((0, low, width, high))
        target = out_dir / f"{path.stem}_{name}.png"
        crop.save(target)
        print(f"  {name}: y {low}-{high} -> {target}")
    # 一整条竖版也给一张（方便看整体对应）
    print("  峰位:", jar_peak, sauce_peak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
