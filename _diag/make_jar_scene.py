"""在图1 的基础上生成「四个罐子 + 一一对应淋酱」的参考图（图1 当底、图3 当酱的质感参考）。

用户 2026-10-20 口径：「不要生成四个新的瓶子，而是在图一的基础上生成四个瓶子正确浇巧克力酱
一一对应的图片，把图一和图三作为参考」。

上一版错在哪：我用纯文生图凭空造了四个新罐子 —— 模型没有"沿用图1 罐子"这个概念，
于是成片里开头是图1 的罐子、后面换成了我造的那四个 → 用户看到的就是"瓶子直接变了"。

用法：python _diag/make_jar_scene.py [张数]
产物落到 _diag/anchor/scene/。
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
REFS = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
OUT_DIR = ROOT / "_diag" / "anchor" / "scene"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"          # Seedream 5.0 Pro（公司 Key）

FIG1 = REFS / "ref_image-1.png"            # 场景底图（女巫 + 桌面 + 原罐子）
FIG3 = REFS / "ref_image-3.png"            # 淋酱质感参考

PROMPT = (
    "以第 1 张参考图为基础做画面编辑，**保持场景、构图、机位、女巫造型、桌面、紫色盘子、"
    "灯光与紫橙万圣节色调完全不变**：\n"
    "1) 桌上左侧的酱罐**沿用第 1 张参考图里罐子的造型、哑光陶瓷材质与圆顶盖子样式**，"
    "数量改为四个，罐身与盖子分别为正红、明黄、宝蓝、紫罗兰四色，四色区分明显、不加渐变；\n"
    "2) 四个罐子悬浮在紫色盘子上方、四个金黄甜甜圈的正上方，罐口朝下倾斜，各自向下倾倒一股"
    "巧克力酱：从左到右第 1 个罐子对应最左边那只甜甜圈、第 2 个对应第 2 只，依次类推；"
    "四股酱流彼此平行、互不相交、不落到相邻甜甜圈上；\n"
    "3) 巧克力酱的黏稠度、光泽与滴落形态参考第 2 张参考图，四股酱的颜色与对应罐子一致；\n"
    "4) 甜甜圈保持金黄原味、大小一致、2 前 2 后整齐排列；\n"
    "5) 不要出现任何文字、数字、logo、水印、品牌标识；不要出现多余的罐子或甜甜圈。"
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
    body = b"".join(parts)
    request = urllib.request.Request(
        APP + path, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(APP + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    count = sys.argv[1] if len(sys.argv) > 1 else "2"
    for path in (FIG1, FIG3):
        if not path.is_file():
            print("缺参考图：", path)
            return 1
    created = post_multipart(
        "/api/jobs",
        {
            "provider": PROVIDER,
            "model": MODEL,
            "mode": "img2img",
            "aspect_ratio": "9:16",
            "image_size": "2K",
            "response_format": "url",
            "repeat_count": count,
            "concurrency": "1",
            "prompt": PROMPT,
        },
        {"image_1": FIG1, "image_2": FIG3},
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
            target = OUT_DIR / f"scene_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
