"""第 2 版：在图1 基础上生成「四列对齐、甜甜圈已上好酱」的参考图。

用户 2026-10-20 三条口径：
  1) 一一对应必须成立（上一版黄罐的酱滴到紫罐盖上、蓝罐滴到灯笼上 ✗）；
  2) 瓶子必须是**图1 里的瓶子**（不要凭空造形状）；
  3) 甜甜圈要是**已经上好酱**的状态（终态）。

做法：三张参考一起喂 ——
  image_1 = 图1 全图（场景/构图底）
  image_2 = 图1 的罐子特写（**罐子外观基准**：同款圆顶盖、同款陶瓷）
  image_3 = 图3（四色酱的颜色与黏稠光泽参考）

用法：python _diag/make_jar_scene2.py [张数]
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
OUT_DIR = ANCHOR / "scene2"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"

INPUTS = {
    "image_1": SCENE / "ref_image-1.png",   # 场景底
    "image_2": ANCHOR / "fig1_jars.png",    # 图1 的罐子特写（外观基准）
    "image_3": SCENE / "ref_image-3.png",   # 四色酱质感
}

PROMPT = (
    "以第 1 张参考图为基础做画面编辑：**保持场景、构图、机位、女巫造型与姿势、桌面、紫色盘子、"
    "灯光与紫橙万圣节色调完全不变**。要改的只有桌上左侧的酱罐与甜甜圈的状态：\n"
    "1) 罐子**必须沿用第 2 张参考图里那几个罐子的同款造型**——同样的矮胖圆柱、同样的圆顶陶瓷盖、"
    "同样的哑光陶瓷材质与金色小星饰；数量为四个，从左到右依次为正红、明黄、宝蓝、紫罗兰；"
    "不要改形状、不要换成玻璃瓶、不要加高加瘦。\n"
    "2) 四个罐子**悬浮在紫色盘子正上方**，并且与盘子里四只甜甜圈**严格上下对齐成四列**："
    "最左边的罐子正对最左边那只甜甜圈，第 2 个正对第 2 只，依次类推；四列彼此平行、间距均匀。\n"
    "3) 甜甜圈是**已经淋好酱的状态**：每只甜甜圈表面只有一种颜色的巧克力酱，"
    "从左到右依次为红、黄、蓝、紫，与正上方那一列罐子的颜色一一对应；酱的黏稠度、光泽与"
    "缓慢滴落形态参考第 3 张参考图。\n"
    "4) 如果画到从罐口流下的酱流，必须是**笔直垂直**地落在正下方那一只甜甜圈上；"
    "绝对不要让任何一股酱流落到相邻甜甜圈、罐子盖子或旁边的灯笼上，四股之间不交叉、不汇合。\n"
    "5) 不要出现多余的罐子或甜甜圈；灯笼等原有道具保持在画面边缘、不要插在四列之间；"
    "不要出现任何文字、数字、logo、水印、品牌标识。"
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
    for event in (job.get("events") or []):
        print("   event:", event.get("message"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for index, result in enumerate(job.get("results") or [], 1):
        for image_index, image in enumerate(result.get("images") or [], 1):
            url = image.get("image_url") or image.get("url")
            if not url:
                continue
            target = OUT_DIR / f"v2_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
