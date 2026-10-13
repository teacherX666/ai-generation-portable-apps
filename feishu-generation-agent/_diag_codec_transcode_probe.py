"""离线实测：把上游拒掉的 **HEVC 4K** 参考视频转成 H.264（零成本，不调上游）。

证据点：
1. 产物编码 h264（上游认的那个）；
2. **分辨率/帧率/时长全部保留**（这样就能把"4K/60fps 是不是也犯规"这个变量排除掉
   —— 真机 e2e 若通过，说明根因就是编码）；
3. 能完整解码（`verify_reference_video` 自检通过）；
4. 体积没有离谱地膨胀。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from feishu_generation_agent.integrations.video_reference import (  # noqa: E402
    compress_video,
    probe_video_info,
    verify_reference_video,
)

HEVC = (
    ROOT
    / "data/runs/PmN5dYXLEoa0gAxz3CjcnPXKnKf/inputs"
    / "59149a36afd22f2381c6284cc643241157b7322c694d8bb6b8b6c81e38c10147.mp4"
)
H264_BIG = (
    ROOT
    / "data/runs/13268f86-1522-43f4-ada4-35cdfc72b9e6/inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4"
)


def _describe(label: str, path: Path) -> dict:
    info = probe_video_info(path)
    if info is None:
        return {"label": label, "readable": False}
    return {
        "label": label,
        "size": info.size_bytes,
        "MiB": round(info.size_bytes / 1024 / 1024, 3),
        "codec": info.codec,
        "wh": f"{info.width}x{info.height}",
        "fps": round(info.fps, 3) if info.fps else None,
        "duration": round(info.duration, 3),
        "mbps": round((info.bitrate_bps or 0) / 1e6, 3),
        "has_audio": info.has_audio,
    }


async def main() -> None:
    cases = (
        ("HEVC 4K60（上游拒的那条）→ 只转码，不压体积", HEVC, None, 20, None, None),
        ("H.264 34MiB（撞本机闸门的那条）→ 压到 32MiB 内", H264_BIG, 32 * 1024 * 1024, None, None, 0.92),
    )
    for label, source, max_bytes, crf, max_long_side, ratio in cases:
        print("=" * 78)
        print(label)
        before = _describe("before", source)
        print(json.dumps(before, ensure_ascii=False))
        with tempfile.TemporaryDirectory(prefix="diag-codec-") as work_dir:
            out = Path(work_dir) / "out.mp4"
            result = await asyncio.to_thread(
                compress_video,
                source,
                max_bytes,
                out,
                crf=crf,
                max_long_side=max_long_side,
                budget_ratio=ratio if ratio else 0.92,
            )
            if result is None:
                print("  ❌ compress_video 返回 None（自检未过或不达标）")
                continue
            after = _describe("after", out)
            print(json.dumps(after, ensure_ascii=False))
            info = probe_video_info(out)
            print(
                "  自检:",
                verify_reference_video(
                    out,
                    __import__(
                        "feishu_generation_agent.integrations.video_reference",
                        fromlist=["CompressedVideoCheck"],
                    ).CompressedVideoCheck(
                        duration=probe_video_info(source).duration,
                        width=info.width,
                        height=info.height,
                        fps=info.fps,
                        codec="h264",
                    ),
                ),
                "| 分辨率保留:",
                (info.width, info.height)
                == (probe_video_info(source).width, probe_video_info(source).height),
                "| 帧率保留:",
                abs((info.fps or 0) - (probe_video_info(source).fps or 0)) < 0.5,
                "| 时长保留:",
                abs(info.duration - probe_video_info(source).duration) < 0.3,
            )
            print("  大小是否 ≤ 目标:", max_bytes is None or info.size_bytes <= max_bytes)


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
