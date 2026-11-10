"""红幕归一：把成片里的红幕区域**强制**成同一纯色（确定性，不靠模型）。

**为什么要它**（2026-10-28）：用户报「AI 生成的红幕有阴影导致没法纯色抠图」。
这与 2026-10-14 的绿幕事故是**同一个病**：模型把幕布当成**被照亮的物理表面**在画，
于是带明暗渐变、暗角、主体投影。提示词里写"纯色/无阴影"改不动渲染（那次已经证过），
所以只能**成片侧归一**：把「与背景连通的大片红色区域」整片刷成同一个色值，
阴影、暗角、渐变全部被吃掉；主体上的红色靠**连通域面积**排除，不会被误刷。

与绿幕版的差别只有三处：
1. **色相判据要跨 0**：OpenCV 的 H 是 0~179，红色分布在 `[0,10] ∪ [160,179]`；
2. **去溢色方向反过来**：压的是**红通道**（主体边缘被红幕打到的红溢色）；
3. 目标纯色默认 `#FF0000`。

用法：
    python tools/flatten_red_screen.py <in.mp4> [--out x.mp4] [--color FF0000]
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

#: ⚠️ OpenCV **8 位图**的 H 是 **0~179**（角度/2），不是 0~255。红色跨 0 点，要两段。`int(deg*255/180)` 是错的写法。
_HUE_SEGMENTS = ((0, 8), (170, 179))
_SAT_MIN = 70
_VAL_MIN = 30


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
    """红幕掩膜：偏红 + **够大**（连通到画面边缘的大片）—— 小的红块（道具/高光）不动。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for low, high in _HUE_SEGMENTS:
        mask |= cv2.inRange(
            hsv,
            (int(low), _SAT_MIN, _VAL_MIN),
            (int(high), 255, 255),
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
        # 贴着画面边缘的大片 = 红幕；画面中间的大片（≥4% 面积）也算（揭幕初期还没到边缘）。
        if touches_border or area >= height * width * 0.04:
            keep[label] = True
    return keep[labels]


def despill(frame: np.ndarray, mask: np.ndarray, band: int = 12) -> np.ndarray:
    """去红溢色：只对**紧贴幕布边缘的一条带**里「红比绿蓝都高」的像素压红。

    红溢色只发生在**幕布边缘**（红幕的光打到主体轮廓的那一圈）。绿幕版是**全图**压的
    （`~mask` 内所有绿>红蓝的像素），那对绿幕问题不大——主体很少是绿的；
    但红幕场景里**棕红色的木轨道、暖光下的草地、兔子的粉耳朵**全都会被压暗，
    实测帧 190 有 70.8% 的像素被改动、连天空都变了（幕布本体只占 52.8%）。

    所以这里加 `band`：只在 `dilate(mask, band) & ~mask` 这条边带里做，
    并且**不碰幕布本体**（碰了会把标准纯红压暗，背景就不纯了）。
    """
    work = frame.astype(np.int16)
    kernel = np.ones((band * 2 + 1, band * 2 + 1), np.uint8)
    edge = (cv2.dilate(mask.astype(np.uint8), kernel) > 0) & (~mask)
    if not edge.any():
        return frame
    red, green, blue = work[:, :, 0], work[:, :, 1], work[:, :, 2]
    limit = np.maximum(green, blue)
    spill = (red > limit) & edge
    work[:, :, 0] = np.where(spill, limit, red)
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


def write_video(frames: np.ndarray, path: Path, fps: float, source: Path | None = None) -> None:
    """写归一后的视频。**必须把原片音轨带过去**（`source` 给定时 `-c:a copy` 原样复制）。

    2026-10-14 事故：绿幕版第一版写死了 `-an`，于是所有绿幕视频的音轨被整条丢掉。
    **任何重编码步骤都要显式保留音频。**
    """
    height, width = frames.shape[1], frames.shape[2]
    command = [
        FFMPEG, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "-",
    ]
    if source is not None:
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


def red_stats(frame: np.ndarray, min_area_ratio: float) -> dict:
    """红幕区域的颜色离散度（抠图干不干净就看这个：std 越小越干净）。"""
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


def audio_streams(path: Path) -> int:
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
    parser.add_argument("--color", default="FF0000", help="目标纯色，默认纯红 #FF0000")
    parser.add_argument("--min-area-ratio", type=float, default=0.002)
    parser.add_argument("--preview-dir")
    args = parser.parse_args()

    source = Path(args.video)
    target = Path(args.out) if args.out else source.with_name(source.stem + "-flatred.mp4")
    color = tuple(int(args.color[i : i + 2], 16) for i in (0, 2, 4))

    width, height, fps = probe(source)
    frames = read_frames(source, width, height)
    print(f"源：{source.name}  {width}x{height}  {fps:g}fps  {frames.shape[0]} 帧")

    sample = frames.shape[0] - 3
    print("归一前红幕统计：", red_stats(frames[sample], args.min_area_ratio))

    flattened = flatten(frames, color, args.min_area_ratio)
    print("归一后红幕统计：", red_stats(flattened[sample], args.min_area_ratio))

    write_video(flattened, target, fps, source=source)
    print(f"输出：{target}  {target.stat().st_size/1024/1024:.2f}MB（音轨沿用原片）")

    # 不变量：**后处理绝不能静默丢音轨**
    if audio_streams(source) and not audio_streams(target):
        print("错误：原片有音轨但归一后没有 —— 拒绝交付，调用方应保留原片", file=sys.stderr)
        return 2

    preview_dir = Path(args.preview_dir) if args.preview_dir else target.parent
    try:
        from PIL import Image

        from PIL import Image as _I
        for index in (frames.shape[0] // 2, sample):
            a = _I.fromarray(frames[index])
            b = _I.fromarray(flattened[index])
            canvas = _I.new("RGB", (a.width * 2 + 12, a.height), (255, 255, 255))
            canvas.paste(a, (0, 0))
            canvas.paste(b, (a.width + 12, 0))
            path = preview_dir / f"{target.stem}-对比-第{index}帧.png"
            canvas.save(path)
            print(f"对比图：{path}")
    except Exception as exc:  # noqa: BLE001 —— 对比图只是附赠
        print(f"（对比图生成失败，忽略：{exc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
