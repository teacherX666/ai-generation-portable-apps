"""离线验证压缩：把真实超限参考视频压到本机闸门以内（零成本，不调上游）。

证明：
1. `compress_video` 的输出**时长不变、帧数不变（不抽帧）、分辨率不被放大**；
2. 压完能过 `SeedanceVideoGenerator(max_input_bytes=32MiB)` 的闸门；
3. 逐轮收敛：如果只降码率不够，会降分辨率再试。
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
)

RUN = "13268f86-1522-43f4-ada4-35cdfc72b9e6"
VIDEO = (
    ROOT
    / "data"
    / "runs"
    / RUN
    / "inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4"
)
LIMIT = 32 * 1024 * 1024
STEPS = ((None, 0.92), (1280, 0.88), (854, 0.82))


async def main() -> None:
    before = probe_video_info(VIDEO)
    print("--- 压缩前 ---")
    print(
        json.dumps(
            {
                "size": before.size_bytes,
                "MiB": round(before.size_bytes / 1024 / 1024, 3),
                "wh": f"{before.width}x{before.height}",
                "fps": before.fps,
                "duration": before.duration,
                "mbps": round((before.bitrate_bps or 0) / 1e6, 3),
                "has_audio": before.has_audio,
            },
            ensure_ascii=False,
        )
    )
    with tempfile.TemporaryDirectory(prefix="diag-compress-") as work_dir:
        for index, (max_long_side, ratio) in enumerate(STEPS, start=1):
            out = Path(work_dir) / f"c{index}.mp4"
            result = await asyncio.to_thread(
                compress_video,
                VIDEO,
                LIMIT,
                out,
                max_long_side=max_long_side,
                budget_ratio=ratio,
            )
            if result is None:
                print(f"round {index}: FAILED")
                continue
            info = result.info
            print(f"--- round {index} (long_side={max_long_side}, ratio={ratio}) ---")
            print(
                json.dumps(
                    {
                        "size": info.size_bytes,
                        "MiB": round(info.size_bytes / 1024 / 1024, 3),
                        "under_limit": info.size_bytes <= LIMIT,
                        "wh": f"{info.width}x{info.height}",
                        "fps": info.fps,
                        "duration": info.duration,
                        "duration_delta": round(info.duration - before.duration, 3),
                        "mbps": round((info.bitrate_bps or 0) / 1e6, 3),
                        "video_kbps": result.video_kbps,
                        "has_audio": info.has_audio,
                    },
                    ensure_ascii=False,
                )
            )
            break


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
