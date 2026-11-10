"""幕布抠像 -> 透明通道 MOV（支持绿幕 / 红幕）。

与 `red_screen_to_alpha.py` 的区别（也是防"把兔子脸抠掉"的关键）：
**只抠与画面边缘连通的大块幕布，内部孤立色块一律不碰。** 兔子脸在画面内部，
即使它的色相落在幕布色相段里，也不会被抠 —— 2026-10-28 用户报「扣红色老是把兔子脸
扣掉一部分」就是这个原因（那条素材里兔脸的 H≈12~15、R 明显高于 G/B，和红幕撞色相）。

之前的版本靠"填内部孔洞"来处理幕布上的阴影，而填孔洞会把**被幕布包围的主体**也填掉；
这里改成：阴影靠**放宽色相段的饱和度/亮度下限**去覆盖（阴影仍是幕布色），
孔洞只填**极小的**（<0.1% 画面），大块内部区域绝不填。

用法：
    python tools/screen_to_alpha.py <in.mp4> --screen green|red [--out x.mov]
                                    [--sat-min 45] [--val-min 25] [--min-area-ratio 0.01]
                                    [--close 15] [--max-hole-ratio 0.001] [--feather 1.5]
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

#: ⚠️ OpenCV **8 位图**的 H 是 **0~179**（角度/2），不是 0~255。
#: 红：0~12° → H 0~6；348~360° → H 174~179。绿：约 70~150° → H 35~75。
SCREENS = {
    "red": {"hues": ((0, 8), (170, 179)), "channel": 0},   # 压红通道
    "green": {"hues": ((35, 85),), "channel": 1},          # 压绿通道
}


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den)


def read_frames(path: Path, w: int, h: int) -> np.ndarray:
    proc = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo",
                           "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    size = w * h * 3
    n = len(buf) // size
    return buf[: n * size].reshape(n, h, w, 3)


def screen_mask(frame, *, hues, sat_min, val_min, min_area_ratio, close, max_hole_ratio,
                channel: int = 1, domination: int = 4, large_ratio: float = 0.03,
                small_min_area: float = 0.0002, merge: int = 31,
                largest_only: bool = True, union_raw: bool = True):
    """幕布掩膜 = 色相段 **或** 通道占优。

    ⚠️ 加"通道占优"是为了**暗部阴影**：幕布在阴影里时很暗很闷，色相/饱和度/亮度三个下限
    一卡就漏（实测开头那段阴影 RGB=[19,27,11]，绿只比红高 8）。占优判据只看
    `通道 - 其余两通道的最大值`，不受亮度影响，阈值给到 4 就能把暗绿也收进来。
    主体（白兔 / 棕色轨道）不是通道占优的，所以放宽它是安全的 —— 再加"只取贴边大块"
    这一层，主体内部仍然不受影响。
    """
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    raw = np.zeros((h, w), np.uint8)
    for lo, hi in hues:
        raw |= cv2.inRange(hsv, (lo, sat_min, val_min), (hi, 255, 255))

    if domination > 0:
        d = frame.astype(np.int16)
        others = [i for i in (0, 1, 2) if i != channel]
        dom = d[:, :, channel] - np.maximum(d[:, :, others[0]], d[:, :, others[1]])

        # ── 高置信种子 + 测地膨胀（2026-10-28 修「把云也抠了」）──
        # 实测（帧 190）：**云**的绿占优最多到 8（94% 都在 0 以下），**幕布** 97.6% 在 25 以上
        # —— 阈值给 4 时，被绿幕打光的云正好落在 4~8，被一起抠掉。
        # 但幕布的**暗部阴影**也只有 4~8，单纯抬阈值会把阴影丢掉。
        # 所以：先用高阈值取"确定的幕布"当种子，再只向**与种子连通**的淡绿像素（阴影）扩张。
        # 云跟幕布在空间上不连通，膨胀过不去 → 云保住。
        seed = ((dom >= domination) | (raw > 0)).astype(np.uint8)
        # 候选集：放宽饱和度（幕布本体亮部 S≈104、暗部 S≈182；而**云只有 38~53**）。
        # 实测帧 190 顶部被误抠的像素 RGB=[129,132,126]、绿占优 **-6**、S=38 —— 那是云，
        # 靠"与幕布连通"这一条就能排除：云跟幕布在空间上不相连，测地膨胀过不去。
        cand_sat = max(20, sat_min // 3)
        cand = np.zeros((h, w), np.uint8)
        for lo, hi in hues:
            cand |= cv2.inRange(hsv, (lo, cand_sat, val_min), (hi, 255, 255))
        cand = np.clip(cand.astype(np.int16) + (dom >= 3).astype(np.int16) * 255, 0, 255).astype(np.uint8)
        # 对数级重建：8 次膨胀即可覆盖 ~255px，等价于逐像素重建但快得多
        for k in (3, 5, 9, 17, 33, 65, 129, 257):
            grown = cv2.dilate(seed, np.ones((k, k), np.uint8))
            new = cv2.bitwise_and(grown, cand)
            if int(new.sum()) <= int(seed.sum()):
                break
            seed = new
        raw = (seed * 255).astype(np.uint8)
    raw = raw
    if close > 0:
        raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))

    cnt, lab, st, _ = cv2.connectedComponentsWithStats(raw, 8)
    keep = np.zeros(cnt, bool)
    min_area = max(64, int(h * w * min_area_ratio))
    for i in range(1, cnt):
        # ⚠️ 这里**不能**再用 `min_area_ratio` 提前 continue —— 那会把贴边的小碎片
        # 在到达下面的保留规则之前就丢掉。实测（2026-10-28）：帧 12/18/24 透明区
        # 整整是 **0.00%**，因为该帧最大绿块只有 4356px < min_area(5529px)，
        # 全被这道早退门槛 `continue` 掉了，新加的 small_min_area 根本没机会生效。
        # 真正的最小面积判据在下面（贴边用 small_min_area，不贴边用 large_ratio）。
        l, t, bw, bh = st[i, 0], st[i, 1], st[i, 2], st[i, 3]
        # ★ 保留规则（2026-10-28 二次修正）：
        #   a) **贴画面边缘**：几乎不看面积（>= small_min_area 就收）。
        #      实锤：这条素材的绿**碎成上百个小块**（帧 24 有 138 个连通域，最大只占 0.47%），
        #      按"每块都要够大"的门槛会**全军覆没** —— 帧 24 的暗部阴影 34241px
        #      一个都没进透明区，就是这么丢的。
        #   b) **不贴边但够大**（>= large_ratio）：幕布还没展开到边缘时用。
        # 之所以敢对贴边块几乎不设面积门槛，是因为判据里加了 channel domination：
        # 白兔、棕色轨道都不是绿占优的，碎片放进来也不会误伤主体（主体保护由
        # "只取这些块 + 不填大孔洞 + 主体是内部孤立区"三层保证）。
        touches = l <= 1 or t <= 1 or l + bw >= w - 1 or t + bh >= h - 1
        area = st[i, cv2.CC_STAT_AREA]
        if (touches and area >= max(64, int(h * w * small_min_area))) or (
            area >= h * w * large_ratio
        ):
            keep[i] = True
    mask = keep[lab].astype(np.uint8)

    # 把保留下来的碎片**连成一片**：幕布的绿被纹理/阴影切得很碎（实测帧 24 有 138 个
    # 连通域），不合并的话透明区会是千疮百孔的麻点，视觉上就是"没抠干净"。
    if merge > 0 and mask.any():
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((merge, merge), np.uint8))

    # ★ 只保留**最大的一块连通域**（2026-10-28，修「背后的云也被扣掉」）。
    # 幕布在画面里必然是一整块巨大的连通区域；云、树上反光的绿叶、道具上的绿点
    # 都是**离散的小碎片**。实测顶部被误抠的像素 RGB=[129,132,126]、绿占优 **-6** ——
    # 那是云。靠"它是独立小块"这条排除，比调色相/饱和度阈值可靠得多。
    if largest_only and mask.any():
        cnt2, lab2, st2, _ = cv2.connectedComponentsWithStats(mask, 8)
        if cnt2 > 1:
            big = max(range(1, cnt2), key=lambda i: int(st2[i, cv2.CC_STAT_AREA]))
            if st2[big, cv2.CC_STAT_AREA] >= h * w * 0.005:
                mask = (lab2 == big).astype(np.uint8)
        # 再把**高饱和的绿**并回来：幕布的暗部阴影常常与主体断开（离散碎片），
        # 只留最大块会把它们一起丢掉（实测帧 0 的暗部覆盖 63.8% → 26.4%）。
        # 云/树上反光虽然也偏绿，但**饱和度只有 38~53**；幕布暗部是 **182** ——
        # 所以用 `raw`（色相段 + sat_min 门槛）并回来，只补阴影、不补云。
        if union_raw:
            mask = np.clip(mask.astype(np.int16) + (raw > 0).astype(np.int16), 0, 1).astype(np.uint8)

    # 只填极小的孔洞（幕布上的小暗斑）；大块内部区域绝不填。
    if mask.any():
        pad = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
        flood = pad.copy()
        cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 1)
        holes = (flood[1:-1, 1:-1] == 0).astype(np.uint8)
        if holes.any():
            max_hole = max(32, int(h * w * max_hole_ratio))
            hc, hl, hs, _ = cv2.connectedComponentsWithStats(holes, 8)
            small = np.zeros(hc, bool)
            for i in range(1, hc):
                if hs[i, cv2.CC_STAT_AREA] <= max_hole:
                    small[i] = True
            mask = np.clip(mask + small[hl].astype(np.uint8), 0, 1).astype(np.uint8)
    return mask


def despill_edges(rgb: np.ndarray, mask: np.ndarray, channel: int, band: int = 24) -> np.ndarray:
    """只在幕布边缘一带压掉幕布色的溢色（不碰幕布本体、不碰远处主体）。"""
    work = rgb.astype(np.int16)
    edge = (cv2.dilate(mask, np.ones((band * 2 + 1, band * 2 + 1), np.uint8)) > 0) & (mask == 0)
    if not edge.any():
        return rgb
    ch = work[:, :, channel]
    others = [i for i in (0, 1, 2) if i != channel]
    limit = np.maximum(work[:, :, others[0]], work[:, :, others[1]])
    work[:, :, channel] = np.where((ch > limit) & edge, limit, ch)
    return np.clip(work, 0, 255).astype(np.uint8)


def audio_streams(path: Path) -> int:
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return len([x for x in r.stdout.splitlines() if x.strip()])


def write_mov(rgba, path: Path, fps: float, source: Path) -> None:
    h, w = rgba.shape[1], rgba.shape[2]
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio_streams(source):
        cmd += ["-i", str(source), "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
    cmd += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
            "-shortest", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.stdin.write(np.ascontiguousarray(rgba).tobytes())
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"写 MOV 失败 {proc.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--screen", choices=["green", "red"], required=True)
    ap.add_argument("--out")
    ap.add_argument("--sat-min", type=int, default=70)
    ap.add_argument("--val-min", type=int, default=25)
    ap.add_argument("--min-area-ratio", type=float, default=0.01)
    ap.add_argument("--close", type=int, default=15)
    ap.add_argument("--max-hole-ratio", type=float, default=0.001)
    ap.add_argument("--feather", type=float, default=1.5)
    ap.add_argument("--choke", type=float, default=2, help="掩膜外扩像素数，吃掉边缘残绿")
    ap.add_argument("--domination", type=int, default=15,
                    help="通道占优的**高置信**阈值（种子）；实测云最多到 8、幕布多在 25 以上")
    ap.add_argument("--large-ratio", type=float, default=0.03,
                    help="不贴边但面积占比超过它也算幕布（幕布还没展开到边缘时用）")
    ap.add_argument("--allow-multiple-blobs", action="store_true",
                    help="允许保留多块（默认只保留最大一块连通域，用来排除云等离散碎片）")
    ap.add_argument("--no-union-raw", action="store_true",
                    help="不把高饱和绿（幕布暗部阴影碎片）并回来")
    ap.add_argument("--small-min-area", type=float, default=0.0002,
                    help="贴边碎块的面积下限（幕布被纹理切碎时要用很小的值）")
    ap.add_argument("--preview-dir")
    args = ap.parse_args()
    args.largest_only = not args.allow_multiple_blobs
    args.union_raw = not args.no_union_raw

    src = Path(args.video)
    out = Path(args.out) if args.out else src.with_name(f"{src.stem}_{args.screen}_alpha.mov")
    spec = SCREENS[args.screen]
    w, h, fps = probe(src)
    frames = read_frames(src, w, h)
    print(f"源：{src.name} {w}x{h} {fps:g}fps {len(frames)} 帧  幕布={args.screen}")

    rgba = np.zeros((len(frames), h, w, 4), np.uint8)
    cover = []
    for i in range(len(frames)):
        rgb = frames[i]
        mask = screen_mask(rgb, hues=spec["hues"], sat_min=args.sat_min, val_min=args.val_min,
                           min_area_ratio=args.min_area_ratio, close=args.close,
                           max_hole_ratio=args.max_hole_ratio,
                           channel=spec["channel"], domination=args.domination,
                           large_ratio=args.large_ratio, small_min_area=args.small_min_area,
                           largest_only=args.largest_only, union_raw=args.union_raw)
        if args.choke > 0:
            # 收缩（choke）：掩膜往外扩一点，吃掉主体轮廓上那圈幕布色的光。
            k = int(args.choke) * 2 + 1
            mask = cv2.dilate(mask, np.ones((k, k), np.uint8))
        cover.append(float(mask.mean() * 100))
        rgb = despill_edges(rgb, mask, spec["channel"])
        a = np.where(mask > 0, 0.0, 1.0).astype(np.float32)
        if args.feather > 0:
            a = cv2.GaussianBlur(a, (0, 0), args.feather)
        rgba[i, :, :, :3] = rgb
        rgba[i, :, :, 3] = np.clip(a * 255, 0, 255).astype(np.uint8)

    print(f"透明区占比：首 {cover[0]:.1f}%  中 {cover[len(cover)//2]:.1f}%  末 {cover[-1]:.1f}%  最大 {max(cover):.1f}%")
    write_mov(rgba, out, fps, src)
    print(f"输出：{out.name}  {out.stat().st_size/1024/1024:.2f}MB  音轨{'已保留' if audio_streams(src) else '（原片无）'}")
    if audio_streams(src) and not audio_streams(out):
        print("错误：原片有音轨但输出没有", file=sys.stderr); return 2

    pdir = Path(args.preview_dir) if args.preview_dir else out.parent
    try:
        from PIL import Image

        def checker(hh, ww, s=24):
            y, x = np.mgrid[0:hh, 0:ww]
            return np.where(((y // s + x // s) % 2)[..., None] == 0, 205, 248).astype(np.uint8).repeat(3, 2)

        for idx in (len(frames) // 2, len(frames) - 3):
            a = rgba[idx, :, :, 3].astype(np.float32) / 255.0
            comp = (rgba[idx, :, :, :3] * a[..., None] + checker(h, w) * (1 - a[..., None])).astype(np.uint8)
            Image.fromarray(comp).save(pdir / f"{out.stem}-透明预览-第{idx}帧.png")
            print(f"预览：{pdir / f'{out.stem}-透明预览-第{idx}帧.png'}")
    except Exception as exc:  # noqa: BLE001
        print(f"（预览失败，忽略：{exc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
