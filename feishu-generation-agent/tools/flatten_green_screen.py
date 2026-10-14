"""绿幕归一：把成片里的绿幕区域**强制**成同一纯色（确定性，不靠模型）。

**为什么要它**（2026-10-14）：用户报「刮奶油的视频绿幕会出现阴影等影响抠图的东西」。
提示词里补了物理约束（`planner._ensure_green_screen_constraints`）+ 长期口径种子之后，
**今天新生成的那条（run `296a79bc`）依然有阴影** —— 文字约束到头了。绿幕区就算有阴影/
渐变，那些像素**仍然是偏绿的**，所以可以在成片侧把它归一：把「与背景连通的大片绿色区域」
整片刷成同一个色值，阴影、暗角、渐变全部被吃掉；主体上的绿色（糖豆、绿美甲）靠
**连通域面积**排除，不会被误刷。

用法：
    python _diag/flatten_green_screen.py <in.mp4> [--out x.mp4] [--color 00B140]
                                          [--min-area-ratio 0.002] [--preview-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

FFMPEG = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffprobe.exe"

#: OpenCV 的 H 是 0~179（360° 折半）。绿幕大致落在 35~85；再靠饱和度/亮度排除灰暗像素。
_HUE_LOW, _HUE_HIGH = 35, 90
_SAT_MIN = 60
_VAL_MIN = 40


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    return int(stream["width"]), int(stream["height"]), float(num) / float(den)


def read_frames(path: Path, width: int, height: int) -> np.ndarray:
    """整个视频读进内存（720×1280×8s ≈ 0.5GB 以内，够用）。"""
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    buffer = np.frombuffer(proc.stdout, dtype=np.uint8)
    frame_size = width * height * 3
    count = len(buffer) // frame_size
    return buffer[: count * frame_size].reshape(count, height, width, 3)


def background_mask(frame: np.ndarray, min_area_ratio: float) -> np.ndarray:
    """绿幕掩膜：偏绿 + **够大**（连通到画面边缘的大片）—— 小绿点（糖豆/绿美甲）不动。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(
        hsv,
        (int(_HUE_LOW * 255 / 180), _SAT_MIN, _VAL_MIN),
        (int(_HUE_HIGH * 255 / 180), 255, 255),
    )
    if not mask.any():
        return np.zeros(mask.shape, dtype=bool)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    height, width = mask.shape
    min_area = max(64, int(height * width * min_area_ratio))
    keep = np.zeros(count, dtype=bool)
    for label in range(1, count):
        area = stats[label, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        left, top = stats[label, cv2.CC_STAT_LEFT], stats[label, cv2.CC_STAT_TOP]
        w, h = stats[label, cv2.CC_STAT_WIDTH], stats[label, cv2.CC_STAT_HEIGHT]
        touches_border = left <= 1 or top <= 1 or left + w >= width - 1 or top + h >= height - 1
        # 贴着画面边缘的大片 = 绿幕；画面中间的大片（≥4% 面积）也算（揭幕初期还没到边缘）。
        if touches_border or area >= height * width * 0.04:
            keep[label] = True
    return keep[labels]


def despill(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """去绿溢色：把**主体侧**「绿比红蓝都高」的像素的绿压到红蓝均值。

    2026-10-14 用户：「现在这般效果还是较差」—— 归一只解决**背景不平**；抠图成品发绿的主因是
    **主体边缘的绿溢色**（绿幕的光打到主体边缘）。标准 chroma key 的这一步必须补上。

    只动主体侧（`~mask`）：绿幕本身也是绿的，一起压会把标准绿 #00B140 压成 (0,64,64)，
    背景就不纯了（第一版踩过）。
    """
    work = frame.astype(np.int16)
    red, green, blue = work[:, :, 0], work[:, :, 1], work[:, :, 2]
    limit = np.maximum(red, blue)
    spill = (green > limit) & (~mask)
    work[:, :, 1] = np.where(spill, limit, green)
    return np.clip(work, 0, 255).astype(np.uint8)


def flatten(frames: np.ndarray, color: tuple[int, int, int], min_area_ratio: float) -> np.ndarray:
    out = frames.copy()
    for index in range(frames.shape[0]):
        mask = background_mask(frames[index], min_area_ratio)
        if mask.any():
            out[index][mask] = color
            # 背景刷完再对**主体侧**去溢色（顺序不能反：反了会把纯色背景也压暗）。
            out[index] = despill(out[index], mask)
    return out


def write_video(
    frames: np.ndarray, path: Path, fps: float, source: Path | None = None
) -> None:
    """写归一后的视频。**必须把原片音轨带过去**（`source` 给定时 `-c:a copy` 原样复制）。

    2026-10-14 事故：第一版写死了 `-an`，于是**所有绿幕视频的音轨被整条丢掉** ——
    用户报「为什么泡沫爆炸没有声音」。视频提示词明确要了音效（「每颗泡沫破裂时伴一声
    清脆的『啵』」），结果交付的是无声片。**任何重编码步骤都要显式保留音频**。
    """
    height, width = frames.shape[1], frames.shape[2]
    command = [
        FFMPEG, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "-",
    ]
    if source is not None:
        # 原片作为第二路输入：只取它的音轨，**不重编码**（`-c:a copy`）。
        command += ["-i", str(source), "-map", "0:v", "-map", "1:a?", "-c:a", "copy", "-shortest"]
    command += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    proc.stdin.write(frames.astype(np.uint8).tobytes())
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"写视频失败 returncode={proc.returncode}")


def green_stats(frame: np.ndarray, min_area_ratio: float) -> dict:
    """绿幕区域的颜色离散度（抠图干不干净就看这个：std 越小越干净）。"""
    mask = background_mask(frame, min_area_ratio)
    if not mask.any():
        return {"pixels": 0}
    region = frame[mask].astype(np.float32)
    return {
        "pixels": int(mask.sum()),
        "mean": [round(float(x), 1) for x in region.mean(axis=0)],
        "std": [round(float(x), 2) for x in region.std(axis=0)],
        "range": [int(region[:, i].max() - region[:, i].min()) for i in range(3)],
    }


def save_preview(before: np.ndarray, after: np.ndarray, index: int, path: Path) -> None:
    from PIL import Image

    a = Image.fromarray(before[index])
    b = Image.fromarray(after[index])
    canvas = Image.new("RGB", (a.width * 2 + 12, a.height), (255, 255, 255))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + 12, 0))
    canvas.save(path)


def audio_streams(path: Path) -> int:
    """这个文件有几条音轨。"""
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return len([line for line in completed.stdout.splitlines() if line.strip()])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--out")
    parser.add_argument("--color", default="00B140", help="目标纯色，默认标准抠像绿 #00B140")
    parser.add_argument("--min-area-ratio", type=float, default=0.002)
    parser.add_argument("--preview-dir")
    args = parser.parse_args()

    source = Path(args.video)
    target = Path(args.out) if args.out else source.with_name(source.stem + "-flatgreen.mp4")
    color = tuple(int(args.color[i : i + 2], 16) for i in (0, 2, 4))

    width, height, fps = probe(source)
    frames = read_frames(source, width, height)
    print(f"源：{source.name}  {width}x{height}  {fps:g}fps  {frames.shape[0]} 帧")

    sample = frames.shape[0] - 3          # 结尾那几帧绿幕最完整
    print("归一前绿幕统计：", green_stats(frames[sample], args.min_area_ratio))

    flattened = flatten(frames, color, args.min_area_ratio)
    print("归一后绿幕统计：", green_stats(flattened[sample], args.min_area_ratio))

    write_video(flattened, target, fps, source=source)
    print(f"输出：{target}  {target.stat().st_size/1024/1024:.2f}MB（音轨沿用原片）")

    # 不变量：**后处理绝不能静默丢音轨**（2026-10-14 事故：第一版写死 `-an`，绿幕视频
    # 全部变无声，用户报「为什么泡沫爆炸没有声音」）。原片有声而输出没有 → 非零退出，
    # 调用方（`integrations/green_screen.flatten_video_bytes`）会看到失败并**保留原片**。
    if audio_streams(source) and not audio_streams(target):
        print("错误：原片有音轨但归一后没有 —— 拒绝交付，调用方应保留原片", file=sys.stderr)
        return 2

    preview_dir = Path(args.preview_dir) if args.preview_dir else target.parent
    for index in (frames.shape[0] // 2, sample):
        path = preview_dir / f"{target.stem}-对比-第{index}帧.png"
        save_preview(frames, flattened, index, path)
        print(f"对比图：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
