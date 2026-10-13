"""只读证据表：最近所有带**参考视频**的 run → 视频编码/规格 → 该 run 的结果。

用来判定 `invalid_media` 的根因是不是「素材编码上游不认」（H.265/HEVC）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from feishu_generation_agent.integrations.video_reference import (  # noqa: E402
    probe_video_info,
)

_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")
CODECS = None


def video_codec(path: Path) -> str:
    import subprocess

    from feishu_generation_agent.integrations.video_reference import _binary

    try:
        out = subprocess.run(
            [
                _binary("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,profile",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        stream = json.loads(out)["streams"][0]
        return f"{stream.get('codec_name')}/{stream.get('profile')}"
    except Exception as exc:  # noqa: BLE001
        return f"<读不出:{type(exc).__name__}>"


con = sqlite3.connect(ROOT / "data" / "agent.sqlite3")
con.row_factory = sqlite3.Row
ccon = sqlite3.connect(ROOT / "data" / "checkpoints.sqlite3")
ccon.row_factory = sqlite3.Row

runs = con.execute(
    "select * from runs order by created_at desc limit 60"
).fetchall()

print(
    f"{'run':10} {'created':16} {'status':22} {'产物':5} "
    f"{'参考视频编码':18} {'规格':26} {'role'}"
)
print("-" * 120)
for run in runs:
    thread = run["thread_id"]
    plans = ""
    assets = ""
    for row in ccon.execute(
        "select channel, value from writes where thread_id=? "
        "order by checkpoint_id, idx",
        (thread,),
    ):
        if row["channel"] not in {"media_assets", "approved_tasks"}:
            continue
        blob = row["value"]
        if not isinstance(blob, bytes):
            continue
        text = _PRINTABLE.sub(" ", blob.decode("utf-8", "replace"))
        if row["channel"] == "media_assets":
            assets = text
        else:
            plans = text
    if "reference_video" not in plans:
        continue
    video_ids = {
        match.group(1)
        for match in re.finditer(
            r"asset_id (\S+) role reference_video", plans.replace("  ", " ")
        )
    }
    video_ids |= {
        match.group(1)
        for match in re.finditer(r"(\S+) reference_video", plans)
    }
    chunks = re.split(r"asset_id ", assets)
    found = []
    for chunk in chunks[1:]:
        asset_id = chunk.split(" ", 1)[0]
        mime = re.search(r"mime_type (\S+)", chunk)
        path = re.search(r"local_path (\S+)", chunk)
        if not mime or "video/" not in mime.group(1):
            continue
        local = path.group(1).lstrip("q") if path else ""
        candidate = ROOT / local.replace("\\", "/") if local else None
        exists = bool(candidate and candidate.is_file())
        codec = video_codec(candidate) if exists else "<文件不在>"
        info = probe_video_info(candidate) if exists else None
        spec = (
            f"{info.width}x{info.height} {info.fps:.1f}fps {info.duration:.1f}s "
            f"{(info.bitrate_bps or 0)/1e6:.1f}Mbps"
            if info
            else "-"
        )
        found.append((asset_id, codec, spec))
    artifacts = con.execute(
        "select count(*) from artifacts where run_id=?", (run["run_id"],)
    ).fetchone()[0]
    err = ""
    if "invalid_media" in assets.lower():
        err = " [blob含 invalid_media]"
    for index, (asset_id, codec, spec) in enumerate(found):
        print(
            f"{run['run_id'][:8]:10} {run['created_at'][5:16]:16} "
            f"{run['status'][:21]:22} {artifacts:<5} {codec:18} {spec:26} "
            f"{asset_id}{err if index == 0 else ''}"
        )
    if not found:
        print(
            f"{run['run_id'][:8]:10} {run['created_at'][5:16]:16} "
            f"{run['status'][:21]:22} {artifacts:<5} {'<素材未落盘>':18}"
        )
sys.stdout.flush()
