"""为「拼图式参考图」定位素材：图1 里的罐子、图3 里四只淋好酱的甜甜圈。

原理：图3 背景是纯粉、图1 桌面是木色，彩色物体用色相+饱和度就能切出来。
输出每块的包围盒（打印用），并可裁出预览图人工/视觉模型核对。

用法：python _diag/locate_assets.py [--crop]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FIG1 = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61" / "ref_image-1.png"
FIG3 = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61" / "ref_image-3.png"
OUT = ROOT / "_diag" / "anchor" / "assets"

RANGES = {  # PIL hue 0-255
    "红": [(0, 12), (240, 255)],
    "黄": [(20, 52)],
    "蓝": [(140, 186)],
    "紫": [(186, 238)],
}


def mask_colors(hsv: np.ndarray, sat_min=80, val_min=70) -> np.ndarray:
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    out = np.zeros(hue.shape, dtype=bool)
    for ranges in RANGES.values():
        for low, high in ranges:
            out |= (hue >= low) & (hue <= high)
    return out & (sat > sat_min) & (val > val_min)


def blobs(mask: np.ndarray, min_px: int = 800) -> list[tuple[int, int, int, int, int]]:
    """极简连通域（按行扫描 + 队列），返回 (x0,y0,x1,y1,px)。"""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    found: list[tuple[int, int, int, int, int]] = []
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            if not mask[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            x0 = x1 = x
            y0 = y1 = y
            count = 0
            while stack:
                cy, cx = stack.pop()
                count += 1
                x0, x1 = min(x0, cx), max(x1, cx)
                y0, y1 = min(y0, cy), max(y1, cy)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if count >= min_px:
                found.append((x0, y0, x1, y1, count))
    found.sort(key=lambda b: -b[4])
    return found


def main() -> int:
    do_crop = "--crop" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)

    for label, path in (("图1", FIG1), ("图3", FIG3)):
        with Image.open(path) as image:
            width, height = image.size
            hsv = np.asarray(image.convert("HSV")).astype(np.int16)
        mask = mask_colors(hsv)
        print(f"\n{label} {path.name} {width}x{height}  彩色像素 {mask.sum():,}")
        for index, (x0, y0, x1, y1, count) in enumerate(blobs(mask)[:12], 1):
            print(
                f"  #{index} box=({x0},{y0})-({x1},{y1}) "
                f"size={x1-x0}x{y1-y0} px={count:,} "
                f"横向 {x0/width:.0%}-{x1/width:.0%} 纵向 {y0/height:.0%}-{y1/height:.0%}"
            )
            if do_crop:
                target = OUT / f"{path.stem}_blob{index}.png"
                with Image.open(path) as image:
                    image.crop((x0, y0, x1 + 1, y1 + 1)).save(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
