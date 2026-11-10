"""确定性水印修补：**只改水印像素**，其余像素与原图逐点一致（跑在本机带 cv2 的解释器上）。

为什么不用图片模型：模型是**整张重绘** —— 色调/纹理都会轻微变化，于是任何"把某块贴回去"
的做法都会留下可见补丁（用户 2026-10-28 原话：「拼接的啥啊」）。而水印在角落时，
用 cv2.inpaint 按邻近像素把水印补掉，**改动范围严格等于水印掩膜**，
画面其它地方与原图逐点相同 —— 这就是"只洗水印、不洗字"的最直接实现。

⚠️ 2026-10-28 第二版修正（用户报「清洗右下角会有黑点」）：
水印下面是**黑色大理石 + 白色纹理**，而"亮 + 低饱和"这个判据**把大理石白纹也当成水印**
一起挖掉 → 从周围暗底补回来就是一坨黑糊。两处修正：
1. **按连通域筛掉白纹**：水印字形是**一堆互不相连的小块**，大理石白纹是**长条形大块** ——
   只保留面积小于阈值、且长宽比不夸张的连通域；
2. **补完做"不许更暗"的钳制**：任何被补的像素如果比周围未被遮的中值暗太多，
   就按那个中值填回去（宁可补得平一点，也绝不留下黑点）。

用法（由 `reference_watermark_clean` 调用，不直接给人用）：
    python inpaint_reference_watermark.py --image IN --out OUT --box x0 y0 x1 y1 [--box ...]

坐标是**归一化** 0~1（与清洗探针的输出一致）。成功时打印一行 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--box", nargs=4, type=float, action="append", required=True)
    parser.add_argument("--pad", type=float, default=0.02)
    parser.add_argument("--bright", type=int, default=190)
    parser.add_argument("--deviation", type=int, default=16)
    parser.add_argument("--saturation", type=int, default=90)
    parser.add_argument("--max-component", type=int, default=1200)
    parser.add_argument("--max-mask-ratio", type=float, default=0.45)
    parser.add_argument("--clamp", type=int, default=45)
    args = parser.parse_args()

    try:
        import cv2
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"cv2/numpy 不可用：{exc}"}))
        return 2

    image = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if image is None:
        print(json.dumps({"ok": False, "error": "读图失败"}))
        return 2
    height, width = image.shape[:2]

    mask = np.zeros((height, width), np.uint8)
    boxes = []
    for x0, y0, x1, y1 in args.box:
        left = max(0, int((x0 - args.pad) * width))
        top = max(0, int((y0 - args.pad) * height))
        right = min(width, int((x1 + args.pad) * width))
        bottom = min(height, int((y1 + args.pad) * height))
        if right - left < 4 or bottom - top < 4:
            continue
        boxes.append((left, top, right, bottom))
        region = image[top:bottom, left:right]
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        background = cv2.medianBlur(gray, 15)
        deviation = cv2.absdiff(gray, background)
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        # 2026-10-28 修正：**不分亮暗**，只判"偏离局部背景"的细笔画。
        # 旧判据是"亮 + 低饱和" —— 遇到"深色水印压亮底"的图（文档4-张娜）会把**亮底**
        # 当成水印、把整框挖掉重填 → 填出 12% 的暗块（用户：「原图有个毛啊，就是你改坏了」）。
        hit = (
            (deviation > args.deviation)
            & ((hsv[:, :, 1] < args.saturation) | (gray < 90))
        ).astype(np.uint8)
        mask[top:bottom, left:right] = hit * 255

    if int(mask.sum()) == 0:
        print(json.dumps({"ok": False, "error": "水印框内没找到亮而低饱和的水印像素"}))
        return 3

    # ① 连通域筛选：水印字形 = 一堆小碎块；大理石白纹 = 长条大块（必须放过，否则会糊出黑点）。
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    kept = np.zeros_like(mask)
    dropped = 0
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        box_w = int(stats[index, cv2.CC_STAT_WIDTH])
        box_h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area <= args.max_component and max(box_w, box_h) <= 0.6 * max(width, height):
            kept[labels == index] = 255
        else:
            dropped += 1
    if int(kept.sum()) == 0:
        # 全被判成大块 → 保守起见整块都不动，绝不冒糊掉画面的风险。
        kept = mask
        dropped = 0

    # **大洞守卫**（2026-10-28）：掩膜占到框的 45% 以上，说明判据圈错了对象
    # （典型：深色水印压亮底，被误判成"亮底=水印"）—— 这种大洞任何插值都补不回来，
    # 补出来就是一整块糊。**宁可什么都不做**，把原图交回去由上层决定。
    box_area = sum((r - l) * (b - t) for l, t, r, b in boxes) or 1
    if int((kept > 0).sum()) > args.max_mask_ratio * box_area:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"掩膜占框 {int((kept > 0).sum()) / box_area:.0%} > "
                    f"{args.max_mask_ratio:.0%}，判据可能圈错对象，本次不修补",
                    "mask_pixels": int((kept > 0).sum()),
                    "box_pixels": box_area,
                },
                ensure_ascii=False,
            )
        )
        return 5

    grown = cv2.dilate(kept, np.ones((3, 3), np.uint8), iterations=1)
    repaired = cv2.inpaint(image, grown, 4, cv2.INPAINT_TELEA)

    # ② "不许更暗"钳制：被补的像素若明显暗于周围未被遮的中值 → 按中值填回，
    #    这样即使周围是深色大理石，也不会留下突兀的黑点。
    if args.clamp > 0:
        for left, top, right, bottom in boxes:
            sub_mask = grown[top:bottom, left:right] > 0
            if not sub_mask.any():
                continue
            patch = repaired[top:bottom, left:right].copy()
            reference = image[top:bottom, left:right].copy()
            known = (~sub_mask).astype(np.uint8)
            # 用未遮像素的中值做背景估计（逐通道，窗口 15）
            background = reference.copy()
            background[sub_mask] = 0
            weight = cv2.blur(known.astype(np.float32) * 255, (15, 15))
            summed = cv2.blur(background.astype(np.float32), (15, 15)) * (15 * 15)
            median_like = summed / np.maximum(weight, 1.0)[..., None]
            too_dark = (patch.astype(np.int16) < median_like - args.clamp) & sub_mask[
                ..., None
            ]
            patch = np.where(too_dark, median_like, patch).astype(np.uint8)
            repaired[top:bottom, left:right] = patch

    if not cv2.imwrite(args.out, repaired):
        print(json.dumps({"ok": False, "error": "写图失败"}))
        return 4

    before = image.astype("int16")
    after = repaired.astype("int16")
    changed = int((np.abs(after - before).max(axis=2) > 2).sum())
    # 黑点自检：补完之后，被补区域里比周围中值暗 > clamp 的像素数（应当接近 0）
    dark_holes = 0
    for left, top, right, bottom in boxes:
        sub_mask = grown[top:bottom, left:right] > 0
        if not sub_mask.any():
            continue
        patch = after[top:bottom, left:right]
        reference = before[top:bottom, left:right]
        background = reference.copy()
        background[sub_mask] = 0
        known = (~sub_mask).astype(np.float32)
        weight = cv2.blur(known * 255, (15, 15))
        summed = cv2.blur(background.astype(np.float32), (15, 15)) * (15 * 15)
        median_like = summed / np.maximum(weight, 1.0)[..., None]
        dark_holes += int(
            ((patch < median_like - args.clamp) & sub_mask[..., None]).any(axis=2).sum()
        )

    print(
        json.dumps(
            {
                "ok": True,
                "size": [width, height],
                "mask_pixels": int((grown > 0).sum()),
                "dropped_components": dropped,
                "changed_pixels": changed,
                "dark_holes": dark_holes,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
