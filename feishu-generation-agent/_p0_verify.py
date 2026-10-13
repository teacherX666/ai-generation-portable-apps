"""P0 验证：对成片抽帧做与诊断报告同一口径的客观像素统计 + 生成对比图。

与 `_quality_analysis/sand/q_pixel.py` 完全同一套口径（HSV 紫色/粉红/黄绿/木色占比、
逐帧 L1 差异），这样「修复前 0.17→0.46 vs 修复后」才是可比的。

用法：.venv\\Scripts\\python.exe _p0_verify.py
"""

from __future__ import annotations

import glob
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SAND = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\sand")
OUT = SAND / "p0_verify"


def _binary(name: str) -> str:
    """ffmpeg/ffprobe 定位（与 integrations/video_reference.py 同一套候选路径）。

    本脚本要用**带 numpy 的解释器**跑（仓库 .venv 里没有 numpy），所以不 import 仓库
    模块，把这 5 行复制过来。
    """
    import shutil

    resolved = shutil.which(name)
    if resolved:
        return resolved
    candidates = (
        f"C:/ProgramData/chocolatey/bin/{name}.exe",
        f"C:/ffmpeg/bin/{name}.exe",
        f"C:/Program Files/ffmpeg/bin/{name}.exe",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit(f"{name} not available")


def load(path: Path, size=(180, 320)):
    image = Image.open(path).convert("RGB").resize(size, Image.BILINEAR)
    return np.asarray(image, dtype=np.float32)


def hue_stats(arr):
    a = arr.reshape(-1, 3) / 255.0
    mx = a.max(axis=1)
    mn = a.min(axis=1)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    r, g, b = a[:, 0], a[:, 1], a[:, 2]
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    idx = m & (mx == r)
    h[idx] = ((g[idx] - b[idx]) / d[idx]) % 6
    idx = m & (mx == g)
    h[idx] = ((b[idx] - r[idx]) / d[idx]) + 2
    idx = m & (mx == b)
    h[idx] = ((r[idx] - g[idx]) / d[idx]) + 4
    h = h * 60.0
    sat_ok = (s > 0.18) & (v > 0.15)

    def frac(lo, hi):
        sel = sat_ok & ((h >= lo) & (h < hi) if lo <= hi else (h >= lo) | (h < hi))
        return float(sel.mean())

    return {
        "purple_magenta_260_330": frac(260, 330),
        "pink_330_350": frac(330, 350),
        "red_350_20": frac(350, 20),
        "yellowgreen_40_160": frac(40, 160),
        "wood_orange_20_45": frac(20, 45),
        "blue_200_260": frac(200, 260),
        "sat_frac": float(sat_ok.mean()),
        "mean_rgb": [float(x) for x in arr.reshape(-1, 3).mean(axis=0)],
    }


def probe(path: Path) -> dict:
    ffprobe = _binary("ffprobe")
    out = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries",
            "stream=width,height,duration,codec_type",
            "-show_entries", "format=duration,size",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out)


def contact_sheet(frames: list[Path], target: Path, cols: int = 4) -> None:
    thumbs = [Image.open(f).convert("RGB").resize((180, 320)) for f in frames]
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 180, rows * 340), "white")
    draw = ImageDraw.Draw(sheet)
    for index, thumb in enumerate(thumbs):
        x = (index % cols) * 180
        y = (index // cols) * 340
        sheet.paste(thumb, (x, y))
        draw.text((x + 4, y + 322), f"f{index + 1:02d}", fill="black")
    sheet.save(target)


def main() -> None:
    videos = sorted(glob.glob(str(OUT / "OUTPUT_*.mp4")))
    if not videos:
        raise SystemExit("no OUTPUT_*.mp4 in p0_verify")
    video = Path(videos[-1])
    print("video:", video, video.stat().st_size, "bytes")
    meta = probe(video)
    print("meta:", json.dumps(meta, ensure_ascii=False))

    frames_dir = OUT / "frames_fixed"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    subprocess.run(
        [
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vf", "fps=1.5,scale=360:-1",
            "-y", str(frames_dir / "f%02d.png"),
        ],
        check=True,
        timeout=300,
    )
    frames = sorted(frames_dir.glob("*.png"))
    print("frames:", len(frames))

    frames_arrays = [load(f) for f in frames]
    per = []
    for arr in frames_arrays:
        h, _, _ = arr.shape
        per.append(hue_stats(arr[int(h * 0.25):, :, :]))  # 中下部 = 主体区
    purple = [round(item["purple_magenta_260_330"], 4) for item in per]
    pinkred = [round(item["pink_330_350"] + item["red_350_20"], 4) for item in per]
    diffs = [
        round(float(np.abs(frames_arrays[i] - frames_arrays[i - 1]).mean()), 2)
        for i in range(1, len(frames_arrays))
    ]
    result = {
        "video": str(video),
        "size_bytes": video.stat().st_size,
        "meta": meta,
        "n_frames": len(frames),
        "purple_magenta_series": purple,
        "pink_red_series": pinkred,
        "yellowgreen_series": [
            round(item["yellowgreen_40_160"], 4) for item in per
        ],
        "wood_series": [round(item["wood_orange_20_45"], 4) for item in per],
        "mean_rgb_first": [round(x, 1) for x in per[0]["mean_rgb"]],
        "mean_rgb_last": [round(x, 1) for x in per[-1]["mean_rgb"]],
        "frame_diffs": diffs,
        "max_frame_diff": max(diffs) if diffs else None,
        "purple_jump_before_4s": purple[:6],
        "purple_jump_after_4s": purple[6:],
    }
    (OUT / "pixel_fixed.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=1))
    contact_sheet(frames, OUT / "sheet_FIXED.png")

    # 关键对比图：抽帧素材（修复前被当参考图的那张） vs 修复后成片首/尾帧
    ref_frame = SAND / "refkey" / "EXTRACTED_REF_FRAME.jpg"
    if ref_frame.is_file():
        pairs = [("EXTRACTED_REF_FRAME", ref_frame)] + [
            (f"FIXED_{frames[i].stem}", frames[i])
            for i in (0, len(frames) // 2, len(frames) - 1)
        ]
        thumbs = [
            (label, Image.open(p).convert("RGB").resize((180, 320)))
            for label, p in pairs
        ]
        sheet = Image.new("RGB", (len(thumbs) * 180, 350), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (label, thumb) in enumerate(thumbs):
            sheet.paste(thumb, (index * 180, 0))
            draw.text((index * 180 + 4, 326), label, fill="black")
        sheet.save(OUT / "compare_extractedframe_vs_FIXED.png")
        print("wrote", OUT / "compare_extractedframe_vs_FIXED.png")

    # 参考视频本身的首/中/尾帧，用于人眼比对运动与主体
    ref_video = Path(
        r"data\runs\4154013f-7d69-4825-86a3-8f234609c713\inputs"
        r"\367ea9fb04383a4ef1328a0b559104d454516777e9ef4b5ef195f8b379131bbc.mp4"
    )
    if ref_video.is_file():
        ref_frames_dir = OUT / "frames_refvideo"
        ref_frames_dir.mkdir(parents=True, exist_ok=True)
        for old in ref_frames_dir.glob("*.png"):
            old.unlink()
        subprocess.run(
            [
                _binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-i", str(ref_video), "-vf", "fps=1.5,scale=360:-1",
                "-y", str(ref_frames_dir / "r%02d.png"),
            ],
            check=True,
            timeout=600,
        )
        ref_frames = sorted(ref_frames_dir.glob("*.png"))
        print("ref frames:", len(ref_frames))
        contact_sheet(ref_frames[:12], OUT / "sheet_REFVIDEO_first12.png")
        contact_sheet(ref_frames[-12:], OUT / "sheet_REFVIDEO_last12.png")


if __name__ == "__main__":
    main()
