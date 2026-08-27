from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True, slots=True)
class ExtractedVideoFrame:
    index: int
    path: Path
    mime_type: str = "image/jpeg"


def _binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    for candidate in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        path = Path(candidate)
        if path.is_file():
            return str(path)
    raise RuntimeError(f"{name} is not available")


def _video_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            _binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    value = result.stdout.strip()
    try:
        duration = float(value)
    except ValueError as exc:
        raise RuntimeError("ffprobe returned an invalid duration") from exc
    if duration <= 0:
        raise RuntimeError("ffprobe returned a non-positive duration")
    return duration


def extract_video_frames(
    video_path: Path,
    frame_count: int,
    output_dir: Path,
) -> list[Path]:
    """抽取视频中间均匀分布的 N 帧，返回 JPEG 文件路径。

    帧采样点取 duration * (i + 1) / (frame_count + 1)，避免抽到黑帧/片头片尾。
    任何一帧失败都整体失败：视频参考要么可读，要么明确走失败分支，避免拿
    半截帧列表继续规划。
    """
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = _video_duration(video_path)
    paths: list[Path] = []
    ffmpeg = _binary("ffmpeg")
    for index in range(frame_count):
        timestamp = duration * (index + 1) / (frame_count + 1)
        output_path = output_dir / f"frame-{index + 1:02d}.jpg"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no frame")
        paths.append(output_path)
    return paths
