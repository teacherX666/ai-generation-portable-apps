"""第 3 版：把罐子的**器型**按图1 的实测特征写死（尖形小盖 / 矮胖圆润 / 陶瓷）。

v2 的问题（用 DeepSeek 视觉模型对比图1 得出，可信）：
  * 图1 的罐子 = 尖形小盖（顶部细长尖塞）+ 矮胖、瓶身圆润 + 陶瓷
  * v2 的罐子   = 圆顶球把手 + 高瘦圆柱 + 玻璃感  ✗
v2 已经对的部分（保留）：四列上下对齐、一一对应、无跨列酱流、甜甜圈已淋好对应颜色的酱。

用法：python _diag/make_jar_scene3.py [张数]
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
OUT_DIR = ANCHOR / "scene5"
PROVIDER = "gemini"
MODEL = "banana2-ssvip"

INPUTS = {
    "image_1": SCENE / "ref_image-1.png",   # 场景底 + 罐子原型
    "image_2": ANCHOR / "fig1_jars.png",    # 图1 罐子特写
    "image_3": SCENE / "ref_image-3.png",   # 四色酱质感
}

PROMPT = (
    "以第 1 张参考图为基础做画面编辑：保持场景、构图、机位、女巫造型与姿势、桌面、紫色盘子、"
    "灯光与紫橙万圣节色调完全不变。只改桌上左侧的酱罐与甜甜圈状态：\n"
    "1) 罐子**严格照第 1、2 张参考图里的罐子器型**：矮胖、瓶身圆润（像小陶罐，不是细长圆柱）；"
    "盖子必须是**尖形小盖**——顶部收成一个细长小尖塞（像小尖帽 / 挤花嘴），**不要圆球形把手、"
    "不要平顶、不要圆顶**；材质为**哑光陶瓷**（**不要玻璃质感、不要透明**）；"
    "数量四个，从左到右依次为正红、明黄、宝蓝、紫罗兰。四个罐子的器型、大小完全一致，"
    "不要加高、不要变瘦、不要改成别的瓶型。\n"
    "2) 四个罐子悬浮在紫色盘子正上方，并与盘里四只甜甜圈**上下严格对齐成四列**："
    "最左的罐子正对最左那只甜甜圈，第 2 个对第 2 只，依次类推；四列平行、间距均匀。\n"
    "3) 甜甜圈是**已经淋好酱的终态**：每只上面只有一种颜色的酱，从左到右红、黄、蓝、紫，"
    "与正上方同列罐子颜色一致；酱黏稠厚重、光泽细腻，参考第 3 张参考图的质感。\n"
    "4) 若画到从罐口流下的酱流，必须笔直垂直落在正下方那只甜甜圈上；不要落到相邻甜甜圈、"
    "罐盖或旁边的灯笼上，四股不交叉不汇合。\n"
    "5) 不要多余的罐子或甜甜圈；灯笼等原道具留在画面边缘、不要插在四列之间；"
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
            target = OUT_DIR / f"v5_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
