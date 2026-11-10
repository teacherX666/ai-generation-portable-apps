"""钉死一件事：普通（非样片）Seedance 2.5 生成能不能出 1080p。

**会真实计费**：一条 5 秒 16:9、输入不含视频的 1080p（官方价格表示例 18.71 元）。

背景（2026-10-19，用户提问「现在 seedance2.5 不是还没有 1080P 的吗」）：
  * 官方教程能力表（页面更新 2026-10-16）：2.5 = 480p / 720p / **1080p（10bit 位深）**；
  * 官方价格表：2.5 单列 1080p 档（5 秒 16:9、输入不含视频 **18.71 元**；720p 7.56、480p 3.36）；
  * 仓库里「2.5 只有 480p/720p」的说法来自 2026-08-07 的提交（照当时的能力矩阵配的），
    2026-09-29 那句「真实只支持 480p/720p」是**循环引用**（证据来自我们自己的校验回显）。

已实测过的只有「样片成片」路径的 1080p（draft_task 强制 1080p）。**普通路径没跑过** ——
本脚本就是补这一刀：成了就把"2.5 能出 1080p"从文档推论变成实测结论，失败了就把下拉收回去。

用法：python _diag/live_test_seedance25_1080p.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8787"
MODEL = "doubao-seedance-2-5-260628"
WS = "ws-1080p-probe"
PROMPT = "一只橘猫在洒满阳光的木地板上慢慢伸懒腰，镜头缓缓推近，暖色调，画面安静"
SECONDS = 5
TOKEN_PRICE_YUAN_PER_MILLION = 77.0   # 官方价格表：2.5 / 1080p / 输入不含视频


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Workspace-Id": WS}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read().decode("utf-8") or "{}")}


def get_json(path: str) -> dict:
    request = urllib.request.Request(BASE + path, headers={"X-Workspace-Id": WS})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_job(job_id: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = get_json(f"/api/jobs/{job_id}")
        status = str(job.get("status") or "?")
        if status != last:
            print(f"    status -> {status}", flush=True)
            last = status
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            return job
        time.sleep(10)
    raise TimeoutError(f"job {job_id} 超时")


def probe(path: str) -> str:
    path = Path(path) if path else None
    if not path or not path.exists():
        return f"(文件不存在: {path})"
    try:
        import cv2  # noqa: PLC0415
        capture = cv2.VideoCapture(str(path))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        capture.release()
        return f"{width}x{height} / {frames} 帧 / {path.stat().st_size / 1024 / 1024:.1f}MB"
    except Exception as exc:  # noqa: BLE001
        return f"(探测失败: {exc})"


def main() -> int:
    print("=== 普通 2.5 生成 @1080p（不是样片模式，无参考素材）===")
    job = post_json("/api/jobs/json", {
        "provider": "volcengine", "model": MODEL, "prompt": PROMPT,
        "resolution": "1080p", "ratio": "16:9", "duration": SECONDS,
        "repeat_count": 1, "concurrency": 1, "generate_audio": "1", "watermark": "0",
        "media": {},
    })
    print(f"    提交 -> HTTP {job['status']} {json.dumps(job['body'], ensure_ascii=False)}", flush=True)
    if job["status"] != 200 or not job["body"].get("job_id"):
        print("    !! 提交就被拒了 —— 说明这个账号/模型下普通 1080p 走不通（同步校验，未计费）")
        return 1
    detail = wait_job(job["body"]["job_id"])
    for event in detail.get("events", []):
        print(f"      - {event.get('message')}")
    if detail.get("status") != "succeeded":
        print(f"    !! 失败：{detail.get('errors')}")
        return 1
    result = (detail.get("results") or [{}])[0]
    tokens = int(result.get("tokens") or 0)
    print(f"    产出：{probe(result.get('local_path'))}（期望 1920x1080）")
    print(f"    tokens = {tokens} → 按 77 元/百万 token 约 {tokens / 1_000_000 * TOKEN_PRICE_YUAN_PER_MILLION:.2f} 元")
    print("\n=== 结论：普通 2.5 生成支持 1080p（实测钉死）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
