"""真实端到端：超限参考视频 → 自动压缩 → 真实出片（受预算约束，最多 1 次生成）。

复刻用户那条报错 run 的现场（`13268f86-1522-43f4-ada4-35cdfc72b9e6`）：

1. `POST /api/runs` 建一条**独立运行**（直连入口，没有任何多维表格绑定 → 不会写回
   飞书表；跑完停在 `waiting_review` 等人工审片，也不会自动交付）；
2. 用**真实接口** `POST /api/runs/{id}/references` 把用户那条 34.06MiB 参考视频
   挂到任务上（用户当初就是这么传的）；
3. `POST /api/runs/{id}/decision {"action":"approve","selected_task_ids":[task]}` 真实
   批准**一条**任务 → 执行 → 观察「体积超限 → 自动压缩」是否发生、是否出片；
4. 下载成片、打印压缩事件里的具体数字；
5. `DELETE /api/runs/{id}` 清理。

用法：
    .venv\\Scripts\\python.exe _e2e_oversize_compression.py A   # 建运行 + 挂视频
    .venv\\Scripts\\python.exe _e2e_oversize_compression.py B   # 批准 + 等出片
    .venv\\Scripts\\python.exe _e2e_oversize_compression.py C   # 清理（DELETE）
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
#: 用户报错那条需求（run 13268f86 的 source_url）。
SOURCE_URL = "https://redcqchina.feishu.cn/wiki/A82AwG6Avi2rxSk7woUcksU4nLe"
#: 用户那条 34.06MiB 的参考视频（从报错 run 的 inputs/ 里取）。
VIDEO = (
    APP_DIR
    / "data"
    / "runs"
    / "13268f86-1522-43f4-ada4-35cdfc72b9e6"
    / "inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4"
)
STATE = APP_DIR / "_e2e_oversize_state.json"
OUT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\oversize_compression")
APPROVAL_STATUSES = {"waiting_approval", "waiting_review"}
GENERATION_LIMIT = 32 * 1024 * 1024


def _state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def _save(data: dict) -> None:
    STATE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _dump(name: str, payload) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print("wrote", path)
    return path


def _client(timeout: float = 180.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=timeout, trust_env=False
    )


def _wait(client: httpx.Client, run_id: str, want, timeout: float = 900.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    view: dict = {}
    while time.monotonic() < deadline:
        view = client.get(f"/api/runs/{run_id}").json()
        status = view.get("status")
        if status != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status}")
            last = status
        if want(view):
            return view
        time.sleep(5)
    return view


def _events(view: dict) -> str:
    lines = []
    for event in view.get("events") or []:
        if isinstance(event, dict):
            lines.append(
                f"[{event.get('node')}/{event.get('status')}] "
                f"{event.get('summary')}"
            )
        else:
            lines.append(str(event))
    return "\n".join(lines)


def stage_a() -> None:
    if not VIDEO.is_file():
        raise SystemExit(f"参考视频不存在：{VIDEO}")
    size = VIDEO.stat().st_size
    print(
        f"参考视频：{VIDEO.name} {size} B = {size / 1024 / 1024:.2f} MiB "
        f"（本机闸门 {GENERATION_LIMIT} B，超 {size - GENERATION_LIMIT} B）"
    )
    with _client() as client:
        response = client.post(
            "/api/runs",
            json={"source_url": SOURCE_URL, "planning_mode": "video"},
        )
        print("POST /api/runs ->", response.status_code, response.text[:300])
        response.raise_for_status()
        run_id = response.json()["run_id"]
        print("run_id =", run_id)
        _save({"run_id": run_id, "video": str(VIDEO), "video_bytes": size})

        view = _wait(
            client,
            run_id,
            lambda v: v.get("status") in APPROVAL_STATUSES
            or v.get("status") in {"failed", "cancelled"},
            timeout=1200,
        )
        _dump("A_planning_view.json", view)
        if view.get("status") not in APPROVAL_STATUSES:
            raise SystemExit(f"没有进入待审批：{view.get('status')}")

        tasks = (view.get("approval") or {}).get("tasks") or []
        print("tasks:", [(t.get("task_id"), t.get("task_type")) for t in tasks])
        task = next(
            (
                item
                for item in tasks
                if item.get("task_type") == "image_to_video"
            ),
            None,
        )
        if task is None:
            task = tasks[0]
        print("选中 task:", task.get("task_id"), task.get("task_type"))
        print("原有引用：", json.dumps(task.get("reference_images"), ensure_ascii=False))
        existing = task.get("reference_images") or []
        order = max([int(ref.get("order") or 0) for ref in existing] or [0]) + 1
        upload = client.post(
            f"/api/runs/{run_id}/references",
            files={"file": (VIDEO.name, VIDEO.read_bytes(), "video/mp4")},
            data={
                "task_id": task["task_id"],
                "role": "reference_video",
                "order": str(order),
            },
        )
        print("POST references ->", upload.status_code, upload.text[:300])
        upload.raise_for_status()

        view = client.get(f"/api/runs/{run_id}").json()
        _dump("A_with_video_view.json", view)
        refreshed = next(
            item
            for item in (view["approval"]["tasks"])
            if item["task_id"] == task["task_id"]
        )
        print(
            "挂上视频后的引用：",
            json.dumps(refreshed.get("reference_images"), ensure_ascii=False),
        )
        print("提示词（前 400）：")
        print((refreshed.get("prompt") or "")[:400])
        videos = [
            asset
            for asset in (view["approval"].get("media_assets") or [])
            if str(asset.get("mime_type", "")).startswith("video/")
        ]
        print("view 里的视频素材：", json.dumps(videos, ensure_ascii=False))
        uploaded = APP_DIR / "data" / "runs" / run_id / "inputs"
        if uploaded.is_dir():
            for path in sorted(uploaded.iterdir()):
                print(
                    f"  落盘 {path.name} {path.stat().st_size} B "
                    f"超闸门={path.stat().st_size > GENERATION_LIMIT}"
                )
        _save({**_state(), "task_id": task["task_id"]})


def stage_b() -> None:
    data = _state()
    run_id = data["run_id"]
    task_id = data["task_id"]
    with _client(timeout=300) as client:
        response = client.post(
            f"/api/runs/{run_id}/decision",
            json={"action": "approve", "selected_task_ids": [task_id]},
        )
        print("POST decision ->", response.status_code, response.text[:400])
        response.raise_for_status()

        view = _wait(
            client,
            run_id,
            lambda v: bool(v.get("artifacts"))
            or v.get("status") in {"failed", "cancelled", "waiting_review"},
            timeout=2400,
        )
        _dump("B_final_view.json", view)
        print("status:", view.get("status"))
        print("execution_records:", json.dumps(
            view.get("execution_records"), ensure_ascii=False, indent=1
        )[:3000])
        print("=== 事件（压缩相关）===")
        text = _events(view)
        for line in text.splitlines():
            if any(
                key in line
                for key in ("压缩", "裁剪", "抽帧", "参考视频", "体积")
            ):
                print(" ", line)
        print("=== 全部事件 ===")
        print(text[:4000])
        artifacts = view.get("artifacts") or []
        print("artifacts:", [(a.get("artifact_id"), a.get("kind"), a.get("size")) for a in artifacts])
        for artifact in artifacts:
            artifact_id = artifact.get("artifact_id")
            content = client.get(
                f"/api/runs/{run_id}/artifacts/{artifact_id}/content"
            )
            print("download", artifact_id, "->", content.status_code, len(content.content))
            if content.status_code == 200 and content.content:
                suffix = ".mp4" if artifact.get("kind") != "image" else ".png"
                path = OUT / f"OUTPUT_{artifact_id[:12]}{suffix}"
                path.write_bytes(content.content)
                print("saved", path, path.stat().st_size)


def stage_c() -> None:
    data = _state()
    run_id = data["run_id"]
    with _client(timeout=120) as client:
        response = client.delete(f"/api/runs/{run_id}")
        print("DELETE ->", response.status_code, response.text[:300])
        probe = client.get(f"/api/runs/{run_id}")
        print("DELETE 后 GET ->", probe.status_code, probe.text[:120])


if __name__ == "__main__":
    stage = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    if stage == "A":
        stage_a()
    elif stage == "B":
        stage_b()
    elif stage == "C":
        stage_c()
    else:
        raise SystemExit("unknown stage")
