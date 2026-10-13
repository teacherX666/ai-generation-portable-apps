"""P0 验证补充：客观角部水印检查 + 与修复前各版的运动/紫色对比。

不依赖视觉模型（诊断报告里视觉模型对水印有过误判），只用像素：
- 角部边缘密度：平台水印是**后期叠加**的边缘锐利小块；被污染的那版会把水印复现出来。
- 与 step7_pixel.json（修复前 6 版）的紫色序列/逐帧差异并排对照。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageStat

SAND = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\sand")
OUT = SAND / "p0_verify"


def corner_penalty(path: Path) -> float:
    with Image.open(path) as image:
        grey = image.convert("L")
        width, height = grey.size
        box_width = max(8, width // 4)
        box_height = max(8, height // 6)
        boxes = (
            (0, height - box_height, box_width, height),
            (width - box_width, height - box_height, width, height),
        )
        return max(
            float(
                ImageStat.Stat(
                    grey.crop(box).filter(ImageFilter.FIND_EDGES)
                ).mean[0]
            )
            for box in boxes
        )


def main() -> None:
    report: dict[str, object] = {}

    reference = SAND / "refkey" / "EXTRACTED_REF_FRAME.jpg"
    fixed = sorted((OUT / "frames_fixed").glob("*.png"))
    polluted = sorted((SAND / "frames" / "r3_4154013f(video)").glob("*.png"))
    clean = sorted((SAND / "frames" / "r1_9bc3fa4b(no-video)").glob("*.png"))

    def series(paths):
        return [round(corner_penalty(p), 2) for p in paths]

    report["ref_frame_with_xiaohongshu_watermark"] = round(
        corner_penalty(reference), 2
    )
    report["FIXED_corner_penalty"] = series(fixed)
    report["BEFORE_video_polluted_corner_penalty"] = series(polluted)
    report["BEFORE_no_video_corner_penalty"] = series(clean)
    report["fixed_mean"] = round(float(np.mean(series(fixed))), 2)
    report["polluted_mean"] = round(float(np.mean(series(polluted))), 2)
    report["no_video_mean"] = round(float(np.mean(series(clean))), 2)

    baseline = json.loads((SAND / "step7_pixel.json").read_text(encoding="utf-8"))
    fixed_pixel = json.loads((OUT / "pixel_fixed.json").read_text(encoding="utf-8"))
    comparison = {}
    for key, value in baseline.items():
        if key.startswith("_"):
            continue
        comparison[key] = {
            "purple": value["purple_magenta_series"],
            "max_frame_diff": value["max_frame_diff"],
            "mean_frame_diff": value["mean_frame_diff"],
        }
    comparison["FIXED_f28f740b(video,reference_video直传)"] = {
        "purple": fixed_pixel["purple_magenta_series"],
        "max_frame_diff": fixed_pixel["max_frame_diff"],
        "mean_frame_diff": round(
            float(np.mean(fixed_pixel["frame_diffs"])), 2
        ),
    }
    report["comparison"] = comparison

    (OUT / "watermark_and_motion_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
