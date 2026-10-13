"""只读：把失败 run 用到的**参考素材**（asset_id / mime / size / origin）抠出来。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
ccon.row_factory = sqlite3.Row
_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")

THREADS = {
    "fa2fae44": "ca4486f3-954e-4f36-bf1a-b5ed668b96ed",
    "d92a4c4b": "618008b8-6a0f-490a-934e-c9ef0df82d1e",
    "a342abe6": "a41d0987-e536-45f7-aa55-6114b40f754d",
}

for label, thread in THREADS.items():
    print("=" * 78)
    print(label, thread)
    best: dict[str, str] = {}
    for row in ccon.execute(
        "select channel, value from writes where thread_id=? "
        "order by checkpoint_id, idx",
        (thread,),
    ):
        channel = row["channel"]
        if channel not in {
            "media_assets",
            "approved_tasks",
            "task_plan",
            "approved_plan",
        }:
            continue
        blob = row["value"]
        if not isinstance(blob, bytes):
            continue
        text = _PRINTABLE.sub(" ", blob.decode("utf-8", "replace"))
        best[channel] = text
    for channel, text in best.items():
        if channel == "media_assets":
            for match in re.finditer(
                r"asset_id ([^\s]+) source_block_id ([^\s]+) origin ([^\s]+)"
                r"[^\n]{0,200}?mime_type ([^\s]+) size (\d+)",
                text,
            ):
                print("  asset:", match.group(1), match.group(4), match.group(5), "origin=", match.group(3))
            # 兜底：直接找 mime_type
            for match in re.finditer(
                r"asset_id ([^\s]+)[^@]{0,160}?mime_type ([^\s]+) size (\d+)",
                text,
            ):
                print("  asset?", match.group(1), match.group(2), match.group(3))
        else:
            hits = re.findall(
                r"(reference_video|reference_image|first_frame|last_frame|"
                r"reference_audio|video_provider|seedance[0-9.]*)",
                text,
            )
            print(f"  {channel}: tokens=", sorted(set(hits)))
            for match in re.finditer(r"asset_id ([^\s]+)", text):
                print(f"    {channel} 引用 ->", match.group(1))
sys.stdout.flush()
