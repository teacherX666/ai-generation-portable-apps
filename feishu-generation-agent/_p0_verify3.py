"""P0 验证补充 2：水印阳性对照图 + 运动对照图（给视觉模型看的拼图）。

角部边缘密度这种客观指标**不具判别力**（实测 11.25 / 10.34 / 14.12 三档混在一起），
所以改用**阳性对照**：把「确定带小红书水印」的抽帧素材底栏与修复后成片底栏拼在一张
图里放大，让视觉模型指出哪几行有平台水印文字 —— 有阳性对照时它的回答才可信。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SAND = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\sand")
OUT = SAND / "p0_verify"


def bottom_strip(path: Path, height_ratio: float = 0.28) -> Image.Image:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        top = int(height * (1 - height_ratio))
        strip = rgb.crop((0, top, width, height))
        return strip.resize((720, int(strip.height * 720 / strip.width)))


def main() -> None:
    rows: list[tuple[str, Path]] = [
        ("A_REF_FRAME_KNOWN_WATERMARK", SAND / "refkey" / "EXTRACTED_REF_FRAME.jpg"),
        ("B_FIXED_f02", OUT / "frames_fixed" / "f02.png"),
        ("C_FIXED_f06", OUT / "frames_fixed" / "f06.png"),
        ("D_FIXED_f12", OUT / "frames_fixed" / "f12.png"),
    ]
    strips = [(label, bottom_strip(path)) for label, path in rows if path.is_file()]
    row_height = max(strip.height for _, strip in strips)
    sheet = Image.new("RGB", (720, (row_height + 26) * len(strips)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, strip) in enumerate(strips):
        y = index * (row_height + 26)
        draw.text((6, y + 6), label, fill="black")
        sheet.paste(strip, (0, y + 22))
    target = OUT / "watermark_positive_control.png"
    sheet.save(target)
    print("wrote", target, sheet.size)

    # 运动对照：参考视频 4 帧（上排）vs 修复后成片 4 帧（下排）
    ref_frames = sorted((OUT / "frames_refvideo").glob("*.png"))[2:6]
    fixed_frames = sorted((OUT / "frames_fixed").glob("*.png"))[1:5]
    cells = [
        ("REF", path) for path in ref_frames
    ] + [("FIXED", path) for path in fixed_frames]
    thumbs = [
        (label, Image.open(path).convert("RGB").resize((180, 320)))
        for label, path in cells
    ]
    motion = Image.new("RGB", (720, 350), "white")
    draw = ImageDraw.Draw(motion)
    for index, (label, thumb) in enumerate(thumbs):
        x = index * 180
        motion.paste(thumb, (x, 0))
        draw.text((x + 4, 326), f"{label}_{index + 1}", fill="black")
    motion_target = OUT / "motion_ref_vs_fixed.png"
    motion.save(motion_target)
    print("wrote", motion_target, motion.size)


if __name__ == "__main__":
    main()
