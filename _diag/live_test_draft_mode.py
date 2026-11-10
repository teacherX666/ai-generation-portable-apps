"""真机实测 Seedance 2.5 样片模式（Draft）两段式。

**会真实计费**：一次 480p 样片 + 一次 720p 成片（用户 2026-10-15 明确同意花这一次实测）。

要验的三件事（都是「本地单测证明不了、只能真打一次」的）：
  1. 第一段故意带 `resolution=720p`：本地必须把它钉成 480p 再下发（官方硬约束），
     事件里要写明这次改写 —— 顺带拿到真实的 Draft 任务 id；
  2. 第二段只发 `draft_task.id` + 目标分辨率（官方说「部分参数禁止重复传入」，
     但逐条表格我们没读到）—— 验证这个最小请求体能过；
  3. 用 ffprobe 量两条产物的分辨率，确认「480P 试错 → 720P 成片」。

用法：python _diag/live_test_draft_mode.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"
MODEL = "doubao-seedance-2-5-260628"
WS = "ws-draft-selftest"
PROMPT = "一只橘猫在洒满阳光的木地板上慢慢伸懒腰，镜头缓缓推近，暖色调，画面安静"
DRAFT_SECONDS = 5


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Workspace-Id": WS},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read().decode("utf-8") or "{}")}


def get_json(path: str) -> dict:
    request = urllib.request.Request(BASE + path, headers={"X-Workspace-Id": WS})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_job(job_id: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = get_json(f"/api/jobs/{job_id}")
        status = str(job.get("status") or "?")
        if status != last:
            print(f"    status -> {status} ({int(time.time() - (deadline - timeout))}s)")
            last = status
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            return job
        time.sleep(8)
    raise TimeoutError(f"job {job_id} 超过 {timeout}s 还没结束")


def probe(path: str) -> str:
    if not path or not Path(path).exists():
        return f"(文件不存在: {path})"
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration", "-of", "csv=p=0", str(path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        return (out.stdout or "").strip() or (out.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(ffprobe 失败: {exc})"


def main() -> int:
    print("=== 第一段：样片（draft=1，故意带 resolution=720p 验本地改写）===")
    stage1 = post_json("/api/jobs/json", {
        "provider": "volcengine", "model": MODEL, "prompt": PROMPT,
        "draft": "1", "resolution": "720p", "ratio": "16:9",
        "duration": DRAFT_SECONDS, "repeat_count": 1, "concurrency": 1,
        "generate_audio": "1", "watermark": "0", "media": {},
    })
    print(f"    提交 -> HTTP {stage1['status']} {json.dumps(stage1['body'], ensure_ascii=False)}")
    if stage1["status"] != 200 or not stage1["body"].get("job_id"):
        print("    !! 第一段提交失败，停止（没花钱或只花了被拒的一次）")
        return 1
    job1 = wait_job(stage1["body"]["job_id"])
    print("    事件：")
    for event in job1.get("events", []):
        print(f"      - {event.get('message')}")
    if job1.get("status") != "succeeded":
        print(f"    !! 第一段失败：{job1.get('errors')}")
        return 1
    result1 = (job1.get("results") or [{}])[0]
    draft_id = str(result1.get("task_id") or "")
    print(f"    Draft 任务 id = {draft_id}")
    print(f"    样片分辨率 = {probe(result1.get('local_path'))}（期望 854x480）")
    if not draft_id:
        print("    !! 没拿到 Draft 任务 id，第二段无从谈起")
        return 1

    print("\n=== 第二段：成片（content 只发 draft_task.id + resolution=720p）===")
    stage2 = post_json("/api/jobs/json", {
        "provider": "volcengine", "model": MODEL,
        "prompt": "用样片出成片", "draft_task_id": draft_id,
        "resolution": "720p", "ratio": "adaptive", "duration": -1,
        "repeat_count": 1, "concurrency": 1, "media": {},
    })
    print(f"    提交 -> HTTP {stage2['status']} {json.dumps(stage2['body'], ensure_ascii=False)}")
    if stage2["status"] != 200 or not stage2["body"].get("job_id"):
        print("    !! 第二段提交失败（最小请求体可能被拒 —— 这正是本次要验的点）")
        return 2
    job2 = wait_job(stage2["body"]["job_id"])
    print("    事件：")
    for event in job2.get("events", []):
        print(f"      - {event.get('message')}")
    if job2.get("status") != "succeeded":
        print(f"    !! 第二段失败：{job2.get('errors')}")
        return 2
    result2 = (job2.get("results") or [{}])[0]
    print(f"    成片任务 id = {result2.get('task_id')}")
    print(f"    成片分辨率 = {probe(result2.get('local_path'))}（期望 1280x720）")
    print(f"    成片时长字段 = {result2.get('duration')}s；tokens = {result2.get('tokens')}")

    print("\n=== 结论 ===")
    print("  两段都成功：draft=1 能出 480p 样片；基于它 + 只重新指定分辨率能出成片。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
