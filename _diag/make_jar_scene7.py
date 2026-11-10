"""第 7 版参考图：**罐子留在桌上原位**（不悬浮、不搬家），只做三处局部编辑。

为什么这样改：前 6 版我都要模型"把四个罐子悬浮到甜甜圈正上方"—— 那是一次**大姿态改造**
（换位置 + 缩放 + 复制成四个），模型一改就把器型换成了细长玻璃瓶。
用户 2026-10-21 口径：先出参考图，「四个罐子倒对应颜色的巧克力酱到对应的甜甜圈上」。

所以这一版只要求：
  ① 桌上左侧的罐子 → 沿用图1 那几只的**同款器型**（梨形身+圆盘盖+细长圆柱提手+金色卡扣+不透明高光陶瓷），
     变成并排四个、换色红/黄/蓝/紫，**位置留在桌面原位、不悬浮不抬手**；
  ② 盘子里的甜甜圈 → 淋好对应颜色的酱（终态）；
  ③ 每股酱从对应罐口斜向落到同色甜甜圈上（第1罐→第1只），四股不交叉、不落到别处。

输入三张：图1（底）、图1 单个罐子放大特写（器型基准）、图3（酱的质感）。

用法：python _diag/make_jar_scene7.py [张数]
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
ROOT = Path(__file__).resolve().parents[1]          # ai-generation-portable-apps
SCENE = ROOT / "feishu-generation-agent" / "_diag" / "frames" / "35586c61"
ANCHOR = ROOT / "_diag" / "anchor"
OUT_DIR = ANCHOR / "scene7"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"

INPUTS = {
    "image_1": SCENE / "ref_image-1.png",      # 底图（场景、桌面、罐子原位）
    "image_2": ANCHOR / "fig1_jar_big.png",    # 图1 那个罐子的放大特写（器型基准）
    "image_3": SCENE / "ref_image-3.png",      # 四色酱的质感
}

PROMPT = (
    "以第 1 张参考图为底图，**只做下面三处局部编辑，其余画面一律不动**"
    "（女巫、姿势、桌面、盘子位置、背景、灯光、色调都保持原样）：\n"
    "1) 桌上左侧的酱罐：沿用第 2 张参考图（第 1 张图里罐子的放大特写）的**同款器型**——"
    "瓶身下宽上窄的梨形、线条圆润、中等偏矮；盖子是**扁平圆盘**，中央竖着一个**细长圆柱形提手**，"
    "盖子边缘一圈**金色金属卡扣**；表面是**不透明光滑高光釉面陶瓷**（明亮反光）。"
    "把它们变成**并排的四个**（就摆在原来那几只罐子的位置附近、间距均匀、同一条桌面直线上），"
    "从左到右颜色依次为正红、明黄、宝蓝、紫罗兰（提手浅灰白、卡扣金色）。"
    "**罐子必须留在桌面上、保持原来的高度与大小，不要悬浮、不要升起、不要倾斜、不要变高变瘦、"
    "不要透明玻璃质感、不要球形瓶身、不要删掉提手与金色卡扣。**\n"
    "2) 盘子里四只甜甜圈改成**已经淋好酱的终态**：从左到右分别淋正红、明黄、宝蓝、紫罗兰四色"
    "巧克力酱，每只甜甜圈表面只有一种颜色，酱黏稠厚重、缓慢垂落、表面光泽细腻"
    "（质感参考第 3 张参考图）。\n"
    "3) 从每个罐口画出一股巧克力酱，**斜向流到与自己同色的那只甜甜圈上**：第 1 个罐子只浇第 1 只、"
    "第 2 个只浇第 2 只，依次类推；四股酱流彼此不交叉、不汇合，不落到别的甜甜圈、罐盖或灯笼上。\n"
    "不要出现任何文字、数字、logo、水印、品牌标识；不要新增多余的罐子、甜甜圈或其他道具。"
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
            target = OUT_DIR / f"v7_{index}_{image_index}.png"
            with urllib.request.urlopen(url, timeout=180) as response:
                target.write_bytes(response.read())
            saved += 1
            print(f"  -> {target} ({target.stat().st_size:,} bytes)")
    print(f"共保存 {saved} 张")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
