"""生成「四个彩色复古陶瓷酱罐」锚点图（走本机图片模块 8797 + 公司方舟 Key）。

用途：女巫甜甜圈那条文档要求「四个酱罐全程不变」，但参考素材里**没有罐子特写** ——
模型没有依据，只能自己造（成片里变成了四个彩色玻璃瓶）。这里补一张外观基准图。

用法：
    python _diag/make_jar_anchor.py            # 生成 2 张（方便挑）
    python _diag/make_jar_anchor.py 4          # 生成 4 张
产物落到 _diag/anchor/。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = "http://127.0.0.1:8797"
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "_diag" / "anchor"
PROVIDER = "volcengine"
MODEL = "ep-20260912121809-6wt62"          # Seedream 5.0 Pro（公司 Key）
PROMPT = (
    "产品级单张参考图：四个并排站立的复古陶瓷巧克力酱罐，正面平视、等距排列、高度与大小完全一致。"
    "罐体为温润哑光陶瓷，圆顶陶瓷盖与罐身同色，盖子顶部各有一个小金星星装饰；罐身有细金色描边与极简"
    "万圣节南瓜暗纹。四个罐子的主色依次为：正红、明黄、宝蓝、紫罗兰，四色饱和度适中、彼此区分非常明显、"
    "不要渐变不要混色。纯深紫色背景，影棚柔和布光、光从右前上方来，罐子底部有轻微接触阴影，"
    "陶瓷高光细腻、边缘干净。画面里不要出现任何文字、数字、logo、水印、品牌标识；不要出现人物与桌面杂物。"
    "构图居中、四个罐子完整入画、清晰锐利。"
)


def post_form(path: str, fields: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        APP + path, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(APP + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    count = sys.argv[1] if len(sys.argv) > 1 else "2"
    created = post_form("/api/jobs", {
        "provider": PROVIDER,
        "model": MODEL,
        "mode": "text2img",
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "response_format": "url",
        "repeat_count": count,
        "concurrency": "1",
        "prompt": PROMPT,
    })
    job_id = created.get("job_id")
    print("提交:", json.dumps(created, ensure_ascii=False))
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
    results = job.get("results") or []
    print(f"结果 {len(results)} 条；错误: {job.get('errors')}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(results, 1):
        url = item.get("download_url") or item.get("url")
        if not url:
            continue
        if url.startswith("/"):
            url = APP + url
        target = OUT_DIR / f"jars_{index}.png"
        with urllib.request.urlopen(url, timeout=120) as response:
            target.write_bytes(response.read())
        print(f"  -> {target} ({target.stat().st_size:,} bytes)  filename={item.get('filename')}")
    for event in (job.get("events") or [])[-6:]:
        print("   event:", event.get("message"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
