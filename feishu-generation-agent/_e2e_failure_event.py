"""零成本活体验证：**任何失败尝试都要在事件里留带原文的一条**。

做法：建一条独立 run，挂一个 **>32MiB 的假 mp4**（本机闸门必然拒它，且压缩不可能成功
→ 一定会走到「如实失败」那条路），批准后检查运行事件里是否出现
`生成失败（generation_input_too_large）：参考素材无法用于生成`。

**零上游费用**：本机闸门在发出任何请求之前就拒了（`_diag_repro_input_too_large.py`
已证明 0 次上游请求）。用完 `DELETE /api/runs/{id}` 清理。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

APP_DIR = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Portal-User-Id": "6b34d9e8-3058-41b6-89b2-4cedac2362ee"}
SOURCE_URL = "https://redcqchina.feishu.cn/wiki/A82AwG6Avi2rxSk7woUcksU4nLe"
FAKE = APP_DIR / "_e2e_fake_oversize.mp4"
STATE = APP_DIR / "_e2e_failure_event_state.json"


def client(timeout: float = 180.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=timeout, trust_env=False
    )


def get_view(run_id: str) -> dict:
    with client() as c:
        return c.get(f"/api/runs/{run_id}").json()


def main() -> None:
    # 造一个 >32MiB 的假 mp4（真 ftyp 头 + 垃圾字节）：本机闸门必拒，
    # 压缩也不可能成功（解不出帧数）。
    with FAKE.open("wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypmp42")
        handle.write(b"\x00" * (33 * 1024 * 1024))
    size = FAKE.stat().st_size
    print(f"假参考视频 {FAKE.name} {size} B（{size / 1024 / 1024:.2f} MiB）")

    with client() as c:
        response = c.post(
            "/api/runs",
            json={"source_url": SOURCE_URL, "planning_mode": "video"},
        )
        response.raise_for_status()
        run_id = response.json()["run_id"]
    print("run_id =", run_id)
    STATE.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    deadline = time.monotonic() + 1500
    view: dict = {}
    while time.monotonic() < deadline:
        view = get_view(run_id)
        if view.get("status") in {"waiting_approval", "waiting_review", "failed"}:
            break
        time.sleep(8)
    print("status:", view.get("status"))
    tasks = (view.get("approval") or {}).get("tasks") or []
    task = next(
        (item for item in tasks if item.get("task_type") == "image_to_video"),
        tasks[0],
    )
    task_id = task["task_id"]
    order = max(
        [int(ref.get("order") or 0) for ref in (task.get("reference_images") or [])]
        or [0]
    ) + 1
    with client(timeout=300) as c:
        upload = c.post(
            f"/api/runs/{run_id}/references",
            files={"file": (FAKE.name, FAKE.read_bytes(), "video/mp4")},
            data={
                "task_id": task_id,
                "role": "reference_video",
                "order": str(order),
            },
        )
    print("POST references ->", upload.status_code, upload.text[:200])
    upload.raise_for_status()

    with client(timeout=300) as c:
        decision = c.post(
            f"/api/runs/{run_id}/decision",
            json={"action": "approve", "selected_task_ids": [task_id]},
        )
    print("POST decision ->", decision.status_code, decision.text[:200])

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        view = get_view(run_id)
        if view.get("status") in {"failed", "waiting_review", "cancelled"}:
            break
        time.sleep(6)

    print("status:", view.get("status"))
    print("=== 事件 ===")
    for event in view.get("events") or []:
        summary = str(event.get("summary") if isinstance(event, dict) else event)
        print("  -", summary[:300])
    print("=== execution_records ===")
    print(json.dumps(view.get("execution_records"), ensure_ascii=False)[:1200])
    print("last_error:", json.dumps(view.get("last_error"), ensure_ascii=False)[:400])

    with client() as c:
        print("DELETE ->", c.delete(f"/api/runs/{run_id}").text[:200])
    FAKE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
