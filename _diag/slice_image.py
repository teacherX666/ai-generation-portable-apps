"""把整张图按横条切片，便于逐条用视觉模型定位物体（罐子/甜甜圈/酱流）。

用法：python _diag/slice_image.py <图片路径> <输出目录> [条数]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        width, height = image.size
        step = height // count
        for index in range(count):
            top = index * step
            bottom = height if index == count - 1 else (index + 1) * step
            crop = image.crop((0, top, width, bottom))
            target = out_dir / f"slice{index:02d}_y{top}-{bottom}.png"
            crop.save(target)
            print(f"{target}  ({width}x{bottom - top})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
