"""只读：失败 run 的 media_assets 原文 + 磁盘上对应素材的真实参数（ffprobe）。"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from feishu_generation_agent.integrations.video_reference import (  # noqa: E402
    probe_video_info,
)

ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
ccon.row_factory = sqlite3.Row
_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")

THREADS = {
    "fa2fae44": "ca4486f3-954e-4f36-bf1a-b5ed668b96ed",
    "d92a4c4b": "618008b8-6a0f-490a-934e-c9ef0df82d1e",
}

for label, thread in THREADS.items():
    print("=" * 78)
    print(label, thread)
    text = ""
    for row in ccon.execute(
        "select value from writes where thread_id=? and channel='media_assets' "
        "order by checkpoint_id, idx",
        (thread,),
    ):
        blob = row["value"]
        if isinstance(blob, bytes):
            text = _PRINTABLE.sub(" ", blob.decode("utf-8", "replace"))
    print(text[:2500])
    for match in re.finditer(
        r"(asset_id|local_path|file_token|mime_type|size|origin|width|height) "
        r"([^ ]+)",
        text,
    ):
        key, value = match.group(1), match.group(2)
        if key in {"local_path", "size", "mime_type", "origin", "asset_id"}:
            print(f"   {key} = {value}")
sys.stdout.flush()

print("=" * 78)
print("磁盘上相关素材（data/runs 下最近修改的视频）")
videos = sorted(
    (ROOT / "data" / "runs").rglob("*.mp4"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)[:8]
for path in videos:
    info = probe_video_info(path)
    print(
        f"{path.relative_to(ROOT)} size={path.stat().st_size} "
        + (
            f"{info.width}x{info.height} fps={info.fps} dur={info.duration:.2f} "
            f"bitrate={info.bitrate_bps} audio={info.has_audio}"
            if info
            else "**ffprobe 读不出**"
        )
    )
    # 容器 magic
    head = path.read_bytes()[:32]
    print("    head:", head[:24])
sys.stdout.flush()
