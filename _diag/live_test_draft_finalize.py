"""样片模式**第二段**的补测：用已有 Draft 任务 id 出 1080p 成片。

为什么单独一个脚本：2026-10-15 第一次真机实测时，第一段（480p 样片）成功、第二段因为
`resolution=720p` 被方舟同步拒 ——

    the parameter resolution specified in the request is not valid for model
    doubao-seedance-2-5 in draft_task final stage; received: "720p"

随后从官方文档的 Markdown 版（页面地址后加 `.md`，绕开抓取截断）拿到**权威参数表**：
成片阶段 `resolution` 必须传、且**仅支持 1080p**；提示词/素材/时长/比例/种子/有声/任务类型
全部自动复用、禁止传入。本脚本就是按新规则重跑第二段（不再多花一次 480p 的钱）。

**会真实计费**：一次 1080p 成片（时长沿用样片）。

用法：python _diag/live_test_draft_finalize.py [draft_task_id]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8787"
MODEL = "doubao-seedance-2-5-260628"
WS = "ws-draft-selftest"
DEFAULT_DRAFT_ID = "cgt-20261019145332-mk7hx"          # 第一次实测产出的 Draft 任务 id（7 天有效）
DRAFT_FILE = Path(__file__).resolve().parent.parent / "seedance" / "outputs" / "unknown" / "2026-10-19" / "20261019_145454_run1_cgt-20261019145332-mk7hx.mp4"


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


def wait_job(job_id: str, timeout: float = 1200) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = get_json(f"/api/jobs/{job_id}")
        status = str(job.get("status") or "?")
        if status != last:
            print(f"    status -> {status}")
            last = status
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            return job
        time.sleep(10)
    raise TimeoutError(f"job {job_id} 超时未结束")


def probe(path: str | Path) -> str:
    """用 cv2 读分辨率（本机 PATH 上没有 ffprobe；全局 Python312 有 cv2）。"""
    path = Path(path)
    if not path.exists():
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
    draft_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DRAFT_ID
    print(f"样片文件：{probe(DRAFT_FILE)}（期望 854x480）")
    print(f"Draft 任务 id = {draft_id}")
    print("\n=== 第二段：resolution=1080p（官方 Step2 参数表要求）===")
    job = post_json("/api/jobs/json", {
        "provider": "volcengine", "model": MODEL, "prompt": "用样片出成片",
        "draft_task_id": draft_id, "resolution": "1080p", "duration": -1,
        "ratio": "adaptive", "repeat_count": 1, "concurrency": 1, "media": {},
    })
    print(f"    提交 -> HTTP {job['status']} {json.dumps(job['body'], ensure_ascii=False)}")
    if job["status"] != 200 or not job["body"].get("job_id"):
        print("    !! 提交失败")
        return 1
    detail = wait_job(job["body"]["job_id"])
    print("    事件：")
    for event in detail.get("events", []):
        print(f"      - {event.get('message')}")
    if detail.get("status") != "succeeded":
        print(f"    !! 成片失败：{detail.get('errors')}")
        return 1
    result = (detail.get("results") or [{}])[0]
    print(f"    成片任务 id = {result.get('task_id')}")
    print(f"    成片：{probe(result.get('local_path'))}（期望 1920x1080）")
    print(f"    tokens = {result.get('tokens')}，时长字段 = {result.get('duration')}s")
    print("\n=== 结论：样片模式两段式在线上通了（480p 样片 → 1080p 成片）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
