"""在图1 上画出候选矩形，交给视觉模型确认「矩形里是否完整包含罐子、有没有切到」。

用法：python _diag/mark_rects.py <输出图> [x0 y0 x1 y1 x0 y0 x1 y1 ...]
    不带坐标时用默认的两个候选簇。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61" / "ref_image-1.png"

DEFAULT = [(0, 1930, 380, 2660), (1040, 1840, 1420, 2520)]


def main() -> int:
    out = Path(sys.argv[1])
    args = [int(v) for v in sys.argv[2:]]
    rects = [tuple(args[i:i + 4]) for i in range(0, len(args), 4)] or DEFAULT
    with Image.open(SCENE) as image:
        canvas = image.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        for index, (x0, y0, x1, y1) in enumerate(rects, 1):
            draw.rectangle((x0, y0, x1, y1), outline=(255, 64, 64), width=8)
            draw.text((x0 + 12, y0 + 8), f"R{index}", fill=(255, 255, 0))
        canvas = canvas.resize((canvas.width // 2, canvas.height // 2), Image.LANCZOS)
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out)
        print("输出:", out, canvas.size, "rects=", rects)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
