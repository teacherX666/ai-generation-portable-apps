"""第 8 版：拿 v7_1_1（静态、器型已验证正确）当底，**只把四个罐子抬起来漂浮**。

思路（2026-10-21）：
  * v7_1_1 已经过了核对：罐子是图1 的器型、四个、红/黄/蓝/紫、甜甜圈淋好酱、一一对应；
  * 用户的诉求只是「让罐子漂浮起来浇」——那就把这次编辑**缩小成一件事**：
    **只改变四个罐子的垂直位置**（抬到甜甜圈正上方），其余一律不动；
  * 改动越小，模型越不会顺手"改良"器型（这是前 6 版失败的原因：一次要它搬+复制+重排+变四个）。

输入：image_1 = v7_1_1（底）、image_2 = 图1 罐子放大特写（器型基准）、image_3 = 图3（酱的质感）。

用法：python _diag/make_jar_scene8.py [张数]
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
OUT_DIR = ANCHOR / "scene8"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"

INPUTS = {
    "image_1": ANCHOR / "scene7" / "v7_1_1.png",   # ← 底：静态那版（罐子在桌上、器型正确）
    "image_2": ANCHOR / "fig1_jar_big.png",        # 器型基准
    "image_3": SCENE / "ref_image-3.png",          # 酱的质感
}

PROMPT = (
    "以第 1 张图为底图，**只做一个改动**：把桌面上那四个陶瓷酱罐**整体抬起来、漂浮到盘子里"
    "四只甜甜圈的正上方**（第 1 个罐子正对第 1 只甜甜圈，第 2 个对第 2 只，依次类推，四列对齐、间距均匀），"
    "并在每个罐口下方画出一股**与罐子同色**的巧克力酱流、落到正下方那只甜甜圈上。\n"
    "**除了罐子的垂直位置，其它一律保持原样**：四个罐子的形状、大小、颜色、朝向、盖子（扁平圆盘+"
    "中央细长圆柱提手+金色卡扣）与**不透明高光陶瓷**材质完全不变（参考第 2 张图）；"
    "女巫、姿势、桌面、盘子、甜甜圈与四色酱、灯光、背景都保持原样。\n"
    "原来放罐子的桌面位置空出来、露出木桌即可。\n"
    "四股酱流彼此不交叉、不汇合、不落到别的甜甜圈或桌面；不要新增或删除任何物体；"
    "不要出现文字、数字、logo、水印、品牌标识。"
)
#: 注：这类"只抬一个位置"的最小改动，是唯一在实测里保住过器型的做法（v7 静态版就是这么过的）。


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
            target = OUT_DIR / f"v8_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
