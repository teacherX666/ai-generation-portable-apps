"""真实端到端：参考视频「体积超限 / 编码不被上游接受」→ 自动修复 → 真实出片。

两个用例（每次真实生成 **1 条**，预算内）：

- `size`：用户那条 **34.06 MiB H.264** 参考视频（撞本机 32 MiB 闸门
  → `generation_input_too_large`）；
- `codec`：用户那条 **4K HEVC** 参考视频（上游 `invalid_media` 拒掉的那条原文件）。

流程（两个用例相同，尽量贴近用户真实操作）：

1. `POST /api/runs` 建一条**独立运行**（直连入口，**没有任何多维表格绑定** →
   不会写回飞书表；跑完停在 `waiting_review` 等人工审片，也不会自动交付）；
2. `POST /api/runs/{id}/references` 用**真实接口**把参考视频挂到任务上
   （用户当初就是这么传的）；
3. `POST /api/runs/{id}/decision {"action":"approve","selected_task_ids":[task]}`
   真实批准**一条**任务 → 执行；
4. 打印**压缩/转码事件里的具体数字**、下载成片、核对执行记录；
5. `DELETE /api/runs/{id}` 清理。

用法：
    .venv\\Scripts\\python.exe _e2e_reference_repair.py A size
    .venv\\Scripts\\python.exe _e2e_reference_repair.py B size
    .venv\\Scripts\\python.exe _e2e_reference_repair.py C size
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
#: 用户报错的那条需求（run 13268f86 的 source_url）。该文档只有图片，没有视频，
#: 所以「参考视频」完全由我上传的那条决定 —— 实验变量干净。
SOURCE_URL = "https://redcqchina.feishu.cn/wiki/A82AwG6Avi2rxSk7woUcksU4nLe"
VIDEOS = {
    # 用户那条撞本机 32MiB 闸门的参考视频（720x1280 H.264 30fps 20.9s 34.06MiB）。
    "size": APP_DIR
    / "data/runs/13268f86-1522-43f4-ada4-35cdfc72b9e6/inputs"
    / "752f4f4197fa196dd669a68f24f3cf1b1ffbf0aff295c02e46a8f59ea41a7bfb.mp4",
    # 用户那条被上游 `invalid_media` 拒掉的参考视频（2160x3840 HEVC Main 59.3fps 2.49s）。
    "codec": APP_DIR
    / "data/runs/PmN5dYXLEoa0gAxz3CjcnPXKnKf/inputs"
    / "59149a36afd22f2381c6284cc643241157b7322c694d8bb6b8b6c81e38c10147.mp4",
}
OUT_ROOT = Path(r"C:\Users\123\Documents\LTX\_quality_analysis\reference_repair")
APPROVAL_STATUSES = {"waiting_approval", "waiting_review"}
LIMIT = 32 * 1024 * 1024


def _state(case: str) -> dict:
    path = APP_DIR / f"_e2e_repair_state_{case}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save(case: str, data: dict) -> None:
    path = APP_DIR / f"_e2e_repair_state_{case}.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _out(case: str) -> Path:
    path = OUT_ROOT / case
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dump(case: str, name: str, payload) -> None:
    path = _out(case) / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print("wrote", path)


def _client(timeout: float = 300.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE, headers=HEADERS, timeout=timeout, trust_env=False
    )


def _wait(client: httpx.Client, run_id: str, want, timeout: float = 1800.0) -> dict:
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


def _events(view: dict) -> list[str]:
    lines = []
    for event in view.get("events") or []:
        if isinstance(event, dict):
            lines.append(
                f"[{event.get('node')}/{event.get('status')}] "
                f"{event.get('summary')}"
            )
        else:
            lines.append(str(event))
    return lines


def stage_a(case: str) -> None:
    video = VIDEOS[case]
    if not video.is_file():
        raise SystemExit(f"参考视频不存在：{video}")
    size = video.stat().st_size
    print(f"[{case}] 参考视频：{video.name} {size} B（{size / 1024 / 1024:.2f} MiB）")
    with _client() as client:
        response = client.post(
            "/api/runs",
            json={"source_url": SOURCE_URL, "planning_mode": "video"},
        )
        print("POST /api/runs ->", response.status_code, response.text[:300])
        response.raise_for_status()
        run_id = response.json()["run_id"]
        print("run_id =", run_id)
        _save(case, {"run_id": run_id, "video": str(video), "video_bytes": size})

        view = _wait(
            client,
            run_id,
            lambda v: v.get("status") in APPROVAL_STATUSES
            or v.get("status") in {"failed", "cancelled"},
            timeout=1500,
        )
        _dump(case, "A_planning_view.json", view)
        if view.get("status") not in APPROVAL_STATUSES:
            raise SystemExit(f"[{case}] 没有进入待审批：{view.get('status')}")

        tasks = (view.get("approval") or {}).get("tasks") or []
        print("tasks:", [(t.get("task_id"), t.get("task_type")) for t in tasks])
        task = next(
            (item for item in tasks if item.get("task_type") == "image_to_video"),
            tasks[0],
        )
        existing = task.get("reference_images") or []
        print(
            "选中 task:", task.get("task_id"), "| 原有引用:",
            json.dumps(existing, ensure_ascii=False),
        )
        doc_videos = [
            asset
            for asset in (view["approval"].get("media_assets") or [])
            if str(asset.get("mime_type", "")).startswith("video/")
        ]
        print("文档自带视频素材：", json.dumps(doc_videos, ensure_ascii=False)[:400])
        order = max([int(ref.get("order") or 0) for ref in existing] or [0]) + 1
        upload = client.post(
            f"/api/runs/{run_id}/references",
            files={"file": (video.name, video.read_bytes(), "video/mp4")},
            data={
                "task_id": task["task_id"],
                "role": "reference_video",
                "order": str(order),
            },
        )
        print("POST references ->", upload.status_code, upload.text[:300])
        upload.raise_for_status()

        view = client.get(f"/api/runs/{run_id}").json()
        _dump(case, "A_with_video_view.json", view)
        refreshed = next(
            item
            for item in view["approval"]["tasks"]
            if item["task_id"] == task["task_id"]
        )
        print(
            "挂上视频后的引用：",
            json.dumps(refreshed.get("reference_images"), ensure_ascii=False),
        )
        inputs = APP_DIR / "data" / "runs" / run_id / "inputs"
        if inputs.is_dir():
            for path in sorted(inputs.iterdir()):
                print(f"  落盘 {path.name} {path.stat().st_size} B")
        _save(
            case,
            {**_state(case), "task_id": task["task_id"]},
        )


def stage_b(case: str) -> None:
    data = _state(case)
    run_id = data["run_id"]
    task_id = data["task_id"]
    with _client() as client:
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
        _dump(case, "B_final_view.json", view)
        print("status:", view.get("status"))
        print(
            "execution_records:",
            json.dumps(view.get("execution_records"), ensure_ascii=False)[:2500],
        )
        print("=== 修复相关事件 ===")
        for line in _events(view):
            if any(
                key in line
                for key in ("压缩", "转码", "重编码", "裁剪", "抽帧", "体积", "编码", "生成失败")
            ):
                print("  ", line)
        artifacts = view.get("artifacts") or []
        print(
            "artifacts:",
            [(a.get("artifact_id"), a.get("kind"), a.get("size")) for a in artifacts],
        )
        for artifact in artifacts:
            artifact_id = artifact.get("artifact_id")
            content = client.get(
                f"/api/runs/{run_id}/artifacts/{artifact_id}/content"
            )
            print("download", artifact_id, "->", content.status_code, len(content.content))
            if content.status_code == 200 and content.content:
                suffix = ".mp4" if artifact.get("kind") != "image" else ".png"
                path = _out(case) / f"OUTPUT_{artifact_id[:12]}{suffix}"
                path.write_bytes(content.content)
                print("saved", path, path.stat().st_size)


def stage_c(case: str) -> None:
    data = _state(case)
    run_id = data["run_id"]
    with _client(timeout=120) as client:
        response = client.delete(f"/api/runs/{run_id}")
        print("DELETE ->", response.status_code, response.text[:300])
        probe = client.get(f"/api/runs/{run_id}")
        print("DELETE 后 GET ->", probe.status_code, probe.text[:120])


if __name__ == "__main__":
    stage = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    case = (sys.argv[2] if len(sys.argv) > 2 else "size").lower()
    if case not in VIDEOS:
        raise SystemExit(f"unknown case {case}; use size|codec")
    if stage == "A":
        stage_a(case)
    elif stage == "B":
        stage_b(case)
    elif stage == "C":
        stage_c(case)
    else:
        raise SystemExit("unknown stage")
