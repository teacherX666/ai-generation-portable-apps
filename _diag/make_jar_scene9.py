"""第 9 版：**直接用图1 当底图**，只把桌上原有的**四只**罐子抬起来漂浮淋酱。

⚠️ 修正（2026-10-21 用户指出）：图1 桌上其实有**四只**罐子 —— 左边三只 + 右边一只。
我前几版只裁了左边（x 0-1100）就断定"只有三只"，于是推出"3→4 必然跳转"的错误结论，
还去生成了"四只一排"的重绘场景（v7/v8），那反而让布局和图1 对不上。
用户原话：「图一左边三个罐子右边一个，加起来没毛病的」。

所以这一版：底图 = **图1 原图**（场景/女巫/背景全是真原图，没有"重绘版"折损），
唯一动作 = 把桌上那四只罐子**整体抬起漂浮**到四只甜甜圈上方 + 各自淋同色的酱。

用法：python _diag/make_jar_scene9.py [张数]
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
import urllib.request
import uuid
from pathlib import Path

APP = "http://127.0.0.1:8797"
ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
ANCHOR = ROOT / "_diag" / "anchor"
OUT_DIR = ANCHOR / "scene9"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"

INPUTS = {
    "image_1": SCENE / "ref_image-1.png",      # ← 真·原图当底（场景/女巫/背景都不动）
    "image_2": ANCHOR / "fig1_jar_big.png",    # 罐子放大特写（器型基准）
    "image_3": SCENE / "ref_image-3.png",      # 酱的质感
}

PROMPT = (
    "以第 1 张图为底图（**场景、女巫、姿势、桌面、盘子、灯光、背景全部保持原样**），"
    "只做一个改动：把桌面上**原来那四只陶瓷酱罐**（左边三只 + 右边一只）"
    "**整体抬起来、漂浮到盘子里四只甜甜圈的正上方**——四个罐子与四只甜甜圈**上下对齐成四列**"
    "（第 1 个罐子正对第 1 只甜甜圈，第 2 个对第 2 只，依次类推，间距均匀），"
    "并从每个罐口垂直落下一股**与罐子同色**的巧克力酱到正下方那只甜甜圈上。\n"
    "**四只罐子的形状、大小、盖子、材质、颜色都按原图保持不变**"
    "（下宽上窄的梨形瓶身、扁平圆盘盖、盖子中央的细长圆柱提手、不透明高光釉面陶瓷，"
    "底色分别是红/蓝/橙/黄——保持它们各自原本的颜色与外观，不要改成玻璃瓶、不要改形状、"
    "不要增加或减少罐子数量）；只是把它们的**位置抬高**到甜甜圈上方。\n"
    "盘里四只甜甜圈改成**已经淋好酱的终态**：每只表面只有一种颜色的巧克力酱"
    "（与正上方那一只罐子的颜色一致），酱黏稠厚重、缓慢垂落、光泽细腻（质感参考第 3 张图）。\n"
    "原来放罐子的桌面位置空出来、露出木桌即可。四股酱流彼此不交叉、不汇合、不落到别的甜甜圈"
    "或桌面上。不要出现任何文字、数字、logo、水印、品牌标识；不要新增或删除任何物体。"
)


def post_multipart(path: str, fields: dict[str, str], files: dict[str, Path]) -> dict:
    boundary = "----dsh" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for key, file_path in files.items():
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{file_path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8")
        )
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib.request.Request(
        APP + path, data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(APP + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    count = sys.argv[1] if len(sys.argv) > 1 else "2"
    for key, path in INPUTS.items():
        if not path.is_file():
            print("缺参考图：", key, path)
            return 1
    created = post_multipart(
        "/api/jobs",
        {
            "provider": PROVIDER, "model": MODEL, "mode": "img2img",
            "aspect_ratio": "9:16", "image_size": "2K", "response_format": "url",
            "repeat_count": count, "concurrency": "1", "prompt": PROMPT,
        },
        INPUTS,
    )
    job_id = created.get("job_id")
    print("提交:", json.dumps(created, ensure_ascii=False), flush=True)
    if not job_id:
        return 1
    deadline = time.time() + 900
    while time.time() < deadline:
        job = get(f"/api/jobs/{job_id}")
        status = str(job.get("status") or "?")
        print("  status ->", status, flush=True)
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            break
        time.sleep(10)
    print("错误:", job.get("errors"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for index, result in enumerate(job.get("results") or [], 1):
        for image_index, image in enumerate(result.get("images") or [], 1):
            url = image.get("image_url") or image.get("url")
            if not url:
                continue
            target = OUT_DIR / f"v9_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
