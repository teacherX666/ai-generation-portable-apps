"""把候选框裁成一张对照图，供视觉模型逐块确认（用于精确定位图1 里的罐子）。

用法：python _diag/make_contact_sheet.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61" / "ref_image-1.png"
OUT = ROOT / "_diag" / "anchor" / "contact_sheet.png"

#: 视觉模型给的候选框（百分比 → 像素会按图1 尺寸换算），括号里是我加的边距说明
BOXES = {
    "A": (0.02, 0.72, 0.12, 0.85),
    "B": (0.12, 0.76, 0.20, 0.86),
    "C": (0.08, 0.82, 0.18, 0.91),
    "D": (0.71, 0.69, 0.80, 0.82),
    "E": (0.72, 0.76, 0.81, 0.86),
}
MARGIN = 0.02


def main() -> int:
    with Image.open(SCENE) as image:
        width, height = image.size
        cells = []
        for label, (x0, y0, x1, y1) in BOXES.items():
            box = (int((x0 - MARGIN) * width), int((y0 - MARGIN) * height),
                   int((x1 + MARGIN) * width), int((y1 + MARGIN) * height))
            box = (max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3]))
            crop = image.crop(box)
            # 统一高度便于并排
            target_h = 520
            crop = crop.resize((max(1, int(crop.width * target_h / crop.height)), target_h), Image.LANCZOS)
            cells.append((label, crop, box))
        total_w = sum(c.width for _, c, _ in cells) + 12 * (len(cells) + 1)
        sheet = Image.new("RGB", (total_w, 560), (24, 24, 32))
        draw = ImageDraw.Draw(sheet)
        x = 12
        for label, crop, box in cells:
            sheet.paste(crop, (x, 30))
            draw.text((x + 4, 8), f"{label}  box={box}", fill=(255, 255, 255))
            x += crop.width + 12
        OUT.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(OUT)
        print("输出:", OUT, sheet.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
