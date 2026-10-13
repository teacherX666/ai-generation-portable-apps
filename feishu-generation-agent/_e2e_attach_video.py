"""稳健版 stage A：等审批 → 挂参考视频 → 把其它引用摘掉（每次请求新连接）。

用法：python _e2e_attach_video.py <run_id> <size|codec>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
APP_DIR = Path(__file__).resolve().parent
VIDEOS = {
    "size": APP_DIR
    / "data/runs/13268f86-1522-43f4-ada4-35cdfc72b9e6/inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4",
    "codec": APP_DIR
    / "data/runs/PmN5dYXLEoa0gAxz3CjcnPXKnKf/inputs"
    / "59149a36afd22f2381c6284cc643241157b7322c694d8bb6b8b6c81e38c10147.mp4",
}
APPROVAL = {"waiting_approval", "waiting_review"}

run_id = sys.argv[1]
case = sys.argv[2]
video = VIDEOS[case]


def client(timeout: float = 300.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=timeout, trust_env=False
    )


def get_view() -> dict:
    with client() as c:
        return c.get(f"/api/runs/{run_id}").json()


deadline = time.monotonic() + 1800
view: dict = {}
while time.monotonic() < deadline:
    try:
        view = get_view()
    except Exception as exc:  # noqa: BLE001
        print("poll error:", type(exc).__name__)
        time.sleep(10)
        continue
    if view.get("status") in APPROVAL or view.get("status") in {"failed", "cancelled"}:
        break
    time.sleep(8)

print("status:", view.get("status"))
tasks = (view.get("approval") or {}).get("tasks") or []
print("tasks:", [(t.get("task_id"), t.get("task_type")) for t in tasks])
task = next(
    (item for item in tasks if item.get("task_type") == "image_to_video"), tasks[0]
)
task_id = task["task_id"]
existing = task.get("reference_images") or []
print("原有引用:", json.dumps(existing, ensure_ascii=False))
print("size:", video.stat().st_size)
order = max([int(ref.get("order") or 0) for ref in existing] or [0]) + 1
with client() as c:
    upload = c.post(
        f"/api/runs/{run_id}/references",
        files={"file": (video.name, video.read_bytes(), "video/mp4")},
        data={"task_id": task_id, "role": "reference_video", "order": str(order)},
    )
print("POST references ->", upload.status_code, upload.text[:300])
upload.raise_for_status()
keep = upload.json()["asset_id"]
print("keep =", keep)

view = get_view()
task = next(
    item for item in view["approval"]["tasks"] if item["task_id"] == task_id
)
with client() as c:
    for reference in list(task.get("reference_images") or []):
        if reference["asset_id"] == keep:
            continue
        response = c.delete(
            f"/api/runs/{run_id}/tasks/{task_id}/references/{reference['asset_id']}"
        )
        print("prune", reference["asset_id"], "->", response.status_code)

view = get_view()
task = next(
    item for item in view["approval"]["tasks"] if item["task_id"] == task_id
)
print("最终引用：", json.dumps(task.get("reference_images"), ensure_ascii=False))
print("status:", view.get("status"))
Path(APP_DIR / f"_e2e_repair_state_{case}.json").write_text(
    json.dumps(
        {"run_id": run_id, "task_id": task_id, "keep": keep, "video": str(video)},
        ensure_ascii=False,
        indent=1,
    ),
    encoding="utf-8",
)
