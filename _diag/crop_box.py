"""按像素框裁图。用法：python _diag/crop_box.py <输入> <输出> <x0> <y0> <x1> <y1>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    x0, y0, x1, y1 = (int(v) for v in sys.argv[3:7])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        width, height = image.size
        box = (max(0, x0), max(0, y0), min(width, x1), min(height, y1))
        image.crop(box).save(dst)
        print(f"{src.name} {width}x{height} -> {dst} box={box} size={box[2]-box[0]}x{box[3]-box[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
