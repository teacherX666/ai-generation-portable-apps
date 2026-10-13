"""P0 验证补充 3：把角部**放大 4 倍**做水印阳性对照。

7B 视觉模型在整帧上读不出水印（诊断报告也记过这次误判），所以先做**可读性阳性对照**：
把确定带水印的抽帧素材左下/右下角放大 4 倍，看模型能不能读出文字；能读出来，
同样处理修复后成片的角部才有判别力。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SAND = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\sand")
OUT = SAND / "p0_verify"


def corner(path: Path, side: str, scale: int = 4) -> Image.Image:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        box_width = int(width * 0.5)
        box_height = int(height * 0.16)
        if side == "left":
            box = (0, height - box_height, box_width, height)
        else:
            box = (width - box_width, height - box_height, width, height)
        crop = rgb.crop(box)
        return crop.resize(
            (crop.width * scale, crop.height * scale), Image.LANCZOS
        )


def main() -> None:
    rows = [
        ("A_REF_KNOWN_WATERMARK_left", SAND / "refkey" / "EXTRACTED_REF_FRAME.jpg", "left"),
        ("B_REF_KNOWN_WATERMARK_right", SAND / "refkey" / "EXTRACTED_REF_FRAME.jpg", "right"),
        ("C_FIXED_f06_left", OUT / "frames_fixed" / "f06.png", "left"),
        ("D_FIXED_f06_right", OUT / "frames_fixed" / "f06.png", "right"),
        ("E_FIXED_f12_left", OUT / "frames_fixed" / "f12.png", "left"),
        ("F_FIXED_f12_right", OUT / "frames_fixed" / "f12.png", "right"),
    ]
    crops = [
        (label, corner(path, side)) for label, path, side in rows if path.is_file()
    ]
    width = max(crop.width for _, crop in crops)
    total = sum(crop.height + 30 for _, crop in crops)
    sheet = Image.new("RGB", (width, total), "white")
    draw = ImageDraw.Draw(sheet)
    y = 0
    for label, crop in crops:
        draw.text((6, y + 8), label, fill="black")
        sheet.paste(crop, (0, y + 28))
        y += crop.height + 30
    target = OUT / f"watermark_zoom_{len(crops)}rows.png"
    sheet.save(target)
    print("wrote", target, sheet.size)


if __name__ == "__main__":
    main()
