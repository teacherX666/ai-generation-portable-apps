"""把红幕区域抠成透明，输出带 alpha 的 MOV（ProRes 4444）。

**为什么不用简单色键**：AI 生成的红幕带明暗渐变与阴影（原片幕布内部 std 高达 51.6），
按纯红设阈值会抠不干净、还会在阴影处留一圈不透明的斑块。这里的做法是：
1. 色相 + 连通域找出「与背景连通的大片红色区域」= 红幕；
2. **形态学闭运算 + 填内部孔洞** —— 幕布上的阴影/暗角/绳桩全都属于幕布本身，一起抠掉；
3. 掩膜外扩一点点做**羽化**，避免硬锯齿；
4. **只在幕布边缘一条带里**去红溢色（全图去溢色会把棕红木轨道、暖光草地也压暗）；
5. alpha = 幕布区 0（透明）、其余 255（保留），RGB 用去溢色后的原图；
6. 输出 `prores_ks / profile 4444 / yuva444p10le`，**音轨原样复制**。

用法：
    python tools/red_screen_to_alpha.py <in.mp4> [--out x.mov]
                                        [--val-min 30] [--min-area-ratio 0.002]
                                        [--close 25] [--feather 1.5] [--preview-dir DIR]
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

#: ⚠️ OpenCV **8 位图**的 H 是 **0~179**（= 角度/2），不是 0~255。红色跨 0 点，要两段：
#:   0~12°  -> H 0~6      348~360° -> H 174~179
#: 这里放宽到 [0,8] / [170,179]，兼顾暗红与洋红边的红。
#: 2026-10-28 踩坑：第一版按 `int(deg*255/180)` 换算成了 [0,17]/[223,253] ——
#: `[0,17]` 实际是 0~34°，把**橙色**（木轨道、暖光草地）整片吃进掩膜；`[223,253]`
#: 超出 8 位上限 179 完全不生效，330~360° 那段红反而漏掉。实测掩膜里 30.6% 是橙黄。
_HUE_SEGMENTS = ((0, 8), (170, 179))
_SAT_MIN = 60


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den)


def read_frames(path: Path, width: int, height: int) -> np.ndarray:
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    size = width * height * 3
    n = len(buf) // size
    return buf[: n * size].reshape(n, height, width, 3)


def screen_mask(
    frame: np.ndarray,
    *,
    val_min: int,
    min_area_ratio: float,
    close: int,
    max_hole_ratio: float = 0.005,
) -> np.ndarray:
    """红幕掩膜：偏红 + 够大 + **把阴影/孔洞一起吃掉**。"""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    raw = np.zeros((h, w), np.uint8)
    for lo, hi in _HUE_SEGMENTS:
        raw |= cv2.inRange(
            hsv, (int(lo), _SAT_MIN, val_min), (int(hi), 255, 255)
        )

    # 闭运算：把布料纹理/渐变造成的细碎断裂桥接起来
    if close > 0:
        raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))

    cnt, lab, stats, _ = cv2.connectedComponentsWithStats(raw, 8)
    keep = np.zeros(cnt, bool)
    min_area = max(64, int(h * w * min_area_ratio))
    for i in range(1, cnt):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        left, top = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        touches = left <= 1 or top <= 1 or left + bw >= w - 1 or top + bh >= h - 1
        if touches or area >= h * w * 0.04:
            keep[i] = True
    mask = keep[lab].astype(np.uint8)

    # **只填小孔洞**：幕布自身的阴影/暗角/绳桩属于幕布，要一起抠掉；
    # 但红幕把木轨道、草地"圈"在里面时，那是**真的主体**，绝不能整块填掉。
    # 实测（2026-10-28）：用无上限的 floodFill 填孔洞，首帧透明区从真实的 19% 涨到 57.3%，
    # 其中 62% 是误抠 —— 被误抠区域均值 RGB[118,81,50]、色相 51（橙黄的木轨道），根本不是红。
    # 所以按**面积上限**区分：小于 max_hole_ratio 的孔洞才填。
    if mask.any():
        pad = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
        flood = pad.copy()
        cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 1)
        holes = (flood[1:-1, 1:-1] == 0).astype(np.uint8)
        if holes.any():
            max_hole = max(64, int(h * w * max_hole_ratio))
            hc, hl, hstats, _ = cv2.connectedComponentsWithStats(holes, 8)
            small = np.zeros(hc, bool)
            for i in range(1, hc):
                if hstats[i, cv2.CC_STAT_AREA] <= max_hole:
                    small[i] = True
            mask = np.clip(mask + small[hl].astype(np.uint8), 0, 1).astype(np.uint8)
    return mask


def despill_edges(rgb: np.ndarray, mask: np.ndarray, band: int = 12) -> np.ndarray:
    """只在幕布边缘一带压红，消除主体轮廓上的红边（不碰幕布本体、不碰远处主体）。"""
    work = rgb.astype(np.int16)
    edge = (cv2.dilate(mask, np.ones((band * 2 + 1, band * 2 + 1), np.uint8)) > 0) & (mask == 0)
    if not edge.any():
        return rgb
    r, g, b = work[:, :, 0], work[:, :, 1], work[:, :, 2]
    limit = np.maximum(g, b)
    work[:, :, 0] = np.where((r > limit) & edge, limit, r)
    return np.clip(work, 0, 255).astype(np.uint8)


def build_rgba(frames: np.ndarray, args) -> tuple[np.ndarray, list[float]]:
    n, h, w = frames.shape[0], frames.shape[1], frames.shape[2]
    out = np.zeros((n, h, w, 4), np.uint8)
    cover: list[float] = []
    for i in range(n):
        rgb = frames[i]
        mask = screen_mask(
            rgb, val_min=args.val_min, min_area_ratio=args.min_area_ratio, close=args.close,
            max_hole_ratio=args.max_hole_ratio,
        )
        cover.append(float(mask.mean() * 100))
        rgb = despill_edges(rgb, mask)
        # alpha：幕布区 0（透明），其余 255（保留）；mask 边界做羽化
        alpha = np.where(mask > 0, 0.0, 1.0).astype(np.float32)
        if args.feather > 0:
            alpha = cv2.GaussianBlur(alpha, (0, 0), args.feather)
        out[i, :, :, :3] = rgb
        out[i, :, :, 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
    return out, cover


def audio_streams(path: Path) -> int:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return len([x for x in r.stdout.splitlines() if x.strip()])


def write_alpha_mov(rgba: np.ndarray, path: Path, fps: float, source: Path) -> None:
    h, w = rgba.shape[1], rgba.shape[2]
    cmd = [
        FFMPEG, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
    ]
    if audio_streams(source):
        cmd += ["-i", str(source), "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
    cmd += [
        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
        "-shortest", str(path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    proc.stdin.write(np.ascontiguousarray(rgba).tobytes())
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"写 MOV 失败 returncode={proc.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out")
    ap.add_argument("--val-min", type=int, default=30, help="亮度下限，阴影多就调低")
    ap.add_argument("--min-area-ratio", type=float, default=0.002)
    ap.add_argument("--close", type=int, default=25, help="闭运算核，纹理断裂多就调大")
    ap.add_argument("--feather", type=float, default=1.5)
    ap.add_argument("--max-hole-ratio", type=float, default=0.005,
                    help="只填小于此面积占比的内部孔洞（防把被圈住的主体整块抠掉）")
    ap.add_argument("--preview-dir")
    args = ap.parse_args()

    source = Path(args.video)
    target = Path(args.out) if args.out else source.with_name(source.stem + "_alpha.mov")

    w, h, fps = probe(source)
    frames = read_frames(source, w, h)
    print(f"源：{source.name}  {w}x{h}  {fps:g}fps  {frames.shape[0]} 帧")

    rgba, cover = build_rgba(frames, args)
    print(f"透明区占比：首 {cover[0]:.1f}%  中 {cover[len(cover)//2]:.1f}%  末 {cover[-1]:.1f}%  最大 {max(cover):.1f}%")

    write_alpha_mov(rgba, target, fps, source)
    have_audio = audio_streams(source)
    print(f"输出：{target.name}  {target.stat().st_size/1024/1024:.2f}MB  音轨{'已保留' if have_audio else '（原片无）'}")

    if have_audio and not audio_streams(target):
        print("错误：原片有音轨但输出没有 —— 拒绝交付", file=sys.stderr)
        return 2

    pdir = Path(args.preview_dir) if args.preview_dir else target.parent
    try:
        from PIL import Image

        def checker(hh, ww, s=24):
            y, x = np.mgrid[0:hh, 0:ww]
            return np.where(((y // s + x // s) % 2)[..., None] == 0, 205, 248).astype(np.uint8).repeat(3, 2)

        for idx in (len(frames) // 2, len(frames) - 3):
            a = rgba[idx, :, :, 3].astype(np.float32) / 255.0
            comp = (rgba[idx, :, :, :3] * a[..., None] + checker(h, w) * (1 - a[..., None])).astype(np.uint8)
            Image.fromarray(comp).save(pdir / f"{target.stem}-透明预览-第{idx}帧.png")
            print(f"透明预览：{pdir / f'{target.stem}-透明预览-第{idx}帧.png'}")
    except Exception as exc:  # noqa: BLE001
        print(f"（预览生成失败，忽略：{exc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
